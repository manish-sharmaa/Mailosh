"""Quick-settings prefs endpoint (design spec §10): `POST /prefs` persists a
per-user subset of `mailosh.db.models.UiPref` — the three *appearance*
fields the popover has always offered (`theme`, `density`, `shortcuts`) plus
the four *reading* ones whose consumers exist: `mark_read_delay` and
`auto_advance` (read by `mailosh.web.mail` on every conversation it
renders), `dark_restyle` (folded into `mailosh.render.dark.restyle_mode` by
`mailosh.web.frames`) and `remote_images` (`mailosh.render.image_policy.
decide`).

`conversation_view`, `reading_pane`, `undo_send_seconds` and `font_size` are
deliberately absent. This route and `shell/quick_settings.html` are one
contract — a field the panel cannot offer is a preference nobody can reach,
and a control for a preference nothing reads is exactly the promise spec §3
forbids the UI to make. Nothing in this phase branches on any of the four:
the list is always conversation-grouped, there is no reading pane, send is
1C's and the font-size control is 1D's. Each arrives with its consumer.

One thin handler, the same shape as `mailosh.web.actions`'s six: CSRF
protected (`deps.csrf_protect`, applied router-level exactly like
`actions.router`, so a missing/bad token 403s with zero downstream work —
no field validated, no row read or written) and authenticated via the
existing session (`deps.current_user`). It answers `204 No Content` with an
`HX-Trigger` header rather than a body — the panel applies
`data-theme`/`data-density` to `<html>` optimistically the instant a
control is clicked, so this response only needs to confirm what was
actually persisted, as a canonical payload the client-side `ui` store
(`static/js/app.js`) broadcasts from:

    HX-Trigger: {"om:prefs": {"theme": "dark"}}

Each field is `Form(...)`-optional (`None` when the client didn't send
it), so a request naming only one control is a genuine partial update —
`update_prefs` below only ever passes the fields actually present in the
body through to `mailosh.db.repo.set_prefs`, never the others at
whatever value they currently happen to hold. Every field's legal values
are spelled out as a `Literal` type rather than a bare `str`: FastAPI/
Pydantic rejects anything outside that set on its own, before this
function's body ever runs — the `422` the brief calls for on an invalid
value, with no bespoke validation code here that could drift out of sync
with the value sets.

That is also why `mark_read_delay` arrives as a **string** `Literal` and is
cast on the way to the database rather than typed as an `int`: an `int`
field would happily accept `7`, and this route would then have to grow a
range check of its own — a second, hand-written statement of what the
control offers, free to drift from the four values the popover renders. The
`Literal` is what rejects `7` before the handler body runs; the `int()` is
what keeps the column an integer.

**Two client halves, and the split between them is the difference between
an appearance preference and a reading one.** `theme`/`density`/
`shortcuts` change what is already on screen, so `app.js`'s `ui` store
owns them: it writes `<html>`'s dataset first and posts second, and
`shell/quick_settings.html` marks those three `data-pref` for it. The four
reading fields change nothing on the current page — they are read by the
*next* render, server-side (`mailosh.web.mail`) or by the frame routes —
so their controls post themselves with a plain `hx-post="/prefs"` and no
store entry at all. An optimistic paint for a preference with nothing to
paint would be ceremony around a no-op.

The router is importable and testable on its own: `tests/unit/
test_prefs.py` mounts it on a bare `FastAPI()`, exactly like
`tests/unit/test_actions.py` does for `mailosh.web.actions`.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db import repo
from mailosh.db.models import AppUser
from mailosh.web import deps

router = APIRouter(tags=["prefs"], dependencies=[Depends(deps.csrf_protect)])

#: The quick-settings value sets (design spec §10) — the *only* thing that
#: decides what's legal here. Kept as named aliases (rather than spelled out
#: inline on `update_prefs`'s signature) so each legal-values list exists
#: exactly once.
Theme = Literal["system", "light", "dark"]
Density = Literal["compact", "standard", "comfortable"]
#: Seconds before an open conversation marks itself read, as
#: `mailosh.web.mail` hands it to the page and `static/js/actions.js` arms
#: its timer from: `0` immediately, `-1` never.
MarkReadDelay = Literal["0", "1", "3", "-1"]
AutoAdvance = Literal["older", "newer", "list"]
RemoteImages = Literal["ask", "always", "contacts"]
#: The two boolean columns' wire form. `"true"`/`"false"` rather than a bare
#: `bool` for the same reason as `MarkReadDelay`: a form field is text, and
#: naming the two strings is what makes anything else a 422 rather than
#: something Pydantic coerces (`"yes"`, `"1"`, `"on"`) into a value the
#: control that posted it could never have produced.
Flag = Literal["true", "false"]

UserDep = Annotated[AppUser, Depends(deps.current_user)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]
ThemeForm = Annotated[Theme | None, Form()]
DensityForm = Annotated[Density | None, Form()]
ShortcutsForm = Annotated[Flag | None, Form()]
MarkReadDelayForm = Annotated[MarkReadDelay | None, Form()]
AutoAdvanceForm = Annotated[AutoAdvance | None, Form()]
RemoteImagesForm = Annotated[RemoteImages | None, Form()]
DarkRestyleForm = Annotated[Flag | None, Form()]


def _trigger(changed: dict[str, object]) -> dict[str, str]:
    """`changed` as the `om:prefs` `HX-Trigger` header value.

    `ensure_ascii=True` (`json.dumps`'s default, same as
    `mailosh.web.actions._serialize`) is kept for the same reason that
    module keeps it: consistency with the one other place this codebase
    builds an `HX-Trigger` value, even though every legal value of every
    field here is plain ASCII already and could never actually need the
    escaping.
    """
    return {"HX-Trigger": json.dumps({"om:prefs": changed}, separators=(",", ":"))}


@router.post("/prefs")
async def update_prefs(
    user: UserDep,
    db: DbDep,
    theme: ThemeForm = None,
    density: DensityForm = None,
    shortcuts: ShortcutsForm = None,
    mark_read_delay: MarkReadDelayForm = None,
    auto_advance: AutoAdvanceForm = None,
    remote_images: RemoteImagesForm = None,
    dark_restyle: DarkRestyleForm = None,
) -> Response:
    """Persist whichever of the seven fields were posted, to *this*
    session's own user (`user.id`, read from the authenticated session —
    never a client-supplied id, so one user's request can never reach
    another's `UiPref` row) — then answer `204` plus an `om:prefs` trigger
    naming exactly what changed.

    A request naming none of them (the empty subset — legal per the brief's
    "any subset") makes no database call at all: calling `repo.set_prefs`
    anyway would still create a first-time-defaults `UiPref` row as a side
    effect (`repo.get_prefs`'s own "create on first look-up" behaviour),
    which a request that changed nothing has no business triggering.

    Each field is read into `changed` at the type its column holds — text
    stays text, `"true"`/`"false"` become `bool`, `mark_read_delay` becomes
    `int` — because `repo.set_prefs` sets attributes straight onto the ORM
    row: a string reaching an `Integer` column persists on sqlite and then
    reads back as a string, so the page it configures would compare `"3"` to
    a number and quietly behave as though the reader had chosen nothing.
    """
    changed: dict[str, object] = {}
    if theme is not None:
        changed["theme"] = theme
    if density is not None:
        changed["density"] = density
    if shortcuts is not None:
        changed["shortcuts"] = shortcuts == "true"
    if mark_read_delay is not None:
        changed["mark_read_delay"] = int(mark_read_delay)
    if auto_advance is not None:
        changed["auto_advance"] = auto_advance
    if remote_images is not None:
        changed["remote_images"] = remote_images
    if dark_restyle is not None:
        changed["dark_restyle"] = dark_restyle == "true"

    if changed:
        await repo.set_prefs(db, user.id, **changed)

    return Response(status_code=204, headers=_trigger(changed))
