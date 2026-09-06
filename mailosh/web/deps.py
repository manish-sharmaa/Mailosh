"""FastAPI dependency providers for the web app (Task 5, design spec §9):
resolving the session cookie into a `SessionRow`/`AppUser`, gating routes
that require one, CSRF protection, the per-session JMAP client, and UI
prefs. Every dependency here reads something `create_app`'s lifespan
already built on `app.state` (`settings`, `sessionmaker`, `pool`) — none of
them construct a `Settings()`/engine/client of their own, the same "read
app.state, don't build" shape `mailosh.db.session.get_db` already used.

`get_db` is re-exported from `mailosh.db.session` rather than redefined
here, so every route/other dependency can import one `deps.get_db` without
needing to know it happens to live in a different module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.db.models import AppUser, SessionRow, UiPref
from mailosh.db.session import get_db
from mailosh.jmap.client import JmapClient
from mailosh.jmap.pool import ClientPool
from mailosh.security import csrf, sessions

__all__ = [
    "SessionRequired",
    "client_for",
    "csrf_protect",
    "current_session",
    "current_user",
    "get_db",
    "prefs_for",
    "require_session",
]


class SessionRequired(Exception):
    """Raised by `require_session` when no valid session cookie is present.

    Not an `HTTPException`: the two "please log in" shapes the brief
    specifies (a 303 redirect for a full page, a 401 + `HX-Redirect` header
    for an HX request) depend on a request header `require_session` itself
    has no business inspecting — that's `create_app`'s own exception
    handler's job (registered once, in `mailosh.web.app`), which is what
    actually catches this and builds the right response.
    """

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url
        super().__init__(f"session required, redirecting to login (next={next_url!r})")


def _next_url(request: Request) -> str:
    """The path (+ query string, if any) `request` was trying to reach —
    threaded through to `/login?next=...` so a successful login lands the
    user back where they started, instead of always at `/`.
    """
    path = request.url.path
    return f"{path}?{request.url.query}" if request.url.query else path


async def current_session(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> SessionRow | None:
    """The current request's `SessionRow`, or `None` if there's no cookie,
    or it names a session that doesn't exist / has expired
    (`sessions.load_session` never raises for either case).
    """
    settings: Settings = request.app.state.settings
    sid = request.cookies.get(sessions.cookie_name(settings))
    if sid is None:
        return None
    return await sessions.load_session(db, sid, settings)


async def require_session(
    request: Request, session: Annotated[SessionRow | None, Depends(current_session)]
) -> SessionRow:
    """`current_session`, but raises `SessionRequired` instead of returning
    `None` — the dependency every protected route/other dependency
    (`current_user`, `csrf_protect`, `client_for`) actually depends on.
    """
    if session is None:
        raise SessionRequired(next_url=_next_url(request))
    return session


async def current_user(
    session: Annotated[SessionRow, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppUser:
    """The `AppUser` `session` belongs to. `SessionRow.user_id` is an
    `app_user.id` foreign key, so a missing row here would mean the user
    was deleted out from under a still-valid session — never expected in
    practice, but treated the same as "please log in again" rather than a
    500 if it ever happened.
    """
    user = await db.get(AppUser, session.user_id)
    if user is None:
        raise SessionRequired(next_url="/")
    return user


async def csrf_protect(
    request: Request, session: Annotated[SessionRow, Depends(require_session)]
) -> None:
    """Guard clause for every mutating route: requires a valid session
    (`require_session`, so an unauthenticated POST redirects to login
    rather than 403ing on a CSRF check it could never pass anyway) AND a
    matching CSRF token (design spec §9) — the header htmx sends via
    `hx-headers`, or a `csrf_token` form field for a plain `<form>` POST
    (`mailosh.web.auth`'s `/logout`/`/logout/all`).
    """
    form_token: str | None = None
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        value = form.get("csrf_token")
        form_token = value if isinstance(value, str) else None
    csrf.validate(request, session.csrf_token, form_token)


async def client_for(
    request: Request, session: Annotated[SessionRow, Depends(require_session)]
) -> JmapClient:
    """The connected `JmapClient` for the current session, from
    `app.state.pool` (`mailosh.jmap.pool.ClientPool`) — built once per
    session and reused, never reconnected per request.
    """
    pool: ClientPool = request.app.state.pool
    settings: Settings = request.app.state.settings
    return await pool.get(session, settings)


async def prefs_for(
    user: Annotated[AppUser, Depends(current_user)], db: Annotated[AsyncSession, Depends(get_db)]
) -> UiPref:
    """`user`'s `UiPref` row (`mailosh.db.repo.get_prefs`, which creates an
    all-defaults row the first time a user is looked up) — the `prefs`
    template context every authenticated page's `layouts/app.html` needs
    for `data-theme`/`data-density`.
    """
    return await repo.get_prefs(db, user.id)


def viewer_now(request: Request) -> datetime:
    """The current time *in the reader's timezone*, for every "today" /
    "yesterday" / clock-time decision a page makes.

    `mailosh.ui.format.format_date` has always done its calendar arithmetic
    in `now.tzinfo`; every caller passed `datetime.now(UTC)`, so every time
    in the UI was a UTC time and "today" turned over at midnight UTC — 05:30
    in the morning for a reader in India. `app.js` writes the browser's IANA
    zone into a `tz` cookie on load; this reads it back. An unknown or
    missing zone falls back to UTC rather than failing the page.
    """
    name = request.cookies.get("tz", "")
    if name and len(name) <= 64:
        try:
            return datetime.now(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now(UTC)
