"""``GET /palette/index`` — the data half of the ⌘K command palette (design
spec §6.1/§6.2, Task 11). The browser half (``static/js/palette.js``, the
``<dialog>`` template, and the ``keys.js``/``app.js`` wiring that actually
opens it) is a later dispatch; this module only has to hand back the four
groups the palette's four modes draw from.

**actions** — a fixed, Python-owned list of the mutating commands the
palette's default ("command") mode searches. Design spec §6.1's keyboard map
lists far more shortcuts than this (``j``/``k`` list navigation, ``l``/``v``
mode-openers, ``c`` compose, ``r``/``a``/``f`` reply, ``z`` undo, ...), but
this list is deliberately narrower: only the six mutations
``mailosh.web.actions``/``mailosh.services.actions`` (Task 9) already
implement, which is also exactly what 1A's own scope names ("optimistic
archive/star/read/delete" per the phase plan). Everything else is left out
for a reason, not an oversight:

- ``l`` (label as…)/``v`` (move to…) preselect the palette's own ``label``/
  ``move`` *mode* directly (this task's own brief: "opening from l/v
  preselects the mode") — they never run through this "actions" list at
  all, so they need no entry here.
- ``c``/``r``/``a``/``f`` (compose/reply/reply-all/forward) have no route to
  dispatch to yet — no compose dock exists in this codebase (1B/1C).
- ``z`` (undo) is only ever available for the ~10 s after the action it
  reverses, which is browser-held state (``om:done``'s ``HX-Trigger``) this
  *cached* (60 s, per the brief) endpoint has no way to know about — a
  stale "Undo" entry that does nothing for 59 of every 60 seconds is worse
  than no entry at all.
- ``/`` (search) is the palette's own free-text fallback ("Search mail for
  “…”"), never a discrete, listed command.

Every label below is the imperative form of the exact toast string
``mailosh.services.actions`` already produces for that action
(``"Archived"`` -> "Archive conversation", ``"Marked as read"`` -> "Mark as
read", ...) so the command a user picks and the confirmation they see
afterwards agree.

The ids are this module's own invention, not copied from anywhere —
``static/js/keys.js`` (not yet written) is what the brief calls "the single
source of truth" for these ids once it exists, and it must define entries
with exactly these ids for ``run()`` (``palette.js``'s dispatch for an
``"action"``-kind result) to have anything to call. Until then, this list
*is* the registry.

**goto** — every system/"more" nav item (``mailosh.services.mailbox_tree.
build_nav``, all eight: Inbox, Starred, Sent, Drafts, All mail, Archive,
Spam, Trash) plus every visible label, flattened. ``keys`` carries the
design spec's five fixed ``g`` sequences (``g i``, ``g s``, ``g t``, ``g
d``, ``g a``) where one exists and ``[]`` where it doesn't (Archive/Spam/
Trash and every label have no dedicated sequence — reachable only by typing
the name).

**labels** — the same visible labels as ``goto``, shaped for ``label``
mode's multi-apply picker instead: ``id`` is the bare mailbox id here, not
``"goto:<id>"``, since this is what ``om.act("label", …)`` posts against,
not a navigation target. Colour is narrowed through ``label_color`` exactly
the way ``shell/nav.html``/``list/row.html`` already do, so a hostile stored
``LabelMeta.color`` can't reach the client unchecked here either.

Both ``goto`` and ``labels`` drop whatever ``mailosh.services.mailbox_tree.
hidden_in_nav`` says to hide (a ``show_if_unread`` label with nothing unread
right now), on top of the ``hide``-visibility labels ``build_nav`` itself
never puts in the tree at all — "hidden labels never produce chips/nav
rows" (mailbox_tree's own controller decision 3) extends here to "and no
palette entry either."

**settings** — one entry per legal value of the three quick-toggle prefs
Task 12's ``POST /prefs`` accepts (``theme``: system/light/dark, ``density``:
compact/standard/comfortable, ``shortcuts``: on/off) — a fixed, static list
independent of the caller's *current* prefs, deliberately: this response is
cached client-side for 60 s (the brief's own §6.2 note), and a list that had
to be invalidated the moment a user's theme changed would defeat that cache
the same second it mattered.

Every dependency here is one ``mailosh.web.mail``/``mailosh.web.actions``
already use (``deps.client_for``, ``deps.current_user``, ``deps.get_db``) —
this router is **not** registered in ``mailosh.web.app.create_app`` (a
later dispatch mounts every new Task 11/12 router together); it is only
wired into a bare ``FastAPI()`` here and in ``tests/unit/test_palette.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db import repo
from mailosh.db.models import AppUser
from mailosh.jmap.client import JmapClient
from mailosh.services.mailbox_tree import LabelNode, NavModel, build_nav, hidden_in_nav
from mailosh.ui.format import label_color
from mailosh.web import deps

router = APIRouter(prefix="/palette", tags=["palette"])

ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
UserDep = Annotated[AppUser, Depends(deps.current_user)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]


class PaletteAction(BaseModel):
    """One "command" mode result — see this module's own docstring for
    exactly which six actions exist and why nothing else does yet.
    """

    id: str
    label: str
    keys: list[str]
    kind: str = "action"
    needs: str | None = None


class PaletteGoto(BaseModel):
    """One "goto" mode result: a system/more nav item or a label, always
    navigable via ``htmx.ajax("GET", href)`` (the brief's own wording for
    how ``Enter`` runs a goto result).
    """

    id: str
    label: str
    keys: list[str]
    href: str


class PaletteLabel(BaseModel):
    """One "label" mode result: an existing label a selection/open thread
    can be filed under. No ``keys`` field — labels are picked by fuzzy name
    match, never a fixed shortcut.
    """

    id: str
    label: str
    color: str


class PaletteSetting(BaseModel):
    """One "settings" result: a single POST that flips one Quick Settings
    toggle to one concrete value (Task 12's ``POST /prefs``).
    """

    id: str
    label: str
    post: str
    values: dict[str, str | bool]


class PaletteIndex(BaseModel):
    """The whole ``GET /palette/index`` response body."""

    actions: list[PaletteAction]
    goto: list[PaletteGoto]
    labels: list[PaletteLabel]
    settings: list[PaletteSetting]


#: The six mutations already wired in `mailosh.web.actions` — see this
#: module's docstring for why nothing else (compose, reply, label-as,
#: move-to, undo, search) is here yet. Every one needs an active selection
#: or open thread (there is nothing else in this app to run them against),
#: hence the uniform `needs="selection"`.
#:
#: `keys` for `mark-read`/`mark-unread` is `["shift", "i"]`/`["shift", "u"]`
#: — a held modifier plus a letter, as a two-element list — deliberately
#: distinct from a *sequential* two-keystroke entry like goto's `["g", "i"]`
#: only by which physical keys they name; `static/js/keys.js` (not yet
#: written) is what actually has to tell the two apart (a `keydown` chord
#: vs. two separate `keydown`s inside its 1000 ms sequence window).
ACTIONS: tuple[PaletteAction, ...] = (
    PaletteAction(id="archive", label="Archive conversation", keys=["e"], needs="selection"),
    PaletteAction(id="delete", label="Delete conversation", keys=["#"], needs="selection"),
    PaletteAction(id="spam", label="Report spam", keys=["!"], needs="selection"),
    PaletteAction(id="star", label="Star conversation", keys=["s"], needs="selection"),
    PaletteAction(id="mark-read", label="Mark as read", keys=["shift", "i"], needs="selection"),
    PaletteAction(id="mark-unread", label="Mark as unread", keys=["shift", "u"], needs="selection"),
)

#: One entry per legal value of the three Task 12 `POST /prefs` fields — see
#: this module's docstring for why this is a fixed list, not shaped by the
#: caller's own current prefs.
SETTINGS: tuple[PaletteSetting, ...] = (
    PaletteSetting(
        id="prefs:theme:system", label="Theme: system", post="/prefs", values={"theme": "system"}
    ),
    PaletteSetting(
        id="prefs:theme:light", label="Theme: light", post="/prefs", values={"theme": "light"}
    ),
    PaletteSetting(
        id="prefs:theme:dark", label="Theme: dark", post="/prefs", values={"theme": "dark"}
    ),
    PaletteSetting(
        id="prefs:density:compact",
        label="Density: compact",
        post="/prefs",
        values={"density": "compact"},
    ),
    PaletteSetting(
        id="prefs:density:standard",
        label="Density: standard",
        post="/prefs",
        values={"density": "standard"},
    ),
    PaletteSetting(
        id="prefs:density:comfortable",
        label="Density: comfortable",
        post="/prefs",
        values={"density": "comfortable"},
    ),
    PaletteSetting(
        id="prefs:shortcuts:on",
        label="Keyboard shortcuts: on",
        post="/prefs",
        values={"shortcuts": True},
    ),
    PaletteSetting(
        id="prefs:shortcuts:off",
        label="Keyboard shortcuts: off",
        post="/prefs",
        values={"shortcuts": False},
    ),
)

#: `key` -> the design spec's fixed `g`-sequence for it, for the five nav
#: items that have one. Archive/Spam/Trash and every label have none — they
#: are still full `goto` entries (typeable by name), just with `keys=[]`.
_GOTO_KEYS: dict[str, list[str]] = {
    "inbox": ["g", "i"],
    "starred": ["g", "s"],
    "sent": ["g", "t"],
    "drafts": ["g", "d"],
    "all": ["g", "a"],
}


def _flatten_visible_labels(nodes: list[LabelNode]) -> list[LabelNode]:
    """Every label in `nodes`, pre-order (a node before its own children),
    dropping whatever `hidden_in_nav` says to hide.

    Mirrors `shell/nav.html`'s own `label_rows` macro exactly: that macro
    also recurses into `node.children` unconditionally and only wraps the
    node's *own* row in the `hidden_in_nav` check — so a visible child of a
    currently-hidden `show_if_unread` parent still gets its own row (and,
    here, its own palette entry) rather than being dropped along with it.
    """
    visible: list[LabelNode] = []
    for node in nodes:
        if not hidden_in_nav(node):
            visible.append(node)
        visible.extend(_flatten_visible_labels(node.children))
    return visible


def _goto_entries(nav: NavModel) -> list[PaletteGoto]:
    """`goto`: every system/more item, in `build_nav`'s own order, followed
    by every visible label (nested labels included) in the label tree's own
    pre-order — a label's `key` is its own mailbox id (it has no separate
    symbolic nav key), matching `resolve_mailbox`'s own "pass it straight
    through" contract.
    """
    entries = [
        PaletteGoto(
            id=f"goto:{item.key}",
            label=item.label,
            keys=_GOTO_KEYS.get(item.key, []),
            href=f"/mail/{item.key}",
        )
        for item in (*nav.system, *nav.more)
    ]
    entries.extend(
        PaletteGoto(
            id=f"goto:{node.mailbox_id}",
            label=node.name,
            keys=[],
            href=f"/mail/{node.mailbox_id}",
        )
        for node in _flatten_visible_labels(nav.labels)
    )
    return entries


def _label_entries(nav: NavModel) -> list[PaletteLabel]:
    """`labels`: every visible label, coloured the same way a sidebar dot or
    a row chip is — `label_color(stored, seed=mailbox_id)` — so an
    uncoloured label still gets a stable colour instead of `None` reaching
    the client, and a hostile stored value never reaches it unchecked.
    """
    return [
        PaletteLabel(
            id=node.mailbox_id, label=node.name, color=label_color(node.color, node.mailbox_id)
        )
        for node in _flatten_visible_labels(nav.labels)
    ]


@router.get("/index", response_model=PaletteIndex)
async def index(client: ClientDep, user: UserDep, db: DbDep) -> PaletteIndex:
    """The whole palette payload in one `Mailbox/get` (via `build_nav`) plus
    one `label_meta` lookup — no more than what any other authenticated page
    in this app already pays to render its own nav.

    `active_key=""`, matching `mailosh.web.actions._nav`'s own convention:
    nothing here reads `NavItem.active`, so there is no "current view" for
    the palette to mark.
    """
    label_meta = await repo.label_meta_map(db, user.id, client.account_id)
    nav = await build_nav(client, active_key="", label_meta=label_meta)
    return PaletteIndex(
        actions=list(ACTIONS),
        goto=_goto_entries(nav),
        labels=_label_entries(nav),
        settings=list(SETTINGS),
    )
