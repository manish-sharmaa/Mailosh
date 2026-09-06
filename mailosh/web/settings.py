"""Settings (design spec §10): `/settings/*`, the six full pages behind the
gear's quick-settings popover — Appearance, Reading, Compose, Labels,
Account, Security.

One router, prefix `/settings`, CSRF-protected at the router exactly the
way `mailosh.web.prefs` and `mailosh.web.labels` are (`csrf.validate`
exempts the GETs on method, so a page render pays only for the session
lookup it needs anyway). Every page is one template, `settings/page.html`,
that extends the same two layouts every other page in this app does —
`layouts/app.html` for a full navigation, `layouts/fragment.html` when
htmx asked for the `#main` half (the same `_is_fragment` test
`mailosh.web.mail` makes, restated here rather than imported because that
module's helpers are private to it) — and includes one `settings/<page>.html`
partial for its body. The sub-nav is a plain list of links in the same
`hx-get` → `#main` shape as `shell/nav.html`'s items, so moving between
the six pages is a fragment swap with the URL pushed, never a full load.

**Every save answers the contract the rest of the app already speaks.**
A form here posts through htmx with `hx-swap="none"`, and the route
answers `204 No Content` plus an `HX-Trigger` naming what happened:
`om:done` with a toast (drawn by `static/js/actions.js`'s body-level
listener, the same one every mutation in this app reports through) and,
for a preference, `om:prefs` with the canonical stored values (applied to
`<html>` by `static/js/app.js`'s `ui` store — which is what keeps the
quick-settings popover's radios in agreement with a theme changed here).
A refusal is a `200` + `om:error` with a sentence the reader can read,
never a 4xx (`mailosh.web.app._error_toast` explains why the status has
to be 200), and never a bare 500.

The `prefs` pages reuse `mailosh.web.prefs`'s `Literal` value sets rather
than restating them: a value outside the set is a 422 before the handler
body runs, from the same place the popover's own route gets that check.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db import repo
from mailosh.db.models import AppUser, SessionRow, UiPref, Visibility
from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import JmapError, TransportError
from mailosh.jmap.models import Identity
from mailosh.security import sessions
from mailosh.security.exchange import verify_password
from mailosh.services.compose import clean_signature, list_identities
from mailosh.services.mailbox_tree import NavModel, build_nav
from mailosh.ui.format import LABEL_COLORS
from mailosh.web import deps
from mailosh.web.labels import VISIBILITIES
from mailosh.web.prefs import (
    AutoAdvance,
    DefaultReply,
    Density,
    Flag,
    FontSize,
    MarkReadDelay,
    ReadingPane,
    RemoteImages,
    Theme,
    UndoSend,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(deps.csrf_protect)])

SessionDep = Annotated[SessionRow, Depends(deps.require_session)]
UserDep = Annotated[AppUser, Depends(deps.current_user)]
PrefsDep = Annotated[UiPref, Depends(deps.prefs_for)]
ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]

#: The six pages, in sub-nav order (spec §10's own order). The key is the
#: URL segment and the partial's file name; the label is what the sub-nav
#: and the `<title>` say.
PAGES: tuple[tuple[str, str], ...] = (
    ("appearance", "Appearance"),
    ("reading", "Reading"),
    ("compose", "Compose"),
    ("labels", "Labels"),
    ("account", "Account"),
    ("security", "Security"),
)
Page = Literal["appearance", "reading", "compose", "labels", "account", "security"]

#: Where a bare `/settings` lands — the first page, which is also the one
#: the quick-settings popover is a subset of.
FIRST_PAGE = "/settings/appearance"


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _is_fragment(request: Request) -> bool:
    """Whether to answer with the bare `#main` fragment — the same two-header
    test `mailosh.web.mail._is_fragment` makes, for the same reason: a
    history-restore fetch wants a whole document back.
    """
    return (
        request.headers.get("hx-request") == "true"
        and request.headers.get("hx-history-restore-request") != "true"
    )


def _trigger(payload: dict[str, object]) -> dict[str, str]:
    """One `HX-Trigger` header value, serialized the way every other module
    in this app serializes one (`ensure_ascii` on, compact separators).
    """
    return {"HX-Trigger": json.dumps(payload, separators=(",", ":"))}


def _saved(toast: str = "Saved", *, prefs: dict[str, object] | None = None) -> Response:
    """`204` + `om:done` for a save with nothing to undo — a preference is
    reversible by hand, and an undo token for "set theme back" would be a
    second copy of the control that just did it.

    `undo: null` and `refresh: false` are named explicitly because
    `applyDone` (`static/js/actions.js`) reads both. `prefs`, when given,
    rides along as `om:prefs`, the trigger `static/js/app.js` applies to
    `<html>` — which is how a theme saved here changes the page it was
    saved on, and how the quick-settings popover learns about it.
    """
    payload: dict[str, object] = {
        "om:done": {"toast": toast, "undo": None, "removed": [], "counts": {}, "refresh": False}
    }
    if prefs:
        payload["om:prefs"] = prefs
    return Response(status_code=204, headers=_trigger(payload))


def _refused(message: str) -> Response:
    """A refusal the reader can read, as a `200` + `om:error` — see
    `mailosh.web.labels._error` for why the status is not a 4xx.
    """
    return Response(
        status_code=200,
        headers={
            "HX-Reswap": "none",
            "HX-Push-Url": "false",
            **_trigger({"om:error": {"toast": message, "retry": False}}),
        },
    )


async def _nav_for(client: JmapClient, db: AsyncSession, user: AppUser) -> NavModel:
    """The sidebar's model — one `Mailbox/get` plus this user's `LabelMeta`,
    the same pair every page render pays for. `active_key=""`: a settings
    page is no mailbox, so nothing in the rail is current.
    """
    label_meta = await repo.label_meta_map(db, user.id, client.account_id)
    return await build_nav(client, active_key="", label_meta=label_meta)


async def _context(
    request: Request,
    page: str,
    *,
    session: SessionRow,
    user: AppUser,
    prefs: UiPref,
    client: JmapClient,
    db: AsyncSession,
    **extra: object,
) -> dict[str, object]:
    """Everything `settings/page.html` reads: the layout keys every page in
    this app needs (`prefs`, `csrf_token`, `nav`, `user`, `key`), which page
    this is, and whatever the page's own partial needs on top.
    """
    context: dict[str, object] = {
        "prefs": prefs,
        "csrf_token": session.csrf_token,
        "nav": await _nav_for(client, db, user),
        "user": user,
        "key": "",
        "layout": "layouts/app.html",
        "fragment": False,
        "page": page,
        "page_label": dict(PAGES)[page],
        "pages": PAGES,
        **extra,
    }
    if _is_fragment(request):
        context["layout"] = "layouts/fragment.html"
        context["fragment"] = True
    return context


def _render(request: Request, context: dict[str, object]) -> HTMLResponse:
    return _templates(request).TemplateResponse(request, "settings/page.html", context)


@router.get("")
async def settings_root() -> RedirectResponse:
    """`/settings` is the section, not a page: it lands on the first one."""
    return RedirectResponse(url=FIRST_PAGE, status_code=303)


@router.get("/{page}", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    page: Page,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
) -> HTMLResponse:
    """One of the six pages, as a whole document or as the `#main` fragment.

    `page` is a `Literal`, so `/settings/anything-else` is FastAPI's own
    422 rather than a template lookup that fails inside Jinja. Each page's
    extra context is gathered by the loader registered for it below; a page
    with no loader renders from `prefs` alone.
    """
    loader = _LOADERS.get(page)
    extra = await loader(request, user=user, client=client, db=db) if loader else {}
    context = await _context(
        request, page, session=session, user=user, prefs=prefs, client=client, db=db, **extra
    )
    return _render(request, context)


# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------


@router.post("/appearance")
async def save_appearance(
    user: UserDep,
    db: DbDep,
    theme: Annotated[Theme, Form()],
    density: Annotated[Density, Form()],
    reading_pane: Annotated[ReadingPane, Form()],
    font_size: Annotated[FontSize, Form()],
) -> Response:
    """Persist the four Appearance fields and echo them as `om:prefs`.

    All four are required, unlike `POST /prefs`'s partial updates: this is
    a whole form with a Save button, and a form always posts every one of
    its radio groups. The `om:prefs` payload names all four so the page
    that posted them — and the quick-settings popover behind the gear —
    end up showing exactly what was stored, whatever they showed before.
    """
    changed: dict[str, object] = {
        "theme": theme,
        "density": density,
        "reading_pane": reading_pane,
        "font_size": font_size,
    }
    await repo.set_prefs(db, user.id, **changed)
    return _saved(prefs=changed)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@router.post("/reading")
async def save_reading(
    user: UserDep,
    db: DbDep,
    conversation_view: Annotated[Flag, Form()],
    mark_read_delay: Annotated[MarkReadDelay, Form()],
    auto_advance: Annotated[AutoAdvance, Form()],
    remote_images: Annotated[RemoteImages, Form()],
    dark_restyle: Annotated[Flag, Form()],
) -> Response:
    """Persist the five Reading fields.

    Each lands at its column's own type — the two flags as `bool`, the
    delay as `int` — for the reason `mailosh.web.prefs.update_prefs` gives:
    `repo.set_prefs` sets attributes straight onto the row, and a string in
    an integer column reads back as a string on sqlite. `om:prefs` carries
    them too; `app.js` ignores names it does not own, and
    `static/js/settings.js` uses them to re-check the popover's radios.
    """
    changed: dict[str, object] = {
        "conversation_view": conversation_view == "true",
        "mark_read_delay": int(mark_read_delay),
        "auto_advance": auto_advance,
        "remote_images": remote_images,
        "dark_restyle": dark_restyle == "true",
    }
    await repo.set_prefs(db, user.id, **changed)
    return _saved(prefs=changed)


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

#: A signature longer than this is not a signature, it is a document. The
#: ceiling is checked on the *submitted* HTML, before sanitising, so a
#: megabyte of markup is refused rather than parsed first.
MAX_SIGNATURE_CHARS = 20_000


async def _identities(client: JmapClient) -> list[Identity]:
    """The account's send-as addresses, or none if they cannot be listed.

    Swallowing every exception, exactly as `mailosh.web.compose._identities`
    does and for the same reason: an `Identity/get` that is unavailable
    must not take down a settings page whose other half is a pair of
    radio groups this reader can still usefully change.
    """
    try:
        return list(await list_identities(client))
    except Exception:
        logger.warning("settings: could not list identities; signatures omitted", exc_info=True)
        return []


async def _compose_page(
    request: Request, *, user: AppUser, client: JmapClient, db: AsyncSession
) -> dict[str, object]:
    """The Compose page's own context: one signature textarea per identity.

    One `Identity/get` and one `signature_map` query, whatever the account
    has — never one lookup per address. An identity with no row saved gets
    `""`, which is what an empty textarea posts back, so "never configured"
    and "deliberately blank" render identically because they mean the same
    thing.
    """
    identities = await _identities(client)
    saved = await repo.signature_map(db, user.id, client.account_id)
    return {
        "identities": identities,
        "signatures": {identity.id: saved.get(identity.id, "") for identity in identities},
    }


@router.post("/compose")
async def save_compose(
    user: UserDep,
    db: DbDep,
    undo_send_seconds: Annotated[UndoSend, Form()],
    default_reply: Annotated[DefaultReply, Form()],
) -> Response:
    """Persist the undo-send window and what a plain Reply means.

    `undo_send_seconds` arrives as a string `Literal` and is cast on the
    way to its `Integer` column, the same pair of reasons
    `mailosh.web.prefs` gives for `mark_read_delay`: the `Literal` is what
    refuses a number the control could never have produced, and the cast is
    what keeps the column an integer on sqlite.

    Both ride out on `om:prefs`, which is how the compose dock currently on
    screen would learn a new window without a reload — `data-undo-send-ms`
    is rendered per dock, so the *next* one opened picks it up either way.
    """
    changed: dict[str, object] = {
        "undo_send_seconds": int(undo_send_seconds),
        "default_reply": default_reply,
    }
    await repo.set_prefs(db, user.id, **changed)
    return _saved(prefs=changed)


@router.post("/compose/signature")
async def save_signature(
    user: UserDep,
    db: DbDep,
    client: ClientDep,
    identity_id: Annotated[str, Form()],
    html: Annotated[str, Form()] = "",
) -> Response:
    """Save one identity's signature, sanitised.

    `identity_id` is checked against the account's *actual* identities
    rather than trusted: it is a form field, and a row keyed by an id this
    account does not own would be a signature nobody could ever see or
    delete again.

    The stored value is `clean_signature`'s — the project's nh3 pipeline —
    so a `<script>`, an `onerror=` or a `javascript:` href never reaches
    the table, let alone somebody else's mailbox. `mailosh.web.compose`
    runs the same pass again on the way into a draft; see
    `mailosh.services.compose.clean_signature` for why both.
    """
    if len(html) > MAX_SIGNATURE_CHARS:
        return _refused(f"That signature is too long (limit {MAX_SIGNATURE_CHARS:,} characters).")
    identities = await _identities(client)
    if not any(identity.id == identity_id for identity in identities):
        return _refused("That isn't one of your addresses.")
    await repo.set_signature(db, user.id, client.account_id, identity_id, clean_signature(html))
    return _saved("Signature saved")


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


async def _labels_page(
    request: Request, *, user: AppUser, client: JmapClient, db: AsyncSession
) -> dict[str, object]:
    """Every label this account has, with its colour, visibility and parent
    — the page's whole model, in one `Mailbox/get` and one `label_meta_map`.

    Built here rather than from `build_nav`, and that is the point of the
    page. `mailosh.services.mailbox_tree._build_label_tree` drops every
    `hide` label structurally (so a hidden label can never become a chip),
    which is right for the nav and exactly wrong here: this is the one
    surface that has to be able to un-hide one. Reading the mailboxes
    directly is what keeps a hidden label reachable by the only control
    that can bring it back.

    Rows are flat and ordered by their full path, each carrying its nesting
    `depth` for the indent — a tree of `<table>` rows would have to be
    flattened for rendering anyway, and the depth is the only part of the
    nesting this page draws.
    """
    mailboxes = await client.get_mailboxes()
    meta = await repo.label_meta_map(db, user.id, client.account_id)
    labels = [mailbox for mailbox in mailboxes if mailbox.role is None]
    by_id = {mailbox.id: mailbox for mailbox in labels}

    def path(mailbox: object) -> list[str]:
        """This label's name preceded by its ancestors', lowercased.

        `seen` is not defensive tidiness: `parent_id` comes from the
        server, and a cycle in it would spin here forever rather than
        render a page.
        """
        parts: list[str] = []
        seen: set[str] = set()
        node = mailbox
        while node is not None and node.id not in seen:
            seen.add(node.id)
            parts.append(node.name.lower())
            node = by_id.get(node.parent_id) if node.parent_id else None
        parts.reverse()
        return parts

    rows: list[dict[str, object]] = []
    for mailbox in sorted(labels, key=path):
        row_meta = meta.get(mailbox.id)
        rows.append(
            {
                "id": mailbox.id,
                "name": mailbox.name,
                "depth": len(path(mailbox)) - 1,
                "color": row_meta.color if row_meta is not None else None,
                "visibility": Visibility.parse(
                    row_meta.visibility if row_meta is not None else None
                ).value,
                "parent_id": mailbox.parent_id or "",
                "unread": mailbox.unread_emails,
            }
        )
    return {"labels": rows, "colors": LABEL_COLORS, "visibilities": VISIBILITIES}


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

#: The shortest password this page will set. Stalwart itself imposes no
#: minimum, so if this one did not exist the page would happily accept a
#: single character — and the reader would have no way of knowing the
#: server did not have an opinion about it.
MIN_PASSWORD_CHARS = 10

#: Longest display name accepted. `AppUser.display_name` is `String(255)`
#: and Stalwart's `description` is free text; the ceiling is here so a
#: paste of an entire document is refused with a sentence rather than by a
#: database error.
MAX_DISPLAY_NAME_CHARS = 120


async def _revoke_other_sessions(
    request: Request, db: AsyncSession, user: AppUser, keep: str
) -> int:
    """Sign every *other* browser of `user`'s out, and return how many.

    Called after a password change, which is the one moment where "sign out
    everywhere" is not a separate thing the reader might also want: a
    password is changed either because it is being rotated or because
    somebody else may have it, and both answers are the same.

    Not `sessions.revoke_all`, which would take this session down too and
    bounce the reader to /login the instant their change succeeded. The
    Stalwart API key every session of theirs shares is deliberately left
    alone for the same reason `mailosh.web.auth._destroy_key_if_last_session`
    leaves it alone while any session remains: this one is still using it.
    """
    rows = (
        (await db.execute(select(SessionRow).where(SessionRow.user_id == user.id))).scalars().all()
    )
    pool = request.app.state.pool
    revoked = 0
    for row in rows:
        if row.id == keep:
            continue
        await sessions.revoke_session(db, row.id)
        await pool.drop(row.id)
        revoked += 1
    return revoked


@router.post("/account/name")
async def save_display_name(
    request: Request,
    user: UserDep,
    db: DbDep,
    display_name: Annotated[str, Form()] = "",
) -> Response:
    """Set the human label shown for this account, on the mail server and
    on the `AppUser` row that renders the avatar and the account menu.

    Stalwart first, and the local row only once that returned: the mail
    server is where the name is *published* (it is what appears in the From
    of everything sent), so a local row updated ahead of a failed remote
    call would show a name nobody receiving mail would ever see.
    """
    name = display_name.strip()
    if len(name) > MAX_DISPLAY_NAME_CHARS:
        return _refused(f"That name is too long (limit {MAX_DISPLAY_NAME_CHARS} characters).")
    admin = request.app.state.admin
    try:
        await admin.set_display_name(user.stalwart_username, name)
    except TransportError:
        return _refused("Can't reach the mail server right now.")
    except JmapError:
        logger.warning("settings: could not set display name for %s", user.id, exc_info=True)
        return _refused("The mail server wouldn't accept that name.")
    user.display_name = name or None
    await db.commit()
    return _saved("Name saved")


@router.post("/account/password")
async def change_password(
    request: Request,
    session: SessionDep,
    user: UserDep,
    db: DbDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    """Change this account's mailbox password.

    **The current password is verified the way logging in verifies one** —
    `mailosh.security.exchange.verify_password`, a real credential check
    against Stalwart itself — rather than by trusting the session cookie.
    A session is proof that somebody logged in as this user at some point;
    it is not proof that the person at the keyboard right now knows the
    password, and a borrowed laptop is exactly the case this stops.

    On success every *other* session of this user's is revoked and the
    response says how many. That is not a courtesy: the two reasons anyone
    changes a password are rotation and suspicion, and leaving a session
    someone else opened alive would defeat the second one silently.

    A `TransportError` is reported as "can't reach the mail server", never
    as a wrong password — the same distinction `mailosh.web.auth.
    login_submit` draws, and for the same reason: an unreachable server
    says nothing about the credential.
    """
    settings = request.app.state.settings
    if new_password != confirm_password:
        return _refused("Those two passwords don't match.")
    if new_password == current_password:
        return _refused("That is the password you already have.")
    if len(new_password) < MIN_PASSWORD_CHARS:
        return _refused(f"Use at least {MIN_PASSWORD_CHARS} characters.")

    try:
        verified = await verify_password(
            settings.stalwart_url, user.stalwart_username, current_password
        )
    except TransportError:
        return _refused("Can't reach the mail server right now.")
    if verified is None:
        await repo.audit(db, user.id, "password.fail", {"reason": "bad_current"}, None)
        return _refused("That isn't your current password.")

    admin = request.app.state.admin
    try:
        await admin.set_password(user.stalwart_username, new_password)
    except TransportError:
        return _refused("Can't reach the mail server right now.")
    except JmapError:
        logger.warning("settings: could not set password for %s", user.id, exc_info=True)
        return _refused("The mail server wouldn't accept that password.")

    revoked = await _revoke_other_sessions(request, db, user, keep=session.id)
    await repo.audit(db, user.id, "password.change", {"sessions_revoked": revoked}, None)
    if revoked:
        others = "session" if revoked == 1 else "sessions"
        return _saved(f"Password changed — {revoked} other {others} signed out")
    return _saved("Password changed")


#: Per-page context loaders — `page` -> coroutine returning that page's
#: extra template context. Filled in by the sections below as each page's
#: data needs arrive; a page absent here renders from `prefs` alone.
_LOADERS: dict[str, object] = {
    "compose": _compose_page,
    "labels": _labels_page,
}
