"""Compose (design spec §8): the corner dock, the inline reply card, and the
seven routes behind them.

    GET  /compose                    a fresh dock
    GET  /compose/{draft_id}         an existing draft, reopened into a dock
    GET  /compose/reply/{email_id}   reply / reply-all / forward
    POST /compose/draft              autosave -> the saved-state fragment
    POST /compose/send
    POST /compose/discard
    POST /attachments                one file, streamed on to Stalwart

Every one of them is thin. `mailosh.services.compose` owns what a draft
*is* (`DraftInput`, `Recipient`, `AttachmentRef`), what saving/sending one
means (`save_draft`, `send_draft`, `discard_draft`), which addresses the
account may send as (`list_identities`), and how a reply is derived from a
thread (`build_reply`). Nothing here builds a JMAP request, quotes a body,
or decides what "reply all" means; this module turns a form body into a
`DraftInput`, hands it over, and renders the answer.

**The dock is a sibling of the list, never a child of it.** It renders
into `#compose-dock` in `layouts/app.html` — outside `#main`, outside
`#list`, outside every swap target any other route in this app writes to,
and outside the grid that lays the app out (each dock is
`position: fixed`, so the mount point itself holds no height at all). That
is the single property the whole design rests on: the inbox behind an open
dock keeps scrolling, keeps taking clicks, keeps updating over SSE, and
keeps answering `j`/`k`/`e`. Three consequences, all deliberate:

- No route here ever targets `#main`, and no response carries
  `HX-Push-Url`. A draft in progress is not a place, so it is not in the
  address bar and not in history (`hx-history="false"` on the mount).
- Nothing here is a `<dialog>` and nothing renders a backdrop. A modal
  would hand the keyboard and every click to the platform, which is
  exactly the failure this design forbids — `static/js/keys.js` stops
  dispatching entirely while `dialog[open]` matches anything.
- The inline reply card (`GET /compose/reply/...`) is the one exception to
  "outside `#main`", and it is one on purpose: a reply belongs at the end
  of the conversation it answers. It is still not modal, still swaps only
  into its own slot, and "pop out" moves it to a real dock.

CSRF: this router carries its own router-level
`dependencies=[Depends(deps.csrf_protect)]`, exactly like
`mailosh.web.prefs` and `mailosh.web.actions`, so `create_app` passes
nothing when it includes it and the check runs once rather than twice.
`mailosh.security.csrf.validate` exempts GET/HEAD/OPTIONS outright, so the
four read routes above pass straight through it — what they do get from
the dependency is `require_session`, which they need anyway.

**Draft saves are creates, not updates** (spec §8: JMAP bodies are
immutable), which is why `POST /compose/draft` answers with a *new*
`draft_id` every time and the form has to carry it back on the next save.
It comes back two ways in one response: as `data-draft-id` on the state
fragment, and as an out-of-band swap of the form's own hidden
`draft_id` input, so the round trip needs no JavaScript at all.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import formataddr
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from mailosh.db.models import AppUser, SessionRow, UiPref
from mailosh.jmap.client import JmapClient
from mailosh.jmap.models import Identity
from mailosh.services.compose import (
    AttachmentRef,
    ComposeError,
    DraftInput,
    InvalidAddress,
    NoRecipients,
    Recipient,
    UnknownIdentity,
    build_reply,
    discard_draft,
    list_identities,
    save_draft,
    send_draft,
)
from mailosh.services.conversation import size_display
from mailosh.web import deps

logger = logging.getLogger(__name__)

router = APIRouter(tags=["compose"], dependencies=[Depends(deps.csrf_protect)])

SessionDep = Annotated[SessionRow, Depends(deps.require_session)]
UserDep = Annotated[AppUser, Depends(deps.current_user)]
PrefsDep = Annotated[UiPref, Depends(deps.prefs_for)]
ClientDep = Annotated[JmapClient, Depends(deps.client_for)]

#: What `GET /compose/reply/{id}` will build. Spelled as a `Literal` so an
#: unknown mode is FastAPI's own 422 before this module's code runs, rather
#: than a hand-written check here that could drift from what
#: `mailosh.services.compose.build_reply` actually accepts.
ReplyMode = Literal["reply", "reply_all", "forward"]

#: Hard ceiling on one uploaded file, enforced while the body is still
#: being read (`_read_upload`) so an oversized upload is refused after this
#: many bytes rather than after all of them. Spec §8's own number is the
#: *soft* 25 MB warning, which is the client's (`static/js/compose.js`):
#: a warning the reader can overrule needs a limit above it, or the warning
#: would be a refusal wearing the wrong words.
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024

#: How much of an upload is pulled off the wire at a time. `UploadFile`
#: spools to a temporary file past ~1 MB, so this is what keeps a 40 MB
#: attachment from being read into one 40 MB string before the size check
#: has anything to say about it.
_UPLOAD_CHUNK_BYTES = 64 * 1024

#: Fallback content type for a file the browser could not name one for.
_DEFAULT_UPLOAD_TYPE = "application/octet-stream"

#: Everything a body can consist entirely of and still be empty to the
#: person who wrote it: ordinary whitespace, plus the zero-width
#: characters a rich-text editor leaves behind. Squire drops a U+200B into
#: an empty document the moment a block command runs on it (make a list,
#: quote, then undo the formatting), which is enough for `str.strip()` to
#: report "typed" — and that produced a draft in the Drafts folder whose
#: entire body was an invisible character.
_BLANK = "\t\n\r\v\f \u00a0\u200b\u200c\u200d\ufeff"


def _is_blank(value: str) -> bool:
    """Whether `value` has nothing in it a reader would call content.

    Named `_is_blank`, not `_blank`: this module already has a `_blank()`
    that builds the empty `DraftInput` a fresh dock renders, and the two
    silently shadowed each other — every body then read as "typed", which
    is exactly the bug this helper was added to fix.
    """
    return value.strip(_BLANK) == ""


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _dom_id() -> str:
    """A fresh id prefix for one dock/card's own elements.

    Three docks can be open at once and the reply card can be open behind
    them, so every `id`/`for`/`aria-controls` pair inside the component is
    namespaced with this. `secrets.token_hex` rather than a counter: the
    id has to be unique across *documents* too, since a dock opened after
    a history restore shares the page with whatever the restore brought
    back.
    """
    return "c" + secrets.token_hex(6)


def _domain(address: str) -> str:
    """The domain half of an address, lower-cased — `""` when there isn't
    one. Used only to decide whether a chip gets the external-domain hint
    (spec §8), never to validate or route anything.
    """
    _, _, domain = address.rpartition("@")
    return domain.strip().lower()


def _person(recipient: Recipient, *, me_domain: str) -> dict[str, object]:
    """One recipient as `compose/form.html`'s chip macro reads it.

    `value` is what the hidden input posts — `"Ada Lovelace"
    <ada@example.test>` when there is a display name, the bare address
    otherwise (`email.utils.formataddr` quotes the name only when it has
    to). That is the round trip: a draft reopened from Drafts, or a reply
    the service built, renders its recipients back into the exact text
    `_recipients` parses, so a display name survives a save/reopen instead
    of being flattened to an address on the first autosave.

    `external` is spec §8's external-domain hint — true when this address
    is outside the sender's own domain. A *hint*, and nothing more: it
    colours the chip's edge, it does not warn, block or confirm. Most mail
    goes outside your own domain; a control that shouted about it would be
    noise within a day and ignored by the time it mattered.
    """
    return {
        "value": formataddr((recipient.name or "", recipient.email)),
        "name": recipient.name,
        "email": recipient.email,
        "external": bool(me_domain) and _domain(recipient.email) != me_domain,
    }


#: One `Name <addr>` / `"Name" <addr>` recipient. Anything without angle
#: brackets is taken as a bare address.
_ANGLED_RE = re.compile(r"^(?P<name>.*?)<(?P<email>[^<>]*)>$", re.S)


def _split_addresses(raw: str) -> list[str]:
    """Split one field value on `,`, `;` and newlines — but not on a comma
    inside a quoted display name (`"Okafor, Daniel" <d@x.test>`) or inside
    angle brackets.

    A hand-written scan rather than `email.utils.getaddresses`, and the
    reason is specific: from Python 3.13 that function answers `[("", "")]`
    for anything it considers malformed (the CVE-2023-27043 hardening), so
    a single unparsable fragment silently takes the *whole field* with it
    — a paste of five good addresses and one typo becomes zero recipients.
    A compose field is exactly where partially-typed input is normal, so
    splitting is done here and judging what is a real address is left to
    `mailosh.services.compose`, which raises `InvalidAddress` naming the
    offender instead of dropping it.

    `static/js/compose.js`'s `splitAddresses` is the same algorithm, so a
    list pasted into a chip field and one posted whole (a browser with no
    JavaScript) come apart the same way.
    """
    pieces: list[str] = []
    current: list[str] = []
    quoted = False
    angled = False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
        elif ch == "<":
            angled = True
        elif ch == ">":
            angled = False
        if not quoted and not angled and ch in ",;\n":
            pieces.append("".join(current))
            current = []
            continue
        current.append(ch)
    pieces.append("".join(current))
    return [piece.strip() for piece in pieces if piece.strip()]


def _one_recipient(piece: str) -> Recipient:
    """`Ada Lovelace <ada@x.test>` -> `Recipient(email=..., name=...)`; a
    bare address -> a `Recipient` with no name.

    Whatever is between the angle brackets is taken as the address
    verbatim, typo and all. **Nothing is validated here and nothing is
    dropped** — `mailosh.services.compose` raises `InvalidAddress`
    carrying the address and the field, and `compose/state.html` turns
    that into a sentence and a marked chip. A recipient quietly discarded
    on the way in is a message that looks sent and never arrives, which is
    the one failure this whole path is arranged to prevent.
    """
    match = _ANGLED_RE.match(piece)
    if match is None:
        return Recipient(email=piece, name=None)
    name = match.group("name").strip()
    if len(name) > 1 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1].strip()
    return Recipient(email=match.group("email").strip(), name=name or None)


def _recipients(values: Iterable[str]) -> tuple[Recipient, ...]:
    """Parse a recipient field's repeated values into `Recipient`s.

    One field value may itself be a list — a paste of
    `a@x, "B" <b@x>; c@x` that the browser submitted before the client had
    split it into chips — so each value is split first (`_split_addresses`)
    and each piece parsed (`_one_recipient`).

    The one rule applied beyond parsing: duplicates collapse
    case-insensitively on the address, keeping the first spelling. The chip
    UI already prevents them; a pasted address list routinely does not, and
    a message that arrives twice is a message the sender did not write.
    """
    out: list[Recipient] = []
    seen: set[str] = set()
    for raw in values:
        for piece in _split_addresses(raw):
            person = _one_recipient(piece)
            key = person.email.strip().lower()
            if key == "" or key in seen:
                continue
            seen.add(key)
            out.append(person)
    return tuple(out)


def _attachments(values: Iterable[str]) -> tuple[AttachmentRef, ...]:
    """Parse the repeated `attachment` field into `AttachmentRef`s.

    Each value is the JSON object `POST /attachments` handed back for one
    upload — blob id, name, type, size — carried in a hidden input so the
    set of attachments survives an autosave, a reopen and a full page
    reload without this route having to ask Stalwart what a blob is called.

    A malformed value is a `400`, never a silently dropped attachment.
    Nothing a reader can do produces one (the only writer of these fields
    is the fragment this app itself rendered), so it means tampering — and
    the failure mode that must not exist is a message that sends *looking*
    complete with a file quietly missing from it.
    """
    out: list[AttachmentRef] = []
    for raw in values:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="malformed attachment") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="malformed attachment")
        blob_id = data.get("blob_id")
        name = data.get("name")
        if not isinstance(blob_id, str) or not isinstance(name, str) or not blob_id:
            raise HTTPException(status_code=400, detail="malformed attachment")
        mime = data.get("type")
        size = data.get("size")
        cid = data.get("cid")
        out.append(
            AttachmentRef(
                blob_id=blob_id,
                name=name,
                type=mime if isinstance(mime, str) and mime else _DEFAULT_UPLOAD_TYPE,
                size=size if isinstance(size, int) and size >= 0 else 0,
                cid=cid if isinstance(cid, str) and cid else None,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class ComposeForm:
    """The compose form's whole body, parsed once for the two routes that
    take it (`/compose/draft` and `/compose/send`).

    A dataclass over a `Depends`-resolved function rather than two
    identical signatures: the field list *is* the contract between
    `compose/form.html` and this module, and two copies of it is how a
    field starts being posted by the markup and ignored by the server.
    """

    to: tuple[Recipient, ...]
    cc: tuple[Recipient, ...]
    bcc: tuple[Recipient, ...]
    subject: str
    html: str
    text: str
    attachments: tuple[AttachmentRef, ...]
    identity_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    draft_id: str | None
    #: The dock/card this body came from, so the response can be swapped
    #: back into the right one when three are open.
    dom_id: str

    @property
    def empty(self) -> bool:
        """Nothing typed and nothing attached — a compose window that was
        opened and left alone. `POST /compose/draft` answers such a body
        without writing anything: a blank draft in the Drafts folder is
        litter the reader never asked for, and (unlike every other save)
        there is nothing in it to lose.

        Deliberately does *not* consider `in_reply_to`/`references`: an
        untouched reply card carries both and is still empty.

        The body is judged by what it *renders as*, not by its markup, and
        with `_is_blank` rather than `str.strip()`. Both halves are needed
        for one real case: run a formatting command on an empty editor
        (make a list, quote it, clear it) and Squire leaves a zero-width
        space wrapped in a `<div>`. `"<div>\u200b</div>".strip()` is not
        empty, and neither is `"\u200b".strip()` — so a dock that was
        opened, fiddled with and closed put a draft in the Drafts folder
        whose entire body was an invisible character. `body_text` is the
        same string `draft()` sends, so "would this save anything?" and
        "what would it save?" cannot disagree.

        An HTML body with no text at all but a visible `<img>` counts as
        content: it is the one shape that renders as nothing here and as
        something to a reader.
        """
        return not (
            self.to
            or self.cc
            or self.bcc
            or self.attachments
            or not _is_blank(self.subject)
            or not _is_blank(self.body_text)
            or "<img" in self.html.lower()
        )

    @property
    def body_text(self) -> str:
        """The message's `text/plain` part: what the browser sent, or the
        rich body rendered down to text when it sent only that
        (`mailosh.web.app.html_to_text`, which exists for exactly this
        call and says so in its own docstring).

        A message that is HTML-only is one a plain-text reader receives as
        nothing at all, so an outgoing message always carries this part.
        `empty` reads the same property, so the question "is there
        anything here?" is asked of the same string that would be sent.

        **The import is deferred, and it has to be.** `mailosh.web.app`
        imports this module (to register the router) at its own module
        level, so importing it back at *this* module's level is a genuine
        cycle: the real entry point is `mailosh.web.app:create_app`, so
        `app` always executes first, and it reaches its router imports
        long before it defines `html_to_text`. One function-level import
        is the smaller price than moving a pure, separately-tested
        function out of the module whose tests own it.
        """
        from mailosh.web.app import html_to_text

        return self.text if not _is_blank(self.text) else html_to_text(self.html)

    def draft(self) -> DraftInput:
        """This body as the service's own `DraftInput`."""
        text = self.body_text
        return DraftInput(
            to=self.to,
            cc=self.cc,
            bcc=self.bcc,
            subject=self.subject,
            html=self.html,
            text=text,
            attachments=self.attachments,
            identity_id=self.identity_id,
            in_reply_to=self.in_reply_to,
            references=self.references,
            draft_id=self.draft_id,
        )


def _clean(value: str | None) -> str | None:
    """A form field's value, or `None` when the browser sent it empty.

    Every `str | None` field on `DraftInput` means "absent" by `None`, and
    an `<input>` that was never filled in posts `""`, not nothing at all —
    so without this an untouched identity picker would hand the service
    the empty string as an identity id.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


async def compose_form(
    dom_id: Annotated[str, Form()] = "",
    to: Annotated[list[str] | None, Form()] = None,
    cc: Annotated[list[str] | None, Form()] = None,
    bcc: Annotated[list[str] | None, Form()] = None,
    subject: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    attachment: Annotated[list[str] | None, Form()] = None,
    identity_id: Annotated[str | None, Form()] = None,
    in_reply_to: Annotated[str | None, Form()] = None,
    references: Annotated[list[str] | None, Form()] = None,
    draft_id: Annotated[str | None, Form()] = None,
) -> ComposeForm:
    """`ComposeForm` from the request body — the dependency both mutating
    routes take instead of repeating this signature.

    Every field is optional. A dock that has only had one address typed
    into it autosaves with `subject`, `html` and `text` genuinely absent,
    and a required field here would turn that first save into a 422.
    """
    return ComposeForm(
        to=_recipients(to or []),
        cc=_recipients(cc or []),
        bcc=_recipients(bcc or []),
        subject=subject.strip(),
        html=html,
        text=text,
        attachments=_attachments(attachment or []),
        identity_id=_clean(identity_id),
        in_reply_to=_clean(in_reply_to),
        references=tuple(value for value in (references or []) if value.strip()),
        draft_id=_clean(draft_id),
        dom_id=dom_id.strip() or _dom_id(),
    )


FormDep = Annotated[ComposeForm, Depends(compose_form)]


async def _identities(client: JmapClient) -> list[Identity]:
    """The account's send-as addresses (spec §8: the From picker appears
    only when there is more than one).

    A failure to list them is not a failure to compose: an account whose
    `Identity/get` is unavailable still has exactly one thing the reader
    can do about it, which is write the message. The picker is simply
    absent, and `save_draft`/`send_draft` fall back to the server's own
    default identity the same way they do for a single-identity account.
    """
    try:
        return list(await list_identities(client))
    except Exception:
        # Deliberately every exception, not just `JmapError`: whatever went
        # wrong upstream, the answer for this one optional control is the
        # same, and none of it is worth a 500 on a page whose whole purpose
        # is to let someone write a message.
        logger.warning("could not list identities; From picker omitted", exc_info=True)
        return []


@dataclass(frozen=True)
class ComposeProblem:
    """A `ComposeError` as the dock renders it: one sentence, and — when
    the service could say so — which field and which address is wrong.

    `mailosh.services.compose` raises rather than dropping precisely so
    the reader can be pointed at the chip that is wrong ("a silently
    discarded recipient is a message that looks sent and never arrives"),
    and `field`/`address` are what carries that pointing through to
    `static/js/compose.js`. Everything else about the error stays server
    side: the copy here is the reader's, not the exception's.
    """

    message: str
    field: str | None = None
    address: str | None = None


def _problem(exc: ComposeError) -> ComposeProblem:
    """`exc` in the reader's words.

    Deliberately not `str(exc)`. Those messages are written for a log —
    "to: 'ada@' is not a usable email address (not a local@domain
    address)" — and a compose window that printed one would be telling
    somebody who mistyped an address about header fields and grammar.
    """
    if isinstance(exc, InvalidAddress):
        return ComposeProblem(
            message=f"{exc.address or 'That address'} isn't a usable email address",
            field=exc.field,
            address=exc.address,
        )
    if isinstance(exc, NoRecipients):
        return ComposeProblem(message="Add a recipient first", field="to")
    if isinstance(exc, UnknownIdentity):
        return ComposeProblem(message="That From address isn't available", field="from")
    return ComposeProblem(message="This message can't be saved as it is")


def _chip(ref: AttachmentRef) -> dict[str, object]:
    """One attachment as `compose/attachment.html` reads it: the name and
    size a reader sees, and the exact JSON string `_attachments` parses
    back out of the hidden input.

    Built here rather than in the template because the hidden input's
    value and this module's parser are one contract — writing the JSON in
    Jinja would put the two halves of it in different languages, in
    different files, with nothing holding them together.
    """
    record = {"blob_id": ref.blob_id, "name": ref.name, "type": ref.type, "size": ref.size}
    if ref.cid:
        record["cid"] = ref.cid
    return {
        "name": ref.name,
        "size": ref.size,
        "size_display": size_display(ref.size),
        "value": json.dumps(record, separators=(",", ":"), sort_keys=True),
    }


def _context(
    request: Request,
    *,
    session: SessionRow,
    prefs: UiPref,
    draft: DraftInput,
    identities: list[Identity],
    dom_id: str,
    draft_id: str | None,
    title: str,
    surface: str,
    me: str,
) -> dict[str, object]:
    """Everything `compose/form.html` and its two wrappers read.

    `surface` is `"dock"` or `"inline"` and is the *only* difference
    between the two: one component, two frames around it (spec §8's
    "inline composer card at the thread's end ... same component"). It
    reaches the markup as `data-surface`, which is what `static/js/
    compose.js` keys the dock chrome off and what the stylesheet keys the
    two layouts off.

    Recipients are pre-rendered into the same `Name <addr>` text the form
    posts back (`_person`), so a reopened draft and a built reply arrive
    with real chips rather than with JavaScript having to reconstruct them
    from a different spelling.
    """
    me_domain = _domain(me)
    return {
        "prefs": prefs,
        "csrf_token": session.csrf_token,
        "dom_id": dom_id,
        "surface": surface,
        "title": title,
        "draft": draft,
        "draft_id": draft_id,
        "identities": identities,
        "me_domain": me_domain,
        "to": [_person(person, me_domain=me_domain) for person in draft.to],
        "cc": [_person(person, me_domain=me_domain) for person in draft.cc],
        "bcc": [_person(person, me_domain=me_domain) for person in draft.bcc],
        "attachments": [_chip(ref) for ref in draft.attachments],
    }


def _blank(dom_id: str) -> DraftInput:
    """An empty `DraftInput` — what `GET /compose` renders.

    Built here rather than by the service because there is nothing to
    derive: a new message has no thread to reply to, no draft to reopen
    and no body to quote. `save_draft` never sees this object; it is only
    ever the shape the template reads.
    """
    return DraftInput(
        to=(),
        cc=(),
        bcc=(),
        subject="",
        html="",
        text="",
        attachments=(),
        identity_id=None,
        in_reply_to=None,
        references=(),
        draft_id=None,
    )


# ---------------------------------------------------------------------------
# GET: a fresh dock, a reopened draft, a reply
# ---------------------------------------------------------------------------


@router.get("/compose", response_class=HTMLResponse)
async def compose_new(
    request: Request, session: SessionDep, prefs: PrefsDep, client: ClientDep, user: UserDep
) -> Response:
    """A fresh dock, as an htmx fragment appended into `#compose-dock`.

    `hx-swap="beforeend"` at the call site rather than `innerHTML` is what
    lets three of these coexist (spec §8: "up to 3 docks tile leftwards"),
    which is also why every id inside the fragment is namespaced by
    `_dom_id`.
    """
    dom_id = _dom_id()
    return _templates(request).TemplateResponse(
        request,
        "compose/dock.html",
        _context(
            request,
            session=session,
            prefs=prefs,
            draft=_blank(dom_id),
            identities=await _identities(client),
            dom_id=dom_id,
            draft_id=None,
            title="New message",
            surface="dock",
            me=user.email,
        ),
    )


@router.get("/compose/reply/{email_id}", response_class=HTMLResponse)
async def compose_reply(
    request: Request,
    email_id: str,
    session: SessionDep,
    prefs: PrefsDep,
    client: ClientDep,
    user: UserDep,
    thread: Annotated[str, Query()],
    mode: ReplyMode = "reply",
    surface: Literal["inline", "dock"] = "inline",
) -> Response:
    """Reply / reply all / forward, as the inline composer card the
    conversation grows at its end — or straight into a dock when the
    caller asks for one (`?surface=dock`, which is what "pop out" posts).

    `thread` is a query parameter for the same reason `GET
    /m/{id}/frame` takes one: the conversation view already knows the
    thread id (`data-thread-id`), and asking Stalwart which thread an
    email belongs to before fetching that thread is a round trip the
    caller can simply not make. It is passed straight to `get_thread`, so
    a forged value can only ever name a thread in this session's own
    account.

    Everything about what a reply *contains* — who it goes to, the
    `Re:`/`Fwd:` subject, the attribution line, the quoted body, the
    `In-Reply-To`/`References` chain, which attachments a forward carries
    — is `mailosh.services.compose.build_reply`'s. This route fetches the
    thread, names the message being answered, and renders the result.
    """
    messages = await client.get_thread(thread)
    if not messages or all(message.id != email_id for message in messages):
        raise HTTPException(status_code=404, detail="no such message in that conversation")
    try:
        draft = build_reply(messages, email_id, mode, user.email)
    except ComposeError as exc:
        # `mode` is already a `Literal` and the message is already known to
        # be in the thread, so this is unreachable from the UI — which is
        # exactly why it must not be a 500 if the service ever tightens
        # what it accepts.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    dom_id = _dom_id()
    titles = {"reply": "Reply", "reply_all": "Reply all", "forward": "Forward"}
    return _templates(request).TemplateResponse(
        request,
        "compose/dock.html" if surface == "dock" else "compose/inline.html",
        _context(
            request,
            session=session,
            prefs=prefs,
            draft=draft,
            identities=await _identities(client),
            dom_id=dom_id,
            draft_id=draft.draft_id,
            title=titles[mode],
            surface=surface,
            me=user.email,
        ),
    )


@router.get("/compose/{draft_id}", response_class=HTMLResponse)
async def compose_draft(
    request: Request,
    draft_id: str,
    session: SessionDep,
    prefs: PrefsDep,
    client: ClientDep,
    user: UserDep,
) -> Response:
    """One saved draft, reopened into a dock (spec §8: "drafts reopen from
    the Drafts folder into a dock").

    Registered *after* `/compose/reply/{email_id}` so the two never race:
    they cannot actually collide (a reply URL has three path segments and
    this has two), but order is what makes that obvious rather than a
    property of Starlette's router that has to be re-derived.

    Nothing is derived here — a draft is not built from a thread the way
    a reply is, it *is* the previous save — so this is the one place that
    maps an `EmailBody` back into a `DraftInput` field by field. The
    `In-Reply-To`/`References` pair comes back with it, so reopening a
    saved reply and sending it keeps the message in its conversation.
    """
    messages = await client.get_thread(draft_id)
    if not messages:
        raise HTTPException(status_code=404, detail="no such draft")
    # A draft is a single message; `get_thread` is how this client reads a
    # body, and a draft that somehow shares a thread with its own reply
    # chain is still opened at the message the reader asked for.
    body = next((message for message in messages if message.id == draft_id), messages[-1])
    draft = DraftInput(
        to=tuple(Recipient(email=a.email, name=a.name) for a in body.to),
        cc=tuple(Recipient(email=a.email, name=a.name) for a in body.cc),
        bcc=tuple(Recipient(email=a.email, name=a.name) for a in body.bcc),
        subject=body.subject or "",
        html=body.html_body or "",
        text=body.text_body or "",
        attachments=tuple(
            AttachmentRef(
                blob_id=part.blob_id or "",
                name=part.name or "attachment",
                type=part.type,
                size=part.size,
                cid=part.cid,
            )
            for part in body.attachments
            if part.blob_id
        ),
        identity_id=None,
        in_reply_to=body.in_reply_to[0] if body.in_reply_to else None,
        references=tuple(body.references or ()),
        draft_id=body.id,
    )
    dom_id = _dom_id()
    return _templates(request).TemplateResponse(
        request,
        "compose/dock.html",
        _context(
            request,
            session=session,
            prefs=prefs,
            draft=draft,
            identities=await _identities(client),
            dom_id=dom_id,
            draft_id=body.id,
            title=draft.subject or "Draft",
            surface="dock",
            me=user.email,
        ),
    )


# ---------------------------------------------------------------------------
# POST: autosave, send, discard
# ---------------------------------------------------------------------------


@router.post("/compose/draft", response_class=HTMLResponse)
async def compose_autosave(
    request: Request, session: SessionDep, prefs: PrefsDep, client: ClientDep, form: FormDep
) -> Response:
    """Autosave (spec §8: 2 s after the last edit), answering the
    saved-state fragment.

    The fragment carries the new draft id twice — as `data-draft-id` and
    as an out-of-band swap of the form's own hidden `draft_id` input — so
    the next save destroys *this* draft rather than leaving a trail of
    them in the Drafts folder, and it does so with no JavaScript in the
    path at all.

    An untouched form writes nothing (`ComposeForm.empty`). Every other
    body is saved even if it looks unfinished: half an address and no
    subject is exactly the draft a reader most wants to still be there
    tomorrow.
    """
    saved_id = form.draft_id
    problem: ComposeProblem | None = None
    if not form.empty:
        try:
            saved_id = await save_draft(client, form.draft())
        except ComposeError as exc:
            problem = _problem(exc)
    return _templates(request).TemplateResponse(
        request,
        "compose/state.html",
        {
            "prefs": prefs,
            "csrf_token": session.csrf_token,
            "dom_id": form.dom_id,
            # The id from *before* this attempt, so a failed save leaves
            # the form pointing at the draft that is actually stored
            # rather than dropping the thread of it.
            "draft_id": saved_id,
            "state": "error" if problem else ("saved" if saved_id else "idle"),
            "problem": problem,
        },
    )


@router.post("/compose/send")
async def compose_send(session: SessionDep, client: ClientDep, form: FormDep) -> Response:
    """Send, then say so through an `HX-Trigger` and nothing else.

    `204` with no body for the same reason every route in
    `mailosh.web.actions` answers one: by the time this request is made
    the dock has already closed and the "Sending…" toast has already run
    its undo window down, so there is nothing left for a response body to
    swap. What the client still needs is the two ids the submission
    produced, which is what the trigger carries — the `email_id` is what
    the success toast's "View" link opens.

    A failure is *not* swallowed into a 204. `JmapError`/`TransportError`
    propagate to `create_app`'s handlers, which answer the error toast —
    and `static/js/compose.js` reopens the dock with the message still in
    it, which is the whole reason the dock is hidden rather than destroyed
    while a send is in flight.
    """
    try:
        result = await send_draft(client, form.draft())
    except ComposeError as exc:
        # A `400`, and an `om:error` trigger carrying copy the reader can
        # act on. `static/js/compose.js` reads that toast and reopens the
        # dock with the message still in it — `retry: false` because
        # re-posting an address the server just refused would only fail
        # again, which is the opposite of the transient `JmapError` case.
        problem = _problem(exc)
        return Response(
            status_code=400,
            headers={
                "HX-Trigger": json.dumps(
                    {"om:error": {"toast": problem.message, "retry": False}},
                    separators=(",", ":"),
                )
            },
        )
    payload = {"submission_id": result.submission_id, "email_id": result.email_id}
    return Response(
        status_code=204,
        headers={"HX-Trigger": json.dumps({"om:sent": payload}, separators=(",", ":"))},
    )


@router.post("/compose/discard")
async def compose_discard(
    client: ClientDep, session: SessionDep, draft_id: Annotated[str | None, Form()] = None
) -> Response:
    """Throw a draft away.

    A dock that was never autosaved has no `draft_id`, and closing it is
    then purely a client-side removal — this route still answers `204`
    rather than a `400`, because "there was nothing stored to delete" and
    "the stored copy is gone" are the same fact from the reader's chair,
    and making the client branch on which one it is would be ceremony
    around an identical outcome.
    """
    stored = _clean(draft_id)
    if stored is not None:
        await discard_draft(client, stored)
    return Response(
        status_code=204,
        headers={"HX-Trigger": json.dumps({"om:discarded": {}}, separators=(",", ":"))},
    )


# ---------------------------------------------------------------------------
# POST /attachments
# ---------------------------------------------------------------------------


async def _read_upload(upload: UploadFile) -> bytes:
    """`upload`'s bytes, refused the moment they pass
    `MAX_ATTACHMENT_BYTES` rather than after the whole file has been
    accumulated.

    Read in `_UPLOAD_CHUNK_BYTES` chunks off Starlette's own spooled
    temporary file, so the ceiling is enforced against what has actually
    arrived. `UploadFile` gives no length up front that can be trusted
    (`Content-Length` is the client's claim about the whole multipart
    body, not this part), so the count has to be made here.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="that file is too large to attach")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/attachments", response_class=HTMLResponse)
async def upload_attachment(
    request: Request,
    session: SessionDep,
    prefs: PrefsDep,
    client: ClientDep,
    file: Annotated[UploadFile, File()],
    dom_id: Annotated[str, Form()] = "",
) -> Response:
    """One file, on to Stalwart's `uploadUrl`, back as an attachment chip.

    `JmapClient.upload` is the whole of the Stalwart half — this route
    never builds the URL, substitutes the account id or reads the
    `blobId` out of the answer. What it adds is the ceiling
    (`_read_upload`) and the chip: the blob id alone is not something the
    form could carry, because a re-render has to be able to show the
    reader the file's *name* and size without asking the server what a
    blob was called.

    The chip's hidden input is that record, as the same JSON
    `_attachments` parses back — one shape, written in one place, read in
    one place.
    """
    data = await _read_upload(file)
    mime = (file.content_type or "").strip() or _DEFAULT_UPLOAD_TYPE
    blob_id = await client.upload(data, mime)
    return _templates(request).TemplateResponse(
        request,
        "compose/attachment.html",
        {
            "prefs": prefs,
            "csrf_token": session.csrf_token,
            "dom_id": dom_id.strip() or _dom_id(),
            "attachment": _chip(
                AttachmentRef(
                    blob_id=blob_id,
                    name=file.filename or "attachment",
                    type=mime,
                    size=len(data),
                )
            ),
        },
    )
