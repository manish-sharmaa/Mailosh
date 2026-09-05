"""The left nav's view model (design spec §5.2): system items (Inbox,
Starred, Sent, Drafts), the "More" disclosure (All mail, Archive, Spam,
Trash), and the user-label tree (colour dots, nesting, unread counts) —
built from one `Mailbox/get` (`JmapClient.get_mailboxes`) plus this user's
`LabelMeta` rows (`mailosh.db.repo.label_meta_map`).

No Snoozed/Important anywhere (design spec §3: deferred features get no UI
control at all, not even a disabled one) — `build_nav`'s `system`/`more`
lists are drawn from exactly the eight items spec §5.2 names, full stop.
An item is dropped from them only when it names a JMAP role this account
has no mailbox for (`_nav_items`), so the nav never offers a folder that
cannot be opened.

This module also owns the reverse direction — key -> mailbox id
(`resolve_mailbox`), and key -> mailbox id *even if that means creating the
mailbox* (`ensure_role_mailbox`) — because the key/role/name vocabulary the
two spec tables below define is the thing both directions read.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from mailosh.db.models import LabelMeta, Visibility
from mailosh.jmap.client import JmapClient, find_inbox
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Mailbox

#: `key` -> (label, icon, JMAP `Mailbox.role`, count source) for every
#: `system`/`more` item, in the exact order spec §5.2 and controller
#: decision 4 require: system = inbox, starred, sent, drafts; more = all,
#: archive, spam, trash. "starred"/"all" have no `role` — Starred is a
#: `hasKeyword: $flagged` virtual view (`resolve_mailbox` returns `None`
#: for it) and All mail is `inMailboxOtherThan` every other role
#: (`mailosh.services.thread_list.build_page` builds both filters), never
#: a real `Mailbox`. `count_kind` is `None` (no badge — matching every
#: approved mockup, which only ever badges Inbox and Drafts), `"unread"`
#: (`Mailbox.unread_emails`), or `"total"` (`Mailbox.total_emails`, Drafts
#: only, per the Task 6 brief: "Drafts uses totalEmails").
#:
#: Icon names are Lucide static icon names (`mailosh/ui/icons.txt`),
#: chosen to match the approved mockups exactly where they show one:
#: layout.html literally uses Lucide's "file" glyph (not "file-text") for
#: Drafts, and key-moments.html's ⌘K palette reuses the *same* "inbox" icon
#: for its "All mail" command row as for Inbox itself — both echoed here
#: rather than guessed.
_SYSTEM_SPEC: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("inbox", "Inbox", "inbox", "inbox", "unread"),
    ("starred", "Starred", "star", None, None),
    ("sent", "Sent", "send", "sent", None),
    ("drafts", "Drafts", "file", "drafts", "total"),
)
_MORE_SPEC: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("all", "All mail", "inbox", None, None),
    ("archive", "Archive", "archive", "archive", None),
    # Nav key is "spam" (spec §5.2's own wording) but the underlying JMAP
    # role is "junk" (RFC 8621 §2's standard role name) — the one place a
    # nav key and its role name deliberately differ.
    ("spam", "Spam", "shield-alert", "junk", None),
    ("trash", "Trash", "trash-2", "trash", None),
)

#: Every reserved nav key, derived from the two spec tables above rather
#: than restated. `resolve_mailbox` needs this to tell "a reserved key
#: whose mailbox this account does not have" (-> `None`) apart from "a
#: label's own mailbox id" (-> itself): since `_nav_items` no longer
#: renders an item for a role it cannot resolve, the rendered nav is no
#: longer the full key vocabulary and cannot be used as one.
_RESERVED_KEYS: frozenset[str] = frozenset(key for key, *_rest in (*_SYSTEM_SPEC, *_MORE_SPEC))

#: The reserved keys that name a real JMAP role, mapped to the
#: `(display name, role)` a *created* mailbox should carry. "starred"/"all"
#: are deliberately absent — they are virtual views with no backing
#: `Mailbox` by design, so `ensure_role_mailbox` has nothing to ensure for
#: them and raises `KeyError` rather than inventing a folder.
_ROLE_SPEC: dict[str, tuple[str, str]] = {
    key: (label, role)
    for key, label, _icon, role, _count in (*_SYSTEM_SPEC, *_MORE_SPEC)
    if role is not None
}

#: One creation lock per `(account, role)`, so two concurrent archives on
#: the same account cannot both decide the Archive mailbox is missing and
#: both create one. Only ever taken on the rare path where the role
#: mailbox is genuinely absent; once it exists, `build_nav` resolves it and
#: `ensure_role_mailbox` is never called again.
#:
#: In-process only, and deliberately not the sole defence: a second worker
#: (or a second Mailosh instance) shares no lock with this one, which is
#: what `ensure_role_mailbox`'s create-rejected branch exists for.
_role_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass
class NavItem:
    key: str
    label: str
    icon: str
    mailbox_id: str | None
    count: int | None
    active: bool


@dataclass
class LabelNode:
    mailbox_id: str
    name: str
    color: str | None
    count: int
    visibility: str
    children: list["LabelNode"]


@dataclass
class NavModel:
    system: list[NavItem]
    more: list[NavItem]
    labels: list[LabelNode]
    inbox_id: str


def _role_mailbox(mailboxes: list[Mailbox], role: str) -> Mailbox | None:
    """The mailbox with the given `role`, or `None` if the account has
    none — unlike `find_inbox`/the client's own private `_mailbox_id`, this
    does NOT raise. A real Stalwart account always provisions Inbox, and
    the rest of this app's core flows depend on it existing, so a missing
    Inbox is a loud `JmapError` (via `find_inbox`, reused below rather than
    reimplemented). A missing *secondary* role mailbox (no Archive, say) is
    a lesser, tolerable gap — this is a controller decision Task 6's own
    brief doesn't specify, made here so a nav render degrades (to no row at
    all — see `_nav_items`) instead of a hard failure that takes down the
    whole page.
    """
    for mailbox in mailboxes:
        if mailbox.role == role:
            return mailbox
    return None


def _nav_items(
    spec: tuple[tuple[str, str, str, str | None, str | None], ...],
    mailboxes: list[Mailbox],
    active_key: str,
) -> list[NavItem]:
    """The `system`/`more` rows for `spec`, minus any reserved entry whose
    declared role this account has no mailbox for.

    The distinction that matters is "role declared but not found" versus
    "no role by design", *not* "mailbox_id is None" — the two look
    identical on the resulting `NavItem`:

    - `("archive", ..., role="archive", ...)` with no `role=archive`
      mailbox in the account is an item that would render a link to
      `/mail/archive`, which `mailosh.web.mail._valid_keys` then 404s and
      `mailosh.services.actions` cannot resolve. Offering a folder the
      account does not have is the defect; it is skipped.
    - `("starred", ..., role=None, ...)` and `("all", ..., role=None, ...)`
      are virtual views *by design* — Starred is a `$flagged` filter and
      All mail is `inMailboxOtherThan`, and `thread_list.build_page` builds
      both from the key alone. They have no mailbox to miss, so they always
      render.

    Archive comes back on its own once something creates it
    (`ensure_role_mailbox`), because this reads the account's real mailbox
    list on every render.
    """
    items = []
    for key, label, icon, role, count_kind in spec:
        mailbox = _role_mailbox(mailboxes, role) if role else None
        if role is not None and mailbox is None:
            continue
        count = None
        if mailbox is not None and count_kind is not None:
            count = mailbox.total_emails if count_kind == "total" else mailbox.unread_emails
        items.append(
            NavItem(
                key=key,
                label=label,
                icon=icon,
                mailbox_id=mailbox.id if mailbox else None,
                count=count,
                active=key == active_key,
            )
        )
    return items


def _visibility_of(meta: LabelMeta | None) -> Visibility:
    """This label's `Visibility`. No `LabelMeta` row at all defaults to
    `SHOW` — matching `label_meta_map`'s own documented contract ("a
    mailbox with no LabelMeta row simply has no entry — callers fall back
    to defaults... rather than this helper inventing one"). An
    unrecognised stored token is resolved (and logged) by
    `Visibility.parse`, never treated as `SHOW` by accident.
    """
    return Visibility.parse(meta.visibility if meta is not None else None)


def _in_label_tree(visibility: Visibility) -> bool:
    """Whether a label belongs in `NavModel.labels` at all.

    Only `HIDE` is filtered *structurally*, and only because "hidden labels
    never produce chips" (controller decision 3) is enforced by absence:
    `thread_list._chips_for` reads `nav.labels` as its label registry, so a
    `HIDE` label that never enters the tree can never become a chip either.

    `SHOW_IF_UNREAD` deliberately does **not** filter here, even at zero
    unread. That preference is about the *nav row* — "only take up space in
    the sidebar when there's something new" — not about the label's
    identity on a conversation. Filtering it out of the tree would have
    made its chips blink in and out of existence on threads whose contents
    never changed, for a reason having nothing to do with those threads.
    Such a node stays in `labels` carrying its own `visibility` and
    `count`; `hidden_in_nav` below is the single predicate the nav template
    uses to skip its *row*.
    """
    return visibility is not Visibility.HIDE


def hidden_in_nav(node: LabelNode) -> bool:
    """Whether the nav sidebar should skip this label's row (spec §5.2's
    "show if unread"), for the nav template to call.

    A predicate rather than a `LabelNode` field so `NavModel`'s dataclass
    shape stays exactly as the plan's Interfaces block specifies, and a
    function here rather than a comparison written inline in Jinja so the
    `Visibility` vocabulary has exactly one reader. Chips deliberately do
    not consult this — see `_in_label_tree`.
    """
    return Visibility.parse(node.visibility) is Visibility.SHOW_IF_UNREAD and node.count == 0


def _build_label_tree(
    label_mailboxes: list[Mailbox], label_meta: dict[str, LabelMeta]
) -> list[LabelNode]:
    """Nest `label_mailboxes` (every non-role `Mailbox` — a JMAP mailbox
    acting as a Gmail-style label) by `parent_id`, alphabetically ordered
    at every level, excluding whatever `_in_label_tree` says to drop.

    A label whose *parent* is hidden but who is itself visible is promoted
    to top level rather than silently dropped — nothing in the brief
    specifies this edge case, but dropping a visible label just because an
    ancestor is hidden would contradict "hidden labels never produce
    chips/nav rows" turning into "labels can vanish for an unrelated
    reason", which is worse.
    """
    nodes: dict[str, LabelNode] = {}
    for mailbox in label_mailboxes:
        meta = label_meta.get(mailbox.id)
        visibility = _visibility_of(meta)
        if not _in_label_tree(visibility):
            continue
        nodes[mailbox.id] = LabelNode(
            mailbox_id=mailbox.id,
            name=mailbox.name,
            color=meta.color if meta is not None else None,
            count=mailbox.unread_emails,
            visibility=visibility.value,
            children=[],
        )

    roots: list[LabelNode] = []
    for mailbox in label_mailboxes:
        node = nodes.get(mailbox.id)
        if node is None:
            continue
        parent = nodes.get(mailbox.parent_id) if mailbox.parent_id else None
        (parent.children if parent is not None else roots).append(node)

    def _sort(level: list[LabelNode]) -> None:
        level.sort(key=lambda n: n.name.lower())
        for node in level:
            _sort(node.children)

    _sort(roots)
    return roots


async def build_nav(
    client: JmapClient, *, active_key: str, label_meta: dict[str, LabelMeta]
) -> NavModel:
    """Fetch every mailbox (one `Mailbox/get` request) and shape it into
    the left nav's view model: `system`/`more` in the exact order
    controller decision 4 requires, plus the label tree.
    """
    mailboxes = await client.get_mailboxes()
    inbox = find_inbox(mailboxes)  # raises JmapError if genuinely absent
    system = _nav_items(_SYSTEM_SPEC, mailboxes, active_key)
    more = _nav_items(_MORE_SPEC, mailboxes, active_key)
    labels = _build_label_tree([m for m in mailboxes if m.role is None], label_meta)
    return NavModel(system=system, more=more, labels=labels, inbox_id=inbox.id)


def resolve_mailbox(nav: NavModel, key: str) -> str | None:
    """`key` -> JMAP mailbox id, for anything `build_page`/a route needs to
    turn a nav key (or a raw label mailbox id) into a concrete target:
    - a reserved system/more key ("inbox", "sent", ... "trash") resolves to
      that item's `mailbox_id` — `None` for "starred"/"all", which are
      virtual views with no backing `Mailbox` at all;
    - a reserved key with no rendered item resolves to `None` as well.
      `_nav_items` drops an entry whose role this account has no mailbox
      for, so the rendered nav is a subset of the key vocabulary; without
      this branch "archive" on an account with no Archive folder would fall
      through to the pass-through below and be handed to `Email/query` (or
      to an `Email/set` patch) as the literal mailbox id `"archive"`.
    - anything else (a label's own mailbox id, e.g. from clicking it in the
      sidebar) passes through unchanged, since a label *is* already its own
      valid JMAP mailbox id — there's no separate symbolic key for it.
    """
    for item in (*nav.system, *nav.more):
        if item.key == key:
            return item.mailbox_id
    return None if key in _RESERVED_KEYS else key


async def ensure_role_mailbox(client: JmapClient, key: str) -> str:
    """The mailbox id for a reserved key's role, **creating the mailbox if
    the account hasn't got one**, and never returning without an id.

    Stalwart provisions Inbox, Deleted Items, Junk Mail, Drafts and Sent
    Items — not Archive — so on a stock self-hosted deployment archiving an
    Inbox-only message has nowhere to put it (RFC 8621 §4.1 forbids an
    empty `mailboxIds`, so "just drop the Inbox" is not an option). Design
    spec §6.3 says archive "adds the Archive-role mailbox"; this is what
    makes that true on an account that never had one, and it is what every
    other mail client does.

    Called only from an action that actually needs the folder — never from
    a GET, and never as a side effect of rendering a nav — so reading mail
    on an account with no Archive creates nothing.

    Race safety, in two layers, because the wrong outcome here is a *pair*
    of Archive folders that then split the account's mail between them:

    1. `_role_locks` serialises this within the process and the resolve is
       re-done *inside* the lock, so of two concurrent archives only the
       first can reach `create_mailbox` at all; the second finds the
       first's mailbox.
    2. Across processes there is no shared lock, so the create itself is
       the arbiter: RFC 8621 §2 makes a role unique per account, so the
       loser's create is rejected. That rejection is not a failure — the
       folder the caller wanted now exists — so it re-resolves and returns
       the winner. Only a rejection with *still* no such mailbox is a real
       error, and that one propagates unchanged.

    Raises `KeyError` for a key with no role of its own ("starred",
    "all"): those are virtual views, and inventing a folder for one would
    be a bug, not a fallback.
    """
    label, role = _ROLE_SPEC[key]
    async with _role_locks[(client.account_id, role)]:
        existing = _role_mailbox(await client.get_mailboxes(), role)
        if existing is not None:
            return existing.id
        try:
            return await client.create_mailbox(label, role=role)
        except JmapError:
            winner = _role_mailbox(await client.get_mailboxes(), role)
            if winner is None:
                raise
            return winner.id
