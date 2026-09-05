"""DB-backed sessions (design spec §9): one Postgres row per logged-in
browser (`mailosh.db.models.SessionRow`, table `"session"`) holding the
user id, timestamps, UA/IP, a CSRF token, and the per-session Stalwart API
key — Fernet-encrypted (`mailosh.security.crypto`), never plaintext.

Two independent expirations apply to every session:

- **Idle expiry**, sliding: `session_remember_days` (with "keep me signed
  in") or `session_idle_days` otherwise, measured from `last_seen_at`.
  `load_session` advances `last_seen_at` on every valid load, so normal
  use keeps pushing this window forward — throttled to at most once per
  `LAST_SEEN_WRITE_INTERVAL` to avoid a write on every single request.
- **Absolute expiry**: `created_at + session_absolute_days`, fixed once at
  creation and independent of `remember`/activity — no amount of use
  extends it; it is the hard ceiling idle expiry alone can't provide.

Both boundaries are "valid through this instant, inclusive": a session is
still usable exactly *at* its expiry timestamp and becomes invalid the
instant `now` exceeds it (`now > boundary`, never `>=`) — the same
convention for both checks, chosen so a session's last valid moment isn't
silently one tick earlier than its own recorded `expires_at` implies.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.config import Settings
from mailosh.db.models import AppUser, SessionRow
from mailosh.security import crypto, csrf

#: `__Host-` requires `Secure`, `Path=/`, and no `Domain` attribute (all
#: true of `cookie_params` below) — used only when the cookie will in fact
#: be `Secure`, since browsers reject a `__Host-`-prefixed cookie outright
#: otherwise (e.g. plain-http local dev).
COOKIE_SECURE_NAME = "__Host-sid"
COOKIE_PLAIN_NAME = "sid"

#: `load_session` writes `last_seen_at` at most this often per session,
#: not on every request. A public, named constant (rather than a bare
#: literal) so Task 5 can reason about — and if needed, test against —
#: exactly how stale `last_seen_at` is allowed to get under load.
LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=5)


def cookie_name(settings: Settings) -> str:
    """`__Host-sid` when the cookie will be `Secure`; plain `sid` otherwise
    (module docstring above `COOKIE_SECURE_NAME`).
    """
    return COOKIE_SECURE_NAME if settings.cookie_secure else COOKIE_PLAIN_NAME


def cookie_params(settings: Settings) -> dict:
    """The static `Response.set_cookie` kwargs shared by every session
    cookie. The dynamic parts — `key` (`cookie_name`), `value` (the
    session id), `max_age` (depends on `remember`) — are the caller's job
    (Task 5), not this function's.
    """
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.cookie_secure,
        "path": "/",
    }


def _aware(dt: datetime) -> datetime:
    """Coerce a datetime read back from the database to UTC-aware — see
    `mailosh.security.ratelimit._aware`'s docstring for why this is
    necessary (aiosqlite, used in unit tests, drops tzinfo on a fresh row
    load; Postgres never does). Every datetime this module writes starts
    out UTC-aware, so a naive value read back always means UTC.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def create_session(
    db: AsyncSession,
    *,
    user: AppUser,
    remember: bool,
    user_agent: str | None,
    ip: str | None,
    api_key_id: str,
    api_key_secret: str,
    settings: Settings,
) -> SessionRow:
    """Start a new session for `user`.

    The session id and CSRF token are drawn independently (`secrets.
    token_urlsafe(32)` each, the id here and the token via `csrf.
    new_token()`) — the CSRF token is never derived from, or equal to, the
    session id, so a value that leaks through one channel never hands over
    the other. `api_key_secret` is Fernet-encrypted before it is ever
    assigned to the row and is not otherwise retained by this function; its
    plaintext is not returned or logged.
    """
    now = datetime.now(UTC)
    row = SessionRow(
        id=secrets.token_urlsafe(32),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.session_absolute_days),
        remember=remember,
        user_agent=user_agent,
        ip=ip,
        api_key_id=api_key_id,
        api_key_secret_enc=crypto.encrypt(settings.secret_key, api_key_secret),
        csrf_token=csrf.new_token(),
    )
    db.add(row)
    await db.commit()
    return row


async def load_session(
    db: AsyncSession, sid: str, settings: Settings, now: datetime | None = None
) -> SessionRow | None:
    """Look up session `sid`, enforcing both expirations (module
    docstring). Returns `None` — never raises — for an unknown id or one
    that has expired either way.

    A still-valid session has `last_seen_at` advanced to `now` and
    committed, but only once at least `LAST_SEEN_WRITE_INTERVAL` has
    passed since it was last written (`now - last_seen_at >=
    LAST_SEEN_WRITE_INTERVAL`), so a burst of requests from one active user
    costs at most one write per interval.
    """
    now = now or datetime.now(UTC)
    row = await db.get(SessionRow, sid)
    if row is None:
        return None
    if now > _aware(row.expires_at):
        return None
    last_seen_at = _aware(row.last_seen_at)
    idle_days = settings.session_remember_days if row.remember else settings.session_idle_days
    if now - last_seen_at > timedelta(days=idle_days):
        return None
    if now - last_seen_at >= LAST_SEEN_WRITE_INTERVAL:
        row.last_seen_at = now
        await db.commit()
    return row


async def revoke_session(db: AsyncSession, sid: str) -> SessionRow | None:
    """Delete session `sid` and return the row that was deleted — so a
    caller (Task 5's `/logout`) can still read `api_key_id` off it to
    destroy the matching Stalwart API key — or `None` if `sid` doesn't
    exist.
    """
    row = await db.get(SessionRow, sid)
    if row is None:
        return None
    await db.delete(row)
    await db.commit()
    return row


async def revoke_all(db: AsyncSession, user_id: int) -> list[SessionRow]:
    """Delete every session belonging to `user_id` ("sign out everywhere")
    and return the deleted rows, so the caller can destroy each one's
    Stalwart API key in turn.
    """
    result = await db.execute(select(SessionRow).where(SessionRow.user_id == user_id))
    rows = list(result.scalars())
    for row in rows:
        await db.delete(row)
    await db.commit()
    return rows


def _is_live(row: SessionRow, settings: Settings, now: datetime) -> bool:
    """Both expirations, applied to one already-loaded row.

    The same two checks `load_session` makes per request and
    `expired_sessions` makes per row, in one place so a third caller cannot
    implement "still valid" a fourth way.
    """
    if now > _aware(row.expires_at):
        return False
    idle_days = settings.session_remember_days if row.remember else settings.session_idle_days
    return now - _aware(row.last_seen_at) <= timedelta(days=idle_days)


async def live_session_for_user(
    db: AsyncSession, user_id: int, settings: Settings, now: datetime | None = None
) -> SessionRow | None:
    """`user_id`'s most recently used session that is still valid, or `None`.

    The lookup behind a *capability* rather than a cookie. `mailosh.web.
    frames`'s inline-part route is reached by an `<img>` inside an
    opaque-origin document, which carries no session cookie at all; the
    signed token in its URL says which reader it was minted for, and this is
    how the server then finds a credential to act as that reader with — the
    per-session Stalwart API key is the only credential this app holds for a
    user, so "which of their sessions" is a question that has to be answered
    before any of their mail can be read.

    Three things it deliberately is not:

    - **Not a login.** It grants nothing on its own. The only caller reaches
      it having already verified an unexpired HMAC naming that user id, and
      what it hands back is used only to fetch the one part that token names.
    - **Not a way around logging out.** A reader who has signed out
      everywhere has no rows left here, so their outstanding capability URLs
      stop working at that moment rather than at their expiry — which is
      what makes "sign out everywhere" mean it for image URLs too.
    - **Not activity.** Unlike `load_session` this never advances
      `last_seen_at`. A subresource fetch is not a click, and a route with no
      cookie on it must not be able to keep a session alive.

    Ordered by `last_seen_at` so a reader with several devices is served
    through the session they are actually using, which is also the one whose
    client is already connected in `mailosh.jmap.pool`. Rows are filtered in
    Python for the reason `expired_sessions` gives: the idle threshold varies
    per row by its own `remember` flag. A user has a handful of sessions, not
    a table's worth.
    """
    now = now or datetime.now(UTC)
    result = await db.execute(
        select(SessionRow)
        .where(SessionRow.user_id == user_id)
        .order_by(SessionRow.last_seen_at.desc())
    )
    for row in result.scalars():
        if _is_live(row, settings, now):
            return row
    return None


def api_key_secret(session: SessionRow, settings: Settings) -> str:
    """Decrypt `session`'s stored Stalwart API key secret."""
    return crypto.decrypt(settings.secret_key, session.api_key_secret_enc)


async def expired_sessions(
    db: AsyncSession, settings: Settings, now: datetime | None = None
) -> list[SessionRow]:
    """Every session row that is past idle or absolute expiry right now —
    the reaper's "find" half (Task 5, controller ruling #2; design spec §9
    names both expirations, `load_session` above enforces them per-request,
    this is the same two checks applied to every row at once instead of one
    row on demand).

    Read-only: unlike `load_session`, this never touches `last_seen_at` and
    never deletes anything — the caller (`mailosh.web.auth.
    reap_expired_sessions`) decides, per row, whether a Stalwart API key
    needs destroying first (a row's session mate might still be live) before
    it deletes the row itself, so this primitive only ever answers "which
    rows are expired," matching exactly the boundary `mailosh.security`'s
    own package docstring draws: pure library code, no Stalwart calls here.

    Every row is loaded and checked in Python (not a single filtering SQL
    WHERE clause) because the idle threshold itself varies per row
    (`session_remember_days` vs. `session_idle_days`, by that row's own
    `remember` flag) — the same per-row branch `load_session` already makes.
    Fine for a periodic background sweep at this phase's scale; not a query
    any request-path code runs.
    """
    now = now or datetime.now(UTC)
    result = await db.execute(select(SessionRow))
    return [row for row in result.scalars() if not _is_live(row, settings, now)]
