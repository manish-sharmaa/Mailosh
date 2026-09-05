"""Search (design spec §10): the results page, the chips row that edits the
query, and the advanced panel that writes one.

Five routes, and the shape of every one of them is the same shape
`mailosh.web.mail` already uses — full page or bare `#main` fragment
depending on `HX-Request`, `HX-Push-Url` so the address bar follows, the
same list component underneath. Search is a *view of the mailbox*, not a
second application, so it reuses `list/row.html`, `list/range.html`,
`list/skeleton.html` and `list/toolbar_selected.html` verbatim and adds
only the two wrappers whose URLs differ (`search/rows.html`,
`search/toolbar.html`) — the pager, the endless sentinel and the
`mail:changed` refresh all point at `/search/rows` rather than
`/mail/{key}/rows`, and that address is the only thing about them that is
not the list's own.

- ``GET /search?q=`` -> the results, or the operator tips when `q` is
  empty. Never a 500: an unparseable query is a page with a hint on it.
- ``GET /search/rows?q=`` -> one page of rows, for the endless sentinel,
  the toolbar's refresh and the `mail:changed` re-GET.
- ``GET /search/refine?q=&field=&value=`` -> the two free-text chips
  (From, To). Everything else in the chips row is a link whose `q` this
  module already computed while rendering it.
- ``GET /search/advanced?q=`` -> the advanced panel, prefilled.
- ``GET /search/build?...`` -> the advanced panel's nine fields composed
  into one query, then the results for it.

---------------------------------------------------------------------
One rule governs this whole module

**Nothing here builds a JMAP filter.** `mailosh.services.search_query` is
the only thing allowed to decide what a query means, precisely because its
output reaches the server verbatim (`JmapClient.query_search`). What this
module does instead — in the chips row, in `refine`, in `build` — is edit
the reader's *query text*: it adds `is:unread` to a string, or drops every
`newer_than:` token from one, and then hands the result straight back to
the parser like any other typed query. There is exactly one path from text
to filter, so there is exactly one thing to get right.

The corollary is that the chips row can only ever be as literal as the
text: `_active` below asks "is the token `is:unread` in this query", not
"does this query mean unread", and a reader who typed `-is:unread` or
`(is:unread OR is:starred)` gets an unlit chip over a query that still
means what they wrote. Lighting it would require a second, looser reading
of the grammar living here — the exact thing the rule above forbids.

---------------------------------------------------------------------
Hints are the point, not the exhaust

`ParseResult.hints` exists so a reader learns *why* a result set is the
shape it is: `filename:` is approximated through `text`, an unknown
operator was dropped, a `label:` named nothing this account has. Every one
of them is rendered above the results (`search/hints.html`) — a search that
silently returned fewer messages than the reader asked for is the failure
this design exists to avoid, and swallowing the parser's own explanation
of that is how it happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from typing import Annotated, ClassVar
from urllib.parse import quote, unquote_plus, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup, escape
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db.models import AppUser, SessionRow, UiPref
from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import MethodError
from mailosh.services.mailbox_tree import LabelNode, NavModel, resolve_mailbox
from mailosh.services.search_query import ParseResult, parse_query

# Two private helpers, imported rather than reimplemented, and the reason
# is the same in both cases: a second copy is a copy that drifts.
#
# `mailosh.web.mail` owns what "a page in this app" means — which requests
# get the `#main` fragment (`_is_fragment` deliberately excludes htmx's
# history-restore fetch, and a search page that got that wrong would
# restore chrome-less), what every page's context holds, and how the
# toolbar's range readout is worded. `mailosh.services.thread_list` owns
# what a *row* means: `_row_for_thread` is the aggregation
# (senders/unread/starred/attachment/chips over the messages in scope)
# that `list/row.html` is written against, and `_other_than_scope` is the
# predicate that mirrors an `inMailboxOtherThan` filter.
#
# Both are private because neither module wants a second caller deciding
# these things; this module is not deciding them, it is asking.
from mailosh.services.thread_list import (
    ThreadPage,
    ThreadRow,
    _other_than_scope,
    _row_for_thread,
)
from mailosh.web import deps
from mailosh.web.mail import (
    MAX_PAGE_SIZE,
    PAGE_SIZE,
    _apply_fragment,
    _base_context,
    _is_fragment,
    _nav_for,
    _range_label,
)

router = APIRouter(tags=["search"])

SessionDep = Annotated[SessionRow, Depends(deps.require_session)]
UserDep = Annotated[AppUser, Depends(deps.current_user)]
PrefsDep = Annotated[UiPref, Depends(deps.prefs_for)]
ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]

#: Longest query this module will parse. Not a security boundary — the
#: parser is not a `re` backtracker and the filter goes to a server with
#: its own limits — but a query bar is a text field on a GET, and a
#: hand-written 40 KB `q` has no reader behind it. Truncated rather than
#: rejected: a 401st character is not an error worth a page for.
MAX_QUERY = 400

#: Spec §11: HTML answers are `no-cache` and vary on `HX-Request`, because
#: one URL serves two documents here (the whole page and the `#main`
#: fragment) and a cache that could not tell them apart would eventually
#: hand a fragment to a browser asking for a page. `no-cache` (revalidate),
#: not `no-store`: a search result is private and short-lived, not secret
#: in a way that has to be kept out of the disk cache when everything else
#: about the same mailbox already is not.
_HTML_HEADERS = {"Cache-Control": "private, no-cache", "Vary": "HX-Request"}


# ---------------------------------------------------------------------------
# The resolver the parser asks for its mailbox ids
# ---------------------------------------------------------------------------


class NavResolver:
    """`mailosh.services.search_query.MailboxResolver` over the nav model.

    The parser is a pure function over text and cannot fetch anything, so
    the two questions it needs answered about *this account* — "which
    mailbox is the Archive" (`in:archive`) and "which mailbox is the label
    called Clients" (`label:clients`) — arrive as this object. Built from
    the `NavModel` the page is already rendering, so a search costs the
    same one `Mailbox/get` every other page in this app costs.

    `by_role` accepts both vocabularies on purpose. Spec §10 writes the
    user-facing operator as `in:spam`; RFC 8621 §2 calls that mailbox's
    role `junk`, and `mailosh.services.mailbox_tree` keeps the nav key
    "spam" for exactly that reason. Whichever of the two the parser hands
    over, it means the same folder, and a resolver that answered only one
    of them would fail silently — `in:spam` finding nothing is
    indistinguishable, from the outside, from an account with no Spam
    folder.

    `by_label_name` matches case-insensitively, and matches a nested label
    both by its own name ("Clients") and by its full path ("Work/Clients"),
    because both are what a reader sees in the sidebar. Ambiguity resolves
    to the first match in nav order (alphabetical, parents before
    children), which is the order the sidebar itself draws.

    **A hidden label resolves to nothing here, deliberately.** `build_nav`
    drops `visibility="hide"` labels from the tree entirely, and this
    module asks the same tree the sidebar does: a hidden label has no nav
    row, no chip and no URL anywhere in this app, so `label:` naming one
    reports "no label called that" — through a hint the reader can see —
    rather than quietly searching a folder they have hidden from
    themselves.
    """

    #: `in:` scope word -> the JMAP role behind it. Only "spam" differs
    #: from its own role name; the rest are here so one lookup covers both
    #: spellings without a branch.
    _ROLE_KEYS: ClassVar[dict[str, str]] = {
        "inbox": "inbox",
        "sent": "sent",
        "drafts": "drafts",
        "archive": "archive",
        "spam": "spam",
        "junk": "spam",
        "trash": "trash",
    }

    def __init__(self, nav: NavModel) -> None:
        self._nav = nav
        self._labels: dict[str, str] = {}
        self._index(nav.labels, prefix="")

    def _index(self, nodes: list[LabelNode], *, prefix: str) -> None:
        for node in nodes:
            path = f"{prefix}{node.name}"
            # `setdefault`: nav order wins a collision, so two labels of the
            # same name under different parents resolve the way the sidebar
            # reads top to bottom.
            self._labels.setdefault(node.name.casefold(), node.mailbox_id)
            self._labels.setdefault(path.casefold(), node.mailbox_id)
            self._index(node.children, prefix=f"{path}/")

    def by_role(self, role: str) -> str | None:
        key = self._ROLE_KEYS.get(role.strip().casefold())
        return None if key is None else resolve_mailbox(self._nav, key)

    def by_label_name(self, name: str) -> str | None:
        return self._labels.get(name.strip().casefold())


# ---------------------------------------------------------------------------
# Query *text* editing — the chips row's whole mechanism
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Split a query into whitespace-separated tokens, keeping a quoted run
    together (`label:"Client work"` is one token, and so is `"quarterly
    review"`).

    Deliberately not a parser: it knows about the double quote and nothing
    else — no operators, no `OR`, no parentheses, no negation. Everything
    this module does with the result is add a token, drop a token, or ask
    whether one is present, and each of those is true of the *text*
    regardless of what the grammar makes of it.
    """
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


#: Characters that mean something to the grammar and so cannot appear in a
#: bare token: whitespace separates terms, and the brackets group them.
#: `-` and `:` are deliberately absent — both are only special at the front
#: of a term, and both are ordinary inside an address or a label name.
_NEEDS_QUOTES = ("(", ")")


def _quoted(value: str) -> str:
    """`Client work` -> `"Client work"`; `priya@x` -> `priya@x`.

    Quoted only when it has to be, because an unnecessary pair of quotes
    is noise in a URL a reader is meant to be able to read and edit.

    Inner quotes are dropped rather than escaped: the grammar spec §10
    defines has no escape sequence inside a quoted phrase, so a value
    carrying one cannot be spelled at all — and silently producing a token
    that ends where the reader did not expect is worse than losing the
    character.
    """
    cleaned = value.replace('"', "").strip()
    if not cleaned:
        return ""
    special = any(c.isspace() for c in cleaned) or any(c in cleaned for c in _NEEDS_QUOTES)
    return f'"{cleaned}"' if special else cleaned


def _has_token(text: str, token: str) -> bool:
    """Is `token` present in `text`, as a whole token, ignoring case?"""
    wanted = token.casefold()
    return any(t.casefold() == wanted for t in _tokens(text))


def _toggle_token(text: str, token: str) -> str:
    """`is:unread` on if it was off, off if it was on."""
    wanted = token.casefold()
    kept = [t for t in _tokens(text) if t.casefold() != wanted]
    if len(kept) == len(_tokens(text)):
        kept.append(token)
    return " ".join(kept)


def _drop_prefixes(tokens: list[str], prefixes: tuple[str, ...]) -> list[str]:
    lowered = tuple(p.casefold() for p in prefixes)
    return [t for t in tokens if not t.casefold().startswith(lowered)]


def _set_operator(text: str, prefix: str, value: str, *, family: tuple[str, ...] = ()) -> str:
    """Replace whatever `family` of operators `text` carries with
    `prefix + value`, or with nothing when `value` is empty.

    `family` is what makes the time chip a *choice* rather than an
    accumulation: picking "Past week" has to clear `older_than:`,
    `before:` and `after:` as well as the `newer_than:` it replaces, or
    two mutually exclusive answers end up ANDed together in the filter and
    the result set is empty for a reason nothing on screen explains.
    """
    kept = _drop_prefixes(_tokens(text), family or (prefix,))
    quoted = _quoted(value)
    if quoted:
        kept.append(f"{prefix}{quoted}")
    return " ".join(kept)


def _operator_token(text: str, prefixes: tuple[str, ...]) -> str:
    """The first whole token starting with any of `prefixes` — `before:2026-01-01`,
    not `2026-01-01`."""
    lowered = tuple(p.casefold() for p in prefixes)
    for token in _tokens(text):
        folded = token.casefold()
        if folded.startswith(lowered):
            return token
    return ""


def _operator_value(text: str, prefixes: tuple[str, ...]) -> str:
    """The value of the first token starting with any of `prefixes`, with
    its quotes stripped — what the chips and the advanced panel show back
    to the reader as their current setting."""
    token = _operator_token(text, prefixes)
    for prefix in prefixes:
        if token.casefold().startswith(prefix.casefold()):
            return token[len(prefix) :].strip('"')
    return ""


def _search_url(q: str) -> str:
    """`/search?q=…`, or `/search` for an empty query. One function, so no
    template ever assembles this itself and the chips, the pager, the
    recents and the pushed URL can never disagree about the shape of a
    search address."""
    query = q.strip()
    return f"/search?q={quote(query, safe='')}" if query else "/search"


# ---------------------------------------------------------------------------
# The chips row (spec §10: From · To · Any time ▾ · Has attachment ·
# Is unread · Label ▾) — a view model, because every chip is a link whose
# href is "this query, edited", and computing that in a template is how
# the six of them drift apart.
# ---------------------------------------------------------------------------

#: Every operator the time chip owns. Picking one clears the other three.
_TIME_FAMILY = ("newer_than:", "older_than:", "before:", "after:")

#: The time chip's menu: `(value, label)`, value being the `newer_than:`
#: argument spec §10's grammar defines (`Nd|Nw|Nm|Ny`). "" is "Any time",
#: which clears the family instead of setting anything.
_TIME_CHOICES = (
    ("", "Any time"),
    ("1d", "Past day"),
    ("7d", "Past week"),
    ("1m", "Past month"),
    ("6m", "Past 6 months"),
    ("1y", "Past year"),
)

#: `GET /search/refine`'s allowlist. A field not named here is dropped
#: rather than 422'd — the request came from a chip, and the worst
#: outcome of a stale one should be the search the reader already had.
_REFINE_FIELDS = {"from": "from:", "to": "to:", "subject": "subject:"}


@dataclass(frozen=True)
class ChipOption:
    """One row of a chip's dropdown: what it says, where it goes, and
    whether it is the setting currently in force."""

    label: str
    url: str
    current: bool


@dataclass(frozen=True)
class Chip:
    """One chip in the results row.

    `kind` picks the template's branch: `"toggle"` is a link that adds or
    removes its operator, `"menu"` opens a `<details>` of `options`, and
    `"text"` opens a `<details>` holding a one-field form that posts to
    `/search/refine`.
    """

    kind: str
    name: str
    label: str
    value: str
    active: bool
    url: str = ""
    field: str = ""
    options: tuple[ChipOption, ...] = ()


def _time_chip(q: str) -> Chip:
    """The time chip, whose label is the one place a chip has to answer
    "what does this query say about time" without being allowed to read
    the grammar.

    A value the menu itself offers gets the menu's own wording ("Past
    week"). Anything else — `before:2026-01-01`, or `before:notadate` — is
    shown as **the whole token**, not as the bare value: "notadate" in a
    chip labelled with a time reads as a date this app understood, and it
    is not (the parser will have dropped it, and said so in a hint). The
    token is what the reader typed, and it is true whatever the grammar
    made of it.
    """
    current = _operator_value(q, _TIME_FAMILY)
    options = tuple(
        ChipOption(
            label=label,
            url=_search_url(_set_operator(q, "newer_than:", value, family=_TIME_FAMILY)),
            current=value == current,
        )
        for value, label in _TIME_CHOICES
    )
    named = next((label for value, label in _TIME_CHOICES if value and value == current), "")
    return Chip(
        kind="menu",
        name="time",
        label=named or _operator_token(q, _TIME_FAMILY) or "Any time",
        value=current,
        active=bool(current),
        options=options,
    )


def _label_chip(q: str, nav: NavModel) -> Chip:
    current = _operator_value(q, ("label:",))
    options = [
        ChipOption(
            label="Any label", url=_search_url(_set_operator(q, "label:", "")), current=not current
        )
    ]

    def walk(nodes: list[LabelNode], prefix: str) -> None:
        for node in nodes:
            path = f"{prefix}{node.name}"
            options.append(
                ChipOption(
                    label=path,
                    url=_search_url(_set_operator(q, "label:", path)),
                    current=path.casefold() == current.casefold(),
                )
            )
            walk(node.children, f"{path}/")

    walk(nav.labels, "")
    return Chip(
        kind="menu",
        name="label",
        label=current or "Label",
        value=current,
        active=bool(current),
        options=tuple(options),
    )


def _chips(q: str, nav: NavModel) -> list[Chip]:
    """Spec §10's six chips, in its order, each carrying the query it would
    produce."""
    sender = _operator_value(q, ("from:",))
    recipient = _operator_value(q, ("to:",))
    return [
        Chip(
            kind="text",
            name="from",
            label=f"From: {sender}" if sender else "From",
            value=sender,
            active=bool(sender),
            field="from",
        ),
        Chip(
            kind="text",
            name="to",
            label=f"To: {recipient}" if recipient else "To",
            value=recipient,
            active=bool(recipient),
            field="to",
        ),
        _time_chip(q),
        Chip(
            kind="toggle",
            name="attachment",
            label="Has attachment",
            value="",
            active=_has_token(q, "has:attachment"),
            url=_search_url(_toggle_token(q, "has:attachment")),
        ),
        Chip(
            kind="toggle",
            name="unread",
            label="Is unread",
            value="",
            active=_has_token(q, "is:unread"),
            url=_search_url(_toggle_token(q, "is:unread")),
        ),
        _label_chip(q, nav),
    ]


# ---------------------------------------------------------------------------
# The advanced panel
# ---------------------------------------------------------------------------

#: The panel's "Search in" select. `("", …)` is the default scope — no
#: `in:` operator at all, which is what spec §10's "everything except Spam
#: and Trash" means in the grammar.
_SCOPES = (
    ("", "All mail"),
    ("anywhere", "All mail, Spam and Trash"),
    ("inbox", "Inbox"),
    ("sent", "Sent"),
    ("drafts", "Drafts"),
    ("archive", "Archive"),
    ("spam", "Spam"),
    ("trash", "Trash"),
)

#: "Date within", as `newer_than:` arguments.
_WITHIN = (
    ("", "any time"),
    ("1d", "1 day"),
    ("3d", "3 days"),
    ("7d", "1 week"),
    ("14d", "2 weeks"),
    ("1m", "1 month"),
    ("6m", "6 months"),
    ("1y", "1 year"),
)

_SIZE_UNITS = (("k", "KB"), ("m", "MB"), ("g", "GB"))


@dataclass(frozen=True)
class Advanced:
    """The advanced panel's nine fields, read back out of a query so the
    panel opens showing what the reader is already searching for.

    A best-effort *reconstruction*, not a parse: it reads the leading
    `from:`/`subject:`/… token of each kind and treats everything left
    over as "Has the words". A query the panel cannot round-trip (an `OR`,
    a parenthesised group, two `from:` terms) still searches exactly as
    typed — the panel simply shows the part of it that fits in nine boxes,
    and submitting the panel replaces the query with what the boxes say.
    """

    sender: str = ""
    recipient: str = ""
    subject: str = ""
    words: str = ""
    without: str = ""
    size_op: str = "larger"
    size: str = ""
    size_unit: str = "m"
    within: str = ""
    scope: str = ""
    attachment: bool = False


def _read_advanced(q: str) -> Advanced:
    tokens = _tokens(q)
    known = ("from:", "to:", "subject:", "label:", "in:", "has:", "is:", *_TIME_FAMILY)
    size_op, size, size_unit = "larger", "", "m"
    for token in tokens:
        for op in ("larger:", "smaller:"):
            if token.casefold().startswith(op):
                raw = token[len(op) :].strip('"')
                size_op = op[:-1]
                size = raw[:-1] if raw[-1:].casefold() in {"k", "m", "g"} else raw
                size_unit = raw[-1:].casefold() if raw[-1:].casefold() in {"k", "m", "g"} else "m"
    free = _drop_prefixes(tokens, (*known, "larger:", "smaller:"))
    return Advanced(
        sender=_operator_value(q, ("from:",)),
        recipient=_operator_value(q, ("to:",)),
        subject=_operator_value(q, ("subject:",)),
        # `-word` is spec §10's negation, so the panel's "Doesn't have"
        # box is exactly the tokens that start with one.
        words=" ".join(t for t in free if not t.startswith("-")),
        without=" ".join(t[1:] for t in free if t.startswith("-") and len(t) > 1),
        size_op=size_op,
        size=size,
        size_unit=size_unit,
        within=_operator_value(q, ("newer_than:",)),
        scope=_operator_value(q, ("in:",)) or _operator_value(q, ("label:",)),
        attachment=_has_token(q, "has:attachment"),
    )


def _build_query(fields: Advanced) -> str:
    """The nine fields, composed into one query string — *text*, which then
    goes through the parser like anything a reader types.

    This is the whole reason the panel is safe to have: it produces the
    same `from:x subject:"y" -z has:attachment larger:5m newer_than:1m`
    a reader could have written by hand, and nothing downstream can tell
    the difference or needs to.
    """
    parts: list[str] = []
    if fields.sender:
        parts.append(f"from:{_quoted(fields.sender)}")
    if fields.recipient:
        parts.append(f"to:{_quoted(fields.recipient)}")
    if fields.subject:
        parts.append(f"subject:{_quoted(fields.subject)}")
    if fields.words:
        parts.extend(_tokens(fields.words))
    for token in _tokens(fields.without):
        stripped = token.lstrip("-")
        if stripped:
            parts.append(f"-{stripped}")
    if fields.size.strip():
        digits = "".join(c for c in fields.size if c.isdigit())
        unit = fields.size_unit if fields.size_unit in {"k", "m", "g"} else "m"
        operator = "smaller" if fields.size_op == "smaller" else "larger"
        if digits:
            parts.append(f"{operator}:{digits}{unit}")
    if fields.within:
        parts.append(f"newer_than:{fields.within}")
    if fields.scope:
        # The select mixes the seven scope words with the account's own
        # labels; a value that is not one of the words is a label name.
        known = {value for value, _label in _SCOPES if value}
        parts.append(
            f"in:{fields.scope}" if fields.scope in known else f"label:{_quoted(fields.scope)}"
        )
    if fields.attachment:
        parts.append("has:attachment")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# SearchSnippet highlights
# ---------------------------------------------------------------------------


def _highlight(raw: str) -> Markup:
    """One `SearchSnippet` string, re-escaped by us and left with nothing
    but its `<mark>` tags.

    The server hands back markup — the matched words wrapped in `<mark>`,
    everything else HTML-escaped — and this is a mail client, so "the
    server escaped it" is not a thing worth betting a stored-XSS on. The
    string is therefore taken apart on the two tags this app renders,
    every other piece is un-escaped and re-escaped by `markupsafe` (which
    normalises whatever the server did, and neutralises whatever it did
    not), and the marks are re-emitted balanced: an unmatched `</mark>`
    is dropped and an unclosed `<mark>` is closed here. What reaches the
    template is a `Markup` this function built, character by character,
    out of text.
    """
    out: list[str] = []
    depth = 0
    for piece in raw.replace("</mark>", "\x00/\x00").replace("<mark>", "\x00+\x00").split("\x00"):
        if piece == "+":
            out.append("<mark>")
            depth += 1
        elif piece == "/":
            if depth > 0:
                out.append("</mark>")
                depth -= 1
        elif piece:
            out.append(str(escape(unescape(piece))))
    out.append("</mark>" * depth)
    return Markup("".join(out))


def _apply_snippets(rows: list[ThreadRow], snippets: dict[str, object]) -> None:
    """Swap each row's subject/preview for its highlighted form.

    In place, and onto the row the list template already renders, rather
    than through a search-only row type: `list/row.html` is the row
    component this page reuses, and giving it a second field to check
    would have made every future change to a row a change to two
    templates. `ThreadRow.subject`/`.preview` are `str`, and `Markup` *is*
    a `str` — one that Jinja's autoescaping already knows to leave alone.

    `SearchSnippet/get` returns `null` for whichever half the query did
    not match in (a `from:` term matches neither), so each half falls back
    independently to the row's own plain text.
    """
    for row in rows:
        snippet = snippets.get(row.thread_id)
        if snippet is None:
            continue
        if getattr(snippet, "subject", None):
            row.subject = _highlight(snippet.subject)
        if getattr(snippet, "preview", None):
            row.preview = _highlight(snippet.preview)


# ---------------------------------------------------------------------------
# Running a search
# ---------------------------------------------------------------------------


def _scoped_filter(result: ParseResult) -> dict[str, object]:
    """The parser's filter, ANDed with the scope it asked the caller to
    apply.

    `search_query` deliberately does not emit `inMailboxOtherThan` itself
    — its own allow-list forbids the key — and hands the default scope
    back as `exclude_mailbox_ids` instead, "for the caller to apply the way
    `thread_list` already does for its own views". This is that caller
    doing it, and it is the one filter shape this module writes: two
    constant keys around a list of mailbox ids the parser resolved. No
    reader text reaches it, and the parsed half is passed through
    untouched.

    An empty exclusion means the reader named a scope of their own
    (`scope_was_explicit`), so there is nothing to add; a `None` filter
    with an exclusion is "everything except Spam and Trash", which is a
    real search and not an empty one.
    """
    if not result.exclude_mailbox_ids:
        return result.filter or {}
    scope: dict[str, object] = {"inMailboxOtherThan": list(result.exclude_mailbox_ids)}
    if result.filter is None:
        return scope
    return {"operator": "AND", "conditions": [result.filter, scope]}


async def _run(
    client: JmapClient, result: ParseResult, *, nav: NavModel, position: int, limit: int, me: str
) -> ThreadPage:
    """The parser's filter -> one page of rows.

    `result.filter` reaches `query_search` untouched, wrapped only in the
    scope the parser explicitly handed over (`_scoped_filter`).
    `exclude_mailbox_ids` is read twice for that reason: once to build that
    wrapper, and once as the predicate that mirrors it —
    `_row_for_thread` aggregates a row over the messages the server
    actually matched rather than over every member of the thread (see
    `mailosh.services.thread_list._row_for_thread` for the three bugs that
    scoping fixes; a trashed message supplying an Inbox row's date is the
    memorable one).

    A `MethodError` from the snippet call costs the highlights, not the
    page: a JMAP server that does not implement RFC 8621 §5 answers the
    whole batch with an error, so the search is re-run without asking for
    them. Every other `MethodError` fails again identically on the retry
    and propagates to the app's own handler, which is what should happen.
    """
    condition = _scoped_filter(result)
    try:
        query = await client.query_search(
            filter=condition, position=position, limit=limit, snippets=True
        )
    except MethodError:
        query = await client.query_search(filter=condition, position=position, limit=limit)

    in_scope = _other_than_scope(set(result.exclude_mailbox_ids))
    now = datetime.now(UTC)
    rows: list[ThreadRow] = []
    for thread_id in query.thread_order:
        row = _row_for_thread(
            thread_id, query.emails_by_thread.get(thread_id, []), nav, me, now, in_scope
        )
        if row is not None:
            rows.append(row)
    _apply_snippets(rows, query.snippets)

    next_position = query.position + limit if query.position + limit < query.total else None
    return ThreadPage(
        rows=rows,
        position=query.position,
        limit=limit,
        total=query.total,
        next_position=next_position,
    )


def _query_text(request: Request, given: str | None) -> str:
    """The query this request is about: its own `q`, or — for a fragment
    with no `q` of its own — the one in the address bar.

    htmx sends the browser's current URL as `HX-Current-URL`, which is the
    only thing that knows what the reader is looking at when a panel opens
    from the top bar on a page the top bar did not render. The same
    technique, and the same one-line parse, as
    `mailosh.web.mail._referring_key`.
    """
    if given is not None:
        return given[:MAX_QUERY]
    current = request.headers.get("hx-current-url", "")
    if not current:
        return ""
    for part in urlsplit(current).query.split("&"):
        name, _, value = part.partition("=")
        if name == "q":
            return unquote_plus(value)[:MAX_QUERY]
    return ""


async def _context(
    request: Request,
    *,
    q: str,
    position: int,
    limit: int,
    session: SessionRow,
    user: AppUser,
    prefs: UiPref,
    client: JmapClient,
    db: AsyncSession,
    start: int | None = None,
) -> dict[str, object]:
    """Everything both search templates render: the shell, the parsed
    query, the chips, the hints and one page of rows.

    An empty query is not an error and does not reach the server: the page
    renders its operator tips instead (`search/empty.html`), which is also
    what `/search` with no `q` at all is for.
    """
    nav = await _nav_for(client, db, user, "")
    context = _base_context(request, session=session, user=user, prefs=prefs, nav=nav)

    # An empty box is not a search, and does not become one: nothing is
    # parsed and nothing is queried, and the page renders its operator card
    # instead (`search/empty.html`). Running the empty query would ask the
    # mail server for every message in the account to answer a question
    # nobody asked.
    text = q.strip()
    if text:
        result = parse_query(text, resolver=NavResolver(nav), now=datetime.now(UTC))
        page = await _run(client, result, nav=nav, position=position, limit=limit, me=user.email)
    else:
        result = ParseResult(
            filter=None, hints=(), exclude_mailbox_ids=(), scope_was_explicit=False
        )
        page = ThreadPage(rows=[], position=0, limit=limit, total=0, next_position=None)
    start = position if start is None else min(max(0, start), position)

    context.update(
        {
            "q": text,
            "result": result,
            "hints": result.hints,
            "chips": _chips(text, nav),
            "advanced": _read_advanced(text),
            "scopes": _SCOPES,
            "within_choices": _WITHIN,
            "size_units": _SIZE_UNITS,
            "labels_flat": _flat_labels(nav),
            "page": page,
            "range_label": _range_label(page, start),
            "start": start,
            "prev_position": max(0, position - limit) if position > 0 else None,
            "next_position": page.next_position,
            "search_url": _search_url(text),
            # Spec §10's default scope, said out loud. A reader who never
            # typed `in:` is searching everything except Spam and Trash,
            # and the one-click way out of that is a query, not a setting.
            "scope_note": None
            if result.scope_was_explicit or not text
            else _search_url(_set_operator(text, "in:", "anywhere", family=("in:",))),
        }
    )
    return context


def _flat_labels(nav: NavModel) -> list[str]:
    paths: list[str] = []

    def walk(nodes: list[LabelNode], prefix: str) -> None:
        for node in nodes:
            path = f"{prefix}{node.name}"
            paths.append(path)
            walk(node.children, f"{path}/")

    walk(nav.labels, "")
    return paths


def _render(request: Request, context: dict[str, object], *, url: str) -> Response:
    """`search/page.html`, as a page or as the `#main` fragment, with the
    address bar pushed to `url` — the exact split `mailosh.web.mail` makes,
    for the exact reason it makes it (see `_is_fragment`: htmx's own
    history-restore fetch wants a whole document back, not a fragment)."""
    headers = dict(_HTML_HEADERS)
    if _is_fragment(request):
        _apply_fragment(context)
        headers["HX-Push-Url"] = url
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "search/page.html", context, headers=headers)


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    q: str = "",
    position: int = 0,
    limit: int = PAGE_SIZE,
) -> Response:
    """The results (spec §10): the chips row, the parser's hints, and the
    list component underneath.

    There is no query this route answers with a 500. An unknown operator,
    a `label:` naming nothing, a lone `OR`, a bracket that never closes —
    every one of those is the parser's to describe, and it describes them
    in `hints`, which this page renders above the results. The only thing
    that reaches the mail server is a filter that parser built.
    """
    context = await _context(
        request,
        q=q,
        position=max(0, position),
        limit=min(max(1, limit), MAX_PAGE_SIZE),
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
    )
    url = _search_url(str(context["q"]))
    if position > 0:
        url = f"{url}{'&' if '?' in url else '?'}position={position}"
    return _render(request, context, url=url)


# ---------------------------------------------------------------------------
# GET /search/rows
# ---------------------------------------------------------------------------


@router.get("/search/rows", response_class=HTMLResponse)
async def search_rows(
    request: Request,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    q: str = "",
    position: int = 0,
    limit: int = PAGE_SIZE,
    start: int | None = None,
) -> Response:
    """One page of rows — the endless-scroll sentinel, the toolbar's
    refresh, and the `mail:changed` re-GET, exactly as `/mail/{key}/rows`
    serves all three for a mailbox.

    `start` rides along for the same reason it does there: an append leaves
    the rows above it on screen, so the out-of-band range readout has to be
    measured from the top of the reader's list rather than from the page
    being appended (`mailosh.web.mail._range_label`).
    """
    context = await _context(
        request,
        q=q,
        position=max(0, position),
        limit=min(max(1, limit), MAX_PAGE_SIZE),
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
        start=start,
    )
    context["standalone"] = True
    return request.app.state.templates.TemplateResponse(
        request, "search/rows.html", context, headers=dict(_HTML_HEADERS)
    )


# ---------------------------------------------------------------------------
# GET /search/refine — the From and To chips
# ---------------------------------------------------------------------------


@router.get("/search/refine", response_class=HTMLResponse)
async def refine(
    request: Request,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    q: str = "",
    field: str = "",
    value: str = "",
) -> Response:
    """One chip's free-text edit: `q` with its `from:`/`to:`/`subject:`
    term replaced by `value` (or removed, when `value` is blank), then the
    results for that.

    A route rather than a link because the value is typed, not chosen —
    the other four chips need no round trip to compute their own href, and
    do not make one. An unknown `field` is dropped rather than rejected:
    what reaches this URL is a chip, and the worst a stale one should do
    is re-run the search the reader already had.
    """
    prefix = _REFINE_FIELDS.get(field)
    edited = q if prefix is None else _set_operator(q[:MAX_QUERY], prefix, value[:MAX_QUERY])
    context = await _context(
        request,
        q=edited,
        position=0,
        limit=PAGE_SIZE,
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
    )
    return _render(request, context, url=_search_url(str(context["q"])))


# ---------------------------------------------------------------------------
# The advanced panel: GET /search/advanced (the form) and GET /search/build
# ---------------------------------------------------------------------------


@router.get("/search/advanced", response_class=HTMLResponse)
async def advanced_panel(
    request: Request,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    q: str | None = None,
) -> Response:
    """The advanced panel, prefilled from the query being viewed.

    Fetched when the panel opens rather than rendered into every page's
    top bar, because most page loads never open it — and re-fetched on
    every open rather than once, because a search between two opens
    changes what it should show and `#main` is all that swap replaced.
    `q` comes from the request when it has one and from `HX-Current-URL`
    otherwise (`_query_text`), so the panel is right on a page whose top
    bar was rendered before the search was run.
    """
    text = _query_text(request, q)
    nav = await _nav_for(client, db, user, "")
    context = _base_context(request, session=session, user=user, prefs=prefs, nav=nav)
    context.update(
        {
            "q": text,
            "advanced": _read_advanced(text),
            "scopes": _SCOPES,
            "within_choices": _WITHIN,
            "size_units": _SIZE_UNITS,
            "labels_flat": _flat_labels(nav),
        }
    )
    return request.app.state.templates.TemplateResponse(
        request, "search/advanced.html", context, headers=dict(_HTML_HEADERS)
    )


@router.get("/search/build", response_class=HTMLResponse)
async def build(
    request: Request,
    session: SessionDep,
    user: UserDep,
    prefs: PrefsDep,
    client: ClientDep,
    db: DbDep,
    sender: Annotated[str, Query(alias="from")] = "",
    recipient: Annotated[str, Query(alias="to")] = "",
    subject: str = "",
    words: str = "",
    without: str = "",
    size_op: str = "larger",
    size: str = "",
    size_unit: str = "m",
    within: str = "",
    scope: str = "",
    attachment: bool = False,
) -> Response:
    """The advanced panel's action: nine fields in, one query out, then the
    results for it.

    The panel composes *text* (`_build_query`) and the parser reads that
    text — there is no second path from a form field to a JMAP condition,
    which is why the panel can offer "Doesn't have" and "Size" without
    this module ever learning what those mean. It is also why the results
    it lands on are a real, shareable `/search?q=…` URL: what the panel
    built is something the reader could have typed.
    """
    fields = Advanced(
        sender=sender[:MAX_QUERY],
        recipient=recipient[:MAX_QUERY],
        subject=subject[:MAX_QUERY],
        words=words[:MAX_QUERY],
        without=without[:MAX_QUERY],
        size_op=size_op,
        size=size[:16],
        size_unit=size_unit,
        within=within[:8],
        scope=scope[:MAX_QUERY],
        attachment=attachment,
    )
    context = await _context(
        request,
        q=_build_query(fields),
        position=0,
        limit=PAGE_SIZE,
        session=session,
        user=user,
        prefs=prefs,
        client=client,
        db=db,
    )
    return _render(request, context, url=_search_url(str(context["q"])))
