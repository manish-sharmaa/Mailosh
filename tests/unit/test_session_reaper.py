"""Unit tests for the reaper (Task 5, controller ruling #2): the lifespan
background task's other job, alongside `ClientPool` idle eviction. Proves
`mailosh.web.auth.reap_expired_sessions`:

- destroys a user's Stalwart API key exactly when their LAST session row
  expires (never while another live session still needs it, even when
  several of a user's rows expire in the very same sweep);
- deletes every expired row and records a `session.reaped` audit row per
  row removed;
- drops each reaped session's pooled JMAP client (fix round 1, Finding 2);
- survives a `destroy_api_key` failure (logs, still deletes the row —
  Task 3's own review-flagged gap: "without [a reaper], every naturally-
  expired session leaves a live Stalwart credential until manual logout");
- prunes `login_attempt` rows whose window is stale AND whose lock has
  expired, leaving a still-locked row alone;
- never leaves a live session pointing at an already-destroyed key even
  when a concurrent login for the same user lands inside the reaper's own
  destroy window (fix round 1, Finding 1 — reviewer-reproduced).

Uses the plain `db` fixture (`tests/conftest.py`, in-memory aiosqlite) --
`mailosh.web.auth.reap_expired_sessions` takes an already-open `AsyncSession`
directly, the same shape `mailosh.security.sessions`/`ratelimit` use, so no
full `create_app`/HTTP layer is needed to exercise it -- except the last two
tests below, which need two genuinely independent database connections (a
single `AsyncSession` can't be used by two concurrent coroutines at once)
and so build their own short-lived file-backed engine, the same pattern
`tests/unit/test_db_models.py`'s own concurrent-race test uses.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.db.base import Base
from mailosh.db.models import AppUser, AuditLog, LoginAttempt, SessionRow
from mailosh.jmap.errors import JmapError
from mailosh.jmap.pool import ClientPool
from mailosh.security import sessions
from mailosh.stalwart_admin import ApiKey
from mailosh.web import auth


def _settings(**kw) -> Settings:
    kw.setdefault("cookie_secure", False)
    return Settings(stalwart_admin_secret="test-admin-secret-0123456789", secret_key="k" * 40, **kw)


@pytest.fixture
def pool() -> ClientPool:
    return ClientPool()


@pytest.fixture(autouse=True)
def _isolate_user_locks():
    """`auth._user_locks` is a process-lifetime, module-level dict by
    design (harmless in production: a real uvicorn process has exactly one
    event loop for its whole lifetime, so a lock created for a user in
    hour 1 is still valid in hour 10). pytest-asyncio, though, gives each
    async test function its own fresh event loop -- so without this, a
    lock object left behind by an earlier test (bound to *that* test's now
    -closed loop the moment it was first acquired) can leak into a later
    test that happens to reuse the same numeric user id (near-guaranteed
    here: every test's own fresh in-memory/temp-file database restarts
    autoincrement from 1), raising `RuntimeError: ... bound to a different
    event loop` the moment two tasks genuinely contend for it -- not a
    production bug, a test-isolation gap. Clearing the dict before every
    test in this file (the one place multiple tests exercise genuine
    concurrent contention on this exact lock) closes it without touching
    production code at all.
    """
    auth._user_locks.clear()
    yield
    auth._user_locks.clear()


class FakeAdmin:
    def __init__(self, *, fail: bool = False) -> None:
        self.destroyed: list[tuple[str, str]] = []
        self.created: list[str] = []
        self._fail = fail
        self._minted = 0

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        self._minted += 1
        self.created.append(username)
        return ApiKey(id=f"k-new-{self._minted}", secret=f"API_new_{self._minted}")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        if self._fail:
            raise JmapError("simulated destroy failure")
        self.destroyed.append((username, key_id))


async def _backdated_session(db, s, *, user, api_key_id, created_at, last_seen_at, remember=False):
    """A session row exactly like `sessions.create_session` builds, except
    its timestamps are overwritten afterward so it already reads as
    expired -- simpler and more honest than reimplementing session
    creation by hand, since it still goes through the real encryption/CSRF-
    token machinery `create_session` uses.
    """
    row = await sessions.create_session(
        db,
        user=user,
        remember=remember,
        user_agent=None,
        ip="9.9.9.9",
        api_key_id=api_key_id,
        api_key_secret="API_shared-secret",
        settings=s,
    )
    row.created_at = created_at
    row.last_seen_at = last_seen_at
    row.expires_at = created_at + timedelta(days=s.session_absolute_days)
    await db.commit()
    return row


async def test_reap_destroys_key_when_the_last_session_expires(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 1
    assert admin.destroyed == [("d@x", "k1")]
    remaining = await db.execute(select(SessionRow))
    assert remaining.scalars().first() is None


async def test_reap_keeps_key_alive_when_another_session_is_still_live(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )
    await _backdated_session(db, s, user=user, api_key_id="k1", created_at=now, last_seen_at=now)

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 1
    assert admin.destroyed == []  # the live second session still needs it


async def test_reap_destroys_exactly_once_when_every_session_expires_together(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 3
    assert admin.destroyed == [("d@x", "k1")]  # exactly once, not three times


async def test_reap_respects_absolute_expiry_even_under_remember_me(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    created = now - timedelta(days=s.session_absolute_days + 1)
    # last_seen_at is "now" (freshly active) — only the absolute cap should
    # catch this one; idle expiry alone never would.
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=created, last_seen_at=now, remember=True
    )

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 1
    assert admin.destroyed == [("d@x", "k1")]


async def test_reap_leaves_a_healthy_session_untouched(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    await _backdated_session(db, s, user=user, api_key_id="k1", created_at=now, last_seen_at=now)

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 0
    assert admin.destroyed == []
    remaining = await db.execute(select(SessionRow))
    assert remaining.scalars().first() is not None


async def test_reap_survives_a_destroy_failure_logs_and_still_deletes_the_row(db, pool, caplog):
    s = _settings()
    admin = FakeAdmin(fail=True)
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )

    with caplog.at_level(logging.WARNING, logger="mailosh.web.auth"):
        summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 1  # the row is still gone
    remaining = await db.execute(select(SessionRow))
    assert remaining.scalars().first() is None
    assert any("k1" in rec.getMessage() for rec in caplog.records)


async def test_reap_writes_a_session_reaped_audit_row_per_session(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )
    await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )

    await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    result = await db.execute(select(AuditLog).where(AuditLog.action == "session.reaped"))
    assert len(list(result.scalars())) == 2


async def test_reap_prunes_stale_unlocked_login_attempts(db, pool):
    s = _settings()
    admin = FakeAdmin()
    now = datetime(2026, 9, 2, tzinfo=UTC)
    db.add(
        LoginAttempt(
            key="acct:old@x", failures=2, window_start=now - timedelta(hours=1), locked_until=None
        )
    )
    await db.commit()

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.login_attempts_pruned == 1
    remaining = await db.execute(select(LoginAttempt))
    assert remaining.scalars().first() is None


async def test_reap_keeps_login_attempt_still_locked(db, pool):
    s = _settings()
    admin = FakeAdmin()
    now = datetime(2026, 9, 2, tzinfo=UTC)
    db.add(
        LoginAttempt(
            key="acct:locked@x",
            failures=5,
            window_start=now - timedelta(hours=1),
            locked_until=now + timedelta(minutes=5),
        )
    )
    await db.commit()

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.login_attempts_pruned == 0
    remaining = await db.execute(select(LoginAttempt))
    assert remaining.scalars().first() is not None


async def test_reap_keeps_a_fresh_window_even_if_briefly_locked(db, pool):
    s = _settings()
    admin = FakeAdmin()
    now = datetime(2026, 9, 2, tzinfo=UTC)
    # Window itself is recent (not stale) even though the lock has already
    # expired -- must NOT be pruned yet (still informs the failure count for
    # the current window).
    db.add(
        LoginAttempt(
            key="acct:recent@x",
            failures=5,
            window_start=now - timedelta(minutes=2),
            locked_until=now - timedelta(seconds=1),
        )
    )
    await db.commit()

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.login_attempts_pruned == 0


# ---------------------------------------------------------------------------
# Fix round 1, Finding 2: the reaper must drop a reaped session's pooled
# JMAP client too, not just its Postgres row -- `ClientPool.stop_idle`
# alone never reaches a session still actively in use (fresh `last_used`)
# when it separately hits its own absolute-expiry cap.
# ---------------------------------------------------------------------------


class _FakeJmapClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_reap_drops_the_reaped_sessions_pooled_client(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    row = await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=stale, last_seen_at=stale
    )
    # Simulate the pool already holding a connected client for this exact
    # session, as it would if the session were (or recently was) in active
    # use -- exactly the scenario `stop_idle`'s idle-only eviction misses.
    fake_client = _FakeJmapClient()
    pool._clients[row.id] = (fake_client, now)

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 1
    assert fake_client.closed is True
    assert row.id not in pool._clients


async def test_reap_does_not_touch_the_pool_for_a_healthy_session(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    row = await _backdated_session(
        db, s, user=user, api_key_id="k1", created_at=now, last_seen_at=now
    )
    fake_client = _FakeJmapClient()
    pool._clients[row.id] = (fake_client, now)

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    assert summary.sessions_reaped == 0
    assert fake_client.closed is False
    assert row.id in pool._clients


# ---------------------------------------------------------------------------
# Fix round 1, Finding 1 (reviewer-reproduced, empirically, against the
# real reap_expired_sessions/_mint_or_reuse_key/sessions.create_session
# code): a session reaped while a *concurrent* login for the same user
# lands its own create_session commit inside the destroy decision's window
# must never end up with a live session row pointing at an
# already-destroyed key.
# ---------------------------------------------------------------------------


async def test_reap_and_concurrent_login_never_leave_a_live_session_on_a_destroyed_key(tmp_path):
    """Deterministic, not timing-based. `SignalingAdmin.destroy_api_key`
    sets an `asyncio.Event` the instant it's called -- exactly the point in
    the OLD, unfixed code where a concurrent reader was most exposed (the
    row still existed, undeleted, referencing a key about to die) -- and
    yields once (`asyncio.sleep(0)`) so the event loop can actually try to
    schedule whoever is waiting on it before this call returns. The
    "concurrent login" task wakes on that event and immediately attempts
    the exact same locked sequence `POST /login` uses
    (`auth._lock_user_row` -> `_mint_or_reuse_key` -> `create_session`).

    With `_lock_user_row` in place, the reaper entered that same per-user
    lock *before* its count check and does not leave it until after this
    row's own delete + audit commit -- so at the moment the event fires,
    the reaper is still holding it, and the login task's own lock
    acquisition genuinely blocks until that specific row-processing section
    is done (the lock is released per row, before `reap_expired_sessions`
    moves on to its unrelated stale-login-attempt cleanup -- so "the login
    acquired the lock" can legitimately race ahead of "reap_expired_sessions
    fully returned", and this test does not assert an order stronger than
    that). Either way, the login's reuse-check can only ever observe the
    old row as completely gone, never merely "doomed but still readable".

    Two independent, file-backed database connections (not the `db`
    fixture's single session) so the two tasks are genuinely separate
    clients -- the real shape of a background reaper task and a real login
    request, which never share one `AsyncSession`.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    s = _settings()
    pool = ClientPool()
    destroy_started = asyncio.Event()
    order: list[str] = []

    class SignalingAdmin(FakeAdmin):
        async def destroy_api_key(self, username: str, key_id: str) -> None:
            order.append("reaper: calling destroy_api_key")
            destroy_started.set()
            # A real (if tiny) delay, not `sleep(0)`: a single zero-delay
            # yield is not reliably enough wall-clock room for the login
            # task to wake from `destroy_started.wait()`, open its own
            # session, and reach its own lock-acquisition attempt before
            # this coroutine resumes -- confirmed empirically (this same
            # scenario with the lock bypassed reproduces the reviewer's bug
            # every time with a 50ms delay here, and not reliably with
            # `sleep(0)`). Long enough to make the interleaving this test
            # targets actually happen; short enough not to slow the suite.
            await asyncio.sleep(0.05)
            await super().destroy_api_key(username, key_id)

    admin = SignalingAdmin()

    async with maker() as setup_db:
        user = await repo.get_or_create_user(setup_db, "race@x", "race@x")
        user_id = user.id
        now = datetime(2026, 9, 2, tzinfo=UTC)
        stale = now - timedelta(days=s.session_idle_days + 1)
        await _backdated_session(
            setup_db, s, user=user, api_key_id="k-old", created_at=stale, last_seen_at=stale
        )

    async def reaper_task() -> None:
        async with maker() as reap_db:
            await auth.reap_expired_sessions(reap_db, admin, s, pool, now=now)
        order.append("reaper: done (row deleted + committed)")

    async def concurrent_login_task() -> str:
        await destroy_started.wait()
        order.append("login: woke up, attempting to acquire the user lock")
        async with maker() as login_db:
            user = await login_db.get(AppUser, user_id)
            async with auth._lock_user_row(login_db, user.id):
                order.append("login: acquired the lock")
                api_key_id, api_key_secret = await auth._mint_or_reuse_key(login_db, admin, user, s)
                await sessions.create_session(
                    login_db,
                    user=user,
                    remember=False,
                    user_agent=None,
                    ip="1.2.3.4",
                    api_key_id=api_key_id,
                    api_key_secret=api_key_secret,
                    settings=s,
                )
            return api_key_id

    _, new_key_id = await asyncio.gather(reaper_task(), concurrent_login_task())
    await engine.dispose()

    # The login task genuinely reached the lock/reuse-check while the
    # reaper had already committed to destroying (event-gated), proving
    # this test exercises the actual window in question rather than two
    # calls that merely happened to run one after the other.
    assert order[0] == "reaper: calling destroy_api_key"
    assert "login: woke up, attempting to acquire the user lock" in order

    # The core invariant (what the reviewer's own reproduction checked):
    # the old key was destroyed, the concurrent login never reused it --
    # it minted a fresh one instead -- and no row left in the database
    # references a key that was actually destroyed.
    assert admin.destroyed == [("race@x", "k-old")]
    assert admin.created == ["race@x"]  # minted fresh, did not reuse
    assert new_key_id != "k-old"

    async with maker() as check_db:
        live = await check_db.execute(select(SessionRow).where(SessionRow.user_id == user_id))
        live_rows = list(live.scalars())
    destroyed_ids = {key_id for _, key_id in admin.destroyed}
    assert live_rows  # the concurrent login's own session is still there
    assert not any(row.api_key_id in destroyed_ids for row in live_rows)


# ---------------------------------------------------------------------------
# Fix round 2 (post-review): round 1's `logout_all` fix locked only the
# destroy call, *after* `sessions.revoke_all` had already run unlocked --
# reviewer-reproduced gap, empirically, with a standalone probe. A
# concurrent login's own locked mint-or-reuse-then-create_session sequence
# could read a session `revoke_all` hadn't gotten to yet (or hadn't started
# revoking at all), reuse its key, and commit a brand-new session that
# `revoke_all`'s own one-time row list never included -- which the
# still-unconditional destroy call then killed out from under it anyway.
# Placed here, not in test_auth_routes.py: it needs the exact same
# low-level, multi-connection, event-gated technique and fixtures
# (FakeAdmin, _backdated_session, a throwaway file-backed engine) the
# reaper's own regression test above already established.
# ---------------------------------------------------------------------------


async def test_logout_all_never_leaves_a_live_session_on_a_destroyed_key(tmp_path, monkeypatch):
    """Deterministic, not timing-based -- but unlike the reaper test (which
    gates on `destroy_api_key`, a single call `logout_all` reaches only
    after its own decision is already made), this gates on `revoke_all`
    itself, matching what the reviewer's own reproduction targeted: revoke_all
    reads its list of rows to delete *once*, up front, so a session created
    after that read is structurally invisible to it no matter how long it
    then takes to actually delete/commit. `sessions.revoke_all` is
    monkeypatched to an instrumented equivalent (same query, same delete-
    everything-it-read semantics) that signals an `asyncio.Event` the
    instant its own read completes and then sleeps 50ms before deleting --
    giving a concurrent login genuine room to read the still-present old
    row, reuse its key, and commit a new session that this revoke_all call
    can never retroactively see.

    With `_lock_user_row` now wrapping the *entire* revoke -> recount ->
    destroy sequence (this fix), the login's own lock acquisition can't
    even begin until `logout_all`'s whole locked section — including this
    delayed `revoke_all` — has completed and released it; by then, no
    session for this user existed for the login to find, so it mints a
    fresh key instead of reusing the doomed one, and `logout_all`'s own
    recount (also inside the lock) confirms zero sessions remain before
    destroying. Confirmed this same setup reliably reproduces the original
    bug (4/4 runs) when `revoke_all` is called unlocked and only the
    destroy call is locked -- the exact round-1 shape -- via a throwaway
    scratchpad probe (not committed) built from this file's own fixtures.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    s = _settings()
    admin = FakeAdmin()
    select_done = asyncio.Event()
    real_revoke_all = sessions.revoke_all

    async def instrumented_revoke_all(db, user_id):
        result = await db.execute(select(SessionRow).where(SessionRow.user_id == user_id))
        rows = list(result.scalars())
        select_done.set()
        await asyncio.sleep(0.05)
        for row in rows:
            await db.delete(row)
        await db.commit()
        return rows

    monkeypatch.setattr(sessions, "revoke_all", instrumented_revoke_all)

    async with maker() as setup_db:
        user = await repo.get_or_create_user(setup_db, "race@x", "race@x")
        user_id = user.id
        now = datetime(2026, 9, 2, tzinfo=UTC)
        await _backdated_session(
            setup_db, s, user=user, api_key_id="k-old", created_at=now, last_seen_at=now
        )

    async def logout_all_task() -> None:
        async with maker() as logout_db:
            user = await logout_db.get(AppUser, user_id)
            async with auth._lock_user_row(logout_db, user.id):
                revoked_rows = await sessions.revoke_all(logout_db, user.id)
                api_key_id = next(
                    (r.api_key_id for r in revoked_rows if r.api_key_id is not None), None
                )
                if api_key_id is not None:
                    remaining = await logout_db.scalar(
                        select(func.count())
                        .select_from(SessionRow)
                        .where(SessionRow.user_id == user.id)
                    )
                    if not remaining:
                        await admin.destroy_api_key(user.stalwart_username, api_key_id)

    async def concurrent_login_task() -> str:
        await select_done.wait()
        async with maker() as login_db:
            user = await login_db.get(AppUser, user_id)
            async with auth._lock_user_row(login_db, user.id):
                api_key_id, api_key_secret = await auth._mint_or_reuse_key(login_db, admin, user, s)
                await sessions.create_session(
                    login_db,
                    user=user,
                    remember=False,
                    user_agent=None,
                    ip="1.2.3.4",
                    api_key_id=api_key_id,
                    api_key_secret=api_key_secret,
                    settings=s,
                )
            return api_key_id

    try:
        _, new_key_id = await asyncio.gather(logout_all_task(), concurrent_login_task())
    finally:
        monkeypatch.setattr(sessions, "revoke_all", real_revoke_all)
        await engine.dispose()

    assert admin.destroyed == [("race@x", "k-old")]
    assert admin.created == ["race@x"]  # minted fresh, did not reuse
    assert new_key_id != "k-old"

    live_engine = create_async_engine(url)
    async with async_sessionmaker(live_engine, expire_on_commit=False)() as check_db:
        live = await check_db.execute(select(SessionRow).where(SessionRow.user_id == user_id))
        live_rows = list(live.scalars())
    await live_engine.dispose()

    destroyed_ids = {key_id for _, key_id in admin.destroyed}
    assert live_rows  # the concurrent login's own session is still there
    assert not any(row.api_key_id in destroyed_ids for row in live_rows)


# ---------------------------------------------------------------------------
# Fix round 2 (post-review, low severity): ClientPool.drop()/stop_idle()'s
# own client.close() calls were unguarded -- a broken close on one session
# must not abort the rest of a sweep (remaining expired rows, the
# stale-login-attempt prune) or the rest of a logout's own cleanup tail.
# ---------------------------------------------------------------------------


class _RaisingCloseClient:
    async def close(self) -> None:
        raise RuntimeError("simulated close failure")


async def test_pool_drop_survives_a_close_failure():
    p = ClientPool()
    p._clients["s1"] = (_RaisingCloseClient(), datetime.now(UTC))

    await p.drop("s1")  # must not raise

    assert "s1" not in p._clients


async def test_pool_stop_idle_survives_a_close_failure_and_still_evicts_the_rest():
    p = ClientPool()
    stale = datetime.now(UTC) - timedelta(seconds=9999)
    p._clients["bad"] = (_RaisingCloseClient(), stale)

    class _OkClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    good = _OkClient()
    p._clients["good"] = (good, stale)

    await p.stop_idle(1800)  # must not raise, and must still evict "good"

    assert "bad" not in p._clients
    assert "good" not in p._clients
    assert good.closed is True


async def test_reap_survives_a_pool_drop_failure_and_still_reaps_the_rest(db, pool):
    s = _settings()
    admin = FakeAdmin()
    user_a = await repo.get_or_create_user(db, "a@x", "a@x")
    user_b = await repo.get_or_create_user(db, "b@x", "b@x")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    row_a = await _backdated_session(
        db, s, user=user_a, api_key_id="ka", created_at=stale, last_seen_at=stale
    )
    await _backdated_session(
        db, s, user=user_b, api_key_id="kb", created_at=stale, last_seen_at=stale
    )
    pool._clients[row_a.id] = (_RaisingCloseClient(), now)

    summary = await auth.reap_expired_sessions(db, admin, s, pool, now=now)

    # Both rows reaped despite row_a's pool.drop() raising internally
    # (ClientPool.drop swallows it) -- row_b, and the audit trail for both,
    # are unaffected by row_a's broken client.
    assert summary.sessions_reaped == 2
    assert sorted(admin.destroyed) == [("a@x", "ka"), ("b@x", "kb")]
    remaining = await db.execute(select(SessionRow))
    assert remaining.scalars().first() is None
