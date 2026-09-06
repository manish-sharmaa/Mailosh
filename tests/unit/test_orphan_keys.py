"""Orphaned Stalwart API keys are persisted and retried
(`mailosh.web.orphan_keys`, operations hardening).

Before this, `mailosh.web.auth`'s three `destroy_api_key` sites logged a
failure and forgot it -- a Stalwart restart during a logout left a live
Bearer credential nothing would ever revoke. Same fixtures and fake admin
shape as `tests/unit/test_session_reaper.py`: the plain in-memory `db`,
a `FakeAdmin` that can be told to fail.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.db.models import OrphanApiKey, SessionRow
from mailosh.jmap.errors import JmapError
from mailosh.jmap.pool import ClientPool
from mailosh.security import sessions
from mailosh.web import auth, orphan_keys


def _settings() -> Settings:
    return Settings(stalwart_admin_secret="test-admin-secret-0123456789", secret_key="k" * 40, cookie_secure=False)


@pytest.fixture(autouse=True)
def _isolate_user_locks():
    auth._user_locks.clear()
    yield
    auth._user_locks.clear()


class FakeAdmin:
    """`destroy_api_key` fails while `fail` is True, then succeeds -- so one
    instance can play "Stalwart was down, then came back"."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.destroyed: list[tuple[str, str]] = []

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        if self.fail:
            raise JmapError("simulated destroy failure")
        self.destroyed.append((username, key_id))


async def _orphans(db) -> list[OrphanApiKey]:
    return list((await db.execute(select(OrphanApiKey).order_by(OrphanApiKey.id))).scalars())


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


async def test_record_persists_the_key_and_is_idempotent_per_key(db):
    await orphan_keys.record(db, "d@x", "k1", JmapError("first"))
    await orphan_keys.record(db, "d@x", "k1", JmapError("second"))
    await orphan_keys.record(db, "d@x", "k2", JmapError("other key"))
    await db.commit()

    rows = await _orphans(db)
    assert [(r.stalwart_username, r.api_key_id, r.attempts) for r in rows] == [
        ("d@x", "k1", 2),
        ("d@x", "k2", 1),
    ]
    assert rows[0].last_error == "second"


# ---------------------------------------------------------------------------
# the three sites in mailosh.web.auth
# ---------------------------------------------------------------------------


async def test_reaper_records_the_orphan_when_destroy_fails(db):
    """The reaper path: the row is still deleted (a failed destroy must not
    keep an expired session alive), and the key is now queued for retry
    instead of only logged."""
    s = _settings()
    admin = FakeAdmin(fail=True)
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    now = datetime(2026, 9, 6, tzinfo=UTC)
    stale = now - timedelta(days=s.session_idle_days + 1)
    row = await sessions.create_session(
        db,
        user=user,
        remember=False,
        user_agent=None,
        ip="9.9.9.9",
        api_key_id="k-reap",
        api_key_secret="API_x",
        settings=s,
    )
    row.created_at = stale
    row.last_seen_at = stale
    await db.commit()

    summary = await auth.reap_expired_sessions(db, admin, s, ClientPool(), now=now)

    assert summary.sessions_reaped == 1
    assert (await db.execute(select(SessionRow))).scalars().all() == []
    rows = await _orphans(db)
    assert [(r.stalwart_username, r.api_key_id) for r in rows] == [("d@x", "k-reap")]
    assert "simulated destroy failure" in (rows[0].last_error or "")


async def test_last_session_logout_records_the_orphan_when_destroy_fails(db):
    """`_destroy_key_if_last_session` -- the plain-logout path."""
    admin = FakeAdmin(fail=True)
    user = await repo.get_or_create_user(db, "d@x", "d@x")

    await auth._destroy_key_if_last_session(db, admin, user, "k-logout")
    await db.commit()

    assert [(r.stalwart_username, r.api_key_id) for r in await _orphans(db)] == [
        ("d@x", "k-logout")
    ]


async def test_last_session_logout_records_nothing_when_destroy_succeeds(db):
    admin = FakeAdmin()
    user = await repo.get_or_create_user(db, "d@x", "d@x")

    await auth._destroy_key_if_last_session(db, admin, user, "k-ok")
    await db.commit()

    assert admin.destroyed == [("d@x", "k-ok")]
    assert await _orphans(db) == []


# ---------------------------------------------------------------------------
# retry (the maintenance-loop half)
# ---------------------------------------------------------------------------


async def test_retry_destroys_and_forgets_once_stalwart_is_back(db, caplog):
    admin = FakeAdmin(fail=True)
    await orphan_keys.record(db, "d@x", "k1", JmapError("down"))
    await orphan_keys.record(db, "e@x", "k2", JmapError("down"))
    await db.commit()

    # Still down: both stay, attempts climb.
    summary = await orphan_keys.retry(db, admin)
    assert (summary.destroyed, summary.still_pending, summary.given_up) == (0, 2, 0)
    assert [r.attempts for r in await _orphans(db)] == [2, 2]

    # Back: both destroyed, rows gone.
    admin.fail = False
    with caplog.at_level(logging.INFO, logger="mailosh.web.orphan_keys"):
        summary = await orphan_keys.retry(db, admin)
    assert (summary.destroyed, summary.still_pending, summary.given_up) == (2, 0, 0)
    assert admin.destroyed == [("d@x", "k1"), ("e@x", "k2")]
    assert await _orphans(db) == []
    assert any("destroyed orphaned stalwart api key k1" in r.getMessage() for r in caplog.records)


async def test_retry_gives_up_after_the_cap_and_says_so(db, caplog, monkeypatch):
    """A key Stalwart will never destroy (its account was deleted by hand)
    must not be retried forever: at the cap the row goes, and an
    error-level line names the key so it can be revoked by hand."""
    monkeypatch.setattr(orphan_keys, "MAX_ATTEMPTS", 3)
    admin = FakeAdmin(fail=True)
    await orphan_keys.record(db, "d@x", "k-dead", JmapError("notFound"))
    await db.commit()

    assert (await orphan_keys.retry(db, admin)).still_pending == 1  # attempts -> 2
    with caplog.at_level(logging.ERROR, logger="mailosh.web.orphan_keys"):
        summary = await orphan_keys.retry(db, admin)  # attempts -> 3 == cap
    assert (summary.destroyed, summary.still_pending, summary.given_up) == (0, 0, 1)
    assert await _orphans(db) == []
    assert any(
        "giving up on stalwart api key k-dead" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


async def test_retry_with_nothing_queued_is_a_no_op(db):
    admin = FakeAdmin()
    summary = await orphan_keys.retry(db, admin)
    assert (summary.destroyed, summary.still_pending, summary.given_up) == (0, 0, 0)
    assert admin.destroyed == []


def test_maintenance_loop_calls_retry_once_per_sweep():
    """The wiring in `create_app`'s loop is one line; pin that it is there
    and inside the same sweep as the reaper."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "mailosh" / "web" / "app.py").read_text()
    reap = src.index("auth.reap_expired_sessions(")
    retry = src.index("orphan_keys.retry(db, app.state.admin)")
    assert reap < retry < src.index('logger.exception("session reap sweep failed")')
