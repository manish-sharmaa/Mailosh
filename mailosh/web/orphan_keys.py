"""Retry Stalwart API-key destroys that failed (operations hardening).

`mailosh.web.auth` destroys a user's Stalwart `x:ApiKey` when their last
session goes away -- on logout, on "sign out everywhere", and in the
reaper. Each of those used to catch the `JmapError`, log a warning, and
carry on, which is right for the request (a logout must not fail because
the mail server hiccuped) and wrong for the credential: nothing ever came
back for it, so a Stalwart restart at the wrong moment left a live Bearer
token for a user who believed they had signed out.

Now each of those sites calls `record` instead of only logging, and
`create_app`'s maintenance loop (the same 300 s sweep that runs the
reaper) calls `retry` once per sweep. `retry` walks every recorded key,
tries the destroy again, deletes the row on success, bumps `attempts` on
failure, and gives up -- deleting the row and logging at error level --
after `MAX_ATTEMPTS`, so a key that Stalwart itself reports as gone (an
account deleted by hand, say) does not stay on the list forever.

Lives under `mailosh.web` rather than `mailosh.security`: it calls
`StalwartAdmin`, which that package's docstring keeps its pure-library
modules free of, the same reason `reap_expired_sessions` lives in
`mailosh.web.auth`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db.models import OrphanApiKey
from mailosh.jmap.errors import JmapError
from mailosh.stalwart_admin import StalwartAdmin

logger = logging.getLogger(__name__)

#: One attempt per maintenance sweep (300 s), so this is roughly four days
#: of retrying before a key is written off. Long enough to ride out any
#: outage that is still an outage rather than a decommissioning; short
#: enough that a key Stalwart says does not exist stops being reported.
MAX_ATTEMPTS = 1000


async def record(db: AsyncSession, username: str, key_id: str, error: BaseException) -> None:
    """Remember that destroying `key_id` for `username` failed, so the
    maintenance loop retries it. Idempotent per key: a second failure for
    the same key bumps the existing row instead of adding one.

    Flushes, does NOT commit: every caller is inside a `_lock_user_row`
    block whose `SELECT ... FOR UPDATE` lock a commit here would release
    early -- before the session-row delete that the lock protects has
    landed -- and every caller commits moments later anyway (the
    `repo.audit` call that ends each of those blocks).
    """
    now = datetime.now(UTC)
    existing = await db.scalar(
        select(OrphanApiKey).where(
            OrphanApiKey.stalwart_username == username, OrphanApiKey.api_key_id == key_id
        )
    )
    if existing is not None:
        existing.attempts += 1
        existing.last_attempt_at = now
        existing.last_error = str(error)[:1000]
    else:
        db.add(
            OrphanApiKey(
                stalwart_username=username,
                api_key_id=key_id,
                first_failed_at=now,
                last_attempt_at=now,
                attempts=1,
                last_error=str(error)[:1000],
            )
        )
    await db.flush()
    logger.warning(
        "stalwart api key %s for %s could not be destroyed; queued for retry", key_id, username
    )


@dataclass(frozen=True)
class RetrySummary:
    destroyed: int
    still_pending: int
    given_up: int


async def retry(db: AsyncSession, admin: StalwartAdmin) -> RetrySummary:
    """Try every recorded destroy again. Called once per maintenance sweep.

    Each key is its own unit of work: one key's failure does not stop the
    others, and every outcome is committed before the next key is tried, so
    a sweep interrupted halfway leaves the rows it did process correct.
    """
    rows = (await db.execute(select(OrphanApiKey).order_by(OrphanApiKey.id))).scalars().all()
    destroyed = pending = given_up = 0
    for row in rows:
        try:
            await admin.destroy_api_key(row.stalwart_username, row.api_key_id)
        except JmapError as exc:
            row.attempts += 1
            row.last_attempt_at = datetime.now(UTC)
            row.last_error = str(exc)[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                logger.error(
                    "giving up on stalwart api key %s for %s after %d attempts (last error: %s) "
                    "-- revoke it by hand in Stalwart's admin UI",
                    row.api_key_id,
                    row.stalwart_username,
                    row.attempts,
                    row.last_error,
                )
                await db.delete(row)
                given_up += 1
            else:
                pending += 1
            await db.commit()
            continue
        logger.info(
            "destroyed orphaned stalwart api key %s for %s on retry %d",
            row.api_key_id,
            row.stalwart_username,
            row.attempts,
        )
        await db.delete(row)
        await db.commit()
        destroyed += 1
    return RetrySummary(destroyed=destroyed, still_pending=pending, given_up=given_up)
