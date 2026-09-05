"""``GET /events`` — the browser-facing half of per-user live updates
(design spec §6.5, Task 7).

One long-lived SSE response per open tab. It subscribes to the current
user's `SseHub` (`mailosh.sse.HubRegistry`, built by `create_app`'s
lifespan as ``app.state.hubs``) and, on the way in, makes sure that user's
single upstream listener is running — the JMAP EventSource connection to
Stalwart whose state changes become the ``mail`` events streamed back here.

Deliberately *not* a wrapper around the hub's stream: the response is
`EventSourceResponse(hub.subscribe(), ping=25)` and nothing else, so the
frames a browser receives are exactly the ones `stalwart_listener`
published — see `mailosh.sse` for why the ping is sse-starlette's job and
not the hub's.

Why this module has its own session/client dependencies instead of using
`deps.require_session`/`deps.client_for` directly: those raise
`SessionRequired`, which `create_app`'s handler turns into a 303 to the
login page (or a 401 + `HX-Redirect` for an HX request). Neither shape is
usable here — an `EventSource` is not htmx and cannot render a login page;
following a redirect to `text/html` just makes it fail the connection with
an opaque error. `stream_session` wraps the very same `require_session`
call (so any future change to what "a valid session" means applies here
too) and converts that one failure into the plain 401 the EventSource
contract expects, and `stream_client` then reuses `deps.client_for`
verbatim on top of it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette import EventSourceResponse

from mailosh.db.models import SessionRow
from mailosh.jmap.client import JmapClient
from mailosh.sse import HubRegistry
from mailosh.web import deps

#: sse-starlette sends a ``: ping - <timestamp>`` comment frame whenever the
#: stream has produced nothing for this long. Comfortably under the 60s
#: idle timeout typical of reverse proxies, so an idle connection is kept
#: alive rather than silently dropped and reconnected every minute.
_PING_SECONDS = 25

router = APIRouter()


async def stream_session(
    request: Request, session: Annotated[SessionRow | None, Depends(deps.current_session)]
) -> SessionRow:
    """`deps.require_session`, but answering 401 instead of the login
    redirect — see this module's docstring."""
    try:
        return await deps.require_session(request, session)
    except deps.SessionRequired as exc:
        raise HTTPException(status_code=401, detail="Authentication required") from exc


async def stream_client(
    request: Request, session: Annotated[SessionRow, Depends(stream_session)]
) -> JmapClient:
    """The current session's *pooled* `JmapClient` — the same one every
    other route uses. It matters that this is the pooled client and not a
    private connection: the listener it feeds must die with the session
    that authorized it (`mailosh.sse.HubRegistry.ensure_listener`).
    """
    return await deps.client_for(request, session)


@router.get("/events")
async def events(
    request: Request,
    session: Annotated[SessionRow, Depends(stream_session)],
    client: Annotated[JmapClient, Depends(stream_client)],
) -> EventSourceResponse:
    """Stream this user's live mail events until the browser disconnects.

    `ensure_listener` is called per request rather than once at login
    because there is no other moment that reliably has both a user id and a
    live pooled client in hand — and it is idempotent, so the second tab
    (and every reconnect after a dropped connection) costs a dict lookup.
    """
    hubs: HubRegistry = request.app.state.hubs
    await hubs.ensure_listener(session.user_id, client)
    hub = hubs.hub_for(session.user_id)
    return EventSourceResponse(hub.subscribe(), ping=_PING_SECONDS)
