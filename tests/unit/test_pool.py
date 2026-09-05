"""Unit tests for `mailosh.jmap.pool.ClientPool` (Task 5, design spec §9's
"per-user runtime: a `JmapClient` per session (LRU, idle-evicted)"):
one connected `JmapClient` per session id, reused across repeated `get`s
(never re-minting/re-connecting), explicit `drop`, idle eviction via
`stop_idle`, and `close_all` for shutdown.

`mailosh.jmap.pool.JmapClient` is monkeypatched to a fake with no network
of its own — `tests/integration/test_live_auth_flow.py` already proves the
real `JmapClient.connect_bearer` works against a live server; this file only
proves the pool's own bookkeeping (dedup/evict/close) around whatever
client class it's given.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from mailosh.config import Settings
from mailosh.db.models import SessionRow
from mailosh.jmap import pool as pool_module
from mailosh.jmap.pool import ClientPool
from mailosh.security import crypto


def _settings(**kw) -> Settings:
    kw.setdefault("cookie_secure", False)
    return Settings(stalwart_admin_secret="s", secret_key="k" * 40, **kw)


def _session_row(sid: str, settings: Settings, *, api_key_id: str = "k1") -> SessionRow:
    now = datetime.now(UTC)
    return SessionRow(
        id=sid,
        user_id=1,
        created_at=now,
        last_seen_at=now,
        expires_at=now,
        remember=False,
        user_agent=None,
        ip=None,
        api_key_id=api_key_id,
        api_key_secret_enc=crypto.encrypt(settings.secret_key, "API_the-real-secret"),
        csrf_token="tok",
    )


class FakeJmapClient:
    """A fake `JmapClient`: `connect_bearer` counts calls and records the
    token it was given (so a test can prove the pool decrypted the right
    secret before connecting); `close` just flips a flag.
    """

    connect_calls: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self.closed = False

    @classmethod
    async def connect_bearer(cls, base_url: str, token: str) -> "FakeJmapClient":
        cls.connect_calls.append(token)
        return cls()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    FakeJmapClient.connect_calls = []
    monkeypatch.setattr(pool_module, "JmapClient", FakeJmapClient)


async def test_get_builds_exactly_one_client_per_session():
    settings = _settings()
    row = _session_row("s1", settings)
    p = ClientPool()

    c1 = await p.get(row, settings)
    c2 = await p.get(row, settings)

    assert c1 is c2
    assert FakeJmapClient.connect_calls == ["API_the-real-secret"]


async def test_get_decrypts_the_stored_secret_before_connecting():
    settings = _settings()
    row = _session_row("s1", settings)
    p = ClientPool()

    await p.get(row, settings)

    assert FakeJmapClient.connect_calls == ["API_the-real-secret"]


async def test_different_sessions_get_different_clients():
    settings = _settings()
    row1 = _session_row("s1", settings)
    row2 = _session_row("s2", settings, api_key_id="k2")
    p = ClientPool()

    c1 = await p.get(row1, settings)
    c2 = await p.get(row2, settings)

    assert c1 is not c2
    assert len(FakeJmapClient.connect_calls) == 2


async def test_concurrent_get_for_the_same_session_still_connects_once():
    # Real concurrency, not just sequential awaits: proves the pool
    # serializes two simultaneous first-loads of the same session rather
    # than racing two connect_bearer calls (which would leak one client and
    # waste a round trip).
    settings = _settings()
    row = _session_row("s1", settings)
    p = ClientPool()

    c1, c2 = await asyncio.gather(p.get(row, settings), p.get(row, settings))

    assert c1 is c2
    assert len(FakeJmapClient.connect_calls) == 1


async def test_drop_closes_and_evicts():
    settings = _settings()
    row = _session_row("s1", settings)
    p = ClientPool()
    client = await p.get(row, settings)

    await p.drop("s1")

    assert client.closed is True
    # A subsequent get for the same session id reconnects (it was evicted).
    await p.get(row, settings)
    assert len(FakeJmapClient.connect_calls) == 2


async def test_drop_unknown_session_is_a_no_op():
    p = ClientPool()
    await p.drop("never-existed")  # must not raise


async def test_stop_idle_zero_closes_everything():
    settings = _settings()
    row1 = _session_row("s1", settings)
    row2 = _session_row("s2", settings, api_key_id="k2")
    p = ClientPool()
    c1 = await p.get(row1, settings)
    c2 = await p.get(row2, settings)

    await p.stop_idle(0)

    assert c1.closed is True
    assert c2.closed is True


async def test_stop_idle_keeps_recently_used_clients():
    settings = _settings()
    row = _session_row("s1", settings)
    p = ClientPool()
    client = await p.get(row, settings)

    await p.stop_idle(1800)  # just used -> nowhere near 30 minutes idle

    assert client.closed is False


async def test_get_refreshes_last_used_so_stop_idle_does_not_evict_it():
    settings = _settings()
    row = _session_row("s1", settings)
    p = ClientPool()
    client = await p.get(row, settings)
    # Manually age the entry past the idle threshold, then touch it again
    # via get() before sweeping — the touch must reset the idle clock.
    stale_marker = datetime.now(UTC) - timedelta(seconds=9999)
    p._clients["s1"] = (p._clients["s1"][0], stale_marker)
    await p.get(row, settings)

    await p.stop_idle(1800)

    assert client.closed is False


async def test_close_all_closes_every_client_and_empties_the_pool():
    settings = _settings()
    row1 = _session_row("s1", settings)
    row2 = _session_row("s2", settings, api_key_id="k2")
    p = ClientPool()
    c1 = await p.get(row1, settings)
    c2 = await p.get(row2, settings)

    await p.close_all()

    assert c1.closed is True
    assert c2.closed is True
    # The pool is empty afterward: a subsequent get reconnects from scratch.
    await p.get(row1, settings)
    assert len(FakeJmapClient.connect_calls) == 3


# ---------------------------------------------------------------------------
# pool.streaming() / is_streaming() against a REAL JmapClient (review finding
# 1, fix round 4)
#
# Every test above (and every `stalwart_listener` test in test_sse_hub.py)
# drives `streaming`/`is_streaming` through `FakeJmapClient` or
# `PooledStubClient` — plain classes that are hashable and weak-referenceable
# for free, which proves nothing about the real
# `mailosh.jmap.client.JmapClient`. `streaming()`'s whole implementation is
# `_streaming.add(client)` into a module-level `weakref.WeakSet`
# (`mailosh.jmap.pool._streaming`), which silently requires `client` to be
# both hashable (`WeakSet` is a hash-based container) and
# weak-referenceable. `JmapClient` gets both for free today only because
# it's a plain class with no `__eq__`/`__hash__`/`__slots__` of its own —
# nothing enforces that stays true.
# ---------------------------------------------------------------------------


async def test_streaming_pins_a_real_jmap_client(client):
    """Regression guard: `pool.streaming()` must keep working against the
    real `JmapClient` (`mailosh.jmap.client.JmapClient`), not just a stub.

    `client` here is the genuine article — built by `tests/conftest.py`'s
    `client` fixture via a respx-mocked `JmapClient.connect`, not a
    hand-rolled fake — so this exercises the exact type
    `stalwart_listener` wraps in `pool.streaming(client)` for its whole
    life in production.

    If `JmapClient` ever becomes `@dataclass(slots=True)` (no `__weakref__`
    slot unless one is added back explicitly) or gains an `__eq__` with no
    matching `__hash__` (Python drops the default identity hash the moment
    a class defines `__eq__`), `_streaming.add(client)` starts raising
    `TypeError` right here. In production that means `pool.streaming(client)`
    raises at the top of `stalwart_listener`, before it ever reads a single
    event — every SSE listener fails to start, mail stops updating live for
    every user, and this whole test suite otherwise stays green because
    nothing else in it touches a real client through this path. That is
    exactly the failure this test exists to catch first.
    """
    assert pool_module.is_streaming(client) is False
    try:
        with pool_module.streaming(client):
            assert pool_module.is_streaming(client) is True
    except TypeError as exc:
        pytest.fail(
            "pool.streaming() could not hold a real JmapClient in its "
            f"weakref.WeakSet ({exc!r}). JmapClient must stay both hashable "
            "and weak-referenceable: it must not become "
            "`@dataclass(slots=True)` without re-adding a `__weakref__` "
            "slot, and must not define `__eq__` without a matching "
            "`__hash__`. Breaking either silently kills every SSE listener "
            "in production the moment it starts (stalwart_listener runs its "
            "whole life inside `pool.streaming(client)`), while the rest of "
            "the test suite stays green -- see this test's docstring."
        )
    assert pool_module.is_streaming(client) is False
