"""The conversation view's model (design spec §7): one `MessageView` per
message in a thread, oldest -> newest, plus the `ConversationView` wrapper
that carries the subject, the label chips and the two id lists the shell
and the scroll target need.

This module owns "what a message card shows"; `mailosh/web/mail.py`'s
`/t/{thread_id}` owns nothing beyond handing the result to a template. It
is the same split `mailosh.services.thread_list` already draws for the list
page, and the two deliberately share `LabelChip`/`_chips_for` so a label's
colour cannot differ between a row and the conversation that row opens.

Nothing here renders markup. Plain-text bodies come back as
`mailosh.render.plain_text.TextLine`s (escaped and linkified, with their
own quote depth); an HTML body is left entirely alone — `has_html` says the
template should point an `<iframe>` at `/m/{id}/html` and the sanitising
frame document takes it from there.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from mailosh.db.models import LabelMeta
from mailosh.jmap.client import JmapClient
from mailosh.jmap.models import Address, BodyPart, EmailBody
from mailosh.render.plain_text import TextLine, render_plain
from mailosh.ui.format import avatar_color, format_date, initials

from .mailbox_tree import NavModel
from .thread_list import _NO_SUBJECT, LabelChip, _chips_for

__all__ = [
    "PREVIEW_KIND",
    "AttachmentView",
    "ConversationView",
    "MessageView",
    "build_conversation",
]

#: `signed_by` is only ever the `header.d=` of a DKIM signature that
#: **passed**. The `dkim=pass` prefix is load-bearing, not decoration: a
#: `dkim=fail` result carries a `header.d=` too, and rendering "signed-by
#: news.test" off a failed signature would be the app making a claim the
#: message does not support — the exact opposite of what the row is for.
#: `[^;]*?` keeps the match inside one Authentication-Results *method*
#: clause, so a `dkim=pass` for one domain cannot lend its verdict to a
#: `header.d=` that belongs to a later clause.
_SIGNED_BY_RE = re.compile(r"dkim=pass[^;]*?header\.d=([A-Za-z0-9.\-]+)")

#: What a message with no usable `From` renders as — the same string
#: `mailosh.ui.format` uses for a sender-less row, so the two views of the
#: same message agree.
_UNKNOWN_SENDER = "(unknown sender)"

#: Binary units. `_KB`/`_MB`/`_GB` rather than 1000-based SI: every mail
#: client, and every file manager the reader will compare this against on
#: their own machine, reports an attachment in these.
_KB = 1024
_MB = _KB * 1024
_GB = _MB * 1024


@dataclass(frozen=True)
class AttachmentView:
    """One downloadable part, as an attachment chip renders it.

    `icon` is a name from `mailosh/ui/icons.txt`, and `preview` is the
    dialog a click should open (`"image"`/`"pdf"`/`"text"`, or `None` for a
    part with nothing worth showing inline) — both are decided here rather
    than in Jinja so "which MIME types are previewable" has one reader.
    """

    blob_id: str
    name: str
    mime: str
    size: int
    size_display: str
    icon: str
    preview: str | None


@dataclass(frozen=True)
class MessageView:
    """One message card.

    `expanded` is a **snapshot**, taken from the messages as this GET
    fetched them and never recomputed afterwards (spec §7: "snapshot
    `wasUnread` so marking read does not collapse under the reader"). That
    is safe by construction rather than by care: marking a conversation
    read is a separate POST that answers `204`, and the client never
    re-renders the conversation from it — so the only render that decides
    which cards are open is this one, before anything was marked.

    `visible_lines`/`quoted_lines` are populated only for a message this
    view actually renders as text. When `has_html` is true the body belongs
    to the sandboxed frame at `/m/{id}/html`, and rendering the text
    alternative underneath it as well would print the same message twice.

    `truncated` describes **the body being shown**, not whichever part
    happened to hit the JMAP fetch cap: an HTML message reports
    `html_truncated`, a text one reports `text_truncated`. A message whose
    unrendered text alternative was clipped is not a message the reader is
    seeing half of.

    `restyled` says this message's body is being **inverted** by the dark
    restyle right now, and it is the whole gate on the "Original colours"
    strip: a control that undoes an inversion has nothing to say about a
    message that was not inverted. Nothing here can answer that — it needs
    the reader's Appearance setting, their per-sender overrides and the
    mail's own colour-scheme declaration — so it arrives from the route
    through `restyled` below, and defaults to false. False is the safe
    default in both directions: a caller that does not pass the callback
    shows no strip, rather than offering to undo something nothing did.
    """

    id: str
    from_name: str
    from_email: str
    to: list[Address]
    cc: list[Address]
    bcc: list[Address]
    mailed_by: str | None
    signed_by: str | None
    received_at: datetime
    date_display: str
    date_full: str
    initials: str
    avatar_color: int
    unread: bool
    starred: bool
    expanded: bool
    snippet: str
    has_html: bool
    truncated: bool
    visible_lines: list[TextLine]
    quoted_lines: list[TextLine]
    attachments: list[AttachmentView]
    restyled: bool = False


@dataclass(frozen=True)
class ConversationView:
    """A whole conversation, oldest -> newest.

    `email_ids` is every message in the thread — the id set the action bar
    posts, exactly as `ThreadRow.email_ids` is for a row. `unread_ids` is
    the subset the mark-read POST has work to do on, and `first_unread_id`
    is where the page scrolls: the first unread message, or the last
    message when the whole conversation has already been read (there is
    always somewhere to scroll to, so this is only `None` for a
    conversation with no messages — which `build_conversation` returns
    `None` for instead of building).
    """

    thread_id: str
    subject: str
    messages: list[MessageView]
    email_ids: list[str]
    unread_ids: list[str]
    first_unread_id: str | None
    chips: list[LabelChip]


def size_display(size: int) -> str:
    """`123 B` / `2 KB` / `1.4 MB`: whole units up to a megabyte, one
    decimal above it.

    The step from "no decimal" to "one decimal" is where the number stops
    being precise enough to be useful without one — `2 KB` and `2.0 KB` say
    the same thing about a file nobody will wait for, while `1 MB` and
    `1.4 MB` do not.
    """
    if size < _KB:
        return f"{size} B"
    if size < _MB:
        return f"{round(size / _KB)} KB"
    if size < _GB:
        return f"{size / _MB:.1f} MB"
    return f"{size / _GB:.1f} GB"


def _normalise_mime(mime: str) -> str:
    """A declared content type, reduced to `type/subtype` for comparison.

    `Content-Type: IMAGE/PNG; name=x` and `image/png` are the same type, and
    the tables below are keyed on the second spelling. The same reduction
    `mailosh.web.frames._mime_of` performs before it decides what to serve,
    so a chip and the route behind it read one header the same way.
    """
    return mime.split(";")[0].strip().lower()


def _attachment_icon(mime: str) -> str:
    """The chip's glyph, from the vendored Lucide set only
    (`mailosh/ui/icons.txt` — `mailosh.ui.macros.make_icon` renders an
    unlisted name as an empty decorative span, so this may only name icons
    `make icons` has actually fetched).

    `image/` is tested before `text/` because `image/svg+xml` is both a
    picture and, to a parser, a document: it gets the picture's glyph, and
    `PREVIEW_KIND` below separately refuses to offer it a preview.
    """
    kind = _normalise_mime(mime)
    if kind.startswith("image/"):
        return "image"
    if kind.startswith("text/") or kind == "application/pdf":
        return "file-text"
    return "file"


#: Which preview a chip may offer, per content type.
#:
#: The keys MUST be exactly `mailosh.web.frames.PREVIEW_TYPES` — the set
#: that route will serve inline with its real `Content-Type`. Everything
#: else it downgrades to a download, silently and correctly, so a chip
#: offering "Open" for a type missing from this table would hand the reader
#: a download they did not ask for (spec §3: no control that does not do
#: what it says). `image/svg+xml` and `text/html` are the two that make the
#: point: both look previewable by prefix and neither may ever render on
#: this app's own origin.
#:
#: Not imported from `mailosh.web.frames`: `mailosh.services` must not
#: depend on `mailosh.web`. `tests/unit/test_attachments.py` asserts the two
#: have not drifted apart.
PREVIEW_KIND: dict[str, str] = {
    "image/png": "image",
    "image/gif": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "image/bmp": "image",
    "application/pdf": "pdf",
    "text/plain": "text",
}


def _attachment_preview(mime: str) -> str | None:
    """Which preview a chip opens, or `None` for a part that has no inline
    representation and can only be downloaded."""
    return PREVIEW_KIND.get(_normalise_mime(mime))


def _attachment(part: BodyPart) -> AttachmentView | None:
    """One `BodyPart` as a chip, or `None` for a part that is not one.

    Two parts are dropped. A part carrying a `cid` is referenced by the
    message's own HTML (spec §7: "`cid:` parts excluded from the chips
    list") — it is the sender's logo, not a file they attached, and listing
    it would put a chip under every newsletter in the mailbox. A part with
    no `blobId` cannot be fetched at all, so a chip for it would be a
    control that does nothing.
    """
    if part.cid is not None or part.blob_id is None:
        return None
    return AttachmentView(
        blob_id=part.blob_id,
        # RFC 8621 leaves `name` nullable, and a nameless attachment still
        # has to be clickable — the MIME type is the only thing left that
        # describes it.
        name=part.name or part.type,
        mime=_normalise_mime(part.type),
        size=part.size,
        size_display=size_display(part.size),
        icon=_attachment_icon(part.type),
        preview=_attachment_preview(part.type),
    )


def _mailed_by(return_path: str | None) -> str | None:
    """The domain of the `Return-Path` header, or `None` when the header is
    absent or says nothing.

    `<>` — the null return path every bounce message carries — has no
    domain in it, so it produces no row rather than an empty one.
    """
    if not return_path:
        return None
    address = return_path.strip().strip("<>").strip()
    domain = address.rpartition("@")[2].strip()
    return domain or None


def _signed_by(auth_results: str | None) -> str | None:
    """The `header.d=` domain of a *passing* DKIM signature in
    `Authentication-Results`, or `None`. See `_SIGNED_BY_RE`."""
    if not auth_results:
        return None
    match = _SIGNED_BY_RE.search(auth_results)
    return match.group(1) if match else None


def _sender(message: EmailBody, me: str) -> tuple[str, str]:
    """`(display name, address)` for the card's byline.

    The viewer's own messages are bylined `"me"`, unconditionally and even
    when they carry a perfectly good display name — the same substitution
    `mailosh.ui.format.format_senders` already makes in a list row, so a
    thread does not name you one way in the list and another way inside it.
    The address is compared case- and whitespace-insensitively, matching
    the normalisation `avatar_color` applies.

    `from_` is optional in RFC 8621 and genuinely arrives empty, so the
    name falls back to the address and the address falls back to nothing —
    the template prints one line either way instead of null-checking two
    fields.
    """
    if not message.from_:
        return _UNKNOWN_SENDER, ""
    addr = message.from_[0]
    if addr.email.strip().lower() == me.strip().lower():
        return "me", addr.email
    return (addr.name or "").strip() or addr.email, addr.email


def _format_full(dt: datetime, now: datetime) -> str:
    """The details popover's Date row: `Tue, Sep 1, 2026, 10:42 AM`.

    Converted into `now`'s timezone for the same reason `format_date`
    compares in it — the reader's "when" is their own clock's, and a
    popover that disagrees with the timestamp beside it would be worse than
    either being slightly wrong.
    """
    tz = now.tzinfo
    local = (dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)).astimezone(tz)
    return local.strftime("%a, %b %-d, %Y, %-I:%M %p")


def _message_view(
    message: EmailBody, *, me: str, now: datetime, expanded: bool, unread: bool, restyled: bool
) -> MessageView:
    from_name, from_email = _sender(message, me)
    has_html = bool(message.html_body)
    visible_lines: list[TextLine] = []
    quoted_lines: list[TextLine] = []
    if not has_html:
        visible_lines, quoted_lines = render_plain(message.text_body)
    attachments = [
        chip for chip in (_attachment(part) for part in message.attachments) if chip is not None
    ]
    return MessageView(
        id=message.id,
        from_name=from_name,
        from_email=from_email,
        to=list(message.to),
        cc=list(message.cc),
        bcc=list(message.bcc),
        mailed_by=_mailed_by(message.return_path),
        signed_by=_signed_by(message.auth_results),
        received_at=message.received_at,
        date_display=format_date(message.received_at, now),
        date_full=_format_full(message.received_at, now),
        # Seeded with the address, never the display name: the same person
        # writing under two display names keeps one avatar colour, and a
        # sender-less message falls back to its own id so two of them do
        # not share one.
        initials=initials(from_name if message.from_ else None, from_email or message.id),
        avatar_color=avatar_color(from_email or message.id),
        unread=unread,
        starred="$flagged" in message.keywords,
        expanded=expanded,
        snippet=message.preview,
        has_html=has_html,
        truncated=message.html_truncated if has_html else message.text_truncated,
        visible_lines=visible_lines,
        quoted_lines=quoted_lines,
        attachments=attachments,
        restyled=restyled,
    )


async def build_conversation(
    client: JmapClient,
    *,
    thread_id: str,
    me: str,
    now: datetime,
    label_meta: dict[str, LabelMeta],
    nav: NavModel,
    restyled: Callable[[EmailBody], Awaitable[bool]] | None = None,
) -> ConversationView | None:
    """Fetch one thread and shape it into the conversation view, or `None`
    when the thread has no messages — the route renders its own 404 page
    for that rather than a conversation with nothing in it.

    Messages are ordered oldest -> newest (spec §7: "newest at bottom"),
    which is also what makes "the last message" and "the first unread one"
    well defined.

    **The expansion rule is spec §7's union: `wasUnread`, the last
    message, and a single-message thread.** Including the last message
    unconditionally is what makes the single-message case fall out for
    free rather than needing a third branch. The unread half is a snapshot of the keywords
    as this GET fetched them — see `MessageView.expanded` for why that is
    what stops a card collapsing under the reader a second after they
    opened it.

    `me` is the viewer's own address: their own messages are bylined "me"
    (see `_sender`), the way the list already names them. `label_meta` is
    the map `build_nav` was handed to build `nav`, so the chips below
    inherit a colour that tree has already resolved rather than resolving
    it a second time and risking a different answer; taking it here means a
    later per-label reading preference reaches this function without
    changing its signature or the route that calls it.

    `restyled` is asked, per message, whether the dark restyle is actually
    inverting that body. It is a callback rather than a field on this
    signature because the answer needs three things this module must not
    reach for — a `UiPref` row, a per-sender override table and
    `mailosh.render.dark`'s reading of the raw body — and every one of them
    belongs to the route (`mailosh.web.mail._restyled_for`). Omitting it
    answers false for every message, which renders no "Original colours"
    strip at all.
    """
    messages = await client.get_thread(thread_id)
    if not messages:
        return None

    ordered = sorted(messages, key=lambda m: m.received_at)
    unread_flags = ["$seen" not in message.keywords for message in ordered]
    last_index = len(ordered) - 1

    views = [
        _message_view(
            message,
            me=me,
            now=now,
            expanded=unread_flags[index] or index == last_index,
            unread=unread_flags[index],
            restyled=restyled is not None and await restyled(message),
        )
        for index, message in enumerate(ordered)
    ]

    unread_ids = [view.id for view, is_unread in zip(views, unread_flags, strict=True) if is_unread]
    mailbox_ids: set[str] = set()
    for message in ordered:
        mailbox_ids |= message.mailbox_ids

    return ConversationView(
        thread_id=thread_id,
        # The newest message's subject, matching the list row that opened
        # this conversation (`ThreadRow.subject`) — a reply that dropped
        # the "Re:" should not retitle the page under the reader.
        subject=ordered[last_index].subject or _NO_SUBJECT,
        messages=views,
        email_ids=[view.id for view in views],
        unread_ids=unread_ids,
        # Somewhere to scroll to, always: the first unread message, or the
        # newest one when everything has been read.
        first_unread_id=unread_ids[0] if unread_ids else views[last_index].id,
        chips=_chips_for(mailbox_ids, nav),
    )
