"""Compose: draft autosave, sending, and building a reply or forward
(design spec §8).

Three responsibilities, kept in one module because they share one shape —
the `DraftInput` a compose dock round-trips on every keystroke, every save
and the send:

* **Autosave** (`save_draft`). JMAP bodies are immutable, so "saving" a
  draft is creating a *new* Email and destroying the previous one. The
  order those two happen in is the whole safety property of this module and
  `save_draft`'s docstring argues it out.
* **Sending** (`send_draft`). A thin, validating shell over
  `JmapClient.send_message`: the client owns the JMAP wire shape, this owns
  "is this message sendable at all".
* **Replying** (`build_reply`). Pure and synchronous — no client, no I/O,
  no awaits. That is deliberate: reply construction is where the fiddly,
  easily-wrong rules live (whose addresses, which headers, whose HTML), and
  a pure function is one a test can pin exhaustively without a server or a
  fake in the way.

The one thing to know before editing `build_reply`: **the HTML it quotes
was written by a stranger.** A reply embeds the original message's body and
then mails it onward, so every quote goes through
`mailosh.render.html_sanitize` — the same allow-list the reading pane
uses — before it becomes part of an outgoing message. Skipping it would
mean faithfully forwarding somebody's script into a reply that the *next*
client renders, which is the reading-side vulnerability turned inside out
and aimed at third parties who never opened the original.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Address, BodyPart, EmailBody, Identity
from mailosh.render.html_sanitize import BodyTooDeep, SanitizeContext, sanitize_email_html

__all__ = [
    "FORWARD",
    "QUOTE_CLASS",
    "REPLY",
    "REPLY_ALL",
    "REPLY_MODES",
    "AttachmentRef",
    "ComposeError",
    "DraftInput",
    "InvalidAddress",
    "NoRecipients",
    "Recipient",
    "SendResult",
    "UnknownIdentity",
    "build_reply",
    "discard_draft",
    "list_identities",
    "save_draft",
    "send_draft",
]

logger = logging.getLogger(__name__)

#: The three modes `build_reply` accepts, as names rather than bare strings
#: so a caller that mistypes one gets an ImportError at import time instead
#: of a `ComposeError` at request time.
REPLY = "reply"
REPLY_ALL = "reply_all"
FORWARD = "forward"
REPLY_MODES = frozenset({REPLY, REPLY_ALL, FORWARD})

#: The class list on a quote this app composes. `mailosh_quote` is our own
#: marker and `gmail_quote` is the one every other client already knows;
#: `mailosh.render.quote_trim.QUOTE_MATCHERS` carries both, so a reply
#: composed here collapses correctly when *we* read it back and when
#: anybody else does. Design spec §8 names this exact pair.
QUOTE_CLASS = "mailosh_quote gmail_quote"

#: Gmail's own quote styling, verbatim, because it is what twenty years of
#: mail clients have learned to render. Every property here is in
#: `mailosh.render.css_sanitize.ALLOWED_PROPERTIES`, so the bar survives a
#: round trip back through our own reading pane rather than being stripped
#: from the thing we just sent.
_QUOTE_STYLE = "margin:0 0 0 .8ex;border-left:1px solid #cccccc;padding-left:1ex"

#: What the composer types into. One empty line above the quote, so the
#: caret has somewhere to land that is not inside the quoted history.
_COMPOSE_SPACER = "<div><br></div>"

#: RFC 5321 §4.5.3.1.3's total path limit. A longer "address" is not a long
#: address, it is somebody pasting a paragraph into the To field.
_MAX_ADDRESS_LENGTH = 254

#: An address we will put in a header. Deliberately narrower than RFC 5322's
#: full grammar: no quoted local parts (`"a b"@x`), no domain literals
#: (`u@[192.0.2.1]`), no comments. Those are legal and essentially never
#: typed into a webmail To field, and every one of them is a place where a
#: parser disagreement between us and the server could hide a second
#: address. The domain is split on `.` so an empty label (`a@b..c`, `a@.b`)
#: cannot pass; a single-label domain (`root@localhost`) can, because on a
#: self-hosted server it is real.
_ADDRESS_RE = re.compile(r'^[^\s@<>,;:"\\]+@[^\s@<>,;:"\\.]+(?:\.[^\s@<>,;:"\\.]+)*$')

#: C0/C1-adjacent control characters, checked separately from `_ADDRESS_RE`
#: because `\s` does not cover them all and because the *name* field is
#: checked for them too. CR and LF are the ones that matter: a display name
#: carrying one is a header-injection attempt, and while JMAP's structured
#: JSON means the server — not this app — assembles the header, a webmail
#: that hands a mail server a name with a newline in it is relying on
#: somebody else's escaping for its own correctness.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Case-insensitive "does the subject already say this". Only the prefix
#: about to be added is tested, so replying to `Fwd: Notes` yields
#: `Re: Fwd: Notes` — the forward is part of what the subject *says*, and
#: collapsing every prefix would quietly rewrite it.
_RE_PREFIX_RE = re.compile(r"^re\s*:", re.IGNORECASE)
_FWD_PREFIX_RE = re.compile(r"^(?:fwd?)\s*:", re.IGNORECASE)

#: How many References entries an outgoing reply carries. RFC 5322 sets no
#: limit, but a References header grows by one message-id per reply forever
#: and real threads have hit the 998-octet line limit servers do enforce.
#: The trim keeps the **first** entry and the most recent
#: `_MAX_REFERENCES - 1`: the first is the thread root, which is what every
#: References-based threading algorithm (Stalwart's included) anchors on,
#: and dropping from the middle is what RFC 5322 §3.6.4 explicitly permits.
_MAX_REFERENCES = 21

#: Month abbreviations spelled out rather than `%b`, which is locale-
#: dependent: an attribution line is *content of an outgoing message*, and
#: it must not change because the container's LC_TIME did.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

#: What an attribution line calls a message with no usable `From` — the
#: same string `mailosh.services.conversation` uses, so the reply and the
#: card it was composed from agree.
_UNKNOWN_SENDER = "(unknown sender)"

_FORWARD_DIVIDER = "---------- Forwarded message ---------"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ComposeError(ValueError):
    """A draft that cannot be saved or sent as given.

    A `ValueError` subclass, not a bare `Exception`: every one of these is
    "the input is not usable", which is what a route turns into a 400 and
    an inline message in the dock — never a 500. `JmapError` remains
    separate and means the server refused something this module considered
    well-formed.
    """


class InvalidAddress(ComposeError):
    """One recipient address is not one we will put in a header.

    Carries the offending address and which field it came from, because the
    only useful thing a dock can do with this is point at the chip that is
    wrong. Raising rather than dropping is the point: a silently discarded
    recipient is a message that looks sent and never arrives.
    """

    def __init__(self, address: str, field: str, reason: str) -> None:
        super().__init__(f"{field}: {address!r} is not a usable email address ({reason})")
        self.address = address
        self.field = field
        self.reason = reason


class NoRecipients(ComposeError):
    """A send with nothing in To, Cc or Bcc.

    Checked here, before anything is created, rather than left to the
    server: Stalwart accepts the `Email/set` create happily and only
    rejects the `EmailSubmission/set` with `noRecipients` (verified live),
    which leaves an orphan draft behind for every attempt.
    """

    def __init__(self) -> None:
        super().__init__("a message needs at least one recipient in To, Cc or Bcc")


class UnknownIdentity(ComposeError):
    """The requested From identity is not one this account can send as."""

    def __init__(self, identity_id: str) -> None:
        super().__init__(f"no identity with id {identity_id!r} on this account")
        self.identity_id = identity_id


# ---------------------------------------------------------------------------
# The compose model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    """One address in To/Cc/Bcc, with the display name if there is one."""

    email: str
    name: str | None = None


@dataclass(frozen=True)
class AttachmentRef:
    """An already-uploaded blob, ready to attach.

    Attachments reach a draft as blobs first (`JmapClient.upload`), so what
    compose carries is a *reference*, never bytes: `size` and `type` are
    here for the dock's file chip and the 25 MB warning, not for the wire —
    `mailosh.jmap.client` omits `size` from the `bodyStructure` part it
    builds, since RFC 8621 §4.1.4 makes it server-set.

    `cid` set means an inline image: the body references it as
    `cid:<value>` and the part goes out `Content-Disposition: inline`, so a
    reading client draws it in place instead of listing it as a file.
    """

    blob_id: str
    name: str
    type: str
    size: int
    cid: str | None = None


@dataclass(frozen=True)
class DraftInput:
    """Everything a compose dock holds, in one immutable value.

    Frozen, and every collection a tuple, because this is passed between an
    HTTP handler, an autosave and a send: a mutable draft shared across two
    in-flight requests is a race with the user's own words in it.

    `draft_id` is the Email id of the *previous* autosave, not this one —
    the id to destroy once a replacement exists. `save_draft` returns the
    new id and the caller carries it forward as the next `draft_id`.
    """

    to: tuple[Recipient, ...] = ()
    cc: tuple[Recipient, ...] = ()
    bcc: tuple[Recipient, ...] = ()
    subject: str = ""
    html: str = ""
    text: str = ""
    attachments: tuple[AttachmentRef, ...] = ()
    identity_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    draft_id: str | None = None


@dataclass(frozen=True)
class SendResult:
    """What a successful send produced.

    Both ids, because they answer different questions: `submission_id` is
    what an undo-send window cancels (RFC 8621 §7.5), `email_id` is the
    message now sitting in Sent that a "Sent — View" toast links to.
    """

    submission_id: str
    email_id: str


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _checked_address(recipient: Recipient, field: str) -> Address:
    """One `Recipient` as a JMAP `Address`, or `InvalidAddress`.

    Surrounding whitespace is trimmed (a pasted address routinely carries
    it) but nothing else is repaired: an address this cannot vouch for is
    refused, never silently corrected into a different one.
    """
    address = recipient.email.strip()
    if not address:
        raise InvalidAddress(recipient.email, field, "empty")
    if len(address) > _MAX_ADDRESS_LENGTH:
        raise InvalidAddress(address, field, f"longer than {_MAX_ADDRESS_LENGTH} characters")
    if _CONTROL_RE.search(address) or not _ADDRESS_RE.match(address):
        raise InvalidAddress(address, field, "not a local@domain address")
    name = recipient.name
    if name is not None:
        if _CONTROL_RE.search(name):
            raise InvalidAddress(address, field, "display name contains a control character")
        name = name.strip() or None
    return Address(email=address, name=name)


def _checked_addresses(recipients: Sequence[Recipient], field: str) -> list[Address]:
    return [_checked_address(recipient, field) for recipient in recipients]


def _body_parts(attachments: Sequence[AttachmentRef]) -> list[BodyPart]:
    """Attachment references as the `BodyPart`s the JMAP client wants.

    An empty `blob_id` is refused rather than sent: the resulting
    `bodyStructure` part would be rejected by the server with an error that
    names neither the file nor the field, and the likeliest way to get here
    is an upload whose failure the dock did not notice.
    """
    parts: list[BodyPart] = []
    for attachment in attachments:
        if not attachment.blob_id.strip():
            raise ComposeError(f"attachment {attachment.name!r} has no blob id")
        parts.append(
            BodyPart(
                blob_id=attachment.blob_id,
                name=attachment.name or None,
                type=attachment.type,
                size=attachment.size,
                cid=attachment.cid,
            )
        )
    return parts


def _reply_headers(draft: DraftInput) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(inReplyTo, references)` for the wire, from the draft's own fields."""
    in_reply_to = (draft.in_reply_to,) if draft.in_reply_to else ()
    return in_reply_to, tuple(draft.references)


async def _identity_for(client: JmapClient, identity_id: str | None) -> Identity:
    """The identity a draft sends as: the account default when unset.

    A named identity is looked up in the account's real list rather than
    trusted, so a stale or forged `identity_id` from a form post becomes an
    `UnknownIdentity` here instead of an `invalidProperties` rejection from
    the server after a draft has already been created.
    """
    if identity_id is None:
        return await client.get_identity()
    for identity in await client.get_identities():
        if identity.id == identity_id:
            return identity
    raise UnknownIdentity(identity_id)


async def _destroy_superseded(client: JmapClient, draft_id: str | None, kept_id: str) -> None:
    """Destroy the draft `kept_id` replaces, if there is one.

    **Called only once `kept_id` exists.** That ordering is the entire
    point of this module's autosave design; see `save_draft`.

    A failure here is logged, not raised, and that is a deliberate
    asymmetry with every other JMAP call in this module. The new draft is
    already saved, and the caller's *only* way to learn its id is this
    function's caller returning normally: raising would leave the dock
    still holding the old `draft_id`, so its next autosave would destroy
    the old draft and create a third one — orphaning the draft we just made
    and whose id nobody ever learned. A stale revision left in Drafts is a
    smaller, visible, user-fixable problem than a draft nothing points at.
    """
    if not draft_id or draft_id == kept_id:
        return
    try:
        await client.destroy_emails([draft_id])
    except JmapError:
        logger.warning(
            "compose: could not destroy superseded draft %s (replaced by %s)",
            draft_id,
            kept_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


async def save_draft(client: JmapClient, draft: DraftInput) -> str:
    """Autosave `draft`, returning the id of the newly created draft Email.

    **Create first, destroy second, and never the other way round.** JMAP
    Email bodies are immutable (design spec §8), so a save is necessarily a
    create plus a destroy of the previous revision. If the destroy ran
    first and the create then failed — a dropped connection, a quota, a
    server restart, any of which happen mid-typing — the user's draft would
    be *gone*, destroyed to make room for something that never arrived.
    Creating first means the worst case is a duplicate revision in Drafts,
    which the next successful save cleans up.

    So: validate, resolve the identity, create, and only with the new id in
    hand destroy `draft.draft_id`. Anything that raises before the create
    leaves the previous draft exactly where it was.

    Recipients are validated but **not required** — a dock two seconds into
    a new message has a body and no To yet, and that is precisely the draft
    autosave exists to protect. A *malformed* address is still refused,
    because a chip the user typed that silently vanishes from the saved
    draft is worse than an error next to it.
    """
    to = _checked_addresses(draft.to, "to")
    cc = _checked_addresses(draft.cc, "cc")
    bcc = _checked_addresses(draft.bcc, "bcc")
    attachments = _body_parts(draft.attachments)
    in_reply_to, references = _reply_headers(draft)
    identity = await _identity_for(client, draft.identity_id)

    draft_id = await client.create_draft(
        sender=Address(email=identity.email, name=identity.name),
        to=to,
        cc=cc,
        bcc=bcc,
        subject=draft.subject,
        text=draft.text,
        html=draft.html or None,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
    )
    await _destroy_superseded(client, draft.draft_id, draft_id)
    return draft_id


async def send_draft(client: JmapClient, draft: DraftInput) -> SendResult:
    """Send `draft`, and destroy the autosaved revision it supersedes.

    Same create-then-destroy ordering as `save_draft`, for the same reason:
    `JmapClient.send_message` creates the outgoing Email and submits it in
    one batch, and only once that has returned an id is the superseded
    `draft.draft_id` destroyed. A send that fails leaves the user's draft
    intact for the dock to reopen with the error (design spec §8:
    "failures reopen the dock with the error").

    Refuses a message with no recipient at all, before creating anything —
    see `NoRecipients`.
    """
    to = _checked_addresses(draft.to, "to")
    cc = _checked_addresses(draft.cc, "cc")
    bcc = _checked_addresses(draft.bcc, "bcc")
    if not (to or cc or bcc):
        raise NoRecipients()
    attachments = _body_parts(draft.attachments)
    in_reply_to, references = _reply_headers(draft)
    identity = await _identity_for(client, draft.identity_id)

    submission_id, email_id = await client.send_message(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=draft.subject,
        text=draft.text,
        html=draft.html or None,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
        identity=identity,
    )
    await _destroy_superseded(client, draft.draft_id, email_id)
    return SendResult(submission_id=submission_id, email_id=email_id)


async def discard_draft(client: JmapClient, draft_id: str) -> None:
    """Throw a draft away for good.

    Destroy, not a move to Trash: a draft is a revision of something the
    user was writing and has just decided not to write, not received mail
    they might want back. That is what the trash button in the compose
    toolbar means (design spec §8) and what every other client does with
    it.
    """
    await client.destroy_emails([draft_id])


async def list_identities(client: JmapClient) -> list[Identity]:
    """Every "send as" address on the account, for the From picker."""
    return await client.get_identities()


# ---------------------------------------------------------------------------
# Reply / reply-all / forward
# ---------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """`value` in UTC, treating a naive datetime as already being UTC.

    The same rule `mailosh.jmap.client._to_utc_date` and
    `mailosh.ui.format._as_aware` apply: reinterpreting a naive timestamp
    through the container's local zone would move a date by a day for
    reasons nothing in the app can see.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _display_when(message: EmailBody) -> str:
    """`"Sep 1, 2026 at 8:41 PM"` — the date half of an attribution line.

    `sent_at` in preference to `received_at`: an attribution names when the
    author wrote it, not when our server happened to accept it, and for a
    message delayed in a queue those differ by more than rounding.
    """
    when = _as_utc(message.sent_at or message.received_at)
    hour = when.hour % 12 or 12
    meridiem = "AM" if when.hour < 12 else "PM"
    return f"{_MONTHS[when.month - 1]} {when.day}, {when.year} at {hour}:{when:%M} {meridiem}"


def _display_sender(message: EmailBody) -> str:
    """Who the attribution line names: the author, not the Reply-To.

    `Reply-To` says where a reply should *go*; `From` says who wrote the
    words being quoted, and naming a list address as the author of a
    person's message would be wrong in the one place a human reads it.
    """
    sender = message.from_ or message.reply_to
    if not sender:
        return _UNKNOWN_SENDER
    return sender[0].name or sender[0].email


def _format_address(address: Address) -> str:
    return f"{address.name} <{address.email}>" if address.name else address.email


def _format_address_list(addresses: Sequence[Address]) -> str:
    return ", ".join(_format_address(address) for address in addresses)


def _recipients(addresses: Iterable[Address], *, drop: Iterable[str] = ()) -> list[Recipient]:
    """Addresses as deduplicated `Recipient`s, first occurrence winning.

    Deduplication is **case-insensitive on the address** and ignores the
    display name: `Dan <dan@x.test>` and `dan@x.test` are one person, and a
    reply-all that mails them twice is the kind of thing recipients
    remember. `drop` is a set of already-lowercased addresses to leave out
    — the user's own address, and whatever is already in a field built
    before this one.
    """
    seen = {value.strip().lower() for value in drop if value.strip()}
    out: list[Recipient] = []
    for address in addresses:
        email = address.email.strip()
        key = email.lower()
        if not email or key in seen:
            continue
        seen.add(key)
        out.append(Recipient(email=email, name=address.name or None))
    return out


def _reply_recipients(
    original: EmailBody, mode: str, me: str
) -> tuple[tuple[Recipient, ...], tuple[Recipient, ...]]:
    """`(to, cc)` for a reply, per `mode`.

    A plain **reply** goes to `Reply-To` if the sender set one, else to
    `From`, and to nobody else. The one exception is replying to your own
    message: that would address the mail to yourself, so it falls back to
    whoever the original was addressed to — which is what a user means by
    "reply" on something in Sent.

    **Reply-all** is every original recipient plus the sender, minus the
    user: `To` gets the sender and the original `To`, `Cc` keeps the
    original `Cc`, so the shape of the conversation survives instead of
    everyone being flattened into one field. `me` is what stops the classic
    bug — a reply-all that mails you a copy of your own reply — and it is
    applied to both fields, matched case-insensitively on the address.
    `Bcc` is never carried over: those recipients were blind on the
    original and copying them into a visible reply would out them.
    """
    sender = original.reply_to or original.from_
    me_key = me.strip().lower()

    if mode == REPLY:
        to = _recipients(sender)
        if to and all(recipient.email.lower() == me_key for recipient in to):
            to = _recipients(original.to) or to
        return tuple(to), ()

    to = _recipients([*sender, *original.to], drop={me_key})
    cc = _recipients(
        original.cc,
        drop={me_key, *(recipient.email.lower() for recipient in to)},
    )
    if not to and not cc:
        # Everyone on the message was the user: a note they sent only to
        # themselves. Reply to it rather than producing a draft with no
        # recipient at all.
        to = _recipients(sender)
    return tuple(to), tuple(cc)


def _subject_for(original: EmailBody, mode: str) -> str:
    """`Re: `/`Fwd: `, added only when it is not already there."""
    subject = (original.subject or "").strip()
    if mode == FORWARD:
        if not subject:
            return "Fwd:"
        return subject if _FWD_PREFIX_RE.match(subject) else f"Fwd: {subject}"
    if not subject:
        return "Re:"
    return subject if _RE_PREFIX_RE.match(subject) else f"Re: {subject}"


def _bare(message_id: str) -> str:
    """A Message-ID without RFC 5322's angle brackets.

    RFC 8621 §4.1.3's `messageId`/`inReplyTo`/`references` are the
    bracket-less form and the server re-adds the brackets when it writes
    the header. Normalising rather than trusting, because these values also
    reach this module from form posts that round-tripped through a browser.
    """
    return message_id.strip().strip("<>").strip()


def _ancestors(thread: Sequence[EmailBody], original: EmailBody) -> list[EmailBody]:
    """Every message in `thread` older than `original`, oldest first."""
    cutoff = _as_utc(original.received_at)
    older = [
        message
        for message in thread
        if message.id != original.id and _as_utc(message.received_at) < cutoff
    ]
    return sorted(older, key=lambda message: _as_utc(message.received_at))


def _references_for(thread: Sequence[EmailBody], original: EmailBody) -> tuple[str, ...]:
    """The `References` an in-thread reply carries: the original's own
    References plus the original's Message-ID, in that order (RFC 5322
    §3.6.4), deduplicated and trimmed to `_MAX_REFERENCES`.

    When the original carries **no** References at all — it was the first
    message, or was sent by something that dropped the header — the chain
    is rebuilt from the message-ids of every older message in the thread we
    were handed. That is the only reason `build_reply` takes the whole
    thread rather than one message: without it, replying to a threadless
    message re-roots the conversation in every client that threads on
    References, and the symptom (a thread that splits in two, in *other*
    people's mailboxes) is invisible from here.
    """
    chain = [_bare(value) for value in original.references]
    if not chain:
        chain = [
            _bare(message_id)
            for message in _ancestors(thread, original)
            for message_id in message.message_id
        ]
    chain.extend(_bare(value) for value in original.message_id)

    seen: set[str] = set()
    unique = [value for value in chain if value and not (value in seen or seen.add(value))]
    if len(unique) > _MAX_REFERENCES:
        unique = [unique[0], *unique[-(_MAX_REFERENCES - 1) :]]
    return tuple(unique)


def _outbound_context(email_id: str) -> SanitizeContext:
    """The sanitiser context for HTML on its way *out*.

    Every rewrite path in `mailosh.render.html_sanitize` is inert under
    this context, which is exactly what an outgoing quote wants:

    * `cid_parts` is empty, so an inline image the original referenced gets
      no `src` — a reply does not carry the original's parts, so the URL
      would resolve to nothing anyway;
    * `remote=False` with no `sign_image`, so a tracking pixel in the
      quoted message is not re-sent live;
    * `data:` images are kept (they are self-contained and already
      type-checked by the sanitiser), which is how a pasted screenshot
      survives being quoted.

    `origin` is `""` because nothing can read it: it is only ever used to
    *build* a rewritten URL, and no rewrite fires. Passing this
    deployment's real origin instead would put our own URLs into other
    people's mailboxes if that ever changed.
    """
    return SanitizeContext(email_id=email_id, origin="", remote=False)


def _text_as_html(text: str | None) -> str:
    """Plain text as inert HTML: escaped, with `<br>` for line breaks."""
    if not text:
        return ""
    return "<br>".join(escape(line) for line in text.splitlines())


def _quoted_body_html(original: EmailBody) -> str:
    """The original's body, sanitised, ready to embed in a reply.

    The HTML part when there is one, the plain-text part rendered as
    escaped HTML when there is not — and **either way** through
    `sanitize_email_html`, so there is exactly one place in this module
    where a stranger's markup becomes part of an outgoing message and
    exactly one property to test.

    A body that trips `BodyTooDeep` (nested past the sanitiser's limit, a
    shape only a deliberately hostile message reaches) falls back to
    quoting the plain-text alternative, which is escaped before it is
    sanitised and so cannot itself be too deep. The alternative would be
    quoting nothing, and a reply that silently drops the message it is
    replying to is worse than one that quotes it as text.
    """
    context = _outbound_context(original.id)
    if original.html_body:
        try:
            return sanitize_email_html(original.html_body, context).html
        except BodyTooDeep:
            logger.warning(
                "compose: message %s nests too deeply to quote as HTML; quoting its text",
                original.id,
            )
    return sanitize_email_html(_text_as_html(original.text_body), context).html


def _forward_header_lines(original: EmailBody) -> list[str]:
    """Gmail's forwarded-message header block, as plain lines.

    Rendered into both halves of the body (escaped for the HTML one), so a
    recipient reading either sees the same provenance. `Cc` appears only
    when the original had one, the same way a real header block does.
    """
    lines = [
        _FORWARD_DIVIDER,
        f"From: {_format_address_list(original.from_)}",
        f"Date: {_display_when(original)}",
        f"Subject: {original.subject or ''}",
        f"To: {_format_address_list(original.to)}",
    ]
    if original.cc:
        lines.append(f"Cc: {_format_address_list(original.cc)}")
    return lines


def _quote_html(original: EmailBody, mode: str) -> str:
    """The whole quoted block: attribution, then the original's body.

    Both live *inside* the `QUOTE_CLASS` div rather than the attribution
    sitting above it. `mailosh.render.quote_trim.split_html` cuts at the
    marker element, so anything inside it collapses with the quote as one
    unit — which is what the reader wants, since the attribution line is
    part of the history, not part of the reply. (It also has a fallback for
    an attribution left *outside* a marker, for clients that do it the
    other way; not relying on that fallback for our own output is one less
    thing to keep in step.)

    A reply indents the quoted body in a `<blockquote>` with Gmail's own
    styling; a forward does not, because a forwarded message is the
    content, not a citation inside somebody else's reply.
    """
    body = _quoted_body_html(original)
    if mode == FORWARD:
        header = "<br>".join(escape(line) for line in _forward_header_lines(original))
        return f'<div class="{QUOTE_CLASS}"><div class="mailosh_attr">{header}</div>{body}</div>'
    attribution = escape(f"On {_display_when(original)}, {_display_sender(original)} wrote:")
    return (
        f'<div class="{QUOTE_CLASS}">'
        f'<div class="mailosh_attr">{attribution}</div>'
        f'<blockquote class="mailosh_quote_body" style="{_QUOTE_STYLE}">{body}</blockquote>'
        "</div>"
    )


def _quote_text(original: EmailBody, mode: str) -> str:
    """The plain-text half of the quote.

    A reply prefixes every line with `> ` (what `quote_trim.quote_depth`
    and every text-mode mail client since 1980 read as a quote); a forward
    reproduces the header block and then the body unprefixed, which is what
    `quote_trim`'s own forwarded-header heuristic looks for.

    Quoted text comes from the original's **text/plain** part only. A
    message that arrived HTML-only contributes just the attribution here,
    and its content still reaches the recipient through the HTML part —
    deriving text from HTML would mean a second HTML-to-text renderer in a
    module that already has one job.
    """
    body = original.text_body or ""
    if mode == FORWARD:
        header = "\n".join(_forward_header_lines(original))
        return f"\n\n{header}\n\n{body}" if body else f"\n\n{header}\n"
    attribution = f"On {_display_when(original)}, {_display_sender(original)} wrote:"
    if not body:
        return f"\n\n{attribution}\n"
    quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    return f"\n\n{attribution}\n{quoted}"


def _forwarded_attachments(original: EmailBody) -> tuple[AttachmentRef, ...]:
    """The original's attachments, ready to re-attach to a forward.

    Design spec §8: a forward carries the attachments, a reply does not.
    Inline parts keep their `cid` and go back out inline rather than being
    demoted to files, so a forwarded newsletter's images stay where the
    sender put them.

    A part with no `blobId` is skipped — there is nothing to attach.
    """
    return tuple(
        AttachmentRef(
            blob_id=part.blob_id,
            name=part.name or "",
            type=part.type,
            size=part.size,
            cid=part.cid,
        )
        for part in original.attachments
        if part.blob_id
    )


def build_reply(thread: list[EmailBody], reply_to_id: str, mode: str, me: str) -> DraftInput:
    """Build the `DraftInput` a reply, reply-all or forward starts from.

    Pure and synchronous: everything it needs is in the thread the
    conversation view already fetched, so it makes no request and can be
    tested exhaustively — which matters more here than almost anywhere else
    in the app, because the rules it encodes (whose addresses, which
    headers, whose HTML) are each a well-known way to get replies wrong.

    `thread` is the whole conversation (as `JmapClient.get_thread` returns
    it), `reply_to_id` picks the message being replied to, and `me` is the
    user's own address — the parameter that keeps reply-all from mailing
    the user their own reply.

    What each mode produces:

    * **reply** — the sender (or `Reply-To`), `Re: ` subject, threading
      headers, the original quoted, no attachments.
    * **reply_all** — every original recipient plus the sender, minus `me`;
      otherwise identical.
    * **forward** — no recipients (the user picks them), `Fwd: ` subject,
      the original quoted under a forwarded-message header block, **and its
      attachments**. No `In-Reply-To`/`References`: a forward is a new
      conversation aimed at somebody who was not in the old one, and
      threading it into the original would file it under a thread they
      cannot see.

    Raises `ComposeError` for a `mode` outside `REPLY_MODES` or a
    `reply_to_id` that is not in `thread` — both are route-supplied, both
    are 400s, neither should ever reach a server call.
    """
    if mode not in REPLY_MODES:
        raise ComposeError(f"unknown reply mode {mode!r}; expected one of {sorted(REPLY_MODES)}")
    original = next((message for message in thread if message.id == reply_to_id), None)
    if original is None:
        raise ComposeError(f"message {reply_to_id!r} is not in the thread given")

    to, cc = _reply_recipients(original, mode, me)
    if mode == FORWARD:
        to, cc = (), ()
        in_reply_to: str | None = None
        references: tuple[str, ...] = ()
    else:
        in_reply_to = _bare(original.message_id[0]) if original.message_id else None
        references = _references_for(thread, original)

    return DraftInput(
        to=to,
        cc=cc,
        subject=_subject_for(original, mode),
        html=f"{_COMPOSE_SPACER}{_quote_html(original, mode)}",
        text=_quote_text(original, mode),
        attachments=_forwarded_attachments(original) if mode == FORWARD else (),
        in_reply_to=in_reply_to,
        references=references,
    )
