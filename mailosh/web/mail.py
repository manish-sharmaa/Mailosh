"""The list-first mail shell (design spec §4.3/§5/§6.4/§7): the app frame,
the left nav, the conversation list and the conversation itself.

Four routes, all thin — every one of them turns a nav key into a view model
via `mailosh.services` and hands it to a template, with no JMAP filter, date
formatting or label logic of its own:

- ``GET /`` -> 303 ``/mail/inbox``. The one place in the app that decides
  where "the app" starts.
- ``GET /mail/{key}`` -> the full page, or just the ``#main`` fragment when
  htmx asked for it (``HX-Request``), plus an out-of-band re-render of the
  nav so the active item/counts follow along.
- ``GET /mail/{key}/rows`` -> one page of rows, ending in the intersect
  sentinel **only when another page exists**.
- ``GET /t/{thread_id}`` -> the conversation, under the same shell,
  optionally told where in a mailbox it sits (``?key=&pos=``).
- ``GET /mail/{key}/at/{position}`` -> the conversation *at* that place in
  the mailbox, resolved and rendered in one round trip so the header's
  ``<`` / ``>`` arrows cost a single request rather than a redirect.

`key` is always validated against the nav model before anything queries
(`_valid_keys`): a reserved system key, or a real label's own mailbox id.
Anything else is a 404 page. `mailosh.services.mailbox_tree.resolve_mailbox`
deliberately passes an unknown key straight through (a label *is* its own
mailbox id — it has no way to tell a typo from a label), so without this
guard `/mail/<anything>` would reach `Email/query` as an `inMailbox` filter
against an id the server has never heard of.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db import repo
from mailosh.db.models import AppUser, LabelMeta, SessionRow, UiPref
from mailosh.jmap.client import JmapClient
from mailosh.jmap.models import EmailBody
from mailosh.render.css_sanitize import sanitize_stylesheet
from mailosh.render.dark import background_is_light, declares_color_scheme, restyle_mode
from mailosh.render.html_sanitize import extract_styles
from mailosh.services import outbound
from mailosh.services.conversation import ConversationView, build_conversation
from mailosh.services.mailbox_tree import LabelNode, NavModel, build_nav
from mailosh.services.thread_list import ThreadPage, build_page
from mailosh.web import deps

#: Where the app starts, and what a login with no `next` lands on.
INBOX_URL = "/mail/inbox"

#: Rows per page (spec §5.3: "50 per page (100 selectable)"). `limit` is a
#: query parameter so the sentinel can echo the page size it was served
#: with, but it is clamped to this ceiling — a hand-edited `?limit=100000`
#: must not turn one `Email/query` into an unbounded `Email/get` of every
#: message in the account.
PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

router = APIRouter(tags=["mail"])

SessionDep = Annotated[SessionRow, Depends(deps.require_session)]
UserDep = Annotated[AppUser, Depends(deps.current_user)]
PrefsDep = Annotated[UiPref, Depends(deps.prefs_for)]
ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]


def _label_ids(nodes: list[LabelNode]) -> set[str]:
    ids: set[str] = set()
    for node in nodes:
        ids.add(node.mailbox_id)
        ids |= _label_ids(node.children)
    return ids


def _valid_keys(nav: NavModel) -> set[str]:
    """Every `key` this app will route to: the eight reserved system/more
    keys plus each label's own mailbox id, nested labels included.

    A `hide`-visibility label is deliberately absent (`build_nav` drops it
    from the tree entirely), so its URL 404s the same as a typo — consistent
    with "hidden labels produce no nav row and no chip": a hidden label has
    no address in this UI at all.
    """
    return {item.key for item in (*nav.system, *nav.more)} | _label_ids(nav.labels)


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _base_context(
    request: Request, *, session: SessionRow, user: AppUser, prefs: UiPref, nav: NavModel
) -> dict[str, object]:
    """The context every page in this module shares: what `layouts/app.html`
    needs (`prefs`, `csrf_token`) plus the nav model and the viewer, and the
    `fragment` flag that decides which layout the template extends.

    `fragment` is false here; each route flips it (and swaps `layout`) when
    htmx asked for a partial — see `_layout_for`.
    """
    return {
        "prefs": prefs,
        "csrf_token": session.csrf_token,
        "nav": nav,
        "user": user,
        "layout": "layouts/app.html",
        "fragment": False,
        # The nav highlights a label by its own mailbox id; a page with no
        # mailbox of its own (the thread view) simply matches nothing.
        "key": "",
    }


def _is_fragment(request: Request) -> bool:
    """Whether to answer with the bare `#main` fragment.

    `HX-Request` alone is not enough: htmx sets it on a *history restore*
    fetch too (`HX-History-Restore-Request`), and what it wants back there
    is a whole page to rebuild the document from — answering that with a
    fragment leaves the browser showing a chrome-less list.
    """
    return (
        request.headers.get("hx-request") == "true"
        and request.headers.get("hx-history-restore-request") != "true"
    )


def _apply_fragment(context: dict[str, object]) -> None:
    context["layout"] = "layouts/fragment.html"
    context["fragment"] = True


def _bounce_headers(request: Request, context: dict[str, object]) -> dict[str, str]:
    """The `HX-Trigger` that announces newly discovered bounces, or nothing.

    Reuses the app's failure toast verbatim — `om:error`, the same header
    shape `mailosh.web.app._error_toast` sends and the same body-level
    listener in `static/js/actions.js` consumes — because a bounce *is* a
    failure the reader needs to hear about once, and a second toast
    mechanism for it would be one more thing to keep in step. `retry` is
    false: there is nothing for the client to re-request.

    Only for an htmx request: a full-page GET has no htmx to read the
    header, and the toasts were already marked announced when
    `_list_context` took them, so they would be lost. `_list_context` is
    the only writer of `outbound_toasts`, and `take_unannounced_bounces`
    runs inside it, so a full-page render that finds bounces is the one
    case that (deliberately) says nothing — the row's own pill carries the
    state from then on.
    """
    toasts = context.get("outbound_toasts") or []
    if not toasts or request.headers.get("hx-request") != "true":
        return {}
    return {
        "HX-Trigger": json.dumps(
            {"om:error": {"toast": "; ".join(toasts), "retry": False}},
            separators=(",", ":"),
        )
    }


async def _nav_for(
    client: JmapClient, db: AsyncSession, user: AppUser, active_key: str
) -> NavModel:
    label_meta = await repo.label_meta_map(db, user.id, client.account_id)
    return await build_nav(client, active_key=active_key, label_meta=label_meta)


def _not_found(request: Request, context: dict[str, object], *, message: str) -> HTMLResponse:
    """The 404 *page* (never a bare JSON `{"detail": ...}`): the real shell,
    with the real nav, so the way out of a dead URL is one click away — or
    just the `#main` half of it when htmx is the one asking.
    """
    context = {**context, "message": message}
    if _is_fragment(request):
        _apply_fragment(context)
    return _templates(request).TemplateResponse(
        request, "list/not_found.html", context, status_code=404
    )


def _range_label(page: ThreadPage, start: int) -> str:
    """The toolbar's range readout (spec §5.3), en dash and thousands
    separators included. Reads `ThreadPage.total` — the server's own
    `calculateTotal` — never the length of anything rendered.

    It describes *the rows on screen*, which do not always begin where this
    page begins: the endless-scroll sentinel appends page 2 beneath a page 1
    that is still there, so this response's own `position` would read
    "51-100 of 240" over a list showing a hundred rows. `start` is where the
    rendered list begins — carried by the request that extends it
    (`/rows?…&start=`), and defaulting to `page.position`, which is right for
    a first render and right for the `#list` refresh too: that one re-fetches
    from its own position with a `limit` app.js has already grown to cover
    everything the sentinel appended (`syncListLimit`).
    """
    if page.total == 0:
        return "0 of 0"
    first = start + 1
    last = min(page.position + len(page.rows), page.total)
    return f"{first:,}\u2013{last:,} of {page.total:,}"


def _active_item(nav: NavModel, key: str) -> tuple[str, int | None]:
    """`(label, unread count)` for the active view — the document title's
    `(3) Inbox — Mailosh` (spec §6.5) and the list's own heading."""
    for item in (*nav.system, *nav.more):
        if item.key == key:
            return item.label, item.count if item.key == "inbox" else None

    def walk(nodes: list[LabelNode]) -> tuple[str, int | None] | None:
        for node in nodes:
            if node.mailbox_id == key:
                return node.name, node.count or None
            found = walk(node.children)
            if found is not None:
                return found
        return None

    return walk(nav.labels) or (key, None)


async def _list_context(
    request: Request,
    *,
    key: str,
    position: int,
    limit: int,
    session: SessionRow,
    user: AppUser,
    prefs: UiPref,
    client: JmapClient,
    db: AsyncSession,
    start: int | None = None,
) -> tuple[dict[str, object], HTMLResponse | None]:
    """Build the list view's whole template context, or (context, 404) when
    `key` names no mailbox — in which case nothing was queried.

    `start` is where the list *already on screen* begins, for the one caller
    that extends a list rather than replacing it (the endless-scroll
    sentinel); everything else renders a list that starts where this page
    starts, and leaves it None. See `_range_label`.
    """
    start = position if start is None else min(max(0, start), position)
    nav = await _nav_for(client, db, user, key)
    context = _base_context(request, session=session, user=user, prefs=prefs, nav=nav)
    context["key"] = key
    if key not in _valid_keys(nav):
        return context, _not_found(request, context, message="That mailbox doesn't exist.")

    page = await build_page(
        client,
        mailbox_key=key,
        nav=nav,
        position=position,
        limit=limit,
        me=user.email,
        now=deps.viewer_now(request),
    )
    label, unread = _active_item(nav, key)
    # Outbound delivery state (`mailosh.services.outbound`). The poll runs
    # on every list render, whatever the key: this GET is also what a
    # Stalwart `EmailSubmission` push turns into (sse.js -> `mail:changed`
    # -> `#list` re-GET), so it is the moment a state change can be read
    # back — and it costs one SELECT when nothing is in flight. The pill
    # lookup itself is Sent-only, and one query for the whole page.
    await outbound.refresh_if_due(client, db, user.id)
    pills: dict[str, object] = {}
    if key == "sent":
        pills = await outbound.states_for(db, user.id, (row.latest_email_id for row in page.rows))
    context.update(
        {
            "page": page,
            "view_label": label,
            "view_unread": unread,
            "range_label": _range_label(page, start),
            "outbound": pills,
            "outbound_toasts": [
                outbound.bounce_toast(row)
                for row in await outbound.take_unannounced_bounces(db, user.id)
            ],
            # Only the sentinel reads this back (into its own `/rows` URL),
            # so a list that grows by scrolling keeps describing itself from
            # where the reader's list actually begins.
            "start": start,
            "prev_position": max(0, position - limit) if position > 0 else None,
            "next_position": page.next_position,
        }
    )
    return context, None


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """The app's front door. Without it, a login with no `next` (and every
    bookmark of the bare origin) lands on FastAPI's own `{"detail": "Not
    Found"}`.

    Deliberately unauthenticated: an anonymous visitor gets bounced to
    `/mail/inbox`, which is what raises `SessionRequired` and redirects to
    `/login?next=/mail/inbox` — so signing in lands them on the inbox rather
    than back here.
    """
    return RedirectResponse(url=INBOX_URL, status_code=303)


# ---------------------------------------------------------------------------
# GET /mail/{key}
# ---------------------------------------------------------------------------


@router.get("/mail/{key}", response_class=HTMLResponse)
async def mail_view(
    request: Request,
    key: str,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    position: int = 0,
    limit: int = PAGE_SIZE,
) -> Response:
    """One mailbox view: nav + toolbar + the first page of rows.

    Answers the whole page normally and just the `#main` fragment for htmx
    (`hx-target="#main"` on every nav item and pager control), with
    `HX-Push-Url` so the address bar follows even for a caller that did not
    set `hx-push-url` itself.
    """
    position = max(0, position)
    limit = min(max(1, limit), MAX_PAGE_SIZE)
    context, missing = await _list_context(
        request,
        key=key,
        position=position,
        limit=limit,
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
    )
    if missing is not None:
        return missing

    headers = _bounce_headers(request, context)
    if _is_fragment(request):
        _apply_fragment(context)
        url = f"/mail/{key}" + (f"?position={position}" if position else "")
        headers["HX-Push-Url"] = url
    return _templates(request).TemplateResponse(request, "list/page.html", context, headers=headers)


# ---------------------------------------------------------------------------
# GET /mail/{key}/rows
# ---------------------------------------------------------------------------


@router.get("/mail/{key}/rows", response_class=HTMLResponse)
async def mail_rows(
    request: Request,
    key: str,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    position: int = 0,
    limit: int = PAGE_SIZE,
    start: int | None = None,
) -> Response:
    """One page of rows — the target of both the endless sentinel
    (`hx-swap="outerHTML"`, appending) and the list's own `mail:changed`
    re-GET (`hx-swap="morph:innerHTML"`, replacing).

    It also carries a `<title>` and an out-of-band nav, because this is the
    response a *live update* arrives through (spec §6.5: "unread counts and
    the document title `(3) Inbox — Mailosh` update"). Without them the list
    would grow a row while the sidebar badge and the tab title still claimed
    the count from before the mail landed.

    ...and the toolbar's range readout, which lives *outside* `#list` and so
    would otherwise keep claiming "1-24 of 24" over 23 rows. `start` is the
    sentinel's alone: an append leaves the rows above it on screen, so the
    range it reports has to be measured from the top of the reader's list,
    not from the page being appended (`_range_label`).
    """
    position = max(0, position)
    limit = min(max(1, limit), MAX_PAGE_SIZE)
    context, missing = await _list_context(
        request,
        key=key,
        position=position,
        limit=limit,
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
        start=start,
    )
    if missing is not None:
        return missing
    context["standalone"] = True
    return _templates(request).TemplateResponse(
        request, "list/rows.html", context, headers=_bounce_headers(request, context)
    )


# ---------------------------------------------------------------------------
# The conversation: GET /t/{thread_id} and GET /mail/{key}/at/{position}
# ---------------------------------------------------------------------------


#: The page's own mark-read POST target. Named here rather than spelled into
#: the template, so the route this app marks mail read through exists in one
#: place — beside `mailosh.web.actions`'s own table, not beside the markup.
MARK_READ_URL = "/a/read"


@dataclass(frozen=True)
class ThreadPlace:
    """Where one conversation sits in a mailbox: the header's readout, its
    two arrows, which conversation is actually there, and which two are
    on either side of it.

    `prev_url`/`next_url` are `None` at the two ends rather than pointing
    somewhere unreachable, which is what lets the header render that arrow
    *disabled* (spec §7 wants the control to stay put, not to disappear and
    shift the row beside it).

    `prev_thread_id`/`next_thread_id` are the same two neighbours **by
    identity**, captured when this conversation was rendered. They are what
    auto-advance moves by, and the reason is a race the positional form
    cannot win: archiving this conversation removes it from the mailbox, so
    the next-older one slides into *this* position, and whether
    `/mail/{key}/at/{position + 1}` then names that conversation or skips
    it depends entirely on whether the server has reindexed by the time the
    request lands. Positions shift under you; ids do not.
    """

    key: str
    position: int
    total: int
    thread_id: str | None
    prev_url: str | None
    next_url: str | None
    prev_thread_id: str | None = None
    next_thread_id: str | None = None

    @property
    def label(self) -> str:
        """`4 of 1,284` — thousands separators, same as the list toolbar's
        own range readout, and one-based because the reader counts from one
        while `position` is a JMAP offset."""
        return f"{self.position + 1:,} of {self.total:,}"


async def _thread_position(
    client: JmapClient,
    nav: NavModel,
    key: str,
    position: int,
    *,
    me: str,
    now: datetime,
) -> ThreadPlace:
    """One `query_page` over the conversation and its two neighbours,
    shaped into a `ThreadPlace`.

    Three rows, not one (two at the top of a mailbox, where there is no
    newer neighbour to ask for): the row at `position` is what this page
    renders, and the two beside it are what auto-advance moves *to*. Their
    ids have to be captured here, while this conversation is still in the
    mailbox — the whole point of advancing by identity is that the answer
    was taken before the archive that shifts every position after it. Still
    one round trip, and still nothing like a page's worth: the alternative
    was a second query, or an advance that skips a conversation whenever
    the server reindexes fast enough.

    `total` is `Email/query`'s own `calculateTotal` (carried through
    `ThreadPage.total`), never the length of anything rendered — this
    window holds three rows at most, so a length here would report "1 of 3"
    over every conversation in the mailbox.

    Goes through `build_page` rather than `client.query_page` directly so
    the key -> filter mapping (starred's `$flagged`, all-mail's exclusions,
    an unprovisioned role mailbox making no request at all) is the same one
    the list itself uses; two spellings of "which messages are in this
    mailbox" is how a position readout starts disagreeing with the list it
    describes.
    """
    start = max(0, position - 1)
    # Where this conversation sits inside the window: 1, or 0 at the top of
    # the mailbox. `offset + 2` is exactly the newer/current/older run —
    # never a row nobody will read.
    offset = position - start
    page = await build_page(
        client, mailbox_key=key, nav=nav, position=start, limit=offset + 2, me=me, now=now
    )

    def at(index: int) -> str | None:
        return page.rows[index].thread_id if 0 <= index < len(page.rows) else None

    return ThreadPlace(
        key=key,
        position=position,
        total=page.total,
        thread_id=at(offset),
        prev_url=f"/mail/{key}/at/{position - 1}" if position > 0 else None,
        next_url=f"/mail/{key}/at/{position + 1}" if position + 1 < page.total else None,
        prev_thread_id=at(offset - 1) if offset > 0 else None,
        next_thread_id=at(offset + 1),
    )


def _by_identity(place: ThreadPlace, thread_id: str | None, position: int) -> str | None:
    """`/t/{id}?key=&pos=` for one of this conversation's neighbours, or
    `None` when its id was never captured.

    `position` is where that conversation will be *after* the action that
    sent the reader to it. Advancing to the older one removes this
    conversation from the mailbox, so the older one slides into this
    position; the newer one sits above the hole and does not move. The id
    is what decides *which* conversation is rendered either way — the
    position only feeds the readout and the two arrows on the page it
    lands on, and is the half allowed to be a row out of date.
    """
    if thread_id is None:
        return None
    return f"/t/{quote(thread_id, safe='')}?key={quote(place.key, safe='')}&pos={position}"


def _advance_url(prefs: UiPref, *, back_url: str, place: ThreadPlace | None) -> str:
    """Where the reader lands after archiving/deleting/reporting the
    conversation they are reading (spec §10's `auto_advance`).

    **By identity, not by index.** `/mail/{key}/at/{position + 1}` names a
    place in a mailbox that the action itself is about to change: archiving
    this conversation removes it, the next-older one slides into
    `position`, and whether `position + 1` is then that conversation or the
    one after it depends on whether the server has reindexed by the time
    the request lands. Same click, two answers, decided by timing — so the
    URL names the conversation `_thread_position` captured while it was
    still beside this one.

    The positional form stays as the fallback for the case identity cannot
    cover: a neighbour whose id was never captured because the window came
    back short. And `older`/`newer` both need a place to move from, so
    every other case falls back to the list, which is where the back
    control would have sent the reader anyway.
    """
    if place is None or prefs.auto_advance == "list":
        return back_url
    if prefs.auto_advance == "newer":
        return (
            _by_identity(place, place.prev_thread_id, place.position - 1)
            or place.prev_url
            or back_url
        )
    return _by_identity(place, place.next_thread_id, place.position) or place.next_url or back_url


def _referring_key(request: Request) -> str | None:
    """The mailbox key the reader was looking at when they opened this
    conversation, or `None`.

    htmx sends the browser's current URL as `HX-Current-URL`, which is the
    only thing that knows this — the row's own `hx-get="/t/{id}"` carries no
    mailbox (its contract is fixed, and a thread genuinely has no single
    home). Purely a parse; the caller validates the result against the nav
    exactly the way `/mail/{key}` does, so a forged header can never become
    anything but a real mailbox.
    """
    current = request.headers.get("hx-current-url")
    if not current:
        return None
    path = urlsplit(current).path
    prefix = "/mail/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :].split("/", 1)[0] or None


def _restyled_for(
    db: AsyncSession, user: AppUser, prefs: UiPref
) -> Callable[[EmailBody], Awaitable[bool]]:
    """The `restyled` callback `build_conversation` asks per message: is
    the dark restyle **inverting** this body right now?

    It is the same question `GET /m/{id}/html` answers before it emits the
    inversion filter, asked of the same three things, and it exists here
    because it is the only layer holding all three: the reader's Appearance
    setting, their per-sender override row, and the mail's own evidence.
    Asking it separately is what keeps the "Original colours" strip off a
    card where the button would do nothing — a sender already opted out
    (the row it would write is the row that is already there), a mail that
    declares its own colour scheme, and a mail whose background already
    reads dark. The banner's sentence stays true in the last two; the
    button under it is what has nothing to undo.

    `mailosh.render.dark.restyle_mode` makes the decision, exactly as the
    frame route lets it: `"invert"` is the one mode that changed the
    message's own colours, and `"color-scheme"` and `"none"` did not.

    The stylesheet is sanitised before `background_is_light` reads it, and
    the raw body is what `declares_color_scheme` reads — both matching
    `mailosh.web.frames._restyle_for` exactly, because a strip that
    appeared on a different rule than the inversion it describes would be
    wrong precisely in the cases it exists for. Only the CSS is sanitised
    here, not the markup: `SanitizeResult.css` is `extract_styles` piped
    through `sanitize_stylesheet`, so this reads the same string the frame
    will without running nh3 over a body this route never renders.
    """

    async def restyled(message: EmailBody) -> bool:
        raw_html = message.html_body
        if not raw_html or not prefs.dark_restyle or prefs.theme == "light":
            return False
        sender = message.from_[0].email.strip().lower() if message.from_ else ""
        if not sender:
            # Nothing to key an override row on, so nothing the strip's
            # own form could post — `POST /m/{id}/restyle` would refuse it.
            return False
        pref = await repo.sender_pref(db, user.id, sender)
        if pref is not None and pref.dark_restyle is False:
            return False
        blocks = extract_styles(raw_html)
        sanitised = (sanitize_stylesheet(block) for block in blocks)
        mail_css = "\n".join(block for block in sanitised if block)
        return (
            restyle_mode(
                theme=prefs.theme,
                enabled=True,
                declares=declares_color_scheme(raw_html, "\n".join(blocks)),
                light=background_is_light(raw_html, mail_css),
            )
            == "invert"
        )

    return restyled


async def _outbound_pill(db: AsyncSession, user: AppUser, view: ConversationView):
    """The tracked submission for this conversation's latest message, if
    that message is the reader's own outbound one — else `None`.

    "Latest own message is outbound" is read off the cards themselves: the
    newest `MessageView` whose sender is `me`, and only if it is also the
    newest message in the conversation. A reply that has since arrived
    answers the question of whether the message got through better than
    any pill could, so the pill steps aside for it. One query, over the
    conversation's ids (`states_for`), never one per card.
    """
    if not view.messages:
        return None
    latest = view.messages[-1]
    if latest.from_email.lower() != user.email.lower():
        return None
    states = await outbound.states_for(db, user.id, [latest.id])
    return states.get(latest.id)


async def _render_conversation(
    request: Request,
    *,
    thread_id: str,
    url: str,
    back_key: str,
    place: ThreadPlace | None,
    session: SessionRow,
    user: AppUser,
    prefs: UiPref,
    client: JmapClient,
    db: AsyncSession,
    nav: NavModel,
    label_meta: dict[str, LabelMeta],
) -> Response:
    """One conversation, rendered the same way whichever route asked for it.

    `/t/{id}` and `/mail/{key}/at/{n}` are two addresses for one page, and
    the second exists purely so an arrow click is a single round trip. They
    share this helper rather than each building their own context because
    everything below — the cache headers, the fragment split, the pushed
    URL, and above all the six keys `thread/page.html` reads — has to be
    identical for both, and two copies of it is how a page starts behaving
    differently depending on how you arrived at it.

    Nothing here mutates anything, so a `preload="mousedown"` prefetch of
    either URL is free — and it is precisely *because* marking read is a
    separate POST (`MARK_READ_URL`, armed client-side after
    `prefs.mark_read_delay`) that the expansion snapshot in
    `build_conversation` can be taken from this GET's own keywords without a
    card ever collapsing under the reader (see `MessageView.expanded`).

    `place`, `mark_read_delay` and `advance_url` all reach the document:
    the first as the action bar's readout and its two arrows, the other two
    as `data-mark-read-delay`/`data-advance-url` on `.thread-scroll` beside
    `data-thread-id`, which is the element `static/js/actions.js` reads all
    three of together. `list/row.html` carries the other end —
    `?key=&pos=` on the row's own link — which is where `key`/`pos` come
    from at all.
    """
    context = _base_context(request, session=session, user=user, prefs=prefs, nav=nav)
    context["key"] = back_key
    view = await build_conversation(
        client,
        thread_id=thread_id,
        me=user.email,
        now=deps.viewer_now(request),
        label_meta=label_meta,
        nav=nav,
        restyled=_restyled_for(db, user, prefs),
    )
    if view is None:
        return _not_found(request, context, message="That conversation no longer exists.")

    back_url = f"/mail/{back_key}"
    context.update(
        {
            "view": view,
            # The delivery pill beside the subject, when the newest message
            # in this conversation is one the reader sent (`_outbound_pill`).
            "outbound_pill": await _outbound_pill(db, user, view),
            # The viewer's own address, for the recipient disclosure's "to
            # me". It is the one thing a card renders that is about the
            # reader rather than about the message.
            "me": user.email,
            # This app's own origin, for the frame handshake: a sandboxed
            # `<iframe>` without `allow-same-origin` posts from the opaque
            # origin `"null"`, so the parent has to know its own to tell its
            # frames' messages from anyone else's.
            "origin": f"{request.url.scheme}://{request.url.netloc}",
            "back_url": back_url,
            # `None` whenever the caller did not say where this conversation
            # sits: the header then renders no readout and no arrows at all,
            # rather than this route issuing a second query to invent them.
            "place": place,
            # Marking read on open is the client's timer, not this route's
            # write — see `static/js/actions.js`. The delay is the reader's
            # (`-1` never marks); the ids are `view.unread_ids`, which the
            # page already carries as `data-unread-ids`.
            "mark_read_url": MARK_READ_URL,
            "mark_read_delay": prefs.mark_read_delay,
            "advance_url": _advance_url(prefs, back_url=back_url, place=place),
        }
    )
    headers = {
        # Spec §6.4: thread partials are cacheable GETs so the `preload`
        # extension's prefetch is actually reused on the real click.
        "Cache-Control": "private, max-age=60",
        "Vary": "HX-Request",
    }
    if _is_fragment(request):
        _apply_fragment(context)
        headers["HX-Push-Url"] = url
    return _templates(request).TemplateResponse(
        request, "thread/page.html", context, headers=headers
    )


@router.get("/t/{thread_id}", response_class=HTMLResponse)
async def thread_view(
    request: Request,
    thread_id: str,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    key: str = "",
    pos: int | None = None,
) -> Response:
    """The conversation (spec §7): back arrow with its `u` hint, the action
    bar, then one card per message oldest -> newest.

    As thin as the two list routes above it. Everything a card shows is
    `mailosh.services.conversation.build_conversation`'s; this route decides
    only which mailbox the reader came from, where in it this conversation
    sits, and whether to answer with a page or a fragment.

    `key`/`pos` come from the row that was clicked (`list/row.html` writes
    its own absolute position into the link), and both have to be there
    before anything is queried: `pos` alone names a place in no particular
    mailbox, and `key` alone would need a search through the mailbox to find
    this thread — the exact query the row is carrying its position to avoid.
    `key` is validated against the nav exactly as `/mail/{key}` validates it,
    so an unknown one is *ignored* (no readout, no arrows, back to the inbox)
    rather than reaching `Email/query` as an `inMailbox` filter.
    """
    # Reading a conversation leaves the mailbox you came from highlighted in
    # the sidebar (mockup key-moments.html §1) — the conversation is a view
    # *of* that mailbox, not a place of its own. The query string is the
    # reliable half of that; `HX-Current-URL` is the fallback for the paths
    # that carry no position (the palette, a pasted link, history restore).
    back_key = key or _referring_key(request) or "inbox"
    # Not `_nav_for`: `build_conversation` takes the same `label_meta` map
    # the nav was built from, so fetching it once here and passing it to both
    # is one database round trip rather than two, and removes any chance of
    # the chips and the sidebar disagreeing about a label.
    label_meta = await repo.label_meta_map(db, user.id, client.account_id)
    nav = await build_nav(client, active_key=back_key, label_meta=label_meta)
    known = back_key in _valid_keys(nav)
    if not known:
        back_key = "inbox"
        for item in (*nav.system, *nav.more):
            item.active = item.key == back_key

    place = None
    if known and key != "" and pos is not None:
        place = await _thread_position(
            client, nav, key, max(0, pos), me=user.email, now=deps.viewer_now(request)
        )

    return await _render_conversation(
        request,
        thread_id=thread_id,
        url=f"/t/{thread_id}",
        back_key=back_key,
        place=place,
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
        nav=nav,
        label_meta=label_meta,
    )


@router.get("/mail/{key}/at/{position}", response_class=HTMLResponse)
async def mail_at(
    request: Request,
    key: str,
    position: int,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
) -> Response:
    """The conversation at `position` in `key` — what the header's `<` and
    `>` link to.

    One query resolves the position *and* supplies the readout the page it
    renders needs (`_thread_position`), and the conversation comes back
    directly rather than as a redirect: an arrow is one round trip, which is
    the whole reason this route exists next to `/t/{id}` instead of the
    header linking to a `/t/{id}` it would first have to look up.

    A position past the end is the list's own 404 page, not an empty
    conversation — the mailbox really does end, and the way out of a dead
    URL is the nav that page carries.
    """
    position = max(0, position)
    label_meta = await repo.label_meta_map(db, user.id, client.account_id)
    nav = await build_nav(client, active_key=key, label_meta=label_meta)
    context = _base_context(request, session=session, user=user, prefs=prefs, nav=nav)
    context["key"] = key
    if key not in _valid_keys(nav):
        return _not_found(request, context, message="That mailbox doesn't exist.")

    place = await _thread_position(
        client, nav, key, position, me=user.email, now=deps.viewer_now(request)
    )
    if place.thread_id is None:
        return _not_found(request, context, message="That conversation no longer exists.")

    return await _render_conversation(
        request,
        thread_id=place.thread_id,
        url=f"/mail/{key}/at/{position}",
        back_key=key,
        place=place,
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
        nav=nav,
        label_meta=label_meta,
    )
