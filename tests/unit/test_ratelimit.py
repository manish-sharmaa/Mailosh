from datetime import datetime, timedelta, timezone

from mailosh.db.models import LoginAttempt
from mailosh.security import ratelimit
from mailosh.security.ratelimit import LoginLimiter


async def test_account_lockout_after_five_failures(db):
    lim = LoginLimiter()
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for _ in range(5):
        assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=now) == 0
        await lim.record_failure(db, ip="1.1.1.1", account="a@x", now=now)
    assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=now) >= 60
    # account-keyed
    assert await lim.retry_after(db, ip="2.2.2.2", account="a@x", now=now) >= 60
    # other account ok
    assert await lim.retry_after(db, ip="1.1.1.1", account="b@x", now=now) == 0
    later = now + timedelta(minutes=16)
    # window expired
    assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=later) == 0


async def test_ip_lockout_after_twenty(db):
    lim = LoginLimiter()
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for i in range(20):
        await lim.record_failure(db, ip="9.9.9.9", account=f"u{i}@x", now=now)
    assert await lim.retry_after(db, ip="9.9.9.9", account="fresh@x", now=now) >= 60


async def test_reset_clears(db):
    lim = LoginLimiter()
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for _ in range(5):
        await lim.record_failure(db, ip="1.1.1.1", account="a@x", now=now)
    await lim.reset(db, ip="1.1.1.1", account="a@x")
    assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=now) == 0


# ---------------------------------------------------------------------------
# stale_login_attempts (Task 5, controller ruling #2): the reaper's other
# job -- delete login_attempt rows whose window is stale AND whose lock has
# expired, so a naturally-idle rate-limit counter doesn't linger forever.
# ---------------------------------------------------------------------------


async def test_stale_login_attempts_finds_only_stale_and_unlocked_rows(db):
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    db.add(  # stale window, never locked -> stale
        LoginAttempt(
            key="acct:a@x", failures=1, window_start=now - timedelta(hours=1), locked_until=None
        )
    )
    db.add(  # stale window, but still actively locked -> not stale yet
        LoginAttempt(
            key="acct:b@x",
            failures=5,
            window_start=now - timedelta(hours=1),
            locked_until=now + timedelta(minutes=1),
        )
    )
    db.add(  # window itself not stale -> not stale, regardless of lock
        LoginAttempt(key="acct:c@x", failures=1, window_start=now, locked_until=None)
    )
    db.add(  # stale window AND an already-expired lock -> stale
        LoginAttempt(
            key="acct:d@x",
            failures=5,
            window_start=now - timedelta(hours=1),
            locked_until=now - timedelta(minutes=1),
        )
    )
    await db.commit()

    stale = await ratelimit.stale_login_attempts(db, now=now)

    assert sorted(row.key for row in stale) == ["acct:a@x", "acct:d@x"]


async def test_stale_login_attempts_empty_table_returns_empty_list(db):
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    assert await ratelimit.stale_login_attempts(db, now=now) == []
