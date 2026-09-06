"""The list page's view model (design spec §5.3): one `ThreadRow` per
collapsed conversation (sender list, subject/snippet, label chips, date,
unread/starred/attachment state) and the `ThreadPage` wrapper around a
50-per-page slice, built from `JmapClient.query_page`'s one batched HTTP
request.

This module owns "what a row's fields mean" (aggregating across every
message in a thread — see `_row_for_thread`) and "which JMAP filter a nav
key needs" (see `build_page`); `JmapClient.query_page` owns nothing beyond
mechanically executing whatever filter it's given, one request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mailosh.jmap.client import JmapClient
from mailosh.jmap.models import EmailHeader
from mailosh.ui.format import avatar_color, format_date, format_full, format_senders

from .mailbox_tree import LabelNode, NavModel, resolve_mailbox

#: Spec §4.1's 12-colour label palette, exact names/order — mirrors
#: `mailosh/ui/format.py`'s `_LABEL_PALETTE_SIZE` comment and the
#: `--label-*` custom properties in `styles/input.css`. Used only as
#: `LabelChip`'s deterministic fallback colour: `LabelNode.color` may
#: legitimately be `None` (a label the user never assigned a colour to),
#: but `LabelChip.color` — unlike `LabelNode.color` — is a plain `str`,
#: never `None` (a chip's colour is never something the template has to
#: null-check), so this reuses `avatar_color`'s exact "stable SHA-256-mod-12
#: index" technique, generalized from "an email address" to "any stable
#: identifier" (here, the label's own mailbox id) rather than duplicating
#: that hashing logic.
_LABEL_PALETTE = (
    "indigo",
    "emerald",
    "rose",
    "amber",
    "sky",
    "violet",
    "teal",
    "orange",
    "pink",
    "lime",
    "slate",
    "red",
)

#: Row anatomy caps visible chips at "2 + '+N'" (spec §5.3). This view
#: model caps the *list* at 3 (controller decision 3) — exactly enough for
#: the template to show the first 2 plus an accurate "+1" without it having
#: to compute `len(all_chips) - 2` itself against an uncapped list.
_MAX_CHIPS = 3

#: `ThreadRow.subject` is a plain `str`, never `str | None` — a message
#: whose own `subject` is `None`/empty renders this placeholder instead, so
#: the template never null-checks it. Gmail's own convention for a
#: genuinely subject-less message, not invented here.
_NO_SUBJECT = "(no subject)"


@dataclass
class LabelChip:
    mailbox_id: str
    name: str
    color: str


#: Does one message belong to the view being rendered? See
#: `_in_mailbox_scope`/`_other_than_scope`, and `build_page`, which pairs
#: each JMAP filter with the predicate that mirrors it.
InScope = Callable[[EmailHeader], bool]


@dataclass
class ThreadRow:
    """One collapsed conversation as spec §5.3's row renders it.

    Every aggregate field here (`senders`, `count`, `unread`, `starred`,
    `has_attachment`, `chips`, and the `latest`-derived `subject`/`preview`/
    `date_display`/`received_at`/`latest_email_id`) is computed over only
    the messages *in the view being rendered*, not over every member of the
    JMAP thread — see `_row_for_thread`.

    `email_ids` is the deliberate exception: it stays the **full** thread,
    oldest -> newest, including members outside the current view. It is not
    a display field — it is the id set an action operates on, and archiving
    or starring a conversation from the Inbox should act on the whole
    conversation the way Gmail's own row actions do, not just the subset
    that happens to be visible here. Task 9's `mailosh.services.actions`
    is its only consumer. Anything that renders should read the scoped
    fields instead, and `count` is *not* `len(email_ids)` for exactly this
    reason.
    """

    thread_id: str
    email_ids: list[str]
    latest_email_id: str
    senders: str
    count: int
    subject: str
    preview: str
    date_display: str
    #: The same instant spelled out (`Tue, Sep 1, 2026, 10:42 AM`), for the
    #: row's tooltip. `date_display` is the column — three characters where
    #: it can be, because fifty rows are read by scanning them — and this is
    #: the answer to "yes, but when exactly". One `format_full`, shared with
    #: the message card, so a row and the conversation it opens cannot
    #: disagree about when something arrived.
    date_full: str
    received_at: datetime
    unread: bool
    starred: bool
    has_attachment: bool
    chips: list[LabelChip]


@dataclass
class ThreadPage:
    rows: list[ThreadRow]
    position: int
    limit: int
    total: int
    next_position: int | None


def _fallback_chip_color(mailbox_id: str) -> str:
    return _LABEL_PALETTE[avatar_color(mailbox_id)]


def _chips_for(mailbox_ids: set[str], nav: NavModel) -> list[LabelChip]:
    """A thread's chips: the labels in `nav.labels` that this thread's
    messages actually carry (controller decision 3: "user labels...in nav
    order"), flattened parent-then-children in that same nav order, capped
    at `_MAX_CHIPS`.

    `mailbox_ids` is the union over the *scoped* messages only (see
    `_row_for_thread`), so a label carried solely by a trashed or archived
    member of the thread does not chip the Inbox row.

    `nav.labels` is the label registry: `build_nav` drops
    `visibility="hide"` labels from it structurally, which is precisely
    what makes "hidden labels never produce chips" (controller decision 3)
    automatic here — such a label simply never appears for this walk to
    find. `"show_if_unread"` labels are deliberately *not* dropped from the
    tree even at zero unread (`mailbox_tree._in_label_tree`), so they still
    chip normally; that preference governs their nav row only
    (`mailbox_tree.hidden_in_nav`). A chip blinking out because some
    unrelated message elsewhere got read would be a worse bug than the
    sidebar row it was meant to control.
    """
    chips: list[LabelChip] = []

    def walk(nodes: list[LabelNode]) -> None:
        for node in nodes:
            if len(chips) >= _MAX_CHIPS:
                return
            if node.mailbox_id in mailbox_ids:
                color = node.color or _fallback_chip_color(node.mailbox_id)
                chips.append(LabelChip(mailbox_id=node.mailbox_id, name=node.name, color=color))
            walk(node.children)

    walk(nav.labels)
    return chips


def _row_for_thread(
    thread_id: str,
    emails: list[EmailHeader],
    nav: NavModel,
    me: str,
    now: datetime,
    in_scope: InScope,
) -> ThreadRow | None:
    """Aggregate one thread into its row, over the messages *this view*
    actually matched.

    `emails` is every member of the JMAP thread, because that is what
    `Thread/get` returns: thread membership is global and has nothing to do
    with the mailbox being viewed. Folding all of them into the row was
    wrong in three compounding ways, all of which `in_scope` fixes:

    1. **Content leak.** A message the user moved to Trash (or Spam, or
       Archive) would supply the Inbox row's `subject`, `preview`,
       `date_display` and `latest_email_id`, and its sender would appear in
       the Inbox row's sender list — a deleted message still speaking for a
       row in the mailbox it was deleted from.
    2. **The date column would not sort.** `Email/query` + `collapseThreads`
       orders rows by the newest message *matching the filter*, so a row's
       position comes from the scoped set; taking `date_display` from the
       newest message in the *whole* thread makes the rendered dates
       disagree with the order the rows are drawn in. Any Inbox thread with
       a Sent reply, a draft, or an archived member would render visibly
       out of date order.
    3. **Drafts carry no `$seen`.** Once compose ships, every thread with a
       draft reply would have read as unread forever, in every view.

    `in_scope` therefore has to mirror the JMAP filter `build_page` sent for
    this exact page — `build_page` builds the filter and its predicate
    (`_in_mailbox_scope`/`_other_than_scope`) together so they cannot
    drift apart. Everything the row *displays* is then computed over
    `scoped` alone: `senders` (oldest -> newest, per `format_senders`'s own
    contract), `count`, `unread`/`starred`/`has_attachment` ("any" —
    mirroring Gmail: a thread reads as unread until every message in it is
    read), the `mailbox_ids` union `_chips_for` narrows to chips, and the
    single newest scoped message (`latest`) for `subject`/`preview`/
    `received_at`/`date_display`/`latest_email_id`. Only `email_ids` spans
    the full thread — see `ThreadRow`'s own docstring for why.

    Returns `None` when the thread has no messages at all, or none in
    scope. Neither is expected: every id in `QueryPage.thread_order` should
    have a non-empty entry in `emails_by_thread`, and the thread is on this
    page precisely because at least one of its messages matched the filter.
    But a message moved between the `Email/query` and the final `Email/get`
    of the same batch could produce an empty scope, and a row with nothing
    to show is better skipped than crashed on or rendered blank.
    """
    if not emails:
        return None

    oldest_to_newest = sorted(emails, key=lambda e: e.received_at)
    scoped = [email for email in oldest_to_newest if in_scope(email)]
    if not scoped:
        return None

    latest = scoped[-1]
    mailbox_ids: set[str] = set()
    for email in scoped:
        mailbox_ids |= email.mailbox_ids

    return ThreadRow(
        thread_id=thread_id,
        email_ids=[e.id for e in oldest_to_newest],
        latest_email_id=latest.id,
        senders=format_senders(scoped, me=me),
        count=len(scoped),
        subject=latest.subject or _NO_SUBJECT,
        preview=latest.preview,
        date_display=format_date(latest.received_at, now),
        date_full=format_full(latest.received_at, now),
        received_at=latest.received_at,
        unread=any("$seen" not in email.keywords for email in scoped),
        starred=any("$flagged" in email.keywords for email in scoped),
        has_attachment=any(email.has_attachment for email in scoped),
        chips=_chips_for(mailbox_ids, nav),
    )


def _spam_and_trash_ids(nav: NavModel) -> set[str]:
    ids = {resolve_mailbox(nav, "spam"), resolve_mailbox(nav, "trash")}
    return {mailbox_id for mailbox_id in ids if mailbox_id is not None}


def _in_mailbox_scope(mailbox_id: str) -> InScope:
    """Mirrors the JMAP `inMailbox` condition."""

    def in_scope(email: EmailHeader) -> bool:
        return mailbox_id in email.mailbox_ids

    return in_scope


def _other_than_scope(exclude_mailbox_ids: set[str], *, keyword: str | None = None) -> InScope:
    """Mirrors `inMailboxOtherThan` (optionally ANDed with `hasKeyword`).

    RFC 8621 §4.4.1 defines `inMailboxOtherThan` as "an Email must be in at
    least one Mailbox *not* in this list", which is subtly different from
    "not in any of these": a message filed in both Inbox and Trash still
    matches, because Inbox is a mailbox other than Trash. This reproduces
    the server's own semantics (`email.mailbox_ids - exclude` non-empty)
    rather than the looser "not in spam/trash" reading, because the entire
    point of a scope predicate is that it agrees with the filter the server
    applied — the two readings differ only for a message in a
    spam/trash mailbox *and* somewhere else, and disagreeing there would
    reintroduce exactly the row/sort mismatch this scoping exists to fix.
    """

    def in_scope(email: EmailHeader) -> bool:
        if keyword is not None and keyword not in email.keywords:
            return False
        return bool(email.mailbox_ids - exclude_mailbox_ids)

    return in_scope


async def build_page(
    client: JmapClient,
    *,
    mailbox_key: str,
    nav: NavModel,
    position: int,
    limit: int,
    me: str,
    now: datetime,
) -> ThreadPage:
    """Fetch and shape one 50-per-page (spec §5.3) slice of a mailbox view
    — one batched HTTP request (`JmapClient.query_page`).

    `mailbox_key` maps to a JMAP filter per design spec §5.2's nav keys —
    the mapping the Task 6 brief itself specifies:
      - "starred": no real backing mailbox (`mailbox_id=None`) — instead
        `hasKeyword="$flagged"`, excluding Spam/Trash (a message flagged
        while sitting in one of those shouldn't surface in Starred).
      - "all" (All mail): also no real mailbox — every message except
        Spam/Trash.
      - anything else (inbox/sent/drafts/archive/spam/trash, or a raw
        label mailbox id such as one clicked in the sidebar):
        `resolve_mailbox(nav, mailbox_key)` resolves straight to an
        `inMailbox` target.

    Each branch builds its JMAP filter and its `InScope` predicate in the
    same breath, deliberately: `_row_for_thread` aggregates a row over only
    the messages that predicate accepts, and it is correct exactly when it
    agrees with the filter the server applied. Keeping the two adjacent is
    what stops them drifting apart — see `_row_for_thread` for what goes
    wrong when a row is computed over the whole thread instead.

    One guard on that last branch: `resolve_mailbox` returns `None` for a
    reserved key whose role mailbox the account never provisioned (see
    `mailbox_tree._role_mailbox`'s deliberate leniency — a missing Archive
    degrades the nav item instead of raising). Passing that `None` straight
    through to `query_page` would drop `inMailbox` from the filter
    entirely and silently return *every message in the account* under an
    "Archive" heading. An unprovisioned mailbox contains nothing, so this
    returns an empty page — and makes no HTTP request at all — instead.
    "starred"/"all" are the only keys for which a `None` mailbox id is
    meaningful, and both are already handled above.
    """
    if mailbox_key == "starred":
        excluded = _spam_and_trash_ids(nav)
        in_scope = _other_than_scope(excluded, keyword="$flagged")
        query = await client.query_page(
            mailbox_id=None,
            position=position,
            limit=limit,
            exclude_mailbox_ids=excluded,
            has_keyword="$flagged",
        )
    elif mailbox_key == "all":
        excluded = _spam_and_trash_ids(nav)
        in_scope = _other_than_scope(excluded)
        query = await client.query_page(
            mailbox_id=None,
            position=position,
            limit=limit,
            exclude_mailbox_ids=excluded,
        )
    else:
        mailbox_id = resolve_mailbox(nav, mailbox_key)
        if mailbox_id is None:
            return ThreadPage(rows=[], position=position, limit=limit, total=0, next_position=None)
        in_scope = _in_mailbox_scope(mailbox_id)
        query = await client.query_page(mailbox_id=mailbox_id, position=position, limit=limit)

    rows: list[ThreadRow] = []
    for thread_id in query.thread_order:
        row = _row_for_thread(
            thread_id, query.emails_by_thread.get(thread_id, []), nav, me, now, in_scope
        )
        if row is not None:
            rows.append(row)

    next_position = query.position + limit if query.position + limit < query.total else None

    return ThreadPage(
        rows=rows,
        position=query.position,
        limit=limit,
        total=query.total,
        next_position=next_position,
    )
