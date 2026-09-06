"""The mail actions (design spec §6.3) — archive, delete, report spam, star,
mark read, and the Trash/Spam half: restore, not-spam, delete forever and
empty — as JMAP-correct operations over a selection of messages, plus
everything the UI needs to reconcile itself afterwards.

Every action follows the same three beats:

1. **Snapshot** (`JmapClient.get_email_states`, one `Email/get`): where each
   selected message currently sits. Nothing here can be guessed from the id
   alone — whether archiving would leave a message in no mailbox at all,
   which mailboxes to restore on undo, which thread rows disappear, and
   whether a nav badge moves all depend on the *current* placement.
2. **Write** (one `Email/set`): per-id patches, never one call per message.
   A 200-message bulk archive is one round trip, and the "remove Inbox, but
   add Archive only where that would otherwise leave nothing" rule is exactly
   why `set_mailboxes_patch` exists alongside `set_mailboxes`.
3. **Report** (`ActionResult`): the `UndoSpec` to sign, the thread ids whose
   rows leave the list, and the nav badge deltas.

`ActionResult` is wider than the Phase 1A plan's `-> UndoSpec` because the
route contract in the same plan needs `removed` and `counts` in its
`HX-Trigger` payload, and both are computed from the snapshot this module
already holds — recomputing them in the route would mean a second
`Email/get`. `nav` is likewise threaded through `star`/`mark_read` (the plan
shows those two without it) so marking a message read can move the Inbox's
unread badge, which is the whole point of that badge.

Nothing here catches `JmapError`/`TransportError`: a failed action must reach
the app's error handler and become a revert + error toast, never a silent
success (design spec §6.3, "failures revert and toast").
"""

from __future__ import annotations

from dataclasses import dataclass

from mailosh.jmap.client import EmailState, JmapClient
from mailosh.jmap.errors import JmapError
from mailosh.services.mailbox_tree import NavModel, ensure_role_mailbox, resolve_mailbox
from mailosh.services.undo import UndoSpec
from mailosh.services.undo import apply as apply_undo

__all__ = [
    "EMPTYABLE",
    "ActionResult",
    "apply_undo",
    "archive",
    "delete",
    "destroy",
    "empty_mailbox",
    "mark_read",
    "not_spam",
    "restore",
    "spam",
    "star",
]

#: The two nav keys `empty_mailbox` will empty, and the only two. Both hold
#: mail the reader has already given up on; anything else — the Inbox, a
#: label — is a place mail *lives*, and a route that could destroy one of
#: those with a single POST is not a feature.
EMPTYABLE = frozenset({"trash", "spam"})

#: Threads per `Email/query` page while emptying a mailbox. A page is one
#: round trip, and Trash on a busy account can hold thousands of threads,
#: so this is sized to finish in a handful of them rather than one per
#: screenful.
_EMPTY_PAGE = 200

#: RFC 8621 §4.1.1 keywords this module sets. `$seen`/`$flagged` are the two
#: the UI toggles directly; `$junk` rides along with a move to the Junk
#: mailbox so a server-side spam classifier has the signal too.
SEEN = "$seen"
FLAGGED = "$flagged"
JUNK = "$junk"


@dataclass(frozen=True)
class ActionResult:
    """What one action did, in the three shapes the UI needs.

    `spec` is signed into the undo token; `removed` lists the thread ids whose
    rows leave the current list (empty for star/read, which never remove a
    row — whether unstarring drops a row depends on which view is open, and
    only the client knows that); `counts` holds nav badge deltas, keyed by nav
    key, and omits any badge that did not move.

    `undoable` is false for the two actions that *destroy* mail (`destroy`,
    `empty_mailbox`): there is no previous placement to put back, so their
    `spec` carries no ids and the route must say so — not with the "nothing
    changed" code an empty spec would otherwise earn, which would be a lie
    beside a toast reading "Deleted forever".
    """

    spec: UndoSpec
    removed: list[str]
    counts: dict[str, int]
    undoable: bool = True


@dataclass(frozen=True)
class _Change:
    """One message's before/after, from which every part of `ActionResult`
    (and the `Email/set` patch itself) is derived — so the five actions differ
    only in how they compute `after`, never in how the result is assembled.
    """

    before: EmailState
    after: frozenset[str]
    keyword: str | None = None
    on: bool | None = None

    @property
    def moved(self) -> bool:
        return self.after != self.before.mailbox_ids

    @property
    def flagged_change(self) -> bool:
        return self.keyword is not None and (self.keyword in self.before.keywords) is not self.on

    @property
    def changed(self) -> bool:
        return self.moved or self.flagged_change

    @property
    def unread_before(self) -> bool:
        return SEEN not in self.before.keywords

    @property
    def unread_after(self) -> bool:
        if self.keyword == SEEN and self.on is not None:
            return not self.on
        return self.unread_before

    def patch(self) -> dict[str, bool | None]:
        """This message's mailbox patch: add what it gained, remove what it
        lost (`sorted` purely so the wire body is deterministic).
        """
        patch: dict[str, bool | None] = {
            mid: True for mid in sorted(self.after - self.before.mailbox_ids)
        }
        patch.update({mid: None for mid in sorted(self.before.mailbox_ids - self.after)})
        return patch


def _require(nav: NavModel, key: str, what: str) -> str:
    """The mailbox id behind a nav key, or `JmapError`.

    `mailosh.services.mailbox_tree` deliberately degrades to "no link" when a
    role mailbox is missing, which is right for *rendering* a nav. An action
    cannot degrade the same way: quietly not moving a message the user asked
    to move is worse than a visible failure, and for archive it would mean
    leaving a message in no mailbox at all (invalid per RFC 8621 §4.1).
    """
    mailbox_id = resolve_mailbox(nav, key)
    if mailbox_id is None:
        raise JmapError(f"this account has no mailbox with role={key!r}, so {what} is impossible")
    return mailbox_id


async def _snapshot(client: JmapClient, email_ids: list[str]) -> list[EmailState]:
    """Current placement of every id the server still knows (see
    `JmapClient.get_email_states` for why unknown ids are dropped, not raised
    on).
    """
    return await client.get_email_states(email_ids)


async def _write(client: JmapClient, changes: list[_Change]) -> None:
    """Commit every change in ONE `Email/set` (or none at all).

    Mailbox-moving actions go through `set_mailboxes_patch`, which carries a
    per-id patch plus any keyword rider (spam's `$junk`) in a single update.
    A keyword-only action (star, mark read) has no mailbox patch to make, so
    it uses `set_keywords` instead — the same wire body, via the client method
    that already exists for it.

    The one invariant every action shares is enforced here rather than in
    each of them: RFC 8621 §4.1 gives an Email a non-empty `mailboxIds`, so
    no patch may empty it. Archive is the only action that could ever come
    close (it *subtracts* a mailbox; delete/spam replace, star/read don't
    touch membership), and it resolves an Archive mailbox — creating one if
    the account has none — before it reaches this point. Checking at the
    single write choke point means a future action cannot reintroduce the
    hole by forgetting the rule, and the failure is loud and pre-write
    rather than a rejected `Email/set` or, worse, a message no view can
    reach.
    """
    acted = [change for change in changes if change.changed]
    if not acted:
        return
    for change in acted:
        if not change.after:
            raise JmapError(
                f"refusing to leave {change.before.id!r} in no mailbox at all "
                f"(RFC 8621 §4.1 requires a non-empty mailboxIds)"
            )
    # Every `_Change` an action builds carries that action's own single
    # keyword (or none), so the first is the whole story.
    keyword, on = acted[0].keyword, acted[0].on
    patches = {change.before.id: change.patch() for change in acted}
    if any(patches.values()):
        keywords = {keyword: on} if keyword is not None and on is not None else None
        await client.set_mailboxes_patch(patches, keywords=keywords)
    elif keyword is not None and on is not None:
        await client.set_keywords([change.before.id for change in acted], keyword, on)


def _counts(nav: NavModel, changes: list[_Change]) -> dict[str, int]:
    """Nav badge deltas, for the two badges the nav actually renders (Task 6's
    `_SYSTEM_SPEC`): Inbox counts *unread messages*, Drafts counts *total*
    messages.

    Per message, not per thread, which is what `Mailbox.unreadEmails`/
    `totalEmails` themselves count — so a thread with messages spread across
    Inbox and a label moves the badge by exactly the number of its unread
    inbox messages, not by one per row. A message that was already read, or
    was never in the Inbox to begin with, moves nothing.
    """
    inbox_id = nav.inbox_id
    drafts_id = resolve_mailbox(nav, "drafts")
    inbox = sum(
        int(change.unread_after and inbox_id in change.after)
        - int(change.unread_before and inbox_id in change.before.mailbox_ids)
        for change in changes
    )
    drafts = (
        sum(
            int(drafts_id in change.after) - int(drafts_id in change.before.mailbox_ids)
            for change in changes
        )
        if drafts_id is not None
        else 0
    )
    return {key: delta for key, delta in (("inbox", inbox), ("drafts", drafts)) if delta}


def _result(
    kind: str,
    toast: str,
    nav: NavModel,
    changes: list[_Change],
    *,
    removes_rows: bool,
    keyword: str | None = None,
    on: bool | None = None,
    undoable: bool = True,
) -> ActionResult:
    """Assemble the `ActionResult`: undo spec, removed rows, badge deltas.

    `undoable=False` empties the spec on purpose: a destroyed message has no
    `prev` to restore and must never reach `undo.apply`, so the token the
    route would sign is made worthless here rather than trusted to be
    unused.

    `spec.email_ids` holds only the messages that actually changed — undoing
    "star" must not unstar a message that was already starred before the user
    pressed `s`. `spec.prev` holds only the ones whose *mailboxes* changed,
    since that is all undo restores; a keyword-only action leaves it empty,
    which is precisely what stops `undo.apply` from trying to "restore" a
    message into no mailbox at all.

    `removed` covers every snapshotted message's thread, not only the changed
    ones: a thread already out of the Inbox still leaves the Inbox list when
    the user archives it, and the row must not linger.

    `moved` implies `changed` for every real `_Change` (`changed` is
    literally `moved or flagged_change`), so `prev`'s keys are always a
    subset of `email_ids` — asserted below, right where the two are built
    from the same `changes` list, which is the one place this invariant is
    established. `mailosh.services.undo.sign` trusts it rather than
    re-checking it, because by the time `sign` runs the write this spec
    describes has already committed; failing here, in testing, is a far
    better outcome than a future action silently breaking the invariant and
    only finding out via a 500 on an otherwise-successful request.
    """
    email_ids = [c.before.id for c in changes if c.changed] if undoable else []
    prev = {c.before.id: sorted(c.before.mailbox_ids) for c in changes if c.moved and undoable}
    assert set(prev) <= set(email_ids), "a message with `prev` must also be in `email_ids`"
    return ActionResult(
        spec=UndoSpec(
            kind=kind,
            email_ids=email_ids,
            prev=prev,
            keyword=keyword,
            on=on,
            toast=toast,
        ),
        removed=(
            list(dict.fromkeys(change.before.thread_id for change in changes))
            if removes_rows
            else []
        ),
        counts=_counts(nav, changes),
        undoable=undoable,
    )


async def archive(client: JmapClient, nav: NavModel, email_ids: list[str]) -> ActionResult:
    """Remove the Inbox — and only the Inbox — from every selected message.

    Gmail's archive is "take it out of the Inbox, keep every label it has", so
    a message filed under Work stays under Work and simply stops being in the
    Inbox. A message whose *only* mailbox is the Inbox would be left in no
    mailbox at all by that rule (invalid in JMAP, and unreachable in any
    view), so it goes to the Archive mailbox instead — and only it: adding
    Archive to messages that already have somewhere to live would litter the
    Archive folder with copies of everything the user has ever filed.

    If the account has no Archive mailbox, one is created (design spec §6.3's
    "add the Archive-role mailbox", on an account that never had it —
    Stalwart's defaults are Inbox/Deleted Items/Junk Mail/Drafts/Sent Items,
    so this is the ordinary case, not an exotic one). Two conditions guard
    that, and both matter:

    * it happens only when a message in *this* selection would otherwise be
      left homeless — an archive of already-labelled mail creates nothing,
      and neither does looking at a mailbox;
    * it happens before the single `Email/set`, so a failure to provide the
      folder is a failure of the whole action rather than a half-applied
      write. Everything that reaches `_write` therefore lands in at least
      one mailbox.
    """
    inbox_id = nav.inbox_id
    states = await _snapshot(client, email_ids)

    def _homeless(state: EmailState) -> bool:
        """Would removing the Inbox leave this message in no mailbox at all
        (as opposed to leaving it exactly where it already was)?"""
        after = state.mailbox_ids - {inbox_id}
        return not after and after != state.mailbox_ids

    archive_id = resolve_mailbox(nav, "archive")
    if archive_id is None and any(_homeless(state) for state in states):
        archive_id = await ensure_role_mailbox(client, "archive")

    changes = []
    for state in states:
        after = state.mailbox_ids - {inbox_id}
        if _homeless(state):
            # Unreachable with `archive_id` still None: the resolve above ran
            # over these same states.
            assert archive_id is not None, "archive has no mailbox for a homeless message"
            after = frozenset({archive_id})
        changes.append(_Change(before=state, after=after))
    await _write(client, changes)
    return _result("archive", "Archived", nav, changes, removes_rows=True)


async def delete(client: JmapClient, nav: NavModel, email_ids: list[str]) -> ActionResult:
    """Move every selected message to Trash, out of everything else.

    Unlike archive this is a replacement, not a subtraction: a deleted message
    is in Trash and nowhere else, so it stops appearing under its labels. The
    mailboxes it leaves are recorded in the undo spec's `prev`, so undo puts
    the labels back too rather than dropping it into the Inbox.
    """
    trash_id = _require(nav, "trash", "deleting")
    changes = [
        _Change(before=state, after=frozenset({trash_id}))
        for state in await _snapshot(client, email_ids)
    ]
    await _write(client, changes)
    return _result("delete", "Deleted", nav, changes, removes_rows=True)


async def spam(client: JmapClient, nav: NavModel, email_ids: list[str]) -> ActionResult:
    """Move every selected message to the Junk mailbox and set `$junk`.

    Same replacement shape as `delete` (spam does not stay filed under its
    labels), plus the RFC 8621 keyword — which is the part a server-side
    classifier can learn from, and which rides inside the same `Email/set`.
    """
    junk_id = _require(nav, "spam", "reporting spam")
    changes = [
        _Change(before=state, after=frozenset({junk_id}), keyword=JUNK, on=True)
        for state in await _snapshot(client, email_ids)
    ]
    await _write(client, changes)
    return _result("spam", "Reported spam", nav, changes, removes_rows=True, keyword=JUNK, on=True)


async def star(
    client: JmapClient, nav: NavModel, email_ids: list[str], *, on: bool
) -> ActionResult:
    """Set or clear `$flagged`. Mailboxes are untouched, so no row is removed
    (a `Starred` view drops the row, an Inbox view keeps it — the client knows
    which view it is showing; the server does not) and no badge moves (the nav
    does not count Starred).
    """
    changes = [
        _Change(before=state, after=state.mailbox_ids, keyword=FLAGGED, on=on)
        for state in await _snapshot(client, email_ids)
    ]
    await _write(client, changes)
    toast = "Starred" if on else "Unstarred"
    return _result("star", toast, nav, changes, removes_rows=False, keyword=FLAGGED, on=on)


async def mark_read(
    client: JmapClient, nav: NavModel, email_ids: list[str], *, on: bool
) -> ActionResult:
    """Set or clear `$seen`, moving the Inbox unread badge by however many of
    the selected messages were actually in the Inbox *and* actually changed
    state.
    """
    changes = [
        _Change(before=state, after=state.mailbox_ids, keyword=SEEN, on=on)
        for state in await _snapshot(client, email_ids)
    ]
    await _write(client, changes)
    toast = "Marked as read" if on else "Marked as unread"
    return _result("read", toast, nav, changes, removes_rows=False, keyword=SEEN, on=on)


async def restore(client: JmapClient, nav: NavModel, email_ids: list[str]) -> ActionResult:
    """Put every selected message back in the Inbox, out of everything else
    — Trash's answer to delete.

    A replacement rather than "remove Trash": a message in Trash has already
    been stripped of its labels by `delete` (or by whatever client trashed
    it), so subtracting Trash alone would leave it in no mailbox at all.
    The Inbox is the one place a restored message is certain to be found
    again. Undo restores the recorded placement (Trash), as for any move.
    """
    inbox_id = nav.inbox_id
    changes = [
        _Change(before=state, after=frozenset({inbox_id}))
        for state in await _snapshot(client, email_ids)
    ]
    await _write(client, changes)
    return _result("restore", "Restored", nav, changes, removes_rows=True)


async def not_spam(client: JmapClient, nav: NavModel, email_ids: list[str]) -> ActionResult:
    """`spam`'s exact reverse: back to the Inbox, and `$junk` cleared in the
    same `Email/set` so a server-side classifier learns it was wrong.

    Undo re-applies `$junk` and returns the message to the recorded
    placement (Junk), the same way undoing a report-spam clears it.
    """
    inbox_id = nav.inbox_id
    changes = [
        _Change(before=state, after=frozenset({inbox_id}), keyword=JUNK, on=False)
        for state in await _snapshot(client, email_ids)
    ]
    await _write(client, changes)
    return _result("unspam", "Not spam", nav, changes, removes_rows=True, keyword=JUNK, on=False)


async def destroy(client: JmapClient, nav: NavModel, email_ids: list[str]) -> ActionResult:
    """Permanently delete every selected message (`Email/set` `destroy`).

    Not a move, so not through `_write` — there is no "after" placement,
    and `_write`'s refusal to empty `mailboxIds` is precisely the guard
    this action has to go around, on purpose and in one place. The
    snapshot is still taken: it is what names the thread rows that leave
    the list and the badges that move (a destroyed unread Inbox message,
    should a client ever aim this outside Trash, still lowers the count).

    `undoable=False`: nothing can bring a destroyed message back, and the
    result says so rather than handing the route an empty spec to sign.
    """
    states = await _snapshot(client, email_ids)
    changes = [_Change(before=state, after=frozenset()) for state in states]
    await client.destroy_emails([state.id for state in states])
    return _result("destroy", "Deleted forever", nav, changes, removes_rows=True, undoable=False)


async def empty_mailbox(client: JmapClient, nav: NavModel, key: str) -> ActionResult:
    """Destroy every message in Trash or Spam — and only those two (see
    `EMPTYABLE`), and only the messages actually *in* that mailbox.

    Paged through `query_page` rather than a single unbounded query: the
    page comes back thread-collapsed with every message of every thread on
    it, and a thread can span mailboxes, so each message is checked for
    membership before it is destroyed. Emptying Trash must not take a
    message's Inbox copy — a thread with one deleted reply and three live
    ones loses exactly the one. Always from position 0: each destroyed page
    slides the next into place, and a page that destroys nothing ends the
    loop so a server that keeps answering the same ids cannot spin it.
    """
    if key not in EMPTYABLE:
        raise ValueError(f"only {sorted(EMPTYABLE)} can be emptied, not {key!r}")
    mailbox_id = _require(nav, key, "emptying")
    label = "Trash" if key == "trash" else "Spam"
    while True:
        page = await client.query_page(mailbox_id=mailbox_id, position=0, limit=_EMPTY_PAGE)
        doomed = [
            header.id
            for headers in page.emails_by_thread.values()
            for header in headers
            if mailbox_id in header.mailbox_ids
        ]
        if not doomed:
            break
        await client.destroy_emails(doomed)
        if len(page.thread_order) < _EMPTY_PAGE:
            break
    return _result("empty", f"{label} emptied", nav, [], removes_rows=False, undoable=False)
