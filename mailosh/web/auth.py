"""Login, logout, and the session reaper (Task 5, design spec §9).

Three routes (`GET`/`POST /login`, `POST /logout`, `POST /logout/all`) plus
`reap_expired_sessions`, the other half of `create_app`'s periodic
maintenance task (alongside `ClientPool.stop_idle`) — see that function's
own docstring for why it lives here rather than in
`mailosh.security.sessions` (it calls `StalwartAdmin`, which
`mailosh.security`'s package docstring keeps pure-library modules free of).

Controller ruling #1 (one Stalwart API key per USER, not per session — Task
4's live finding that Stalwart caps API keys at 5/account): `_mint_or_reuse_key`
below is the one place that mints; every other session for the same user
copies that key's material instead of minting its own. Ruling #2 (the
reaper must actively destroy Stalwart keys, not just let Postgres rows
expire) is `reap_expired_sessions`. Both are exercised directly by
`tests/unit/test_auth_routes.py`/`test_session_reaper.py`, not only through
the live stack.

Fix round 1 (post-review): every "count how many sessions reference this
user's key, then act on that count" sequence (`_mint_or_reuse_key`,
`_destroy_key_if_last_session`, `logout_all`'s destroy step,
`reap_expired_sessions`) is now serialized per user via `_lock_user_row` —
see that function's own docstring for why it stacks an in-process
`asyncio.Lock` (real, testable, and sufficient given design spec §9's
"single uvicorn worker in Phase 1") together with `SELECT ... FOR UPDATE`
(real, additional protection under Postgres; a documented no-op under
SQLite). Reviewer-reproduced bug this closes: a session reaped (or logged
out) while a *concurrent* login for the same user lands its own
`create_session` commit inside the destroy decision's window ends up with a
brand-new live session row pointing at an already-destroyed key.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.db.models import AppUser, SessionRow
from mailosh.jmap.errors import JmapError, TransportError
from mailosh.jmap.pool import ClientPool
from mailosh.security import csrf, ratelimit, sessions
from mailosh.security.exchange import verify_password
from mailosh.security.ratelimit import LoginLimiter
from mailosh.stalwart_admin import StalwartAdmin
from mailosh.web import deps

logger = logging.getLogger(__name__)

router = APIRouter()

#: Stateless helper (its own docstring: "safe to reuse anywhere") — one
#: instance for the whole app, matching how `mailosh.security.ratelimit`
#: itself expects to be used.
_limiter = LoginLimiter()

#: Per-user in-process locks (fix round 1, Finding 1) — see `_lock_user_row`.
_user_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def _lock_user_row(db: AsyncSession, user_id: int) -> AsyncIterator[None]:
    """Serialize every "count this user's sessions, then act on the count"
    sequence against every other one, for the same user, across two layers:

    1. An in-process `asyncio.Lock`, keyed by `user_id` (same pattern
       `mailosh.jmap.pool.ClientPool._locks` already uses for a different
       resource). Design spec §9 commits Phase 1 to a single uvicorn
       worker — every request handler *and* `create_app`'s own background
       maintenance task (the reaper) share one process and one event loop —
       so this alone gives TRUE mutual exclusion between e.g. a concurrent
       login's `_mint_or_reuse_key` and the reaper's `reap_expired_sessions`
       for the same user, and it is real and effective under whichever
       database backend a test happens to run against (unlike the layer
       below, this is what makes the regression test for this fix
       deterministic under the aiosqlite unit suite).
    2. `SELECT ... FOR UPDATE` on the user's own `app_user` row. Real,
       additional protection once a future multi-worker phase (spec §9:
       "Postgres LISTEN/NOTIFY fan-out is the Phase 2 path to multiple
       workers") removes the single-process guarantee layer 1 alone depends
       on — genuinely blocks a second Postgres transaction's own `FOR
       UPDATE` select on the same row until this one commits or rolls back.
       Silently dropped (not a syntax error, not a behavior change) under
       SQLite, which has no row-level locking — confirmed by inspecting the
       compiled SQL for the sqlite dialect: no `FOR UPDATE` clause appears
       at all. This is *why* layer 1 is not optional: it is the only real
       protection the unit suite's aiosqlite backend gets.

    Callers must keep every read that informs their decision, and the
    write/external call that acts on it, inside this same `async with`
    block — releasing either lock early re-opens exactly the window this
    exists to close.
    """
    async with _user_locks.setdefault(user_id, asyncio.Lock()):
        await db.execute(select(AppUser).where(AppUser.id == user_id).with_for_update())
        yield


#: Rendered for `auth/login.html` before any session (and therefore any
#: `UiPref` row) exists — "system"/"comfortable" are the same values
#: `mailosh.db.models.UiPref`'s own column defaults resolve to, so a
#: freshly-registered user's very first authenticated page renders with
#: identical theme/density to what they just saw on the login screen.
_DEFAULT_PREFS = {"theme": "system", "density": "comfortable"}


def _client_ip(request: Request, settings: Settings) -> str:
    """`request.client.host`, honouring `X-Forwarded-For` only when
    `settings.trust_proxy` is set (design spec §9's "useXForwarded for real
    client IPs" deployment note) — trusting that header unconditionally
    would let anyone spoof the IP the rate limiter/audit log key off of.
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client is not None else "unknown"


#: Where a login with no (or an unsafe) `next` lands. Named explicitly
#: rather than left as `/` — `/` is only a redirect to this same URL
#: (`mailosh.web.mail.root`), and depending on that indirection is how a
#: missing `/` route turned every default login into a bare
#: `{"detail": "Not Found"}`.
_DEFAULT_NEXT = "/mail/inbox"


def _safe_next(path: str | None) -> str:
    """Only ever a same-origin relative path — an open-redirect guard on
    the login form's `next` field. Anything else falls back to the inbox: an
    absolute URL (`https://evil`), a protocol-relative one (`//evil`), or a
    backslash (or mixed-slash) variant of the same trick (`/\\evil`,
    `/\\\\evil`) — a browser's URL parser treats a leading backslash the
    same as a forward slash when resolving the authority component (e.g.
    `new URL("/\\evil.example/", "https://mailosh.example/").host` is
    `"evil.example"`, not this app's own host), so backslashes are
    normalized to forward slashes *before* checking the second character
    rather than only ever matching a literal `//` prefix (fix round 1,
    Finding 3 — the original check missed this; it happened to be inert
    only because `RedirectResponse` percent-encodes a literal backslash in
    the `Location` header, not because the guard itself was correct).
    """
    if not path or not path.startswith("/"):
        return _DEFAULT_NEXT
    normalized = path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == "/":
        return _DEFAULT_NEXT
    return path


def _render_login(
    request: Request,
    *,
    error: str | None,
    status_code: int,
    username: str = "",
    next: str = _DEFAULT_NEXT,
) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            "error": error,
            "username": username,
            "next": next,
            "prefs": _DEFAULT_PREFS,
            # No session exists yet to own a CSRF token — the meta tag
            # renders empty (module docstring: this page is protected by
            # Sec-Fetch-Site + rate limiting instead, not a token).
            "csrf_token": "",
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = _DEFAULT_NEXT) -> HTMLResponse:
    return _render_login(request, error=None, status_code=200, next=next)


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


async def _mint_or_reuse_key(
    db: AsyncSession, admin: StalwartAdmin, user: AppUser, settings: Settings
) -> tuple[str, str]:
    """Controller ruling #1: one Stalwart API key per user. If any of this
    user's existing session rows already carries key material, copy it
    (id + decrypted secret — `create_session` re-encrypts the secret fresh
    onto the new row, same as it does for a newly minted key) rather than
    minting a second one; otherwise mint via `admin.create_api_key`.
    """
    result = await db.execute(
        select(SessionRow)
        .where(SessionRow.user_id == user.id, SessionRow.api_key_id.is_not(None))
        .limit(1)
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing.api_key_id, sessions.api_key_secret(existing, settings)
    key = await admin.create_api_key(user.stalwart_username, "mailosh-session")
    logger.debug("minted stalwart api key for %s: %r", user.stalwart_username, key)
    return key.id, key.secret


@router.post("/login")
async def login_submit(
    request: Request,
    db: Annotated[AsyncSession, Depends(deps.get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    remember: Annotated[str | None, Form()] = None,
    next: Annotated[str, Form()] = _DEFAULT_NEXT,
) -> Response:
    """Rate-limit -> verify -> get_or_create_user -> mint/reuse api key ->
    create_session -> set cookie -> audit `login.ok` -> 303 to `next`.

    Failure paths (design spec §9): a wrong password and an unknown account
    render the exact same copy/status code (`verified is None` covers both —
    `verify_password` itself never distinguishes them, and this route never
    calls `get_or_create_user` until AFTER a credential has actually been
    verified, so a wrong password for a nonexistent account can't even
    create an `AppUser` row as a side effect). A `TransportError` (mail
    server unreachable) renders different copy and, crucially, is NOT
    recorded as a rate-limiter failure — it says nothing about whether this
    password is right.
    """
    settings: Settings = request.app.state.settings
    admin: StalwartAdmin = request.app.state.admin
    ip = _client_ip(request, settings)

    # Login has no session yet to check a CSRF token against — protected by
    # this Fetch Metadata check plus rate limiting instead (module
    # docstring / design spec §9).
    if csrf.is_cross_site(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")

    wait = await _limiter.retry_after(db, ip=ip, account=username)
    if wait > 0:
        return _render_login(
            request,
            error=f"Too many attempts. Try again in {wait}s.",
            status_code=429,
            username=username,
            next=next,
        )

    try:
        verified = await verify_password(settings.stalwart_url, username, password)
    except TransportError:
        await repo.audit(db, None, "login.fail", {"account": username, "reason": "unreachable"}, ip)
        return _render_login(
            request,
            error="Can't reach the mail server right now.",
            status_code=200,
            username=username,
            next=next,
        )

    if verified is None:
        await _limiter.record_failure(db, ip=ip, account=username)
        await repo.audit(
            db, None, "login.fail", {"account": username, "reason": "bad_credentials"}, ip
        )
        return _render_login(
            request,
            error="Wrong email or password.",
            status_code=200,
            username=username,
            next=next,
        )

    await _limiter.reset(db, ip=ip, account=username)
    user = await repo.get_or_create_user(db, verified.username, verified.email)

    remember_bool = remember is not None
    # Locked for the full "check for an existing key to reuse, then create
    # this session row referencing it" sequence (fix round 1, Finding 1):
    # without this, a concurrent reap/logout for the same user could decide
    # to destroy the very key this reuse-check is about to read as valid.
    async with _lock_user_row(db, user.id):
        api_key_id, api_key_secret = await _mint_or_reuse_key(db, admin, user, settings)
        session_row = await sessions.create_session(
            db,
            user=user,
            remember=remember_bool,
            user_agent=request.headers.get("user-agent"),
            ip=ip,
            api_key_id=api_key_id,
            api_key_secret=api_key_secret,
            settings=settings,
        )
    user.last_login_at = datetime.now(UTC)
    await db.commit()
    await repo.audit(db, user.id, "login.ok", {"remember": remember_bool}, ip)

    response = RedirectResponse(url=_safe_next(next), status_code=303)
    cookie_kwargs = dict(sessions.cookie_params(settings))
    if remember_bool:
        cookie_kwargs["max_age"] = settings.session_remember_days * 86400
    response.set_cookie(sessions.cookie_name(settings), session_row.id, **cookie_kwargs)
    return response


# ---------------------------------------------------------------------------
# POST /logout, /logout/all
# ---------------------------------------------------------------------------


async def _destroy_key_if_last_session(
    db: AsyncSession, admin: StalwartAdmin, user: AppUser, api_key_id: str | None
) -> None:
    """After a session row is gone, destroy `user`'s Stalwart API key only
    if no other session row of theirs still references key material —
    controller ruling #1's other half (mint-once-reuse implies
    destroy-only-when-the-last-one-leaves).

    The count-then-destroy sequence is locked (fix round 1, Finding 1):
    without it, a concurrent login's own reuse-check could read this exact
    key as still valid between the count below and the `destroy_api_key`
    call.
    """
    if api_key_id is None:
        return
    async with _lock_user_row(db, user.id):
        remaining = await db.scalar(
            select(func.count()).select_from(SessionRow).where(SessionRow.user_id == user.id)
        )
        if remaining:
            return
        try:
            await admin.destroy_api_key(user.stalwart_username, api_key_id)
            logger.debug(
                "destroyed stalwart api key %s for %s (last session)",
                api_key_id,
                user.stalwart_username,
            )
        except JmapError:
            logger.warning(
                "failed to destroy stalwart api key %s for %s",
                api_key_id,
                user.stalwart_username,
                exc_info=True,
            )


@router.post("/logout")
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(deps.get_db)],
    session: Annotated[SessionRow, Depends(deps.require_session)],
    user: Annotated[AppUser, Depends(deps.current_user)],
    _csrf_ok: Annotated[None, Depends(deps.csrf_protect)],
) -> Response:
    """Revoke the current session, drop its pooled JMAP client, destroy the
    user's Stalwart API key only if this was their last session
    (`_destroy_key_if_last_session` — ruling #1), audit `logout`, and clear
    the cookie.
    """
    settings: Settings = request.app.state.settings
    admin: StalwartAdmin = request.app.state.admin
    pool: ClientPool = request.app.state.pool
    ip = _client_ip(request, settings)

    revoked = await sessions.revoke_session(db, session.id)
    await pool.drop(session.id)
    if revoked is not None:
        await _destroy_key_if_last_session(db, admin, user, revoked.api_key_id)

    await repo.audit(db, user.id, "logout", None, ip)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        sessions.cookie_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout/all")
async def logout_all(
    request: Request,
    db: Annotated[AsyncSession, Depends(deps.get_db)],
    session: Annotated[SessionRow, Depends(deps.require_session)],
    user: Annotated[AppUser, Depends(deps.current_user)],
    _csrf_ok: Annotated[None, Depends(deps.csrf_protect)],
) -> Response:
    """Sign out everywhere: revoke every session row for `user`, destroy
    the Stalwart API key exactly once (they all share the same key material
    — ruling #1), and drop every one of those sessions' pooled JMAP clients.
    The next login mints a fresh key.
    """
    settings: Settings = request.app.state.settings
    admin: StalwartAdmin = request.app.state.admin
    pool: ClientPool = request.app.state.pool
    ip = _client_ip(request, settings)

    # Locked for the FULL revoke -> recount -> destroy sequence (fix round
    # 2: round 1's fix locked only the destroy call, *after* `revoke_all`
    # had already run unlocked -- a concurrent login's own locked
    # mint-or-reuse-then-create_session could read the still-live row
    # `revoke_all` hadn't gotten to yet, or hadn't started yet at all,
    # reuse its key, and commit a brand-new session that `revoke_all`
    # never saw (it didn't exist at the moment `revoke_all` ran) -- which
    # this destroy call would then still destroy out from under it.
    # Acquiring the lock before `revoke_all` even runs closes this the
    # same way the other three sites are closed: a concurrent login can no
    # longer observe (or create against) this user's sessions while this
    # whole sequence is in flight.
    async with _lock_user_row(db, user.id):
        revoked_rows = await sessions.revoke_all(db, user.id)
        for row in revoked_rows:
            await pool.drop(row.id)

        api_key_id = next(
            (row.api_key_id for row in revoked_rows if row.api_key_id is not None), None
        )
        if api_key_id is not None:
            # Defensive re-check, mirroring `_destroy_key_if_last_session`:
            # held continuously since before `revoke_all`, this should
            # always come back zero -- kept explicit, the same reasoning
            # that function's own count is, rather than trusted implicitly.
            remaining = await db.scalar(
                select(func.count()).select_from(SessionRow).where(SessionRow.user_id == user.id)
            )
            if not remaining:
                try:
                    await admin.destroy_api_key(user.stalwart_username, api_key_id)
                    logger.debug(
                        "destroyed stalwart api key %s for %s (sign out everywhere)",
                        api_key_id,
                        user.stalwart_username,
                    )
                except JmapError:
                    logger.warning(
                        "failed to destroy stalwart api key %s for %s",
                        api_key_id,
                        user.stalwart_username,
                        exc_info=True,
                    )

    await repo.audit(db, user.id, "logout.all", {"session_count": len(revoked_rows)}, ip)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        sessions.cookie_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


# ---------------------------------------------------------------------------
# The reaper (controller ruling #2) — called from create_app's periodic
# maintenance task, alongside ClientPool.stop_idle.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReapSummary:
    """One sweep's results, logged at INFO by the caller (`mailosh.web.
    app`'s maintenance loop) as a one-line summary."""

    sessions_reaped: int
    login_attempts_pruned: int


async def reap_expired_sessions(
    db: AsyncSession,
    admin: StalwartAdmin,
    settings: Settings,
    pool: ClientPool,
    now: datetime | None = None,
) -> ReapSummary:
    """Find every session past idle/absolute expiry
    (`sessions.expired_sessions`); for each, destroy its user's Stalwart API
    key first if -- after this row is gone -- that user would have no
    session left (mirrors `_destroy_key_if_last_session` above: count
    *excluding* the row about to be deleted). Drop the row's pooled JMAP
    client (fix round 1, Finding 2 -- `stop_idle` alone never reaches a
    session that is still actively in use when it hits its own absolute
    expiry, leaving a live, connected client cached under a now-deleted
    session id), delete the row, and record a `session.reaped` audit entry
    either way. Finally prune stale `login_attempt` rows (`ratelimit.
    stale_login_attempts`).

    Processes rows one at a time, with the count-check, destroy decision,
    row delete, AND the commit that durably applies the delete
    (`repo.audit`'s own commit) all inside one `_lock_user_row` block (fix
    round 1, Finding 1): a concurrent login's `_mint_or_reuse_key` takes the
    same per-user lock before its own "does a session with this key already
    exist" read, so it can only ever observe this row fully gone (deleted
    *and committed*) or fully untouched -- never the in-between state where
    this sweep has decided to destroy the key but the row protecting that
    decision hasn't landed yet. This also still gets "destroy exactly once"
    right when several of one user's sessions expire in the very same
    sweep: each row's lock is acquired and released in turn, and whichever
    row is processed last sees the others already deleted, so the "any
    other session left?" count comes out right regardless of order, with no
    separate per-user grouping needed.

    A `destroy_api_key` failure is logged (`logger.warning`) and does not
    stop the sweep -- the Postgres row is deleted either way (Task 3's own
    review-flagged gap: an orphaned Stalwart credential is worse left to
    rot silently than surfaced once in the logs and cleaned up by hand).
    """
    now = now or datetime.now(UTC)
    expired = await sessions.expired_sessions(db, settings, now=now)
    for row in expired:
        async with _lock_user_row(db, row.user_id):
            if row.api_key_id is not None:
                remaining = await db.scalar(
                    select(func.count())
                    .select_from(SessionRow)
                    .where(SessionRow.user_id == row.user_id)
                )
                if remaining == 1:  # this row is the only one left -> it's the last
                    user = await db.get(AppUser, row.user_id)
                    if user is not None:
                        try:
                            await admin.destroy_api_key(user.stalwart_username, row.api_key_id)
                        except JmapError:
                            logger.warning(
                                "session reap: failed to destroy stalwart api key %s for %s",
                                row.api_key_id,
                                user.stalwart_username,
                                exc_info=True,
                            )
            await pool.drop(row.id)
            await db.delete(row)
            await repo.audit(
                db,
                row.user_id,
                "session.reaped",
                {"session_id": row.id, "api_key_id": row.api_key_id},
                row.ip,
            )

    stale_attempts = await ratelimit.stale_login_attempts(db, now=now)
    for attempt in stale_attempts:
        await db.delete(attempt)
    if stale_attempts:
        await db.commit()

    return ReapSummary(sessions_reaped=len(expired), login_attempts_pruned=len(stale_attempts))
