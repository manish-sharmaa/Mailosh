"""Labels (design spec §10) — create/rename/nest/delete over JMAP mailboxes,
the label picker and Move popovers, and the colour/visibility metadata that
lives in Postgres.

Eight routes, all under `/labels`, all CSRF-protected at the router (the
same shape `mailosh.web.prefs` and `mailosh.web.compose` use, so nothing
here repeats a `Depends`), and split into exactly two kinds:

**Two that render a popover.** `POST /labels/picker` and
`POST /labels/menu/{id}` answer HTML fragments the client swaps into a
`<dialog>` it owns (`static/js/labels.js`). They are POSTs and that is
deliberate, not laziness: the picker's tri-state checkboxes depend on which
labels the *current selection* already carries, and a selection is a list of
message ids — up to a hundred conversations' worth. A GET would have to
carry them in a query string that a proxy is entitled to truncate at 8 KB
and that would sit in every access log; a form body has neither problem.
Neither route writes anything.

**Six that mutate**, answering `204 No Content` plus an `HX-Trigger`, the
identical contract `mailosh.web.actions`'s six already established:

    HX-Trigger: {"om:done": {"toast": "Labelled “Work”", "undo": "<token>",
                             "removed": [], "counts": {"inbox": -2}},
                 "om:labels": {"changed": true}}

`om:done` is read by `static/js/actions.js`'s existing body-level listener,
so a labelled conversation gets its toast, its Undo and its nav badge
movement through code that already exists rather than a second copy of it.
`om:labels` is this module's own, and says only "the set of labels or their
metadata changed" — `labels.js` drops its cached label list on it, and the
nav re-renders through the `mail:changed` refresh the client fires next.

**Undo.** Applying and moving are undoable and use `mailosh.web.actions`'s
own `/a/undo` route to reverse — the token is the same signed `UndoSpec`,
scoped to the same JMAP account, so nothing new had to be signed or
verified. Creating, renaming, nesting and deleting a label are *not*
undoable and send no token: a rename is trivially reversible by hand, and a
delete destroys the mailbox id every restore would have to name. That is
what `POST /labels/menu/{id}`'s confirmation is for.

**Failures speak.** `mailosh.services.labels.LabelError` carries a finished
sentence (a duplicate name, a cycle, a label with children) and becomes a
`200` + `om:error` — the same shape the app's global `JmapError` handler
produces, so `labels.js` has one failure path rather than one per status
code. `PartialApply` becomes the same thing with an exact count in it, plus
`refresh: true`, because a half-applied write must never be reported as
either a success or a clean failure.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.db.models import AppUser, Visibility
from mailosh.jmap.client import JmapClient
from mailosh.services import labels as service
from mailosh.services.actions import ActionResult
from mailosh.services.labels import MAX_NAME, LabelError, PartialApply
from mailosh.services.mailbox_tree import (
    LabelNode,
    NavModel,
    build_nav,
    hidden_in_nav,
    resolve_mailbox,
)
from mailosh.services.undo import sign
from mailosh.ui.format import LABEL_COLORS
from mailosh.web import deps

router = APIRouter(prefix="/labels", tags=["labels"], dependencies=[Depends(deps.csrf_protect)])

ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
UserDep = Annotated[AppUser, Depends(deps.current_user)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]
IdsForm = Annotated[list[str], Form()]
#: The same field, optional. A form that names no `ids` at all (an empty
#: selection opening the picker) sends nothing rather than an empty list,
#: and `None` is what FastAPI hands back for that — coerced at the top of
#: each handler so nothing below has to think about the two spellings.
OptionalIdsForm = Annotated[list[str] | None, Form()]

#: The three `LabelMeta.visibility` values the hover menu offers (spec §5.2:
#: "colour / show / hide / show if unread"). Read from the enum rather than
#: restated, so a fourth value cannot exist in one place and not the other.
VISIBILITIES: tuple[tuple[str, str], ...] = (
    (Visibility.SHOW.value, "Show"),
    (Visibility.SHOW_IF_UNREAD.value, "Show if unread"),
    (Visibility.HIDE.value, "Hide"),
)


def _trigger(payload: dict[str, object]) -> dict[str, str]:
    """One `HX-Trigger` value, serialized exactly the way
    `mailosh.web.actions` and `mailosh.web.prefs` serialize theirs —
    `ensure_ascii` on (json's default) because an HTTP header value is
    latin-1 and a label name is not.
    """
    return {"HX-Trigger": json.dumps(payload, separators=(",", ":"))}


def _error(message: str, *, refresh: bool = False) -> Response:
    """A refusal the reader can read, as a `200`.

    `200`, not a 4xx, for the reason `mailosh.web.app._error_toast` spells
    out: htmx treats a 4xx as a load failure and hands a listener nothing
    useful, and `labels.js` reads the *event*, never the status. `HX-Reswap:
    none` says there is no body to swap; `HX-Push-Url: false` stops htmx
    recording a URL for a request that changed nothing.
    """
    payload: dict[str, object] = {"toast": message, "retry": False}
    if refresh:
        payload["refresh"] = True
    return Response(
        status_code=200,
        headers={
            "HX-Reswap": "none",
            "HX-Push-Url": "false",
            **_trigger({"om:error": payload}),
        },
    )


def _done(
    request: Request,
    client: JmapClient,
    result: ActionResult,
    *,
    labels_changed: bool = False,
) -> Response:
    """`204` + `om:done`, with the undo token signed exactly as
    `mailosh.web.actions._done` signs one (same `UndoSpec`, same
    account-scoped signature), so the reversal runs through `POST /a/undo`
    and there is one undo implementation in this app rather than two.

    No byte-shedding ladder here, unlike that function: `removed` is empty
    for an apply and a move names at most a hundred threads, and neither
    carries the 2 KB of `removed` a bulk archive can. If that ever changes,
    the answer is to share `_done`, not to grow a second ladder.
    """
    settings: Settings = request.app.state.settings
    token = (
        sign(result.spec, settings.secret_key, scope=client.account_id)
        if result.spec.email_ids
        else None
    )
    payload: dict[str, object] = {
        "om:done": {
            "toast": result.spec.toast,
            "undo": token,
            "removed": result.removed,
            "counts": result.counts,
        }
    }
    if token is None:
        payload["om:done"]["undo_unavailable"] = "no_change"  # type: ignore[index]
    if labels_changed:
        payload["om:labels"] = {"changed": True}
    return Response(status_code=204, headers=_trigger(payload))


def _changed(toast: str) -> Response:
    """`204` for a label-set change with nothing to undo (create, rename,
    nest, delete, colour, visibility).

    Carries `om:done` so the existing toast machinery shows it, with `undo`
    explicitly `null` and `refresh: true` — the nav, the chips on every row
    and the list itself can all have moved, and re-asking the server is both
    cheaper to write and impossible to get wrong compared with patching four
    surfaces from a delta.
    """
    return Response(
        status_code=204,
        headers=_trigger(
            {
                "om:done": {"toast": toast, "undo": None, "removed": [], "refresh": True},
                "om:labels": {"changed": True},
            }
        ),
    )


async def _nav_for(client: JmapClient, db: AsyncSession, user: AppUser) -> NavModel:
    """The nav model these routes read labels out of — one `Mailbox/get`
    plus this user's `LabelMeta` rows, the same pair every page render
    already pays for (`mailosh.web.mail._nav_for`).

    `active_key=""` because nothing here has a current mailbox.
    """
    label_meta = await repo.label_meta_map(db, user.id, client.account_id)
    return await build_nav(client, active_key="", label_meta=label_meta)


def _flatten(nodes: list[LabelNode], depth: int = 0) -> list[tuple[LabelNode, int]]:
    """Every label in the tree, pre-order, with its nesting depth — the
    order and indent the picker draws.

    Unlike `mailosh.web.palette._flatten_visible_labels` this keeps
    `hidden_in_nav` labels: a `show_if_unread` label with nothing unread has
    no *sidebar row* right now, but it is still a label a reader can file
    something under, and dropping it from the picker would make a label they
    deliberately configured unreachable from the one control that exists to
    reach it. Labels at `hide` never enter the tree at all (`build_nav`), so
    the picker cannot offer those either way.
    """
    out: list[tuple[LabelNode, int]] = []
    for node in nodes:
        out.append((node, depth))
        out.extend(_flatten(node.children, depth + 1))
    return out


async def _prune_orphans(
    db: AsyncSession, user: AppUser, client: JmapClient, nav: NavModel
) -> None:
    """Drop `LabelMeta` rows whose mailbox no longer exists.

    Nothing *reads* an orphan row — `build_nav` walks mailboxes and looks
    metadata up by id, so a row for a deleted label is simply never
    consulted and can never render a ghost label in the nav. The reason to
    clean up anyway is id reuse: the row is keyed on an id the server is
    free to hand to the next mailbox created, and a brand-new label
    inheriting a deleted one's `visibility: hide` would be invisible for a
    reason nobody could find.

    **The pre-check is not the authority, and must not be.** `nav` is a
    *filtered* view — `build_nav` drops every `hide` label from the tree —
    so pruning against it would throw away the colour of exactly the labels
    the reader chose to hide. It is used here only to answer "is there
    anything that might be orphaned?", and a false positive (a hidden
    label) costs one `Mailbox/get` and deletes nothing. The prune itself
    goes through `repo.prune_label_meta` against a full, unfiltered
    `Mailbox/get`, which is that helper's own documented requirement.

    So the ordinary case — no orphans — issues no extra request and no
    statement at all. Run on the picker, which the reader opens
    deliberately, and never on the nav's own render path, which every page
    in this app pays for.
    """
    rows = await repo.label_meta_map(db, user.id, client.account_id)
    if not rows:
        return
    known = {node.mailbox_id for node, _depth in _flatten(nav.labels)}
    known |= {item.mailbox_id for item in (*nav.system, *nav.more) if item.mailbox_id is not None}
    if not set(rows) - known:
        return
    live = {mailbox.id for mailbox in await client.get_mailboxes()}
    await repo.prune_label_meta(db, user.id, client.account_id, live)


@router.post("/picker", response_class=HTMLResponse)
async def picker(
    request: Request,
    client: ClientDep,
    user: UserDep,
    db: DbDep,
    ids: OptionalIdsForm = None,
    mode: Annotated[str, Form()] = "label",
) -> HTMLResponse:
    """The label picker (`l`) or the Move chooser (`v`), as a fragment.

    One `Mailbox/get` (through `build_nav`) plus one `Email/get` for the
    selection's current placement — two requests, whatever the selection
    size, because the tri-state of every checkbox comes out of the same
    snapshot. `_prune_orphans` adds a third only on the rare render where
    this user has `LabelMeta` for a mailbox the nav has never heard of.

    `mode` decides which of the two this is, and they differ in exactly two
    ways: Move offers the system mailboxes as well as the labels (moving to
    Archive or Trash is the common case), and its options are radios with no
    "create" row, because "move to a label that does not exist yet" is a
    two-step gesture pretending to be one.
    """
    selection = list(ids or [])
    mode = "move" if mode == "move" else "label"
    nav = await _nav_for(client, db, user)
    await _prune_orphans(db, user, client, nav)
    states = await client.get_email_states(selection) if selection else []

    rows = [
        {
            "id": node.mailbox_id,
            "name": node.name,
            "depth": depth,
            "color": node.color,
            "state": service.selection_state(states, node.mailbox_id),
            "muted": hidden_in_nav(node),
        }
        for node, depth in _flatten(nav.labels)
    ]
    systems = (
        [
            {"key": item.key, "name": item.label, "icon": item.icon, "id": item.mailbox_id}
            for item in (*nav.system, *nav.more)
            if item.mailbox_id is not None
        ]
        if mode == "move"
        else []
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "labels/picker.html",
        {
            "mode": mode,
            "rows": rows,
            "systems": systems,
            "ids": selection,
        },
    )


@router.post("/new", response_class=HTMLResponse)
async def new(request: Request, client: ClientDep, user: UserDep, db: DbDep) -> HTMLResponse:
    """The "New label" form — the `+` beside the sidebar's Labels heading,
    and the picker's own type-to-create row.

    A POST like its two siblings rather than a GET, purely so the three
    fragment routes are one shape the client calls one way; it reads a nav
    and writes nothing. `name` seeds the field from whatever was typed in
    the picker before nothing matched, so "type Receipts, no match, create"
    does not ask the reader to type it again.
    """
    form = await request.form()
    typed = form.get("name")
    # Seeded, never validated here: this route renders a form, and a name
    # too long or full of control characters is something `POST /labels`
    # refuses with a sentence the reader can read. Rejecting it now would
    # mean a popover that fails to open instead of one that explains.
    seed = typed[:MAX_NAME] if isinstance(typed, str) else ""
    nav = await _nav_for(client, db, user)
    return request.app.state.templates.TemplateResponse(
        request,
        "labels/new.html",
        {
            "name": seed,
            "parents": [
                {"id": node.mailbox_id, "name": node.name, "depth": depth}
                for node, depth in _flatten(nav.labels)
            ],
        },
    )


@router.post("/menu/{mailbox_id}", response_class=HTMLResponse)
async def menu(
    request: Request, client: ClientDep, user: UserDep, db: DbDep, mailbox_id: str
) -> HTMLResponse:
    """One label's hover menu (spec §5.2: "colour / show / hide / show if
    unread / edit / remove"), as a fragment.

    Fetched on demand rather than rendered inside every nav row: the menu
    carries twelve colour swatches, three visibility choices and a rename
    field, and rendering that for every label on every page would put it in
    the document dozens of times over for a control that is used seconds at
    a time.

    It also carries the delete confirmation's own numbers
    (`service.delete_plan`), so the question the reader is asked names this
    label's actual mail rather than a generic warning.
    """
    try:
        plan = await service.delete_plan(client, mailbox_id)
    except LabelError as exc:
        # The menu is a fragment the client swaps in, so its failure has to
        # be a fragment too — an `om:error` with no body would swap emptiness
        # into an open popover and leave the reader looking at nothing.
        return request.app.state.templates.TemplateResponse(
            request, "labels/gone.html", {"message": str(exc)}
        )
    meta = await repo.label_meta(db, user.id, client.account_id, mailbox_id)
    nav = await _nav_for(client, db, user)
    parents = [
        {"id": node.mailbox_id, "name": node.name, "depth": depth}
        for node, depth in _flatten(nav.labels)
        if node.mailbox_id != mailbox_id
    ]
    current_parent = next(
        (
            parent.mailbox_id
            for parent, _depth in _flatten(nav.labels)
            for child in parent.children
            if child.mailbox_id == mailbox_id
        ),
        "",
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "labels/menu.html",
        {
            "plan": plan,
            "colors": LABEL_COLORS,
            "color": meta.color if meta is not None else None,
            "visibility": Visibility.parse(meta.visibility if meta is not None else None).value,
            "visibilities": VISIBILITIES,
            "parents": parents,
            "current_parent": current_parent,
        },
    )


@router.post("")
async def create(
    client: ClientDep,
    name: Annotated[str, Form()],
    parent_id: Annotated[str, Form()] = "",
) -> Response:
    """Create a label (spec §10), optionally nested under `parent_id`."""
    try:
        clean = service.clean_name(name)
        await service.create_label(client, clean, parent_id=parent_id or None)
    except LabelError as exc:
        return _error(str(exc))
    return _changed(f"Created “{clean}”")


@router.post("/{mailbox_id}/rename")
async def rename(client: ClientDep, mailbox_id: str, name: Annotated[str, Form()]) -> Response:
    """Rename a label. The toast names what it is called *now*, which after
    `clean_name` is not always what was typed.
    """
    try:
        renamed = await service.rename_label(client, mailbox_id, name)
    except LabelError as exc:
        return _error(str(exc))
    return _changed(f"Renamed to “{renamed}”")


@router.post("/{mailbox_id}/nest")
async def nest(
    client: ClientDep, mailbox_id: str, parent_id: Annotated[str, Form()] = ""
) -> Response:
    """Nest a label under another, or move it back to the top level with an
    empty `parent_id`.

    A `parentId` pointing into the label's own subtree is refused by the
    server (RFC 8621 §2) and reaches the reader as "A label can't be nested
    inside itself." rather than as a JMAP error string — see
    `mailosh.services.labels.nest_label` for why the check is not re-derived
    here.
    """
    try:
        await service.nest_label(client, mailbox_id, parent_id or None)
    except LabelError as exc:
        return _error(str(exc))
    return _changed("Label moved")


@router.post("/{mailbox_id}/meta")
async def meta(
    client: ClientDep,
    user: UserDep,
    db: DbDep,
    mailbox_id: str,
    color: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "",
) -> Response:
    """Set this label's colour and/or visibility — the two halves of
    `LabelMeta` that have a control (spec §5.2's hover menu).

    Both are optional, so the colour swatches and the visibility radios post
    independently and neither overwrites the other. An empty `color` clears
    it back to the hashed fallback (`mailosh.ui.format.label_color`), which
    is why the form value and "leave it alone" have to be different things —
    see `repo.set_label_meta`'s `_KEEP`.

    The value sets are checked here rather than trusted: `color` must be one
    of spec §4.1's twelve palette names (`label_color` narrows it again at
    render time, but a row that can only ever hold a legal value is the
    better place to stop it), and `visibility` must be a `Visibility`
    member — `Visibility.parse` would resolve an unknown token to `HIDE` and
    silently hide a label the reader was trying to colour.

    `sort_order` — spec §10's "pinned order" — is writable in the repo and
    is deliberately **not** exposed here: nothing reads it. The nav orders
    labels alphabetically (`mailosh.services.mailbox_tree._build_label_tree`),
    and a control that persists a preference nothing reads is exactly the
    promise spec §3 forbids.
    """
    if not color and not visibility:
        return _error("Nothing to change.")
    if color and color not in LABEL_COLORS:
        return _error("That isn't a label colour.")
    if visibility and visibility not in {v.value for v in Visibility}:
        return _error("That isn't a visibility setting.")
    try:
        await service.require_label(client, mailbox_id)
    except LabelError as exc:
        return _error(str(exc))
    fields: dict[str, object] = {}
    if color:
        fields["color"] = color
    if visibility:
        fields["visibility"] = visibility
    await repo.set_label_meta(db, user.id, client.account_id, mailbox_id, **fields)  # type: ignore[arg-type]
    return _changed("Label updated")


@router.post("/{mailbox_id}/color/clear")
async def clear_color(client: ClientDep, user: UserDep, db: DbDep, mailbox_id: str) -> Response:
    """Drop a label's chosen colour, back to the hashed fallback every
    uncoloured label already gets. A separate route rather than `color=""`
    on `/meta`, so "no colour" is a thing the reader asks for explicitly and
    an empty form field can never mean it by accident.
    """
    try:
        await service.require_label(client, mailbox_id)
    except LabelError as exc:
        return _error(str(exc))
    await repo.set_label_meta(db, user.id, client.account_id, mailbox_id, color=None)
    return _changed("Colour cleared")


@router.post("/{mailbox_id}/delete")
async def delete(
    request: Request, client: ClientDep, user: UserDep, db: DbDep, mailbox_id: str
) -> Response:
    """Delete a label, keeping every message it held (spec §10:
    "conversations keep their other labels; confirm").

    The confirmation is the client's (`labels.js` draws it from the numbers
    `POST /labels/menu/{id}` handed it), and this route is what happens
    after it: `mailosh.services.labels.delete_label` strips the label off
    every message, moves anything that would be left in no mailbox at all to
    Archive, and only then destroys the mailbox — with
    `onDestroyRemoveEmails` at its default `False`, never `True`.

    The `LabelMeta` row goes last and only on success, so a delete refused
    part-way leaves the label with its colour intact rather than a live
    label that has silently lost it.
    """
    nav = await _nav_for(client, db, user)
    try:
        outcome = await service.delete_label(client, nav, mailbox_id)
    except LabelError as exc:
        return _error(str(exc), refresh=True)
    await repo.forget_label_meta(db, user.id, client.account_id, mailbox_id)
    toast = f"Deleted “{outcome.name}”"
    if outcome.archived:
        moved = "message" if outcome.archived == 1 else "messages"
        toast += f" — {outcome.archived} {moved} moved to Archive"
    return _changed(toast)


@router.post("/apply")
async def apply(
    request: Request,
    client: ClientDep,
    user: UserDep,
    db: DbDep,
    ids: IdsForm,
    add: OptionalIdsForm = None,
    remove: OptionalIdsForm = None,
) -> Response:
    """Apply and/or remove several labels across a whole selection.

    **Two JMAP requests, whatever the size**: one `Email/get` snapshot and
    one `Email/set` carrying a per-message patch. Two labels across twenty
    conversations is not forty round trips — see
    `mailosh.services.labels.apply_labels`.

    A partial failure answers `om:error` with the exact count that landed
    plus `refresh: true`, never a bare success and never a bare failure.
    """
    adding, removing = list(add or []), list(remove or [])
    nav = await _nav_for(client, db, user)
    names = {node.mailbox_id: node.name for node, _depth in _flatten(nav.labels)}
    unknown = [mid for mid in (*adding, *removing) if mid not in names]
    if unknown:
        return _error("One of those labels no longer exists.", refresh=True)
    try:
        result = await service.apply_labels(
            client, nav, ids, add=adding, remove=removing, names=names
        )
    except LabelError as exc:
        return _error(str(exc))
    except PartialApply as partial:
        return _error(
            f"Only {partial.applied} of {partial.total} messages could be updated.", refresh=True
        )
    return _done(request, client, result, labels_changed=True)


@router.post("/move")
async def move(
    request: Request,
    client: ClientDep,
    user: UserDep,
    db: DbDep,
    ids: IdsForm,
    to: Annotated[str, Form()],
) -> Response:
    """Move a selection into one mailbox and out of every other (spec
    §6.1's `v`).

    `to` may be a label's own mailbox id or a reserved nav key ("archive",
    "trash", ...), resolved the same way every other route in this app
    resolves one (`resolve_mailbox`) — so Move offers Archive and Trash
    without a second vocabulary for what those mean. A key with no mailbox
    on this account resolves to `None` and is refused rather than handed to
    `Email/set` as a literal string.
    """
    nav = await _nav_for(client, db, user)
    target = resolve_mailbox(nav, to)
    names = {node.mailbox_id: node.name for node, _depth in _flatten(nav.labels)}
    names.update(
        {item.mailbox_id: item.label for item in (*nav.system, *nav.more) if item.mailbox_id}
    )
    if target is None or target not in names:
        return _error("That folder isn't available on this account.", refresh=True)
    try:
        result = await service.move_to(client, nav, ids, target, name=names[target])
    except PartialApply as partial:
        return _error(
            f"Only {partial.applied} of {partial.total} messages could be moved.", refresh=True
        )
    return _done(request, client, result)
