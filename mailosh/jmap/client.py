"""Async JMAP client: session discovery, batched method-call envelope, and
the mail operations (mailboxes, inbox query, threads, flags, upload/import,
identity, submission) built on top of ``_call`` — plus ``event_stream``, a
push-notification stream over the session's EventSource endpoint
(RFC 8620 §7.3).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from .errors import BlobTooLarge, JmapError, MethodError, TransportError
from .models import (
    Address,
    BodyPart,
    EmailBody,
    EmailHeader,
    Identity,
    Mailbox,
    Session,
    StateChange,
)

logger = logging.getLogger(__name__)

#: Capabilities every request declares. Fixed for now; a future task may grow
#: this per-call (e.g. for blob) the way ihasmail's client does.
#: ``urn:ietf:params:jmap:submission`` was added in Task 9 — found live,
#: not by any mocked unit test (respx doesn't validate capability
#: requirements the way a real JMAP server does): without it, Stalwart
#: rejects ``Identity/get``/``EmailSubmission/set`` with a batch-level
#: ``unknownMethod`` error, since RFC 8621 §7.1 defines both the
#: ``Identity`` and ``EmailSubmission`` data types under this capability,
#: not under ``urn:ietf:params:jmap:mail``.
USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
    "urn:ietf:params:jmap:submission",
]

#: ``Email/get`` properties for an inbox row (``EmailHeader``). Shared as the
#: base of ``_EMAIL_BODY_PROPS`` below rather than duplicated, since a thread
#: fetch needs everything a list row needs plus the body-specific extras.
_EMAIL_LIST_PROPS = [
    "id",
    "threadId",
    "mailboxIds",
    "keywords",
    "from",
    "to",
    "subject",
    "receivedAt",
    "preview",
    "hasAttachment",
]

#: ``Email/get`` properties for an action snapshot (``EmailState``): where a
#: message sits, and nothing about what it says. Deliberately the smallest
#: set ``mailosh.services.actions`` (Task 9) can work from — see
#: ``EmailState``'s own docstring.
_EMAIL_STATE_PROPS = ["id", "threadId", "mailboxIds", "keywords"]

#: ``Email/get`` properties for a thread message (``EmailBody``): the row
#: properties plus every body/metadata extra a conversation view needs —
#: ``cc``/``bcc``/``replyTo``/``sentAt`` (the recipients/date beyond the
#: list row's own ``from``/``receivedAt``), ``blobId`` (the whole-message
#: blob, for "show original"/"download"), the body parts themselves,
#: ``attachments``, and the two RFC 8621 §4.1.7 ``header:<name>:asText``
#: pseudo-properties a conversation view reads for its "mailed by"/"signed
#: by" row. ``textBody``/``htmlBody``/``bodyValues`` must be named here
#: explicitly (RFC 8621 §4.4) — ``fetchTextBodyValues``/
#: ``fetchHTMLBodyValues`` alone only control whether ``bodyValues`` is
#: *populated*, not whether any of those three properties is *returned* at
#: all.
#: ``messageId``/``inReplyTo``/``references`` (RFC 8621 §4.1.3) were added
#: for Phase 1C: ``mailosh.services.compose.build_reply`` is a *pure*
#: function over an already-fetched thread, so the only place a reply's
#: ``In-Reply-To``/``References`` can come from is the thread fetch itself.
#: Without them here, every reply this app composes would break threading
#: in every other mail client — and silently, since nothing else in the app
#: reads them.
_EMAIL_BODY_PROPS = [
    *_EMAIL_LIST_PROPS,
    "cc",
    "bcc",
    "replyTo",
    "sentAt",
    "blobId",
    "textBody",
    "htmlBody",
    "bodyValues",
    "attachments",
    "messageId",
    "inReplyTo",
    "references",
    "header:Return-Path:asText",
    "header:Authentication-Results:asText",
]

#: Cap on fetched body value bytes per RFC 8621 §4.4's ``maxBodyValueBytes``
#: — keeps a pathological plaintext/HTML part from blowing up a thread-fetch
#: response. RFC 8621 defines a single cap covering *both*
#: ``fetchTextBodyValues`` and ``fetchHTMLBodyValues`` (there is no separate
#: per-type limit to set), so raising it to cover real-world HTML
#: newsletters raises it for plaintext too. 512 KiB comfortably covers
#: essentially every real newsletter's HTML part while staying bounded at
#: ~10 MB for a 20-message thread — truncation past this cap is surfaced to
#: the caller via ``EmailBody.text_truncated``/``html_truncated`` rather
#: than silently dropped.
_MAX_BODY_VALUE_BYTES = 512 * 1024

#: ``Email/get`` properties for ``query_page``'s final fetch — one
#: ``ThreadRow`` (``mailosh.services.thread_list``) needs everything here
#: aggregated across every message in its thread (sender list, unread/
#: starred/attachment "any", label chips, latest subject/preview/date).
#: Deliberately narrower than ``_EMAIL_LIST_PROPS`` (no ``to`` — no current
#: view needs the recipient list for a collapsed row) per the Task 6
#: brief's own exact properties list.
_THREAD_ROW_PROPS = [
    "id",
    "threadId",
    "mailboxIds",
    "keywords",
    "from",
    "subject",
    "receivedAt",
    "preview",
    "hasAttachment",
]

#: ``event_stream``'s requested EventSource ``{ping}`` interval, in seconds
#: — also used to size that call's read-timeout override (see its
#: docstring for why the two are related, not independent numbers).
_PING_SECONDS = 30


def _to_utc_date(dt: datetime) -> str:
    """Format ``dt`` as a JMAP UTCDate (RFC 8620 §1.2): trailing ``Z``, never
    a numeric offset (which ``datetime.isoformat()`` produces for an
    aware UTC value, e.g. ``+00:00``, and JMAP's UTCDate grammar rejects).

    A naive ``dt`` (no tzinfo) is treated as already being UTC rather than
    the host's local zone — silently reinterpreting it through local time
    would be a worse surprise for a caller than assuming UTC, which is what
    every other timestamp this client produces already is.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _true_keys(flag_map: dict | None) -> set[str]:
    """The keys a JMAP boolean-set object actually has set — RFC 8620 sends a
    set as ``{"id": true}``, and an explicit ``false`` is as good as absent.

    ``mailosh.jmap.models`` applies this rule to the pydantic models via its
    own ``_flag_map_to_set`` validator; ``EmailState`` is a plain dataclass
    built straight off the wire (no pydantic round trip for a four-property
    snapshot), so it needs the same rule spelled out here.
    """
    return {key for key, on in (flag_map or {}).items() if on}


def _check_updated(response: dict, obj_id: str) -> None:
    """Raise ``JmapError`` unless ``obj_id`` succeeded in an ``Email/set`` update.

    RFC 8620 §5.3: a per-object update failure is reported in ``notUpdated``
    (a map of id -> SetError) alongside an otherwise-normal 2xx response, not
    as a batch-level ``error`` response the way a malformed request is — so
    without this check, ``set_keyword``/``move`` calling a stale or
    forbidden id would return ``None`` exactly as on success. An id reported
    in neither ``updated`` nor ``notUpdated`` is treated as a failure too,
    the same as ``import_email``'s ``created``/``notCreated`` check below: a
    server that says nothing about the id isn't evidence it worked.
    """
    if obj_id in (response.get("updated") or {}):
        return
    error = (response.get("notUpdated") or {}).get(obj_id)
    if error is None:
        raise JmapError(f"Email/set update failed for {obj_id!r}: not reported in response")
    err_type = error.get("type", "error")
    description = error.get("description")
    detail = f"{err_type}: {description}" if description else err_type
    raise JmapError(f"Email/set update failed for {obj_id!r}: {detail}")


def _mailbox_id(mailboxes: list[Mailbox], role: str) -> str:
    """Return the id of the mailbox with the given ``role`` (RFC 8621 §2), or
    raise ``JmapError`` if the account has none.

    Same "fail loudly, don't guess" shape as ``find_inbox`` below — but
    returns just the id, for any role, not the whole ``Mailbox``: used by
    ``send`` to resolve the Drafts/Sent mailbox ids it needs, neither of
    which any caller needs the full object for.
    """
    for mailbox in mailboxes:
        if mailbox.role == role:
            return mailbox.id
    raise JmapError(f"no mailbox with role={role!r} in this account")


def _address_json(address: Address) -> dict[str, str]:
    """One RFC 8621 §4.1.2.3 EmailAddress, with ``name`` omitted when there
    is none — not sent as an explicit ``null``.

    Omission rather than ``{"name": None}`` because a JMAP server is free to
    round-trip a literal null back as a display name, and because it keeps
    the wire body for the overwhelmingly common bare-address case exactly
    what ``send`` has always sent (``{"email": "..."}``).
    """
    if address.name:
        return {"email": address.email, "name": address.name}
    return {"email": address.email}


def _bare_message_id(value: str) -> str:
    """A Message-ID with RFC 5322's angle brackets stripped.

    RFC 8621 §4.1.2.4's ``asMessageIds`` form — which ``inReplyTo``/
    ``references`` are (§4.1.3) — is the id *without* brackets, and the
    server puts them back when it writes the header. Callers hand this
    method either spelling (a header copied out of a raw message carries
    them; a value read back off ``EmailBody.references`` does not), so it
    normalises rather than trusting.
    """
    return value.strip().strip("<>").strip()


def _attachment_part(part: BodyPart) -> dict[str, object]:
    """One ``bodyStructure`` sub-part for an already-uploaded blob.

    Unlike the authored ``text/plain``/``text/html`` parts — which carry a
    ``partId`` keying into a sibling ``bodyValues`` map, because their
    content is being written inline in this same request — an attachment is
    a blob the client uploaded first, so it is referenced by ``blobId`` and
    has no ``bodyValues`` entry at all (RFC 8621 §4.1.4).

    ``disposition`` is derived from ``cid``, not taken from the caller: a
    part with a Content-ID is an image the body references with a
    ``cid:`` URL and must be ``inline`` for a reading client to draw it in
    place rather than list it as a separate file; everything else is
    ``attachment``. The Content-ID goes on the wire **bare**, no angle
    brackets, matching the ``cid`` property's asMessageIds-like spelling —
    verified live: Stalwart accepts the bare form, writes
    ``Content-ID: <bare>`` into the generated MIME, and reads it back bare.

    ``size`` is deliberately **not** sent even though ``BodyPart`` carries
    one: RFC 8621 §4.1.4 marks it server-set. Stalwart happens to tolerate
    a client-supplied ``size`` (checked), but a value we cannot compute
    authoritatively — it is the size *after* content-transfer decoding, not
    the size of the bytes we uploaded — has no business on the wire.
    """
    out: dict[str, object] = {"blobId": part.blob_id, "type": part.type}
    if part.name:
        out["name"] = part.name
    if part.cid:
        out["cid"] = _bare_message_id(part.cid)
        out["disposition"] = "inline"
    else:
        out["disposition"] = "attachment"
    return out


def _body_structure(
    *, text: str, html: str | None, attachments: Sequence[BodyPart]
) -> tuple[dict[str, object], dict[str, dict[str, str]]]:
    """Build ``(bodyStructure, bodyValues)`` for one outgoing message.

    Assembled outwards from the authored body, adding a wrapper only when
    something needs one, so a plain text-only message still serialises to
    the single flat ``{"partId": "t", "type": "text/plain"}`` part
    ``send`` has always produced and no message carries a pointless
    one-child multipart:

    1. the authored body — ``multipart/alternative`` over ``t``
       (text/plain) and ``h`` (text/html) when there is HTML, else ``t``
       alone;
    2. wrapped in ``multipart/related`` if any attachment carries a
       ``cid``, because that is the container that tells a reading client
       "these parts belong *to* that body" rather than "these are files
       sent alongside it" (RFC 2387) — get this wrong and an inline image
       shows up as a download instead of in the text;
    3. wrapped in ``multipart/mixed`` if any attachment does not, which is
       the ordinary "body plus files" envelope.

    A message with both kinds nests all three, inline parts inside the
    ``related`` and files inside the ``mixed`` — the shape a Gmail message
    with a logo and a PDF has, and the shape verified live against
    Stalwart (which parses it back into exactly these ``attachments``
    entries, cid and disposition intact).
    """
    if html:
        body: dict[str, object] = {
            "type": "multipart/alternative",
            "subParts": [
                {"partId": "t", "type": "text/plain"},
                {"partId": "h", "type": "text/html"},
            ],
        }
        body_values = {"t": {"value": text}, "h": {"value": html}}
    else:
        body = {"partId": "t", "type": "text/plain"}
        body_values = {"t": {"value": text}}

    inline = [part for part in attachments if part.cid]
    files = [part for part in attachments if not part.cid]
    if inline:
        body = {
            "type": "multipart/related",
            "subParts": [body, *(_attachment_part(part) for part in inline)],
        }
    if files:
        body = {
            "type": "multipart/mixed",
            "subParts": [body, *(_attachment_part(part) for part in files)],
        }
    return body, body_values


def _draft_creation(
    *,
    drafts_id: str,
    sender: Address,
    to: Sequence[Address],
    cc: Sequence[Address],
    bcc: Sequence[Address],
    subject: str,
    text: str,
    html: str | None,
    attachments: Sequence[BodyPart],
    in_reply_to: Sequence[str],
    references: Sequence[str],
) -> dict[str, object]:
    """The ``Email/set`` ``create`` object for one draft — the single place
    an outgoing message's wire shape is decided, shared by ``create_draft``
    (autosave) and ``send_message`` (the send batch), so a draft the user
    saves and the message that finally goes out cannot disagree about how
    they were built.

    ``cc``/``bcc``/``inReplyTo``/``references`` are omitted entirely when
    empty rather than sent as empty arrays: an ordinary message has none of
    them, and an omitted property is unambiguously "no such header" where
    ``[]`` invites a server to decide for itself whether to emit a bare
    ``Cc:``.

    Always created ``$draft``+``$seen`` in Drafts, even on the send path.
    That is not a detour — RFC 8621 §7.5 has no way to submit a message
    that does not already exist as an Email, and creating it anywhere but
    Drafts would file the user's own outgoing mail into their Inbox for the
    instant before ``onSuccessUpdateEmail`` moves it to Sent.
    """
    body_structure, body_values = _body_structure(text=text, html=html, attachments=attachments)
    draft: dict[str, object] = {
        "mailboxIds": {drafts_id: True},
        "keywords": {"$draft": True, "$seen": True},
        "from": [_address_json(sender)],
        "to": [_address_json(address) for address in to],
        "subject": subject,
        "bodyStructure": body_structure,
        "bodyValues": body_values,
    }
    if cc:
        draft["cc"] = [_address_json(address) for address in cc]
    if bcc:
        draft["bcc"] = [_address_json(address) for address in bcc]
    if in_reply_to:
        draft["inReplyTo"] = [_bare_message_id(value) for value in in_reply_to]
    if references:
        draft["references"] = [_bare_message_id(value) for value in references]
    return draft


def find_inbox(mailboxes: list[Mailbox]) -> Mailbox:
    """Return the ``role == "inbox"`` mailbox out of an already-fetched list.

    The one shared inbox-resolution helper for every caller in this repo
    (phase0 final-review FIX 4) — previously duplicated three different
    ways: this exact role-based scan (formerly private to
    ``mailosh.web.app`` as ``_find_inbox``), and, separately, the idiom
    ``{m.role or m.name: m}["inbox"]`` in ``mailosh/cli.py``,
    ``scripts/measure.py``, and the live integration test. That dict form
    was a real latent bug, not just duplication: a user mailbox merely
    *named* "inbox" (no role set) collides in the dict with the real
    role-tagged inbox under the same key, so whichever one
    ``get_mailboxes()`` happens to list last silently wins — in
    ``cli.py``'s ``import-mbox`` command, that could file imported mail
    into the wrong mailbox. This function only ever matches
    ``role == "inbox"``, so a same-named-but-roleless mailbox can never
    shadow it regardless of list order (see
    ``tests/unit/test_jmap_mail.py::test_find_inbox_ignores_a_same_named_mailbox_with_no_role``).

    Raises ``JmapError`` — not a bare ``LookupError`` — if no mailbox is
    flagged ``role="inbox"``: shouldn't happen against a real JMAP account
    (every server provisions one), but failing loudly beats quietly
    rendering/importing into the wrong mailbox. ``JmapError`` specifically
    so a caller catching just that (per its own docstring: "catching this
    alone covers every failure mode of JmapClient") still catches this one
    too — the same reasoning ``_mailbox_id`` above already uses.
    """
    for mailbox in mailboxes:
        if mailbox.role == "inbox":
            return mailbox
    raise JmapError("no mailbox with role='inbox' in this account")


def _transport_error(exc: httpx.HTTPStatusError | httpx.TransportError) -> TransportError:
    """Translate an httpx-level failure into our TransportError, keeping status/message.

    ``exc`` is either an ``httpx.HTTPStatusError`` (a response came back, but
    ``raise_for_status()`` rejected it) or one of httpx's own
    ``TransportError`` subclasses (no response at all: refused connection,
    timeout, DNS failure, ...) — the two ``except`` sites below are the only
    callers, and both already narrow to this union.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return TransportError(
            f"JMAP request to {exc.request.url} failed: {resp.status_code} {resp.reason_phrase}",
            status_code=resp.status_code,
        )
    return TransportError(f"JMAP request failed: {exc}", status_code=None)


@dataclass(frozen=True)
class SseFrame:
    """One dispatched Server-Sent Event frame: an ``event`` name (``None``
    if the frame carried no ``event:`` field at all) and its ``data``
    (every ``data:`` line seen before dispatch, joined with ``"\\n"``)."""

    event: str | None
    data: str


async def parse_sse_stream(lines: AsyncIterable[str]) -> AsyncIterator[SseFrame]:
    """Parse a raw SSE (``text/event-stream``) line stream into dispatched frames.

    Implements just enough of the WHATWG EventSource "interpret an event
    stream" algorithm for this client's needs: accumulates ``event:``/
    ``data:`` field lines and dispatches one ``SseFrame`` per blank line
    (multiple ``data:`` lines join with ``"\\n"``, matching the spec),
    ignoring comment lines (a leading ``:``) and any other field name
    (``id:``, ``retry:``, ...) — this client has no use for last-event-id
    resumption or a server-suggested retry delay. A single leading space
    after the colon in ``field: value`` is stripped, per the spec.

    Deliberately content-agnostic: it knows nothing about httpx, JMAP, or
    ``StateChange`` — just lines in, frames out — so it has direct unit
    tests with a canned line list (see ``tests/unit/test_sse_hub.py``)
    instead of needing a mocked HTTP stream, and it yields a frame for
    *every* event name it sees, including ones this client doesn't care
    about (e.g. a ``ping`` keepalive) — filtering to ``event: state`` is
    ``event_stream``'s job, not this parser's, so that behavior has its own
    direct test too.

    One deliberate simplification versus the browser spec: a blank line
    dispatches whenever *any* ``event:``/``data:`` field was accumulated
    since the last dispatch (so an event-only frame with no ``data:`` line
    still dispatches, as ``SseFrame(event=..., data="")``) rather than the
    spec's stricter "only dispatch if the data buffer is non-empty" rule —
    a comment-only block (no fields at all) still yields nothing, which is
    the case that actually matters here. An incomplete final block (input
    ends without a trailing blank line) is likewise never dispatched,
    matching the spec.
    """
    event: str | None = None
    data_lines: list[str] = []
    has_content = False
    async for line in lines:
        if line == "":
            if has_content:
                yield SseFrame(event=event, data="\n".join(data_lines))
            event = None
            data_lines = []
            has_content = False
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
            has_content = True
        elif field == "data":
            data_lines.append(value)
            has_content = True
        # else: ignore id/retry/any other field name.


@dataclass(frozen=True)
class Snippet:
    """One ``SearchSnippet`` (RFC 8621 §5): the matched message's subject
    and preview with every match wrapped in ``<mark>``…``</mark>``.

    Both halves are nullable and routinely are: the server returns ``null``
    for whichever of the two the query did not match in (a `from:` term
    matches neither, a body word matches only the preview), and the caller
    is expected to fall back to the message's own plain text there rather
    than render an empty row.

    **The strings are markup, and this layer does not sanitise them.**
    Stalwart escapes the message's own text before inserting the ``<mark>``
    tags, but "the mail server escaped it" is not a property this client
    can check, and the text inside is a stranger's — so whatever renders a
    snippet must re-escape everything that is not a ``<mark>`` tag of its
    own accord (`mailosh.web.search._highlight` is what does).
    """

    subject: str | None
    preview: str | None


@dataclass(frozen=True)
class QueryPage:
    """The raw result of `query_page`'s four-call chain, one step short of
    a view model (`mailosh.services.thread_list.ThreadPage`/`ThreadRow`
    build on top of this, not this client — this layer has no opinion on
    "me", "now", or a nav's label tree).

    `thread_order` is every thread id on this page, newest-first (i.e. in
    `Email/query`'s own collapsed-and-sorted order) — a plain `list`, not a
    `set`, specifically because that order is the point: it's what lets a
    caller lay out rows top-to-bottom. `emails_by_thread` then has, for
    every id in `thread_order`, *all* of that thread's messages (not just
    the one `Email/query` collapsed to) — every message a `ThreadRow`
    needs to aggregate over (its sender list, unread/starred "any",
    attachment "any", label union). `total`/`position` echo `Email/query`'s
    own response (`calculateTotal=True` requests the former).

    `snippets` is empty unless `query_search(..., snippets=True)` asked for
    them, and is keyed by **thread id, not email id** — deliberately, and
    it is the one place this layer joins two of its own calls together.
    `SearchSnippet/get` answers per *matching message*, and which message
    that was is `Email/query`'s own collapsed representative: the join
    lives here because the `{email id -> thread id}` map that performs it
    is built here and exposed nowhere, so a caller keyed by email id could
    not do it. A row therefore looks its highlight up by the same id it
    already draws itself with.
    """

    thread_order: list[str]
    total: int
    emails_by_thread: dict[str, list[EmailHeader]]
    position: int
    snippets: dict[str, Snippet] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailState:
    """Where one message currently *sits* — its thread, its mailbox
    membership, its keywords — and nothing else.

    Deliberately not a JMAP ``state`` string (RFC 8620 §5.1's opaque
    per-datatype version token): "state" here is the message's own placement,
    the four properties `mailosh.services.actions` (Task 9) needs in order to
    decide what an action must patch, what the placement was *before* it (so
    an undo token can restore it exactly), which thread rows leave the list,
    and whether a nav badge moves. Not an ``EmailHeader``, because none of the
    row-shaped properties (``from``/``subject``/``receivedAt``/``preview``/
    ``hasAttachment``) are wanted here — a bulk action over a 200-message
    selection has no business dragging 200 previews across the wire.

    ``mailbox_ids``/``keywords`` are ``frozenset``s rather than ``set``s: a
    snapshot is read-only by construction, so an action computing "before
    minus after" set differences can never mutate the "before" it is
    comparing against.
    """

    id: str
    thread_id: str
    mailbox_ids: frozenset[str]
    keywords: frozenset[str]


class JmapClient:
    """A connected JMAP session plus the ability to make batched method calls."""

    def __init__(self, http: httpx.AsyncClient, session: Session) -> None:
        self._http = http
        self._session = session
        #: Populated by ``get_identity`` on its first successful fetch, and
        #: reused by every call after that (including from within ``send``)
        #: — see that method's own docstring for why this instance
        #: attribute exists rather than re-fetching every time.
        self._identity: Identity | None = None

    @classmethod
    async def _connect_with(cls, base_url: str, **client_kwargs: object) -> JmapClient:
        """Shared body of ``connect``/``connect_bearer``: discover the JMAP
        session and return a connected client, given whichever
        authentication kwargs the caller already filled in for
        ``httpx.AsyncClient`` (``auth=(username, password)`` for Basic,
        ``headers={"Authorization": "Bearer ..."}`` for Bearer).

        Fetches ``GET {base_url}/.well-known/jmap``, parses the session, and
        rebases its advertised apiUrl/uploadUrl/eventSourceUrl onto
        ``base_url`` (see ``Session.rebase``). Any failure during setup
        closes the half-built HTTP client before propagating (as
        ``TransportError`` for a bad/absent response, or unwrapped for
        anything else, e.g. a malformed session body) -- identical either
        way, since nothing about session discovery itself differs by
        authentication scheme.
        """
        http = httpx.AsyncClient(
            http2=False,
            timeout=30,
            follow_redirects=True,  # GET /.well-known/jmap 307s to /jmap/session
            **client_kwargs,
        )
        try:
            resp = await http.get(f"{base_url}/.well-known/jmap")
            resp.raise_for_status()
            session = Session.from_jmap(resp.json()).rebase(base_url)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            await http.aclose()
            raise _transport_error(exc) from exc
        except Exception:
            await http.aclose()
            raise
        return cls(http, session)

    @classmethod
    async def connect(cls, base_url: str, username: str, password: str) -> JmapClient:
        """Discover the JMAP session and return a connected client,
        authenticating with HTTP Basic (``username``/``password``) -- see
        ``_connect_with`` for the shared discovery/error-handling logic.
        """
        return await cls._connect_with(base_url, auth=(username, password))

    @classmethod
    async def connect_bearer(cls, base_url: str, token: str) -> JmapClient:
        """Discover the JMAP session and return a connected client,
        authenticating with ``Authorization: Bearer <token>`` instead of
        HTTP Basic (design spec §9) -- see ``_connect_with`` for the shared
        discovery/error-handling logic, identical either way.

        ``token`` is a per-session Stalwart API key secret minted by
        ``StalwartAdmin.create_api_key`` (Task 4, SPK-3): unlike ``connect``,
        there is no username/password here at all -- this client's whole
        identity comes from the token, exactly the way a browser session
        that has already exchanged a user's password for a Stalwart API key
        (design spec §9's login flow) then never touches that password
        again. Every request this client makes after connecting --
        including its own internal ``_call``/``upload``/``event_stream``,
        all of which reuse ``self._http`` -- carries this same Bearer
        header; there is no separate re-authentication step.
        """
        return await cls._connect_with(base_url, headers={"Authorization": f"Bearer {token}"})

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._http.aclose()

    @property
    def account_id(self) -> str:
        """The primary mail account id resolved at connect time."""
        return self._session.primary_account_id

    async def _post_batch(
        self, method_calls: list[tuple[str, dict, str]]
    ) -> list[tuple[str, dict, str]]:
        """POST a batched JMAP request and return the raw ``methodResponses``
        array verbatim — every response tuple exactly as the server sent it,
        ``error``-named tuples and duplicate call ids included, no
        deduplication, no raising.

        The shared primitive behind both ``_call`` (which maps by call id,
        keeps the first response for a duplicate id, and raises
        ``MethodError`` on the first ``error`` tuple) and ``_call_raw``
        (which hands every tuple back untouched) — the HTTP POST, transport-
        error translation, and "does this response even have a
        ``methodResponses`` array" check are identical either way; only what
        each caller does with the resulting list differs. Not meant to be
        called directly by anything outside this pair.

        Raises ``TransportError`` on a non-2xx response or a connection
        failure, ``JmapError`` if the response body has no
        ``methodResponses`` at all.
        """
        body = {
            "using": list(USING),
            "methodCalls": [[name, args, call_id] for name, args, call_id in method_calls],
        }
        try:
            resp = await self._http.post(self._session.api_url, json=body)
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise _transport_error(exc) from exc
        data = resp.json()

        if "methodResponses" not in data:
            raise JmapError(f"JMAP response missing 'methodResponses': {data!r}")

        return [(name, args, call_id) for name, args, call_id in data["methodResponses"]]

    async def _call(self, method_calls: list[tuple[str, dict, str]]) -> dict[str, dict]:
        """POST a batched JMAP request and return each response keyed by call id.

        Raises ``TransportError`` on a non-2xx response or a connection
        failure, ``JmapError`` if the response body has no
        ``methodResponses``, and ``MethodError`` on the first
        ``error``-named response tuple (RFC 8620 §3.5.1) — scanned in
        response order, so a batch with an earlier success and a later error
        still raises, attributing the right ``call_id``.

        Keeps the *first* response seen for a given call id, not the last —
        found live (Task 9), not by any mocked unit test: RFC 8620 §5.3
        implicit method calls (e.g. the ``Email/set`` update
        ``send()``'s ``onSuccessUpdateEmail`` triggers) are appended to
        ``methodResponses`` *after* every explicit call's own response, "in
        the order they were triggered" — the RFC never requires an implicit
        call's id to differ from its triggering explicit call's, and
        Stalwart's own EmailSubmission/set consistently reuses it. Without
        this, a caller's explicit-call response (e.g. the actual
        ``EmailSubmission/set`` ``created`` map) would be silently
        clobbered by that later, same-id implicit ``Email/set`` response —
        exactly what happened before this fix. Every call id this client
        sends today is otherwise unique per request, so this changes
        nothing for any caller that never triggers an implicit call.

        Every caller except ``send`` uses this method, not ``_call_raw`` —
        this dedup-and-raise behavior is exactly what they want, and none of
        them triggers an implicit call whose own failure needs to be
        inspected separately from its triggering explicit call's.
        """
        results: dict[str, dict] = {}
        for name, args, call_id in await self._post_batch(method_calls):
            if name == "error":
                raise MethodError(args.get("type", "error"), call_id)
            results.setdefault(call_id, args)
        return results

    async def _call_raw(
        self, method_calls: list[tuple[str, dict, str]]
    ) -> list[tuple[str, dict, str]]:
        """POST a batched JMAP request and return every response triple
        verbatim (see ``_post_batch``) — no deduplication by call id, and no
        raising on an ``error``-named tuple.

        Used only by ``send``: unlike every other caller in this client,
        ``send`` needs to see an implicit ``onSuccessUpdateEmail``-triggered
        ``Email/set`` update's own outcome even though it shares a call id
        with (and would otherwise be silently discarded behind, by
        ``_call``'s keep-first deduplication, or turned into an unwanted
        hard failure, by ``_call``'s raise-on-error behavior) the explicit
        ``EmailSubmission/set`` call it came from. See ``send``'s own
        docstring for exactly how it uses this.
        """
        return await self._post_batch(method_calls)

    async def get_mailboxes(self) -> list[Mailbox]:
        """Fetch every mailbox (folder/label) in the account, one HTTP request.

        ``properties`` is omitted deliberately: unlike ``Email/get`` (where
        the body-related properties are expensive and opt-in), a plain
        ``Mailbox/get`` with no ``properties`` returns every standard
        property, and ``Mailbox.model_validate`` already ignores whatever it
        doesn't model.
        """
        out = await self._call([("Mailbox/get", {"accountId": self.account_id}, "m0")])
        return [Mailbox.model_validate(m) for m in out["m0"]["list"]]

    async def get_identity(self) -> Identity:
        """Fetch the account's send-from identity (RFC 8621 §6.1
        ``Identity/get``), caching it on this client after the first
        successful fetch.

        Only the first entry in the response's ``list`` is used: this
        client (and Mailosh's single-demo-account P0 scope) has no UI for
        choosing among several configured identities, so whichever one the
        server lists first is what ``send`` sends as. Cached as
        ``self._identity`` — an instance attribute set once, not a
        class-level cache — so every call after the first (including from
        within ``send``) skips the network round trip entirely; nothing
        currently invalidates the cache, since no code path changes an
        account's identities at runtime. Raises ``JmapError`` if the
        account has no identities at all, the same "a server that says
        nothing usable isn't evidence of success" stance ``_check_updated``
        and ``import_email``'s ``notCreated`` check already take elsewhere
        in this client.
        """
        if self._identity is not None:
            return self._identity
        out = await self._call([("Identity/get", {"accountId": self.account_id}, "i0")])
        identities = out["i0"].get("list") or []
        if not identities:
            raise JmapError("Identity/get returned no identities for this account")
        self._identity = Identity.model_validate(identities[0])
        return self._identity

    async def get_identities(self) -> list[Identity]:
        """Every "send as" identity on this account (RFC 8621 §6.1
        ``Identity/get``), in the order the server lists them.

        The plural sibling of ``get_identity`` above, for compose's From
        picker (design spec §8: "From (identity picker when > 1)"), which
        needs the whole list rather than just the default. Deliberately
        **not** cached: unlike the singular form — which ``send`` calls on
        every send and whose cache is what keeps that to two round trips —
        this runs once when a compose dock opens, and an account whose
        identities were edited in Stalwart's admin UI between two dock
        opens should show the new list rather than a stale one.

        It does populate ``get_identity``'s cache with the first entry as a
        side effect, since it has just paid for exactly that fetch and the
        two would otherwise disagree about which identity is the default.
        Raises ``JmapError`` on an account with no identities at all, same
        as ``get_identity``: a From picker with nothing in it is not a
        state any caller can do something useful with.
        """
        out = await self._call([("Identity/get", {"accountId": self.account_id}, "i0")])
        raw = out["i0"].get("list") or []
        if not raw:
            raise JmapError("Identity/get returned no identities for this account")
        identities = [Identity.model_validate(entry) for entry in raw]
        if self._identity is None:
            self._identity = identities[0]
        return identities

    async def query_inbox(
        self, mailbox_id: str, *, limit: int = 50, position: int = 0
    ) -> list[EmailHeader]:
        """List the (thread-collapsed) messages in a mailbox, newest first.

        One HTTP request: ``Email/query`` (``collapseThreads``, sorted by
        ``receivedAt`` descending) chained via an RFC 8620 §3.7 result
        reference into a single ``Email/get`` for the row data — the id list
        ``Email/query`` returns never has to round-trip back to this caller.
        """
        out = await self._call(
            [
                (
                    "Email/query",
                    {
                        "accountId": self.account_id,
                        "filter": {"inMailbox": mailbox_id},
                        "sort": [{"property": "receivedAt", "isAscending": False}],
                        "collapseThreads": True,
                        "position": position,
                        "limit": limit,
                    },
                    "q0",
                ),
                (
                    "Email/get",
                    {
                        "accountId": self.account_id,
                        "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                        "properties": _EMAIL_LIST_PROPS,
                    },
                    "g0",
                ),
            ]
        )
        return [EmailHeader.model_validate(m) for m in out["g0"]["list"]]

    async def query_page(
        self,
        *,
        mailbox_id: str | None,
        position: int,
        limit: int,
        exclude_mailbox_ids: set[str] = frozenset(),
        has_keyword: str | None = None,
    ) -> QueryPage:
        """List one page of (thread-collapsed) threads, *with every message
        in each of those threads*, newest-thread-first — one HTTP request,
        RFC 8621 §4.10's chained-call pattern taken one step further than
        `query_inbox`: `Email/query` (collapsed+sorted ids) -> `Email/get`
        (just `threadId`, to resolve those ids to their threads) ->
        `Thread/get` (every member email id of those threads) -> `Email/get`
        (the row-shaped properties for *all* of those messages).

        That extra chained hop past `query_inbox` is exactly why this is a
        separate method rather than a `query_inbox` parameter:
        `mailosh.services.thread_list.build_page` needs a whole thread's
        worth of messages per row (to aggregate sender list/unread/starred/
        attachment/labels — see `ThreadRow`), not just the one representative
        message per thread `query_inbox` returns.

        Filter (RFC 8621 §4.10's `FilterCondition` — every property given
        here is ANDed together): `mailbox_id` (when not `None`) becomes
        `inMailbox`; `exclude_mailbox_ids` (when non-empty) becomes
        `inMailboxOtherThan`, sorted for a deterministic wire order rather
        than whatever a Python `set`'s hash-randomized iteration order
        happens to produce; `has_keyword` (when not `None`) becomes
        `hasKeyword`. `mailosh.services.thread_list.build_page` is what
        actually decides which combination a given mailbox key needs
        (design spec §5.2's nav keys): inbox/sent/drafts/archive/spam/trash
        -> `mailbox_id` alone; "starred" -> `has_keyword="$flagged"` +
        `exclude_mailbox_ids={spam, trash}` (a message flagged while sitting
        in Trash/Spam shouldn't surface in Starred); "all mail" ->
        `exclude_mailbox_ids={spam, trash}` alone, `mailbox_id=None`.

        `has_keyword` isn't in the Phase 1A plan's own Interfaces block
        (`mailbox_id`/`position`/`limit`/`exclude_mailbox_ids` only) — added
        as one more optional, keyword-only parameter (default `None`, every
        other parameter's name/type/position/default unchanged) once it
        became clear the "starred" filter design spec §5.2 and the Task 6
        brief both call for (`hasKeyword: $flagged`) has no other way to
        reach this method's `Email/query` call. Purely additive: any caller
        written against the plan's literal signature still works unchanged.

        Response parsing: the *ids* `Email/query` returned (not the second
        `Email/get`'s own `list` order, which RFC 8620 never guarantees
        matches request order) drive `thread_order`, via a `{email_id:
        thread_id}` map built from that second call — collapseThreads
        guarantees at most one representative id per thread, so this is a
        clean, duplicate-free list. The final `Email/get`'s messages are
        grouped by their own `threadId` into `emails_by_thread`.
        """
        filter_condition: dict[str, object] = {}
        if mailbox_id is not None:
            filter_condition["inMailbox"] = mailbox_id
        if exclude_mailbox_ids:
            filter_condition["inMailboxOtherThan"] = sorted(exclude_mailbox_ids)
        if has_keyword is not None:
            filter_condition["hasKeyword"] = has_keyword
        return await self._query_threads(filter_condition, position, limit)

    async def query_search(
        self, *, filter: dict[str, object], position: int, limit: int, snippets: bool = False
    ) -> QueryPage:
        """`query_page` for a filter this client did not build.

        Same one-request chained call, same thread collapsing, same page
        shape -- the only difference is that the filter is handed in whole.
        Search needs `FilterOperator` trees (`AND`/`OR`/`NOT` around
        `from`/`subject`/`body`/`before`/`hasKeyword`/...) that
        `query_page`'s three structured arguments cannot express, and giving
        it a raw-filter escape hatch is smaller and much less error-prone
        than growing it a keyword argument per operator.

        The filter is passed to the server verbatim, so **it must be built
        by `mailosh.services.search_query`, never assembled from user text
        here**. That parser is the only thing that decides what a reader's
        query is allowed to mean; a second, looser path to this method would
        make it the weakest one.

        `snippets=True` adds RFC 8621 §5's `SearchSnippet/get` to the same
        request (never a second round trip) and fills `QueryPage.snippets`
        — the matched subject/preview with `<mark>` around every hit, which
        is what turns a results row from "a message that matched" into "the
        words that matched". Off by default: `query_page` shares this chain
        and an inbox has nothing to highlight.

        Verified live against Stalwart before anything was built on it. The
        session document advertises no capability of its own for this
        method — RFC 8621 defines `SearchSnippet` under
        `urn:ietf:params:jmap:mail`, which every mail server advertises
        whether or not it implements this half of it — so "does the server
        say so" is not a question that can be asked, and the method was
        called instead. A server that does not implement it answers this
        batch with an `unknownMethod` error, which `_call` raises as
        `MethodError`; `mailosh.web.search` catches that one case and
        re-runs the search without snippets rather than losing a page of
        results over a decoration.
        """
        return await self._query_threads(filter, position, limit, snippets=snippets)

    async def _query_threads(
        self,
        filter_condition: dict[str, object],
        position: int,
        limit: int,
        *,
        snippets: bool = False,
    ) -> QueryPage:
        """The chained `Email/query` -> `Email/get` -> `Thread/get` ->
        `Email/get` call shared by `query_page` and `query_search`.

        Split out rather than duplicated: the four-call chain and its
        `#ids` result references are the subtle part (see `query_page`'s
        docstring for why each link is shaped the way it is), and two copies
        would drift the moment either grew a property.

        `snippets` appends a fifth call. It reads the *same* `q0` ids the
        chain already resolved (`#emailIds`, a result reference, so the
        matching messages are named once) and the *same* filter, which is
        what RFC 8621 §5.2 requires — a `SearchSnippet/get` whose filter
        disagrees with the query it describes would highlight words the
        reader never asked for.
        """
        calls: list[tuple[str, dict, str]] = [
            (
                "Email/query",
                {
                    "accountId": self.account_id,
                    "filter": filter_condition,
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "collapseThreads": True,
                    "calculateTotal": True,
                    "position": position,
                    "limit": limit,
                },
                "q0",
            ),
            (
                "Email/get",
                {
                    "accountId": self.account_id,
                    "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                    "properties": ["threadId"],
                },
                "g0",
            ),
            (
                "Thread/get",
                {
                    "accountId": self.account_id,
                    "#ids": {
                        "resultOf": "g0",
                        "name": "Email/get",
                        "path": "/list/*/threadId",
                    },
                },
                "t0",
            ),
            (
                "Email/get",
                {
                    "accountId": self.account_id,
                    "#ids": {
                        "resultOf": "t0",
                        "name": "Thread/get",
                        "path": "/list/*/emailIds/*",
                    },
                    "properties": _THREAD_ROW_PROPS,
                },
                "e0",
            ),
        ]
        if snippets:
            calls.append(
                (
                    "SearchSnippet/get",
                    {
                        "accountId": self.account_id,
                        "filter": filter_condition,
                        "#emailIds": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                    },
                    "n0",
                )
            )
        out = await self._call(calls)

        query_ids = out["q0"].get("ids") or []
        thread_id_by_email_id = {row["id"]: row["threadId"] for row in out["g0"]["list"]}
        thread_order = [
            thread_id_by_email_id[eid] for eid in query_ids if eid in thread_id_by_email_id
        ]

        emails_by_thread: dict[str, list[EmailHeader]] = defaultdict(list)
        for raw in out["e0"]["list"]:
            header = EmailHeader.model_validate(raw)
            emails_by_thread[header.thread_id].append(header)

        # Keyed by thread, via the map built two statements up — see
        # `QueryPage.snippets`. A snippet whose message this chain never
        # resolved to a thread is dropped rather than kept under its email
        # id: nothing in this app can look one up that way.
        by_thread: dict[str, Snippet] = {}
        for raw in out.get("n0", {}).get("list") or []:
            thread_id = thread_id_by_email_id.get(raw.get("emailId"))
            if thread_id is not None:
                by_thread[thread_id] = Snippet(
                    subject=raw.get("subject"), preview=raw.get("preview")
                )

        return QueryPage(
            thread_order=thread_order,
            total=out["q0"].get("total", 0),
            emails_by_thread=dict(emails_by_thread),
            position=out["q0"].get("position", position),
            snippets=by_thread,
        )

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
        """Fetch every message in a thread, bodies included, one HTTP request.

        ``Thread/get`` for the member email ids, chained via a result
        reference into one ``Email/get`` with ``fetchTextBodyValues=True``
        and ``fetchHTMLBodyValues=True``. Each returned list item is handed
        straight to ``EmailBody.model_validate`` — its ``_resolve_text_body``/
        ``_resolve_html_body`` validators collapse the wire-format
        ``textBody``/``htmlBody``/``bodyValues`` into ``text_body``/
        ``html_body`` themselves, so there is no hand-merging to do here.
        """
        out = await self._call(
            [
                ("Thread/get", {"accountId": self.account_id, "ids": [thread_id]}, "t0"),
                (
                    "Email/get",
                    {
                        "accountId": self.account_id,
                        "#ids": {
                            "resultOf": "t0",
                            "name": "Thread/get",
                            "path": "/list/*/emailIds/*",
                        },
                        "properties": _EMAIL_BODY_PROPS,
                        "fetchTextBodyValues": True,
                        "fetchHTMLBodyValues": True,
                        "maxBodyValueBytes": _MAX_BODY_VALUE_BYTES,
                    },
                    "e0",
                ),
            ]
        )
        return [EmailBody.model_validate(m) for m in out["e0"]["list"]]

    async def set_keyword(self, email_id: str, keyword: str, on: bool) -> None:
        """Set or clear a single keyword (e.g. ``$seen``, ``$flagged``) on one message.

        Uses an RFC 8620 §5.3 patch object (``"keywords/$seen": true``)
        rather than replacing the whole ``keywords`` map, so this can't
        clobber a keyword some other client set concurrently. Checks the
        response's ``updated``/``notUpdated`` maps (see ``_check_updated``)
        rather than discarding it, so a rejected update (stale id, no
        permission, ...) raises instead of returning as if it had worked.
        """
        out = await self._call(
            [
                (
                    "Email/set",
                    {
                        "accountId": self.account_id,
                        "update": {email_id: {f"keywords/{keyword}": on}},
                    },
                    "s0",
                )
            ]
        )
        _check_updated(out["s0"], email_id)

    async def move(
        self, email_id: str, add: set[str] = frozenset(), remove: set[str] = frozenset()
    ) -> None:
        """Add/remove mailbox membership on one message via patch objects.

        Same patch mechanism as ``set_keyword``: ``"mailboxIds/<id>": true``
        to add, ``"mailboxIds/<id>": null`` to remove — never a full
        ``mailboxIds`` replace, so a concurrent membership change on another
        mailbox id isn't lost. Same response check as ``set_keyword`` too
        (``_check_updated``), so a rejected move raises instead of returning
        as if it had worked.
        """
        patch: dict[str, bool | None] = {f"mailboxIds/{mid}": True for mid in add}
        patch.update({f"mailboxIds/{mid}": None for mid in remove})
        out = await self._call(
            [
                (
                    "Email/set",
                    {"accountId": self.account_id, "update": {email_id: patch}},
                    "s0",
                )
            ]
        )
        _check_updated(out["s0"], email_id)

    async def set_mailboxes(
        self, email_ids: list[str], *, add: set[str] = frozenset(), remove: set[str] = frozenset()
    ) -> None:
        """Bulk version of `move`: the same mailbox-membership patch
        (`"mailboxIds/<id>": true`/`null`, RFC 8620 §5.3) applied to every
        id in `email_ids`, in one `Email/set` call — a multi-select
        archive/spam/trash/move-to-label action (`mailosh.services.
        actions`, Task 9) costs one HTTP round trip for N selected threads
        instead of N.

        Every id shares the same `add`/`remove` patch object — there is no
        per-id variation, matching every bulk triage action this is meant
        for (a selection is always moved the same way, together). Checks
        `updated`/`notUpdated` for every id the same way `move` checks its
        one id (`_check_updated`), raising on the first rejected id found —
        "bulk patch, raises on any notUpdated" per this method's own brief.
        """
        patch: dict[str, bool | None] = {f"mailboxIds/{mid}": True for mid in add}
        patch.update({f"mailboxIds/{mid}": None for mid in remove})
        out = await self._call(
            [
                (
                    "Email/set",
                    {
                        "accountId": self.account_id,
                        "update": {email_id: patch for email_id in email_ids},
                    },
                    "s0",
                )
            ]
        )
        for email_id in email_ids:
            _check_updated(out["s0"], email_id)

    async def set_keywords(self, email_ids: list[str], keyword: str, on: bool) -> None:
        """Bulk version of `set_keyword`: the same single-keyword patch
        (`"keywords/<kw>": true`/`false`) applied to every id in
        `email_ids`, in one `Email/set` call — a multi-select mark-read/
        unread or star/unstar action (Task 9).
        """
        out = await self._call(
            [
                (
                    "Email/set",
                    {
                        "accountId": self.account_id,
                        "update": {email_id: {f"keywords/{keyword}": on} for email_id in email_ids},
                    },
                    "s0",
                )
            ]
        )
        for email_id in email_ids:
            _check_updated(out["s0"], email_id)

    async def get_email_states(self, email_ids: list[str]) -> list[EmailState]:
        """Snapshot where each of `email_ids` currently sits — one
        `Email/get` asking for four properties only (`_EMAIL_STATE_PROPS`).

        Added for Task 9: every mailbox-moving action needs the *current*
        membership before it can compute a correct patch (does archiving this
        message leave it in no mailbox at all?), the previous membership for
        the undo token's `prev`, the thread ids whose rows leave the list, and
        the unread flags the nav badge deltas come from. One request for the
        whole selection, never one per id.

        States come back in the order `email_ids` asked for them, not the
        order the server happened to list them in (RFC 8620 §5.1 never
        promises request order). Ids the server reports in `notFound` are
        simply absent from the result rather than raising: a stale id from a
        list the user has been looking at for a while is a race, not a bug,
        and the caller acts on whatever still exists. An empty `email_ids`
        makes no request at all.

        **Exactly one state per distinct id**, even if `email_ids` repeats one
        (a selection built from overlapping thread rows can, and a caller has
        no way to notice): a message counted twice would double the nav badge
        delta and be listed twice in the undo token, while the `Email/set` that
        follows collapses the duplicate away — i.e. the count would drift from
        the mailbox. Deduplicated here rather than in each caller, since this
        is the one place that turns a list of ids into a set of facts.
        """
        email_ids = list(dict.fromkeys(email_ids))
        if not email_ids:
            return []
        out = await self._call(
            [
                (
                    "Email/get",
                    {
                        "accountId": self.account_id,
                        "ids": email_ids,
                        "properties": _EMAIL_STATE_PROPS,
                    },
                    "e0",
                )
            ]
        )
        by_id = {
            raw["id"]: EmailState(
                id=raw["id"],
                thread_id=raw["threadId"],
                mailbox_ids=frozenset(_true_keys(raw.get("mailboxIds"))),
                keywords=frozenset(_true_keys(raw.get("keywords"))),
            )
            for raw in out["e0"]["list"]
        }
        return [by_id[email_id] for email_id in email_ids if email_id in by_id]

    async def set_mailboxes_patch(
        self,
        patches: Mapping[str, Mapping[str, bool | None]],
        *,
        keywords: Mapping[str, bool] | None = None,
    ) -> None:
        """Per-id mailbox-membership patches — the thing `set_mailboxes`
        cannot express — in a single `Email/set`.

        `patches` maps an email id to `{mailbox_id: True}` (add) /
        `{mailbox_id: None}` (remove), which this turns into RFC 8620 §5.3
        patch pointers (`"mailboxIds/<id>": true|null`). Unlike
        `set_mailboxes`, every id carries its *own* patch — archiving a
        selection removes the Inbox from all of them but adds the Archive
        mailbox only to the ones that would otherwise be left in no mailbox
        at all, which one shared `add`/`remove` pair simply cannot say.

        `keywords` (e.g. `{"$junk": True}`) rides along inside the *same*
        update objects, so "report spam" — move to the Junk mailbox *and* set
        `$junk` — costs one `Email/set`, not two.

        Patch pointers, never a whole-`mailboxIds` replacement: a membership
        this caller's snapshot never saw (a concurrent move from another
        client, a mailbox it isn't reasoning about) is left alone instead of
        being silently dropped. "Move to Trash" is therefore expressed as
        "add Trash, remove every mailbox we saw", which is the same end state
        for every id the caller actually snapshotted.

        Ids whose computed update object would be empty are skipped rather
        than sent as a no-op `{}` update (a server is free to report those in
        neither `updated` nor `notUpdated`, which `_check_updated` would then
        read as a failure); if that leaves nothing at all, no request is made.
        Every id actually sent is checked the way `set_mailboxes` checks its
        own, raising `JmapError` on the first rejected id.
        """
        update: dict[str, dict[str, object]] = {}
        for email_id, patch in patches.items():
            fields: dict[str, object] = {f"mailboxIds/{mid}": on for mid, on in patch.items()}
            fields.update({f"keywords/{kw}": on for kw, on in (keywords or {}).items()})
            if fields:
                update[email_id] = fields
        if not update:
            return
        out = await self._call(
            [("Email/set", {"accountId": self.account_id, "update": update}, "s0")]
        )
        for email_id in update:
            _check_updated(out["s0"], email_id)

    async def upload(self, data: bytes, content_type: str) -> str:
        """Upload a blob (e.g. a raw RFC 5322 message) and return its blobId.

        POSTs raw bytes to the session's ``uploadUrl`` with ``{accountId}``
        substituted in. Unlike ``_call``, this isn't a batched method call at
        all — RFC 8620 §6.1 defines blob upload as its own dedicated binary
        endpoint, so this bypasses ``_call``'s JSON envelope entirely, but
        still goes through the same ``_transport_error`` translation as
        every other HTTP call site in this client.
        """
        url = self._session.upload_url.replace("{accountId}", quote(self.account_id, safe=""))
        try:
            resp = await self._http.post(url, content=data, headers={"content-type": content_type})
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise _transport_error(exc) from exc
        return resp.json()["blobId"]

    def blob_url(self, blob_id: str, *, mime_type: str, name: str) -> str:
        """Build the absolute URL for downloading one blob (RFC 8620 §6.2),
        substituting all four placeholders in the session's (already-rebased)
        ``downloadUrl`` template: ``{accountId}``, ``{blobId}``, ``{type}``
        (the desired ``Content-Type``, echoed back as the ``?accept=`` query
        value) and ``{name}`` (a suggested filename, e.g. for a browser's
        "Save As"). Every placeholder is substituted with
        ``urllib.parse.quote(..., safe="")`` — not a bare ``str.replace`` of
        the raw value — since any of the four can contain characters
        (``/``, ``?``, ``&``, a space) that would otherwise corrupt the URL
        or smuggle in an unintended query parameter.
        """
        return (
            self._session.download_url.replace("{accountId}", quote(self.account_id, safe=""))
            .replace("{blobId}", quote(blob_id, safe=""))
            .replace("{type}", quote(mime_type, safe=""))
            .replace("{name}", quote(name, safe=""))
        )

    @asynccontextmanager
    async def stream_blob(
        self, blob_id: str, *, mime_type: str, name: str
    ) -> AsyncIterator[httpx.Response]:
        """Open a streamed GET against a blob's download URL without
        buffering its body — an async context manager yielding the live
        ``httpx.Response`` so a caller can read it incrementally (e.g.
        ``resp.aiter_bytes()``) instead of loading a potentially large
        attachment whole. ``fetch_blob`` below is built on this; a future
        streamed-download route can use it directly to proxy an attachment
        straight through to a browser response.

        Same ``_transport_error`` translation as ``upload``/``_call`` for a
        non-2xx response or a connection failure — including one that
        happens mid-stream, after some bytes have already reached the
        caller, since the caller's own consumption happens inside this same
        ``with`` block.
        """
        url = self.blob_url(blob_id, mime_type=mime_type, name=name)
        try:
            async with self._http.stream("GET", url) as resp:
                resp.raise_for_status()
                yield resp
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise _transport_error(exc) from exc

    async def fetch_blob(self, blob_id: str, *, mime_type: str, name: str, max_bytes: int) -> bytes:
        """Download a blob fully into memory, capped at ``max_bytes``.

        Iterates ``stream_blob``'s response ``aiter_bytes()`` chunk by
        chunk, accumulating into a ``bytearray`` and raising
        ``BlobTooLarge`` the *moment* the running total exceeds
        ``max_bytes`` — never after reading and buffering the rest of the
        body and checking its final size, which would defeat the point of a
        cap for a hostile or merely huge attachment.
        """
        async with self.stream_blob(blob_id, mime_type=mime_type, name=name) as resp:
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise BlobTooLarge(blob_id, max_bytes)
            return bytes(data)

    async def import_email(
        self,
        blob_id: str,
        mailbox_ids: set[str],
        keywords: set[str],
        received_at: datetime | None,
    ) -> str:
        """Import an uploaded raw-message blob as an Email, into one or more
        mailboxes at once.

        The multi-mailbox membership in a single ``Email/import`` is the
        whole point for Mailosh's Gmail-style labels: setting several ids
        ``true`` in ``mailboxIds`` files the message under all of them
        simultaneously, rather than importing once and then patching
        membership per label as separate round trips.
        """
        creation: dict[str, object] = {
            "blobId": blob_id,
            "mailboxIds": {mid: True for mid in mailbox_ids},
            "keywords": {kw: True for kw in keywords},
        }
        if received_at is not None:
            creation["receivedAt"] = _to_utc_date(received_at)
        out = await self._call(
            [
                (
                    "Email/import",
                    {"accountId": self.account_id, "emails": {"i0": creation}},
                    "c0",
                )
            ]
        )
        created = out["c0"].get("created") or {}
        if "i0" not in created:
            not_created = (out["c0"].get("notCreated") or {}).get("i0", {})
            raise JmapError(f"Email/import failed: {not_created!r}")
        return created["i0"]["id"]

    async def create_mailbox(self, name: str, *, role: str | None = None) -> str:
        """Create a new mailbox and return its id.

        A ``Mailbox/set`` create with ``name``, plus ``role`` when one is
        asked for — ``parentId``/``sortOrder`` are left server-default,
        since nothing in this client needs either yet. A Gmail-style label
        passes no role at all and the property is omitted entirely rather
        than sent as ``null``, so the wire body for that (much more common)
        case is unchanged.

        ``role`` exists for one caller: an account with no ``archive``
        mailbox, where archiving an Inbox-only message has nowhere to put
        it (``mailosh.services.mailbox_tree.ensure_role_mailbox``). RFC
        8621 §2 makes a role unique within an account — "there MUST NOT be
        two Mailboxes in the same account with the same role" — so a
        *second* create with a role already in use is a rejection, which is
        exactly the race signal that caller relies on. It surfaces here as
        the same ``JmapError`` any other rejected create raises.

        Checks the response's ``created``/``notCreated`` maps the same way
        ``import_email`` checks its own creation above (an id missing from
        *both* is treated as a failure, not a success) rather than assuming
        the client-chosen creation id ``"m0"`` always succeeds.
        """
        creation: dict[str, object] = {"name": name}
        if role is not None:
            creation["role"] = role
        out = await self._call(
            [
                (
                    "Mailbox/set",
                    {"accountId": self.account_id, "create": {"m0": creation}},
                    "c0",
                )
            ]
        )
        created = out["c0"].get("created") or {}
        if "m0" not in created:
            not_created = (out["c0"].get("notCreated") or {}).get("m0", {})
            raise JmapError(f"Mailbox/set create failed: {not_created!r}")
        return created["m0"]["id"]

    async def update_mailbox(self, mailbox_id: str, patch: dict[str, object]) -> None:
        """Rename or re-parent a mailbox (`Mailbox/set` update).

        `patch` is applied verbatim, so a caller renames with
        `{"name": "Work"}` and nests with `{"parentId": "<id>"}` -- the two
        things a label needs, and both are ordinary `Mailbox` properties.
        Passing `{"parentId": None}` moves a label back to the top level,
        which is why `None` is a legitimate value here and the patch is not
        filtered for it.

        `notUpdated` is checked rather than assumed away. Two rejections a
        caller should expect: a name that collides with a sibling, and a
        `parentId` that would make a mailbox its own ancestor -- RFC 8621 §2
        requires the server to refuse the cycle, so this does not re-derive
        that check locally where it could disagree with the server.
        """
        out = await self._call(
            [
                (
                    "Mailbox/set",
                    {"accountId": self.account_id, "update": {mailbox_id: patch}},
                    "u0",
                )
            ]
        )
        not_updated = out["u0"].get("notUpdated") or {}
        if mailbox_id in not_updated:
            raise JmapError(f"Mailbox/set update failed: {not_updated[mailbox_id]!r}")

    async def destroy_mailbox(
        self, mailbox_id: str, *, on_destroy_remove_emails: bool = False
    ) -> None:
        """Delete a mailbox (`Mailbox/set` destroy).

        `on_destroy_remove_emails` defaults to **False**, and that default is
        the point. RFC 8621 §2.5 makes the flag the difference between
        "remove this label" and "delete every message that carried it": with
        it false the server refuses while messages remain *only in* that
        mailbox, and messages that live elsewhere too simply lose the label.
        Design spec §10 asks for exactly that -- "conversations keep their
        other labels" -- so deleting a label must never be a way to lose
        mail, and a caller has to ask for the destructive form explicitly
        rather than get it by forgetting an argument.
        """
        out = await self._call(
            [
                (
                    "Mailbox/set",
                    {
                        "accountId": self.account_id,
                        "destroy": [mailbox_id],
                        "onDestroyRemoveEmails": on_destroy_remove_emails,
                    },
                    "d0",
                )
            ]
        )
        not_destroyed = out["d0"].get("notDestroyed") or {}
        if mailbox_id in not_destroyed:
            raise JmapError(f"Mailbox/set destroy failed: {not_destroyed[mailbox_id]!r}")

    async def create_draft(
        self,
        *,
        sender: Address,
        to: Sequence[Address] = (),
        cc: Sequence[Address] = (),
        bcc: Sequence[Address] = (),
        subject: str = "",
        text: str = "",
        html: str | None = None,
        attachments: Sequence[BodyPart] = (),
        in_reply_to: Sequence[str] = (),
        references: Sequence[str] = (),
    ) -> str:
        """Create one draft Email in the Drafts mailbox and return its id.

        The autosave half of compose (design spec §8: "JMAP bodies are
        immutable, so each save is ``Email/set`` create (``$draft``) +
        destroy previous"). Two HTTP round trips: ``get_mailboxes()`` for
        the Drafts id, then the ``Email/set`` create itself — deliberately
        **only** the create, with no destroy of any previous draft folded
        into the same request. That ordering is
        ``mailosh.services.compose.save_draft``'s to enforce and its
        docstring explains why it is the difference between an interrupted
        autosave costing the user nothing and costing them their draft; a
        combined create-and-destroy here would take the choice away from
        it.

        Recipients are allowed to be empty, unlike on the send path: a
        half-typed draft with a body and no ``To`` yet is the normal state
        of a compose dock two seconds after the user starts typing, and
        refusing to save it would be refusing to save exactly the drafts
        autosave exists for.

        Raises ``JmapError`` if the account has no Drafts mailbox, or if
        the server reports the create in ``notCreated`` — same
        check-then-raise shape as ``import_email``/``create_mailbox``.
        """
        mailboxes = await self.get_mailboxes()
        drafts_id = _mailbox_id(mailboxes, "drafts")
        draft = _draft_creation(
            drafts_id=drafts_id,
            sender=sender,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            text=text,
            html=html,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
        )
        out = await self._call(
            [("Email/set", {"accountId": self.account_id, "create": {"d0": draft}}, "d0")]
        )
        created = (out["d0"].get("created") or {}).get("d0")
        if created is None:
            not_created = (out["d0"].get("notCreated") or {}).get("d0", {})
            raise JmapError(f"Email/set create (draft) failed: {not_created!r}")
        return created["id"]

    async def destroy_emails(self, email_ids: Sequence[str]) -> None:
        """Permanently delete the given Emails (``Email/set`` ``destroy``).

        Destroy, not a move to Trash: the one caller is compose's draft
        lifecycle (``mailosh.services.compose``'s ``discard_draft``, and
        the "destroy the superseded draft" half of ``save_draft``), where
        the object being removed is a superseded revision of something the
        user is still editing, not mail they might want back.

        An empty ``email_ids`` makes no request at all — a JMAP round trip
        that asks the server to destroy nothing is pure latency, and
        ``save_draft`` reaches this with nothing to destroy on every
        first save.

        Raises ``JmapError`` if the server reports any id in
        ``notDestroyed``, for the same reason ``_check_updated`` exists: a
        destroy that quietly did not happen is how a Drafts folder fills up
        with revisions nobody can see the origin of.
        """
        if not email_ids:
            return
        out = await self._call(
            [
                (
                    "Email/set",
                    {"accountId": self.account_id, "destroy": list(email_ids)},
                    "d0",
                )
            ]
        )
        not_destroyed = out["d0"].get("notDestroyed") or {}
        if not_destroyed:
            raise JmapError(f"Email/set destroy failed: {not_destroyed!r}")

    async def send(self, *, to: list[str], subject: str, text: str, html: str | None = None) -> str:
        """Compose a plain message and submit it, returning the submission id.

        The narrow Phase 0 entry point, kept as-is for callers that only
        ever needed bare ``To`` addresses and a body: everything it does is
        ``send_message`` below, which grew out of it to carry cc/bcc,
        attachments and reply headers. Both produce byte-identical requests
        for the arguments this signature can express; this one just drops
        the created Email's id from the result.
        """
        result = await self.send_message(
            to=[Address(email=address) for address in to],
            subject=subject,
            text=text,
            html=html,
        )
        return result[0]

    async def send_message(
        self,
        *,
        to: Sequence[Address],
        cc: Sequence[Address] = (),
        bcc: Sequence[Address] = (),
        subject: str = "",
        text: str = "",
        html: str | None = None,
        attachments: Sequence[BodyPart] = (),
        in_reply_to: Sequence[str] = (),
        references: Sequence[str] = (),
        identity: Identity | None = None,
    ) -> tuple[str, str]:
        """Compose a draft and submit it for delivery (RFC 8621 §7.5
        ``EmailSubmission/set``), returning
        ``(submission id, created email id)``.

        The email id is returned as well as the submission's because the
        two answer different questions and a caller usually needs both: the
        submission id is what an undo-send window cancels, the email id is
        what a "Sent — View" toast links to and what a caller must *not*
        confuse with the superseded autosave draft it is replacing.

        ``identity`` is the "send as" this message goes out under —
        ``None`` means the account default, fetched (and cached) via
        ``get_identity()``. A caller that has already resolved a specific
        identity (compose's From picker, via
        ``mailosh.services.compose``) passes it in rather than making this
        method re-resolve it.

        Three HTTP round trips the first time this (or ``get_identity``) is
        called on a given client; two on every call after that:

        1. ``get_mailboxes()`` — resolves the Drafts/Sent mailbox ids. A
           separate call (like ``archive_email``'s own ``get_mailboxes()``
           lookup in ``mailosh.web.app``); nothing here needs it batched
           with what follows.
        2. ``get_identity()`` — resolves the account's send-from address,
           *skipped entirely* once cached (see that method's docstring).
        3. One batched ``_call`` with exactly two method calls: ``Email/set``
           (create the draft) and ``EmailSubmission/set`` (submit it),
           linked by an RFC 8620 §5.3 creation-id reference (``"#d0"``).

        Design note — ``Identity/get`` is deliberately NOT folded into that
        step-3 batch, even when uncached (i.e. even though that means a 3rd
        HTTP round trip the first time a given client sends). This is a
        considered correction to the task brief's literal "one batched call
        containing Identity/get + Email/set + EmailSubmission/set" wording:
        RFC 8620 §3.7 result references only substitute a *whole,
        top-level* argument of a method call (the ``#ids`` pattern
        ``query_inbox``/``get_thread`` above already use) — they cannot
        inject a fetched value into a *nested* property such as
        ``create.d0.from`` or ``create.s0.identityId``, and §5.3
        creation-id references only resolve an id for an object *being
        created in the same request*, which ``Identity/get`` — a read, not
        a create — never has. Concretely: this client cannot know the
        identity's ``email``/``id`` — both needed to build the draft's
        ``from`` and the submission's ``identityId`` — until
        ``Identity/get``'s response has actually come back, so those two
        creates cannot be constructed in the same request as an uncached
        ``Identity/get`` call. Verified this sends correctly against the
        live server (Task 9's report); the literal 3-methods-one-batch
        version would have to send a placeholder ``from``/``identityId``,
        which Stalwart would reject.

        ``bodyStructure``/``bodyValues`` (RFC 8621 §4.1.4/§4.6) are built
        by ``_body_structure`` — ``multipart/alternative`` over the two
        freshly authored ``partId`` parts (``t`` = text/plain, ``h`` =
        text/html), wrapped in ``multipart/related``/``multipart/mixed``
        when there are inline/file attachments to carry; see that
        function's docstring for why each wrapper is or is not there. The
        draft is created already
        ``$draft``+``$seen`` in Drafts; ``onSuccessUpdateEmail``, keyed by
        the *submission's* own creation reference (``"#s0"``, not the
        email's ``"#d0"`` — RFC 8621 §7.5's ``onSuccessUpdateEmail`` is
        keyed by submission id/creation-id, and patches whichever Email
        that submission's ``emailId`` points at), moves the draft
        Drafts -> Sent and clears ``$draft`` the instant the send succeeds
        — one round trip, not a separate follow-up ``move``/``set_keyword``
        call.

        Raises ``JmapError``/``MethodError`` if either the draft or the
        submission itself fails outright (``notCreated``, or the whole call
        came back as an ``error`` tuple) — the same check-then-raise shape
        ``import_email``/``create_mailbox`` above already use.

        **P0 contract for the implicit ``onSuccessUpdateEmail`` update**:
        step 3 uses ``_call_raw``, not ``_call``, specifically so this method
        can see that implicit ``Email/set`` update's *own* outcome — it
        shares the ``EmailSubmission/set`` call's ``"s0"`` id (see
        ``_call_raw``'s docstring), so ``_call``'s keep-first deduplication
        would silently discard it, and ``_call``'s raise-on-error behavior
        would turn its failure into a hard failure for the whole send. If
        that implicit patch is rejected (ACL, quota, a concurrent
        modification) or itself comes back as an ``error`` tuple, the
        message has still genuinely been *submitted* — Stalwart already
        queued it for delivery — so this method does NOT raise for that;
        raising would misreport a successful send as a failure. Instead it
        logs a ``logger.warning`` (draft id + the ``SetError``/error type)
        and still returns the submission id. The only externally-visible
        symptom of this path is a local one: the draft may be left sitting
        in Drafts with ``$draft`` still set instead of having moved to
        Sent — a caller that cares needs to watch this client's logger.
        """
        mailboxes = await self.get_mailboxes()
        drafts_id = _mailbox_id(mailboxes, "drafts")
        sent_id = _mailbox_id(mailboxes, "sent")
        if identity is None:
            identity = await self.get_identity()

        draft = _draft_creation(
            drafts_id=drafts_id,
            sender=Address(email=identity.email, name=identity.name),
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            text=text,
            html=html,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
        )
        submission = {"emailId": "#d0", "identityId": identity.id}
        patch = {
            f"mailboxIds/{drafts_id}": None,
            f"mailboxIds/{sent_id}": True,
            "keywords/$draft": None,
        }

        responses = await self._call_raw(
            [
                ("Email/set", {"accountId": self.account_id, "create": {"d0": draft}}, "d0"),
                (
                    "EmailSubmission/set",
                    {
                        "accountId": self.account_id,
                        "create": {"s0": submission},
                        "onSuccessUpdateEmail": {"#s0": patch},
                    },
                    "s0",
                ),
            ]
        )

        # Every response tagged "d0"/"s0", in the order the server sent
        # them — RFC 8620 §5.3 guarantees implicit-call responses are
        # appended *after* every explicit call's own response, so the
        # first "s0"-tagged entry is always the explicit
        # EmailSubmission/set response and any later one is the implicit
        # onSuccessUpdateEmail update.
        d0_entries = [(name, args) for name, args, call_id in responses if call_id == "d0"]
        s0_entries = [(name, args) for name, args, call_id in responses if call_id == "s0"]

        if not d0_entries:
            raise JmapError("no response for call id 'd0' (Email/set create)")
        email_name, email_args = d0_entries[0]
        if email_name == "error":
            raise MethodError(email_args.get("type", "error"), "d0")
        email_created = (email_args.get("created") or {}).get("d0")
        if email_created is None:
            not_created = (email_args.get("notCreated") or {}).get("d0", {})
            raise JmapError(f"Email/set create (draft) failed: {not_created!r}")
        draft_id = email_created["id"]

        if not s0_entries:
            raise JmapError("no response for call id 's0' (EmailSubmission/set create)")
        submission_name, submission_args = s0_entries[0]
        if submission_name == "error":
            raise MethodError(submission_args.get("type", "error"), "s0")
        submission_created = (submission_args.get("created") or {}).get("s0")
        if submission_created is None:
            not_created = (submission_args.get("notCreated") or {}).get("s0", {})
            raise JmapError(f"EmailSubmission/set create failed: {not_created!r}")

        # Any further "s0"-tagged entries are the implicit
        # onSuccessUpdateEmail-triggered Email/set update — see this
        # method's own docstring for why a failure here is logged, not
        # raised: the mail was already genuinely submitted by this point.
        for implicit_name, implicit_args in s0_entries[1:]:
            if implicit_name == "error":
                logger.warning(
                    "send(): implicit Drafts->Sent update for email %s failed: %s",
                    draft_id,
                    implicit_args.get("type", "error"),
                )
                continue
            not_updated = (implicit_args.get("notUpdated") or {}).get(draft_id)
            if not_updated is not None:
                err_type = not_updated.get("type", "error")
                description = not_updated.get("description")
                detail = f"{err_type}: {description}" if description else err_type
                logger.warning(
                    "send(): implicit Drafts->Sent update for email %s failed: %s",
                    draft_id,
                    detail,
                )

        return submission_created["id"], draft_id

    async def event_stream(self) -> AsyncIterator[StateChange]:
        """Stream JMAP push ``StateChange`` objects from this session's
        EventSource endpoint (RFC 8620 §7.3), one long-lived streamed GET.

        The session's (already-rebased) ``eventSourceUrl`` is a URI
        template; this substitutes its three placeholders directly (no
        RFC 6570 library needed for three literal swaps):
        ``{types}`` -> ``Email,Mailbox`` (the only two object types
        ``stalwart_listener`` acts on — narrower than ``*`` means the
        server has less to push in the first place), ``{closeafter}`` ->
        ``no`` (keep the connection open indefinitely; the RFC's other
        option, ``after``, closes it right after the first state event),
        ``{ping}`` -> ``_PING_SECONDS`` (ask the server for a keepalive
        ``event: ping`` frame at least that often, so a silently-dropped
        connection is noticed instead of hanging forever).

        Parses the response body with ``parse_sse_stream`` and yields a
        ``StateChange`` for every frame whose ``event`` is ``state``
        (everything else — a ``ping`` keepalive, say — is silently
        skipped here). A single connection attempt for the lifetime of
        this async generator: deliberately no reconnect/retry logic in
        this method. Any transport failure (the initial connect, or one
        that happens mid-stream after already yielding some StateChanges)
        raises ``TransportError``, same translation ``_call``/``upload``
        already use — reconnection policy belongs to the caller
        (``mailosh.sse.stalwart_listener``), not here.

        Overrides this call's *read* timeout rather than inheriting
        ``self._http``'s blanket ``timeout=30`` (set in ``connect()`` for
        ordinary request/response calls): an idle SSE connection with
        nothing new to report is expected to sit silent for up to
        ``_PING_SECONDS`` between frames, and 30s-vs-30s was found live
        (against the real Stalwart eventsource endpoint) to be a genuine
        race — a ping arriving a hair later than exactly 30s after the
        previous frame reliably trips httpx's read timeout first, raising
        ``httpx.ReadTimeout`` on a connection that was never actually
        stuck. Doubling it (60s against a 30s requested ping) leaves
        comfortable slack for that jitter while still giving up — and
        letting ``stalwart_listener`` reconnect — if the server genuinely
        stops responding.
        """
        url = (
            self._session.event_source_url.replace("{types}", "Email,Mailbox")
            .replace("{closeafter}", "no")
            .replace("{ping}", str(_PING_SECONDS))
        )
        stream_timeout = httpx.Timeout(30.0, read=_PING_SECONDS * 2)
        try:
            async with self._http.stream("GET", url, timeout=stream_timeout) as resp:
                resp.raise_for_status()
                async for frame in parse_sse_stream(resp.aiter_lines()):
                    if frame.event != "state":
                        continue
                    yield StateChange.model_validate_json(frame.data)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise _transport_error(exc) from exc
