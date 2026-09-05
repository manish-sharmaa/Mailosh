"""Login rate limiting (design spec §9): Postgres-backed failure counters
that gate a login attempt *before* Stalwart is ever contacted — Stalwart
itself auto-bans a source IP after 100 failures/day, and this app must
never be the reason a shared IP (NAT, office network) trips that ban.

One row per key in `login_attempt` (`mailosh.db.models.LoginAttempt`):
`acct:<account, lower-cased>` and `ip:<ip>`, tracked independently. Every
failed attempt counts against *both* its account and its IP; a lockout on
either key blocks the attempt (`retry_after` takes the max of the two), so
abuse of one key can't be routed around by varying the other (e.g. one
account hammered from many IPs, or many accounts guessed from one IP).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db.models import LoginAttempt

#: Failures within this rolling window count toward a lockout. Once a key
#: has gone quiet for longer than this with no *active* lock, the next
#: failure starts a brand new window (failures reset to zero first) rather
#: than piling onto a stale count left over from an unrelated, long-past
#: incident. A key that is still actively locked never has its window
#: reset out from under it by an incoming failed attempt, however stale —
#: see `LoginLimiter._bump`.
_WINDOW = timedelta(minutes=15)

#: Failures needed to trigger a lockout, per key type (design spec §9).
_ACCOUNT_THRESHOLD = 5
_IP_THRESHOLD = 20

#: Exponential backoff once a key is at or past its threshold:
#: `_BASE_BACKOFF_SECONDS * 2 ** (failures - threshold)`, capped at
#: `_MAX_BACKOFF_SECONDS` — 60s right at the threshold, doubling with every
#: failure past it, never longer than an hour.
_BASE_BACKOFF_SECONDS = 60
_MAX_BACKOFF_SECONDS = 3600


def _aware(dt: datetime) -> datetime:
    """Coerce a datetime read back from the database to UTC-aware.

    Every datetime this module writes starts out UTC-aware. Postgres
    round-trips `DateTime(timezone=True)` faithfully; SQLite (the
    aiosqlite backend `tests/conftest.py`'s `db` fixture runs unit tests
    against) silently drops tzinfo on any row loaded fresh from the
    database rather than served from SQLAlchemy's identity map — comparing
    such a value against an aware `now` would otherwise raise `TypeError`.
    A naive value is always treated as UTC, matching every writer here.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _acct_key(account: str) -> str:
    return f"acct:{account.lower()}"


def _ip_key(ip: str) -> str:
    return f"ip:{ip}"


class LoginLimiter:
    """Stateless helper over the `login_attempt` table — every method takes
    the `AsyncSession` explicitly rather than the instance holding one, so
    a single `LoginLimiter()` has nothing instance-specific to share or
    protect and is safe to reuse anywhere.
    """

    async def retry_after(
        self, db: AsyncSession, *, ip: str, account: str, now: datetime | None = None
    ) -> int:
        """Seconds the caller must wait before attempting this `account`/
        `ip` pair again; `0` means "go ahead now". Never raises — an
        account or IP with no row yet has simply never failed, so it is
        not locked.
        """
        now = now or datetime.now(UTC)
        acct_row = await db.get(LoginAttempt, _acct_key(account))
        ip_row = await db.get(LoginAttempt, _ip_key(ip))
        return max(self._delay(acct_row, now), self._delay(ip_row, now))

    @staticmethod
    def _delay(row: LoginAttempt | None, now: datetime) -> int:
        if row is None or row.locked_until is None:
            return 0
        locked_until = _aware(row.locked_until)
        if now >= locked_until:
            return 0
        return math.ceil((locked_until - now).total_seconds())

    async def record_failure(
        self, db: AsyncSession, *, ip: str, account: str, now: datetime | None = None
    ) -> None:
        """Record one failed login attempt against both `account` and
        `ip`, locking whichever key(s) just reached (or remain past) their
        threshold.
        """
        now = now or datetime.now(UTC)
        await self._bump(db, key=_acct_key(account), threshold=_ACCOUNT_THRESHOLD, now=now)
        await self._bump(db, key=_ip_key(ip), threshold=_IP_THRESHOLD, now=now)
        await db.commit()

    @staticmethod
    async def _bump(db: AsyncSession, *, key: str, threshold: int, now: datetime) -> None:
        row = await db.get(LoginAttempt, key)
        if row is None:
            row = LoginAttempt(key=key, failures=0, window_start=now, locked_until=None)
            db.add(row)
        else:
            window_start = _aware(row.window_start)
            locked_until = _aware(row.locked_until) if row.locked_until is not None else None
            stale = now - window_start > _WINDOW
            locked = locked_until is not None and now < locked_until
            # A stale window only resets when the key is not actively
            # locked: an attacker mid-lockout must not be able to wipe
            # their own failure count/lock early just by retrying after
            # the 15-minute window (but before the — possibly longer —
            # lock itself) has elapsed.
            if stale and not locked:
                row.failures = 0
                row.window_start = now
                row.locked_until = None
        row.failures += 1
        if row.failures >= threshold:
            backoff = min(
                _MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * 2 ** (row.failures - threshold)
            )
            row.locked_until = now + timedelta(seconds=backoff)

    async def reset(self, db: AsyncSession, *, ip: str, account: str) -> None:
        """Clear both counters — called on a successful login so a run of
        past failures doesn't linger against an account/IP that just
        proved itself legitimate.
        """
        for key in (_acct_key(account), _ip_key(ip)):
            row = await db.get(LoginAttempt, key)
            if row is not None:
                await db.delete(row)
        await db.commit()


async def stale_login_attempts(db: AsyncSession, now: datetime | None = None) -> list[LoginAttempt]:
    """Every `login_attempt` row safe to delete outright (Task 5, controller
    ruling #2's reaper): its rolling window has gone stale (module docstring:
    `_WINDOW`) AND, if it was ever locked, that lock has already expired.

    Both conditions matter, not just the window: a row whose window is old
    but which is still *actively* locked must survive (`_bump`'s own "stale
    window, but not while still locked" rule — an attacker mid-lockout must
    not get to wipe their own counter early just because 15 minutes have
    passed). A row failing only the window check (still within its 15-minute
    window) is still live information the next `record_failure`/
    `retry_after` call needs, lock or no lock, so it is left alone too.

    Read-only, mirroring `mailosh.security.sessions.expired_sessions`'s own
    shape: this only finds candidates, the reaper
    (`mailosh.web.auth.reap_expired_sessions`) does the actual deleting —
    kept symmetric with that primitive rather than each inventing its own
    "find vs. act" split.
    """
    now = now or datetime.now(UTC)
    result = await db.execute(select(LoginAttempt))
    stale: list[LoginAttempt] = []
    for row in result.scalars():
        window_stale = now - _aware(row.window_start) > _WINDOW
        lock_expired = row.locked_until is None or now >= _aware(row.locked_until)
        if window_stale and lock_expired:
            stale.append(row)
    return stale
