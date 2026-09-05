"""The action routes (design spec §6.3): `POST /a/{archive,delete,spam,star,
read,undo}`.

Six thin handlers over `mailosh.services.actions` — no JMAP call, no undo
crypto and no view model is built here. Every one of them is `POST`, CSRF
protected (`deps.csrf_protect`, which also refuses `Sec-Fetch-Site:
cross-site` and never trusts `HX-Request` on its own), takes its selection as
repeated `ids` form fields, and answers `204 No Content` with an `HX-Trigger`
header rather than a body: the UI has already updated optimistically, so the
response's only job is to hand back the *canonical* delta.

    HX-Trigger: {"om:done": {"toast": "Archived", "undo": "<token>",
                             "removed": ["<thread id>"], "counts": {"inbox": -2}}}

`undo` answers `{"om:done": {"toast": "Undone", "refresh": true}}` — after a
reversal the client re-fetches rather than trying to re-derive which rows came
back and where they belong.

Deliberately *not* here (a later task owns the browser half): `static/js/
actions.js`, the toast fragment, the row buttons, and mounting this router in
`create_app`. The router is importable and testable on its own —
`tests/unit/test_actions.py` mounts it on a bare `FastAPI()`.

Failures are not swallowed: a `JmapError`/`TransportError` from the service
layer propagates to the app-level handler, which turns it into an error toast
plus a revert. A handler that answered 204 on a failed `Email/set` would leave
the optimistic UI permanently lying about what the mailbox contains.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient
from mailosh.services import actions
from mailosh.services.actions import ActionResult
from mailosh.services.mailbox_tree import NavModel, build_nav
from mailosh.services.undo import sign, verify
from mailosh.web import deps

router = APIRouter(prefix="/a", tags=["actions"], dependencies=[Depends(deps.csrf_protect)])

#: Design spec §6.3: "Bulk actions confirm only when they touch > 100
#: conversations." Counted here in *messages*, which is what the wire actually
#: carries — a selection is always at least as many messages as conversations,
#: so this can only ask a shade earlier than the spec's wording, never miss.
BULK_CONFIRM_OVER = 100

#: Byte budget for the WHOLE serialized `HX-Trigger` value — every part of it
#: (the undo token, the removed-row list, the toast, the counts) charged
#: against one number, because a proxy only ever sees the total.
#:
#: 3840 = nginx's default `proxy_buffer_size` of 4 KB, which has to hold the
#: status line and *every* response header, minus 256 bytes of headroom for the
#: rest of them. Exceeding it is not a truncated toast, it is nginx answering
#: `502 upstream sent too big header` and the action's result never arriving.
#: Apache (`LimitRequestFieldSize`) and Node (`maxHeaderSize`) default to 8 KB,
#: so nginx is the binding constraint.
MAX_TRIGGER_BYTES = 3840

#: Confirmation copy per action kind, for the `om:confirm` trigger below.
_CONFIRM_COPY = {
    "archive": "Archive {count} messages?",
    "delete": "Delete {count} messages?",
    "spam": "Report {count} messages as spam?",
    "star": "Update the star on {count} messages?",
    "read": "Update {count} messages?",
}


def _serialize(payload: dict[str, object]) -> str:
    """`payload` as the `HX-Trigger` header value.

    `ensure_ascii=True` (json's default) is load-bearing, not incidental: HTTP
    header values are latin-1, and a toast or mailbox name outside that range
    would otherwise raise on the way out. It also makes `len()` of this string
    the byte length that actually goes on the wire, which is what
    `MAX_TRIGGER_BYTES` is measured in.
    """
    return json.dumps(payload, separators=(",", ":"))


def _trigger(payload: dict[str, object]) -> dict[str, str]:
    return {"HX-Trigger": _serialize(payload)}


async def _nav(client: JmapClient) -> NavModel:
    """The nav model an action needs — one `Mailbox/get`.

    `label_meta={}` on purpose: an action reads only the *role* mailbox ids
    (Inbox, Archive, Trash, Junk, Drafts) out of this model, and label
    metadata is colour/visibility, i.e. purely how the sidebar renders. Passing
    an empty map keeps the action path free of a database round trip it would
    otherwise make on every keystroke-driven archive.
    """
    return await build_nav(client, active_key="", label_meta={})


def _done(request: Request, client: JmapClient, result: ActionResult) -> Response:
    """`204` + the `om:done` trigger carrying toast, undo token, removed thread
    ids and nav badge deltas.

    The undo token is scoped to this session's JMAP account (see
    `mailosh.services.undo`), so a token that leaks to another logged-in user
    cannot be replayed by them.

    The whole trigger is then fitted into `MAX_TRIGGER_BYTES` by shedding
    content in a fixed order, worst thing first:

    1. `removed` goes, replaced by `refresh: true`. Nothing is lost — naming
       the rows and re-fetching the list reach the same DOM — which is why it
       goes first even though, measured at this boundary (archive, 32-char
       ids, n=55: 1926 B of `removed` against a 1902 B token), it is not
       meaningfully bigger than the token that goes second. Recoverability,
       not size, decides the order.
    2. only if that is still not enough, `undo` goes, and the payload says so
       (`undo_unavailable`) so the toast can explain the missing button
       instead of silently not having one.

    `undo_unavailable` is a stable code (`"too_many"` or `"no_change"`), not
    display copy: nothing reads it yet, but when the toast does, it owns the
    wording — this field must never be turned back into an English fragment,
    or every future copy edit or i18n pass becomes a server change.

    Undo is shed last on purpose: it is the affordance a user cannot recreate
    for themselves, whereas a list re-fetch is one the client makes anyway.
    After step 2 what remains (toast, counts, two flags) is bounded at a couple
    of hundred bytes, so the budget is always met.
    """
    settings: Settings = request.app.state.settings
    token = (
        sign(result.spec, settings.secret_key, scope=client.account_id)
        if result.spec.email_ids
        else None
    )
    payload: dict[str, object] = {
        "toast": result.spec.toast,
        "undo": token,
        "removed": result.removed,
        "counts": result.counts,
    }
    if token is None:
        payload["undo_unavailable"] = "no_change"

    # Shedding `removed` only helps when there was something in it: star and
    # mark-read never remove a row, so `removed` is already `[]` and forcing
    # a `refresh: true` on top of it would buy nothing but a pointless
    # full-list re-fetch.
    if payload["removed"] and len(_serialize({"om:done": payload})) > MAX_TRIGGER_BYTES:
        payload["removed"] = []
        payload["refresh"] = True
    if len(_serialize({"om:done": payload})) > MAX_TRIGGER_BYTES:
        payload["undo"] = None
        payload["undo_unavailable"] = "too_many"
    return Response(status_code=204, headers=_trigger({"om:done": payload}))


def _needs_confirmation(kind: str, ids: list[str], confirm: bool) -> Response | None:
    """`409` + an `om:confirm` trigger when a selection is big enough to want
    a second look, or `None` to go ahead.

    A 409 (not a 4xx the error handler would toast) with the action untouched:
    nothing is read, nothing is written, and the client re-posts the same
    request with `confirm=1` once the user agrees. The ids are deliberately
    *not* echoed back — the client still has the selection, and a few thousand
    of them in a response header is exactly what this guard is trying to avoid.

    Counted as *distinct* ids: the write this guards (and `get_email_states`
    ahead of it) collapses duplicates one layer down, so counting raw form
    fields could ask "Archive 101 messages?" for a selection that is really
    one message repeated. Still deliberately conservative overall (spec
    §6.3's ">100 conversations" is counted here in messages, per
    `BULK_CONFIRM_OVER`'s own comment) — this only removes the part of that
    conservatism a duplicate id was adding for free.
    """
    distinct = len(dict.fromkeys(ids))
    if distinct <= BULK_CONFIRM_OVER or confirm:
        return None
    return Response(
        status_code=409,
        headers=_trigger(
            {
                "om:confirm": {
                    "kind": kind,
                    "count": distinct,
                    "message": _CONFIRM_COPY[kind].format(count=distinct),
                }
            }
        ),
    )


ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
IdsForm = Annotated[list[str], Form()]
ConfirmForm = Annotated[bool, Form()]
OnForm = Annotated[bool, Form()]


@router.post("/archive")
async def archive(
    request: Request, client: ClientDep, ids: IdsForm, confirm: ConfirmForm = False
) -> Response:
    """Take the selection out of the Inbox (spec §6.3, `e`)."""
    if (guard := _needs_confirmation("archive", ids, confirm)) is not None:
        return guard
    return _done(request, client, await actions.archive(client, await _nav(client), ids))


@router.post("/delete")
async def delete(
    request: Request, client: ClientDep, ids: IdsForm, confirm: ConfirmForm = False
) -> Response:
    """Move the selection to Trash (spec §6.3, `#`)."""
    if (guard := _needs_confirmation("delete", ids, confirm)) is not None:
        return guard
    return _done(request, client, await actions.delete(client, await _nav(client), ids))


@router.post("/spam")
async def spam(
    request: Request, client: ClientDep, ids: IdsForm, confirm: ConfirmForm = False
) -> Response:
    """Move the selection to Spam and flag it `$junk` (spec §6.3, `!`)."""
    if (guard := _needs_confirmation("spam", ids, confirm)) is not None:
        return guard
    return _done(request, client, await actions.spam(client, await _nav(client), ids))


@router.post("/star")
async def star(
    request: Request,
    client: ClientDep,
    ids: IdsForm,
    on: OnForm = True,
    confirm: ConfirmForm = False,
) -> Response:
    """Set (`on=1`) or clear (`on=0`) `$flagged` (spec §6.3, `s`)."""
    if (guard := _needs_confirmation("star", ids, confirm)) is not None:
        return guard
    return _done(request, client, await actions.star(client, await _nav(client), ids, on=on))


@router.post("/read")
async def read(
    request: Request,
    client: ClientDep,
    ids: IdsForm,
    on: OnForm = True,
    confirm: ConfirmForm = False,
) -> Response:
    """Set (`on=1`) or clear (`on=0`) `$seen` (spec §6.3, `Shift+I`/`Shift+U`)."""
    if (guard := _needs_confirmation("read", ids, confirm)) is not None:
        return guard
    return _done(request, client, await actions.mark_read(client, await _nav(client), ids, on=on))


@router.post("/undo")
async def undo(request: Request, client: ClientDep, token: Annotated[str, Form()]) -> Response:
    """Reverse whatever `token` describes, if it is still valid for *this*
    account (spec §6.3, `z` or the toast's Undo).

    Expired, tampered, wrongly signed and another account's tokens are all the
    same 400 with the same wording: an undo window is 10 s, so "no longer
    available" is both the honest and the overwhelmingly common answer, and
    saying more would help nobody but someone probing the signature.
    """
    settings: Settings = request.app.state.settings
    try:
        spec = verify(token, settings.secret_key, scope=client.account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="undo is no longer available") from exc
    await actions.apply_undo(client, spec)
    # Undo's own trigger carries no counts and no removed list: rows come
    # *back*, in positions only a fresh list render knows, so the client
    # re-fetches rather than trying to re-derive where each one belongs.
    return Response(
        status_code=204, headers=_trigger({"om:done": {"toast": "Undone", "refresh": True}})
    )
