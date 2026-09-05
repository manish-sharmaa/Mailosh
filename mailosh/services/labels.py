"""Labels (design spec §10) — "Labels = JMAP mailboxes": create, rename,
nest and delete over `Mailbox/set`, plus the two selection operations the
picker (`l`) and Move (`v`) run, over `Email/set`.

Two rules shape everything here, and both are about not losing mail.

**A label is a mailbox, and a mailbox is not a copy of anything.** The name
and the nesting live in JMAP and nowhere else; Postgres holds only colour,
visibility and pinned order (`mailosh.db.models.LabelMeta`). So every write
in this module is a JMAP write, and the only thing `mailosh.web.labels`
saves to the database is display metadata for a mailbox that already exists.

**Deleting a label deletes no mail.** `JmapClient.destroy_mailbox` defaults
`onDestroyRemoveEmails` to `False` and this module never passes anything
else — not on a retry, not behind a flag. Verified against Stalwart 0.16.20
rather than assumed from the RFC's wording: with the flag false a destroy is
refused with `mailboxHasEmail` while the mailbox holds *any* message, not
merely while one lives only there. That is why `delete_label` below empties
the mailbox first, through the ordinary "remove this label from these
messages" path every other caller uses, and only then destroys it. A message
that would be left in no mailbox at all by that removal goes to Archive
(RFC 8621 §4.1 forbids an empty `mailboxIds`, and `mailosh.services.actions.
archive` already had to answer exactly this question) — so the worst thing
deleting a label can do to a message is move it to Archive, and the reader
is told so before it happens *and* told how many afterwards.

---------------------------------------------------------------------
Why this imports three private names from `mailosh.services.actions`

`_Change`, `_write` and `_result` are how the six triage actions batch a
selection into one `Email/set` and report the result (undo spec, removed
rows, nav badge deltas). Applying labels is the same operation with a
different `after` set, so it goes through the same three functions rather
than a second implementation that could batch differently, count badges
differently, or build an undo token the reader's `z` key then could not
reverse. The alternative was copying ~60 lines and letting them drift.

That also settles whether labelling is undoable: **it is**, for free.
`_result` records each message's previous mailbox membership in the undo
spec's `prev`, and `mailosh.services.undo.apply` restores exactly that — so
`z` after "Labelled Work" takes Work back off, and `z` after a Move puts the
conversation back in every mailbox it came from rather than guessing. Only
`delete_label` is *not* undoable, and it cannot be: destroying a mailbox
destroys the id every restore would have to name. That is what the
confirmation is for.

---------------------------------------------------------------------
Turning a JMAP refusal into a sentence

`Mailbox/set` rejects four things this UI can genuinely provoke: a duplicate
sibling name, a `parentId` that would make a label its own ancestor (RFC
8621 §2 requires the server to refuse the cycle — `JmapClient.update_mailbox`
deliberately does not re-derive that check locally, where it could disagree
with the server), destroying a label that still has children, and destroying
one that still holds mail. All four arrive as a `JmapError` whose message
embeds the SetError dict, which is the only structured thing this layer has
to work with (`mailosh.jmap.client` is not this task's to change). `explain`
matches on the SetError *type* token appearing in that message and answers
with copy a reader can act on; anything it does not recognise keeps a
generic sentence rather than putting a raw JMAP error string on screen.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Mailbox
from mailosh.services.actions import ActionResult, _Change, _result, _write
from mailosh.services.mailbox_tree import NavModel, ensure_role_mailbox, resolve_mailbox

__all__ = [
    "DeletePlan",
    "LabelError",
    "PartialApply",
    "apply_labels",
    "clean_name",
    "create_label",
    "delete_label",
    "explain",
    "move_to",
    "nest_label",
    "rename_label",
    "require_label",
    "selection_state",
]

#: Longest label name this app accepts. RFC 8621 sets no limit and Stalwart
#: enforces none, so this is Mailosh's own: a name has to fit a 224 px nav
#: row and a chip on a list row, and a thousand-character mailbox name is a
#: denial-of-service on every future render of that nav rather than a label.
MAX_NAME = 100

#: How many drain passes `delete_label` will make before giving up. Each
#: pass strips a page of conversations out of the label, so a label with
#: fewer than `_DRAIN_PAGE` times this many conversations always finishes; the
#: cap exists so that a server which somehow keeps reporting the same
#: messages cannot spin here forever.
_DRAIN_PASSES = 40

#: Conversations per drain pass. `JmapClient.query_search` collapses
#: threads, so this is a page of *conversations* and the messages it hands
#: back are every message of each — which is exactly what has to be
#: unlabelled, and more than one page's worth of ids per request.
_DRAIN_PAGE = 100

#: Characters no label name may contain. C0/C1 controls (including the
#: newline that would break a `Mailbox/set` name into two lines in every log
#: it appears in) and the Unicode line/paragraph separators.
_CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")

#: SetError `type` -> the sentence a reader gets, in the order they are
#: tried. `{name}` is filled with the label the request was about, already
#: escaped by Jinja wherever this is rendered.
#:
#: Matched as a substring of the `JmapError` message rather than parsed out
#: of it: the client formats the SetError dict with `!r`, so the type token
#: appears verbatim, and every one of these names is distinctive enough that
#: a false positive would need a mailbox literally called "alreadyExists".
#: A repr is a shape to read loosely, not a wire format to parse strictly.
_EXPLANATIONS: tuple[tuple[str, str], ...] = (
    ("alreadyExists", "There's already a label called “{name}” here."),
    ("mailboxHasChild", "“{name}” has labels nested inside it. Delete those first."),
    # Reachable only if something put mail back into the label between the
    # drain and the destroy — a second client filing a message mid-delete.
    # Said plainly, and nothing retries with `onDestroyRemoveEmails`.
    ("mailboxHasEmail", "“{name}” still has mail in it. Try again."),
    ("invalidProperties", "A label can't be nested inside itself."),
    ("notFound", "“{name}” no longer exists — it may have been deleted elsewhere."),
    ("forbidden", "You don't have permission to change “{name}”."),
    ("overQuota", "There's no room for another label on this account."),
)

#: What `explain` says about a refusal it does not recognise. Deliberately
#: not the JMAP error text: a reader can do nothing with `{'type':
#: 'invalidArguments', ...}`, and putting server internals on screen is how
#: an error message becomes an information leak.
_UNEXPLAINED = "Couldn't update “{name}”."


class LabelError(Exception):
    """A refusal with copy a reader can act on.

    Carries a finished sentence, not a code: every one of them is built here
    (or by `explain`) from a server refusal that has already happened, and
    the route's only job is to put it in a toast. `mailosh.web.labels` turns
    this into a `200` with an `om:error` trigger — the same shape the app's
    own `JmapError` handler uses — so the client's one failure path handles
    both without knowing which layer said no.
    """


@dataclass(frozen=True)
class PartialApply(Exception):
    """Some of a multi-apply landed and some did not.

    `Email/set` reports per-message failures in `notUpdated` alongside an
    otherwise-successful response, and `JmapClient.set_mailboxes_patch`
    raises on the first one it finds — so a rejected id in the middle of a
    twenty-conversation apply leaves the other nineteen genuinely changed.
    Reporting that as a flat failure would be a lie in the safer-sounding
    direction: the reader would press the same button again, on a selection
    that is already half-applied.

    So `apply_labels` re-reads the selection after a failed write and counts
    what actually landed. `applied`/`total` are messages, and the route
    turns them into "Labelled 12 of 20 messages" plus a list refresh, which
    is the only honest thing to say.
    """

    applied: int
    total: int

    def __str__(self) -> str:  # pragma: no cover - exercised through the route
        return f"applied to {self.applied} of {self.total} messages"


@dataclass(frozen=True)
class DeletePlan:
    """What deleting one label is about to do, for the confirmation.

    `messages` is `Mailbox.total_emails` — the label's own message count,
    already on the mailbox this nav render fetched, so building this costs
    no extra request at all. `children` names the labels nested inside it,
    because that is the one case where the answer is "you can't yet, here's
    why" rather than a confirmation.

    Deliberately *not* "how many messages live only in this label". That
    number cannot be had honestly at this layer: `query_search` collapses
    threads, so it would count a conversation as filed elsewhere whenever
    any of its messages is, and a confirmation that under-reports how much
    is about to move is worse than one that states the rule instead. The
    rule is exact and short — nothing is deleted; a message with no other
    mailbox moves to Archive — and `delete_label` reports the true count
    afterwards, from the per-message patches it actually made.
    """

    mailbox_id: str
    name: str
    messages: int
    children: list[str]

    @property
    def blocked(self) -> bool:
        return bool(self.children)


@dataclass(frozen=True)
class DeleteOutcome:
    """What deleting one label actually did — the toast's raw material.

    `unlabelled` is how many messages lost the label and kept their other
    mailboxes; `archived` is how many had none and were moved to Archive
    instead of being left in no mailbox at all.
    """

    name: str
    unlabelled: int
    archived: int


def clean_name(raw: str) -> str:
    """Normalise a label name, or raise `LabelError` saying why not.

    NFC first, so two spellings of the same accented name are one name and
    the server's own duplicate check can see them as such. Then control
    characters are refused outright (not stripped — silently accepting a
    name the reader did not type is how a label ends up called something
    nobody can find), surrounding whitespace is trimmed, and internal runs
    collapse to single spaces so `"Work   stuff"` and `"Work stuff"` are not
    two labels a reader has to tell apart by eye.

    Nesting is expressed with `parentId`, never with a separator inside the
    name, so `/` is left alone: a label may legitimately be called
    "Legal/Finance" without becoming two labels.
    """
    name = unicodedata.normalize("NFC", raw)
    if _CONTROL.search(name):
        raise LabelError("A label name can't contain line breaks or control characters.")
    name = " ".join(name.split())
    if not name:
        raise LabelError("Give the label a name.")
    if len(name) > MAX_NAME:
        raise LabelError(f"Label names are limited to {MAX_NAME} characters.")
    return name


def explain(exc: JmapError, *, name: str) -> LabelError:
    """A `JmapError` from `Mailbox/set`, as a sentence about `name`.

    See this module's docstring for why the SetError type is matched as a
    substring. An unrecognised refusal keeps `_UNEXPLAINED` rather than
    surfacing the raw error, and the original is chained (`from exc`) by the
    caller so the real reason is still in the logs.
    """
    message = str(exc)
    for token, copy in _EXPLANATIONS:
        if token in message:
            return LabelError(copy.format(name=name))
    return LabelError(_UNEXPLAINED.format(name=name))


def _label_mailboxes(mailboxes: list[Mailbox]) -> list[Mailbox]:
    """Every mailbox acting as a label — i.e. every one with no JMAP role.

    The same test `mailosh.services.mailbox_tree.build_nav` applies, spelled
    out here too because this module has to answer "is this id a label?"
    about ids that arrive in a form field. A role mailbox reaching a rename
    or a delete would be the reader losing their Inbox to a label control.
    """
    return [mailbox for mailbox in mailboxes if mailbox.role is None]


async def require_label(client: JmapClient, mailbox_id: str) -> Mailbox:
    """The `Mailbox` behind a label id, or `LabelError`.

    Two refusals, one message each, and they are different failures: an id
    the account has no mailbox for (stale form, deleted elsewhere), and an
    id that resolves to a *system* mailbox. Inbox, Sent, Drafts, Archive,
    Spam and Trash are not labels and none of the operations here may touch
    them — renaming Trash or deleting the Inbox is not a thing this UI is
    allowed to express, whatever id a request carries.
    """
    for mailbox in await client.get_mailboxes():
        if mailbox.id != mailbox_id:
            continue
        if mailbox.role is not None:
            raise LabelError(f"“{mailbox.name}” is a system folder, not a label.")
        return mailbox
    raise LabelError("That label no longer exists — it may have been deleted elsewhere.")


async def create_label(client: JmapClient, raw_name: str, *, parent_id: str | None = None) -> str:
    """Create a label and return its mailbox id, optionally nested under
    `parent_id`.

    Two `Mailbox/set` calls when a parent is asked for, and that is a
    property of the client this task may not change:
    `JmapClient.create_mailbox` sends `name` (plus an optional `role`) and
    nothing else, so the nesting is a follow-up `update_mailbox`. The order
    matters — a create that succeeds and a nest that fails leaves a real,
    usable label at the top level rather than nothing at all, which is the
    better of the two half-states — and the nest failure is still reported,
    so the reader is told where the label actually went.
    """
    name = clean_name(raw_name)
    if parent_id is not None:
        await require_label(client, parent_id)
    try:
        mailbox_id = await client.create_mailbox(name)
    except JmapError as exc:
        raise explain(exc, name=name) from exc
    if parent_id is None:
        return mailbox_id
    try:
        await client.update_mailbox(mailbox_id, {"parentId": parent_id})
    except JmapError as exc:
        raise LabelError(f"Created “{name}”, but couldn't nest it. It's at the top level.") from exc
    return mailbox_id


async def rename_label(client: JmapClient, mailbox_id: str, raw_name: str) -> str:
    """Rename a label. Returns the cleaned name that was actually written,
    so a caller's toast says what the label is now called rather than what
    was typed.
    """
    mailbox = await require_label(client, mailbox_id)
    name = clean_name(raw_name)
    if name == mailbox.name:
        return name
    try:
        await client.update_mailbox(mailbox_id, {"name": name})
    except JmapError as exc:
        raise explain(exc, name=name) from exc
    return name


async def nest_label(client: JmapClient, mailbox_id: str, parent_id: str | None) -> None:
    """Move a label under `parent_id`, or back to the top level with `None`.

    The cycle check is the server's, on purpose. RFC 8621 §2 requires it to
    refuse a `parentId` inside the mailbox's own subtree, and
    `JmapClient.update_mailbox`'s docstring says plainly that it does not
    re-derive that locally — a second implementation here could disagree
    with the server about what a cycle is, and the disagreement would show
    up as either a label the UI refuses to move for no reason a reader can
    see, or a 500 on a move the UI thought was fine. What this layer owns is
    the *sentence*: Stalwart answers `invalidProperties` / "Mailbox cannot
    be a parent of itself", and `explain` turns that into "A label can't be
    nested inside itself."

    The one check that *is* local is the cheap, unambiguous one — a label
    cannot be its own parent — because that request never needs to reach the
    server to be wrong.
    """
    mailbox = await require_label(client, mailbox_id)
    if parent_id == mailbox_id:
        raise LabelError("A label can't be nested inside itself.")
    if parent_id is not None:
        await require_label(client, parent_id)
    try:
        await client.update_mailbox(mailbox_id, {"parentId": parent_id})
    except JmapError as exc:
        raise explain(exc, name=mailbox.name) from exc


async def delete_plan(client: JmapClient, mailbox_id: str) -> DeletePlan:
    """What deleting `mailbox_id` would do — one `Mailbox/get`, which is the
    same request every nav render already makes.
    """
    mailboxes = await client.get_mailboxes()
    mailbox = next((m for m in mailboxes if m.id == mailbox_id), None)
    if mailbox is None:
        raise LabelError("That label no longer exists — it may have been deleted elsewhere.")
    if mailbox.role is not None:
        raise LabelError(f"“{mailbox.name}” is a system folder, not a label.")
    children = sorted(m.name for m in _label_mailboxes(mailboxes) if m.parent_id == mailbox_id)
    return DeletePlan(
        mailbox_id=mailbox_id,
        name=mailbox.name,
        messages=mailbox.total_emails,
        children=children,
    )


async def _drain(client: JmapClient, nav: NavModel, mailbox_id: str) -> tuple[int, int]:
    """Take `mailbox_id` off every message that carries it, moving anything
    that would be left in no mailbox at all into Archive.

    Returns `(unlabelled, archived)` in messages.

    Paged, and always from `position=0`: each pass removes exactly the
    messages it just saw, so the next page of "still in this label" is
    always at the start of the query. Advancing the position instead would
    skip everything shifted forward by the pass before it — the classic
    way to leave a drain half-done and then destroy the mailbox on top of
    it.

    Archive is resolved (and created if this account never had one) only
    when a message genuinely needs it, exactly as `mailosh.services.actions.
    archive` does, so deleting a label full of well-filed mail creates no
    folder.
    """
    archive_id = resolve_mailbox(nav, "archive")
    unlabelled = 0
    archived = 0
    for _ in range(_DRAIN_PASSES):
        page = await client.query_search(
            filter={"inMailbox": mailbox_id}, position=0, limit=_DRAIN_PAGE
        )
        headers = [
            header
            for thread in page.emails_by_thread.values()
            for header in thread
            if mailbox_id in header.mailbox_ids
        ]
        if not headers:
            return unlabelled, archived
        homeless = [h for h in headers if h.mailbox_ids == {mailbox_id}]
        if homeless and archive_id is None:
            archive_id = await ensure_role_mailbox(client, "archive")
        patches: dict[str, dict[str, bool | None]] = {}
        for header in headers:
            patch: dict[str, bool | None] = {mailbox_id: None}
            if header.mailbox_ids == {mailbox_id}:
                assert archive_id is not None, "a homeless message with no Archive to go to"
                patch[archive_id] = True
                archived += 1
            else:
                unlabelled += 1
            patches[header.id] = patch
        await client.set_mailboxes_patch(patches)
    raise LabelError("That label has too much mail to remove in one go. Try again.")


async def delete_label(client: JmapClient, nav: NavModel, mailbox_id: str) -> DeleteOutcome:
    """Delete a label without deleting any mail (design spec §10:
    "conversations keep their other labels; confirm").

    Three steps, in this order and for these reasons:

    1. **Refuse a parent.** RFC 8621's `mailboxHasChild` would refuse it
       anyway; catching it here means the reader is told which labels are in
       the way before anything has been touched, rather than after a drain
       that then could not finish.
    2. **Empty it** (`_drain`), through the same `Email/set` patch shape
       every other operation in this module uses. This is what makes the
       destroy legal, and it is the whole of "deleting a label never deletes
       mail": messages lose one mailbox, keep every other, and the ones with
       none left go to Archive.
    3. **Destroy it**, with `on_destroy_remove_emails` left at its default
       `False`. Never `True` — not here, not as a retry, not as an escape
       hatch for a drain that did not finish. If the destroy is still
       refused (a second client filed something into the label between steps
       2 and 3), that refusal is surfaced and the label survives with its
       mail intact, which is the correct direction to fail in.

    Not undoable, and it cannot be: the id every restore would name is gone
    the moment step 3 succeeds. The confirmation is the safeguard, and it is
    why `delete_plan` exists.
    """
    plan = await delete_plan(client, mailbox_id)
    if plan.blocked:
        raise LabelError(f"“{plan.name}” has labels nested inside it. Delete those first.")
    unlabelled, archived = await _drain(client, nav, mailbox_id)
    try:
        await client.destroy_mailbox(mailbox_id)
    except JmapError as exc:
        raise explain(exc, name=plan.name) from exc
    return DeleteOutcome(name=plan.name, unlabelled=unlabelled, archived=archived)


def _toast(add: list[str], remove: list[str], names: dict[str, str]) -> str:
    """The past-tense sentence for one apply (design spec §6.3: "the toast
    names what happened").

    One label in, one label out and the mixed case each get their own
    wording, because "Labels updated" for a single click on a single label
    is the kind of vague confirmation that makes a reader re-check the row.
    """
    if len(add) == 1 and not remove:
        return f"Labelled “{names.get(add[0], 'label')}”"
    if len(remove) == 1 and not add:
        return f"Removed from “{names.get(remove[0], 'label')}”"
    return "Labels updated"


async def _landed(client: JmapClient, changes: list[_Change]) -> int:
    """How many of `changes` actually took effect, re-read from the server.

    Only ever called after a write has already failed, so the extra
    `Email/get` costs nothing on the path that works. Compares against each
    change's intended `after` rather than "did anything change", because a
    message half-patched (one label added, another not) is not applied.
    """
    wanted = {change.before.id: change.after for change in changes}
    states = await client.get_email_states(list(wanted))
    return sum(1 for state in states if state.mailbox_ids == wanted[state.id])


async def apply_labels(
    client: JmapClient,
    nav: NavModel,
    email_ids: list[str],
    *,
    add: list[str],
    remove: list[str],
    names: dict[str, str] | None = None,
) -> ActionResult:
    """Add and/or remove several labels across a whole selection, in **two
    JMAP requests total** — one snapshot, one write — however many labels
    and however many conversations.

    That is the property that matters: two labels applied to twenty
    conversations is not forty round trips and not even two writes, it is
    one `Email/set` carrying twenty per-message patches, because
    `mailosh.services.actions._write` was already built to do exactly that
    for a bulk archive. Adds and removes ride in the same patch, so
    "check Work, uncheck Receipts, apply" is also one write.

    A label removal that would leave a message in no mailbox at all sends it
    to Archive instead — the same rule, and the same reason (RFC 8621 §4.1),
    as archiving an Inbox-only message. Unchecking the only label a message
    has is a real gesture with a real answer; refusing it, or letting the
    write be rejected, is not.

    Raises `PartialApply` when the write is rejected part-way, carrying how
    many messages actually ended up in the state that was asked for. See
    that class for why a flat failure would be the more dangerous answer.
    """
    add_ids = list(dict.fromkeys(add))
    remove_ids = list(dict.fromkeys(remove))
    if not add_ids and not remove_ids:
        raise LabelError("Pick at least one label.")
    overlap = set(add_ids) & set(remove_ids)
    if overlap:
        raise LabelError("A label can't be added and removed at the same time.")

    states = await client.get_email_states(email_ids)
    adding, removing = frozenset(add_ids), frozenset(remove_ids)
    archive_id = resolve_mailbox(nav, "archive")
    afters = [(state, (state.mailbox_ids | adding) - removing) for state in states]
    if any(not after for _state, after in afters) and archive_id is None:
        archive_id = await ensure_role_mailbox(client, "archive")

    changes: list[_Change] = []
    for state, after in afters:
        if not after:
            assert archive_id is not None, "a homeless message with no Archive to go to"
            after = frozenset({archive_id})
        changes.append(_Change(before=state, after=after))

    try:
        await _write(client, changes)
    except JmapError as exc:
        acted = [change for change in changes if change.changed]
        raise PartialApply(applied=await _landed(client, acted), total=len(acted)) from exc

    return _result(
        "label",
        _toast(add_ids, remove_ids, names or {}),
        nav,
        changes,
        # A label change never takes a row out of the list *the server can
        # know about*: whether unlabelling drops a row depends on which
        # mailbox is open, and only the client knows that — the same reason
        # `star`/`mark_read` pass False here.
        removes_rows=False,
    )


async def move_to(
    client: JmapClient, nav: NavModel, email_ids: list[str], target_id: str, *, name: str
) -> ActionResult:
    """Move a selection into one mailbox and out of every other — spec
    §6.1's `v`, "move to…", a single choice rather than the picker's
    checkboxes.

    Replacement, not addition, which is what makes it a *move*: the shape
    `mailosh.services.actions.delete` already uses for Trash, pointed at an
    arbitrary mailbox instead. The mailboxes each message leaves are
    recorded in the undo spec's `prev`, so `z` puts every one of them back
    rather than dropping the conversation into the Inbox.

    Rows do leave the list here (unlike `apply_labels`): a move takes the
    conversation out of whatever mailbox is on screen by definition, unless
    the reader moved it into the mailbox they are already looking at — which
    the server cannot tell, so the honest answer is to name the threads and
    let the client reconcile, exactly as archive/delete/spam do.
    """
    states = await client.get_email_states(email_ids)
    changes = [_Change(before=state, after=frozenset({target_id})) for state in states]
    try:
        await _write(client, changes)
    except JmapError as exc:
        acted = [change for change in changes if change.changed]
        raise PartialApply(applied=await _landed(client, acted), total=len(acted)) from exc
    return _result("move", f"Moved to “{name}”", nav, changes, removes_rows=True)


def selection_state(states: list, mailbox_id: str) -> str:
    """`"on"`, `"mixed"` or `"off"` — whether every, some or none of a
    selection's messages carry `mailbox_id`.

    The picker's checkboxes are tri-state for the same reason Gmail's are:
    with three conversations selected and one of them filed under Work, a
    plain checked box would claim something untrue and clicking it would do
    something surprising. An empty selection reads `"off"` — nothing carries
    the label, and the box is the thing that will apply it.
    """
    if not states:
        return "off"
    carrying = sum(1 for state in states if mailbox_id in state.mailbox_ids)
    if carrying == 0:
        return "off"
    return "on" if carrying == len(states) else "mixed"
