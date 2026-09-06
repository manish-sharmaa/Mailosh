"""In-process Server-Sent Events hubs, the background task that bridges JMAP
push (Stalwart's ``eventSourceUrl``) into them, and the per-user registry
that owns both (design spec §6.5).

Three pieces:

- ``SseHub``: a tiny pub/sub broker for browser-facing SSE. Any number of
  ``GET /events`` requests can each call ``subscribe()`` to get their own
  ``asyncio.Queue``-backed stream of ``ServerSentEvent``s; ``publish()``
  fans a named event out to every currently-subscribed queue. One hub per
  *user* (every tab/device that user has open subscribes to the same hub);
  the routing lives in ``HubRegistry`` below, not in the hub itself.

- ``stalwart_listener``: a long-running task (one per user, spawned lazily
  by ``HubRegistry.ensure_listener`` on that user's first ``GET /events``)
  that holds the *other* end of the SSE story open: a persistent GET
  against Stalwart's own JMAP ``eventSourceUrl``
  (``JmapClient.event_stream()``), turning any Email/Mailbox state change
  it sees into a ``hub.publish("mail", ...)`` for the browser side to react
  to (``mailosh/web/static/js/sse.js`` turns that into one coalesced
  ``mail:changed`` body event, which the list re-fetches itself on).

- ``HubRegistry``: ``user_id -> (SseHub, listener task)``, built once in
  ``create_app``'s lifespan (``app.state.hubs``). It guarantees exactly one
  listener per user however many tabs connect, hands ``stop_idle`` to the
  app's periodic maintenance task so an idle user's upstream connection
  doesn't live forever, and ``close``s everything (cancel *and* await) at
  shutdown. It also owns *recovery*: a listener that stops on its own —
  its pooled client was closed by the session that authorized it logging
  out — is deregistered and its hub's open browser streams are ended, so
  each tab re-dials ``/events`` and comes back on a live client. The
  alternative, and what this used to do, is the worst outcome available:
  a connection that stays up, keeps getting pings, and never carries
  another event (``SseHub.upstream_lost``).

All three live in *process* memory, which is why Phase 1 runs a single
uvicorn worker (design spec §12). ``HubRegistry`` optionally takes a
``NotifyBus`` (``mailosh.db.notify``, Postgres ``LISTEN``/``NOTIFY``) that
lifts that: a listener's change is relayed to peer processes, and a note
arriving from a peer is fanned out to this process's own subscribers for
that user. It is **additive and off by default** (``MAILOSH_SSE_FANOUT``,
``memory`` unless set to ``postgres``) — with no bus, every line below
behaves exactly as it did with none of this here. Read that module's
docstring before changing anything here: the fan-out connection is its own
silent-death surface and gets the same treatment this one does.

Heartbeat choice: rather than have ``SseHub.subscribe()`` hand-roll an idle
timeout that yields its own comment/ping ``ServerSentEvent``, ``GET
/events`` (``mailosh/web/events.py``) passes ``ping=25`` straight to
sse-starlette's ``EventSourceResponse`` — sse-starlette already implements
exactly this (a ``: ping - <timestamp>`` SSE comment sent whenever the
wrapped iterator has gone that many seconds without producing anything),
so duplicating it inside ``subscribe()`` would just be the same behavior
written twice, with its own copy of the "reset the idle clock on every
real event too" bookkeeping. ``subscribe()`` itself stays a plain "wait for
the next published event" loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from functools import partial

from sse_starlette import ServerSentEvent

from mailosh.db.notify import ChangeNote, NotifyBus
from mailosh.jmap import pool
from mailosh.jmap.client import JmapClient
from mailosh.jmap.models import StateChange

logger = logging.getLogger(__name__)

#: Cap on each subscriber's backlog (per-subscriber ``asyncio.Queue``). A
#: subscriber that falls this far behind (a stalled/backgrounded browser
#: tab) gets events dropped rather than letting `publish` block — see
#: `SseHub.publish`.
_QUEUE_MAXSIZE = 100

#: `stalwart_listener`'s reconnect backoff, in seconds: 1, 2, 4, ... capped
#: at 30, reset once a StateChange comes through on the new connection.
_BACKOFF_INITIAL = 1
_BACKOFF_CAP = 30

#: The JMAP object types a mail UI reacts to; everything else Stalwart might
#: push (``Thread``, ``VacationResponse``, ...) is ignored. `event_stream`
#: already asks the server for only these, so this is a second, local guard
#: rather than the only one. ``EmailSubmission`` is here for outbound
#: delivery tracking (`mailosh.services.outbound`): the list's own
#: ``mail:changed`` re-GET is what re-reads a Sent row's delivery pill, so
#: a submission's state change has to reach the browser the same way a new
#: message does.
_MAIL_TYPES = ("Email", "Mailbox", "EmailSubmission")

#: What `HubRegistry` hands `stalwart_listener` so a change it just
#: published locally also reaches other worker processes: called with the
#: same ``(types, state)`` the local `SseHub.publish` used. `None` — the
#: default, and every deployment that has not turned fan-out on — means
#: there is nobody else to tell.
ChangeRelay = Callable[[list[str], str | None], None]


class SseHub:
    """One user's pub/sub broker for browser-facing SSE.

    Every ``GET /events`` request that user has open (one per tab, per
    device) subscribes to the same hub, so one upstream JMAP connection
    fans out to all of them. Which hub a request gets is `HubRegistry`'s
    job — a hub itself has no notion of identity, it only fans events out
    to whoever is currently subscribed.

    `last_activity` is the idle clock `HubRegistry.stop_idle` reads: it
    advances on every publish and whenever a subscriber arrives or leaves,
    so "idle" means *nothing has happened here recently*, not merely "no
    one is connected this instant" (a user clicking between pages drops to
    zero subscribers for a moment on every navigation).

    The one thing a hub tracks besides its subscribers is whether anything
    is still *feeding* it (`upstream_lost`/`upstream_ready`), because a hub
    with no producer must end its streams rather than hold browsers open on
    a promise it can no longer keep. It still doesn't know who that
    producer is, or whose hub this is — `HubRegistry` owns both.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[ServerSentEvent | None]] = set()
        self.last_activity = datetime.now(UTC)
        #: Whether something is currently feeding this hub. Starts `True`
        #: — a hub is only ever created on the way to arming a listener
        #: (`HubRegistry.hub_for`/`ensure_listener`), and a bare `SseHub`
        #: driven straight by a test or a future producer has no registry
        #: to tell it otherwise. See `upstream_lost`.
        self._upstream_live = True

    @property
    def subscriber_count(self) -> int:
        """How many `/events` streams are attached right now."""
        return len(self._subscribers)

    def touch(self) -> None:
        """Reset the idle clock `HubRegistry.stop_idle` measures against."""
        self.last_activity = datetime.now(UTC)

    async def subscribe(self) -> AsyncIterator[ServerSentEvent]:
        """Register a new subscriber queue and yield events published to it.

        Registration/unregistration span exactly this generator's
        lifetime: the queue is added to ``_subscribers`` before the first
        ``yield`` and *always* removed in ``finally`` — whether the caller
        stops iterating (sse-starlette closes this generator when the
        browser disconnects, throwing ``GeneratorExit`` in), the
        surrounding task is cancelled (app shutdown), or anything else
        goes wrong — so a subscriber can never outlive the request that
        created it. Nothing here needs its own locking: both this method
        and `publish` only ever touch `_subscribers` between `await`
        points, and asyncio's single-threaded cooperative scheduling means
        neither can be interrupted mid-mutation by the other.

        Ends — rather than blocking forever — in the two cases where
        staying open would be a lie: a `None` arriving on the queue
        (`upstream_lost` telling live subscribers their feed is gone), and
        a hub whose upstream is *already* gone when this is called. Both
        make sse-starlette finish the response, which is what gets the
        browser's `EventSource` to fire `error` and re-dial `/events` —
        the request that arms a fresh listener. See `upstream_lost`.
        """
        if not self._upstream_live:
            self.touch()
            return
        queue: asyncio.Queue[ServerSentEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        self.touch()
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            self._subscribers.discard(queue)
            self.touch()

    def end_streams(self) -> None:
        """End every *currently open* subscriber stream, without latching:
        a subscriber arriving after this is served normally.

        The non-latching half of `upstream_lost` below, split out for the
        one caller whose feed is not gone — `HubRegistry` when the
        cross-process fan-out connection drops (`mailosh.db.notify`). There
        the local `stalwart_listener` is still very much alive and this
        hub's next `GET /events` must succeed; what is lost is only the
        *peer* half of the feed, and cutting the open streams is how each
        tab is made to re-dial and re-fetch rather than sit on a connection
        that has silently stopped hearing what other workers see. Latching
        that case would be a hot loop instead: `ensure_listener` returns
        early when a listener is already running, so it would never call
        `upstream_ready`, and every re-dial would be cut on arrival.

        Draining a full queue before the sentinel matters: the subscriber
        most in need of being told is exactly the one already backed up
        (`publish` drops events for it), and a `put_nowait` on a full queue
        would raise instead. The backlog is worthless anyway — this stream
        is ending.
        """
        self.touch()
        for queue in self._subscribers:
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(None)

    def upstream_lost(self) -> None:
        """End every open subscriber stream, and end new ones on arrival
        until `upstream_ready` says something is feeding this hub again.

        `HubRegistry` calls this when a user's `stalwart_listener` stops —
        its pooled client was closed by a logout, a session revoke, or (a
        server restart aside) anything else that takes the upstream JMAP
        connection away. Without it that is a *silent* failure and the
        worst possible outcome: sse-starlette keeps pinging, so the
        browser's `EventSource` never errors, never reconnects, and
        `sse.js` never sets `offline` — the tab looks live and simply
        stops receiving mail, forever (review findings 1 and 2). Ending the
        streams converts that into the one thing the browser knows how to
        handle: a dropped connection, which it re-dials within seconds,
        re-running `ensure_listener` on the way in — and if the re-dial
        can't succeed, `sse.js` shows the offline banner (design spec
        §5.4) instead of nothing at all.

        Latched, unlike `end_streams` above: a subscriber that arrives a
        tick later — a second tab whose `GET /events` passed
        `ensure_listener` before the listener died — is ended on arrival
        too, rather than settling into a stream nothing will ever feed.
        """
        self._upstream_live = False
        self.end_streams()

    def upstream_ready(self) -> None:
        """Undo `upstream_lost`: a listener is feeding this hub again, so
        new subscribers are served normally. Called by
        `HubRegistry.ensure_listener` as it arms one — before the same
        `GET /events` request goes on to `subscribe()`.
        """
        self._upstream_live = True

    def publish(self, event: str, data: str = "", event_id: str | None = None) -> None:
        """Fan `event` out to every current subscriber; never blocks.

        Uses `queue.put_nowait` rather than `await queue.put(...)`: a
        subscriber whose queue is already full (a browser tab that
        stopped reading — backgrounded, slow network, whatever) has this
        event silently dropped *for that subscriber only*, instead of
        making this call block — which would stall every other
        subscriber's delivery (and whatever caller, e.g.
        `stalwart_listener`, is calling `publish` in the first place)
        behind one stuck reader.

        `event_id`, when given, becomes the frame's SSE ``id:`` line — for
        `mail` events, the JMAP state string (design spec §6.5: "the SSE
        `id:` carries the JMAP state so the server can replay via
        `Email/changes`"). The browser echoes the most recent one back as
        `Last-Event-ID` when its `EventSource` reconnects.
        """
        sse_event = ServerSentEvent(data=data, event=event, id=event_id)
        self.touch()
        for queue in self._subscribers:
            try:
                queue.put_nowait(sse_event)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full; dropping %r event", event)


def is_mail_change(change: StateChange) -> bool:
    """True if `change` includes an ``Email``, ``Mailbox`` or
    ``EmailSubmission`` type change (`_MAIL_TYPES`), for any account.

    Kept a plain, non-underscored function specifically so it has its own
    direct unit tests (see `tests/unit/test_sse_hub.py`) covering the
    "any account" breadth separately from `stalwart_listener`'s own
    (necessarily async, stub-client-driven) test.
    """
    return any(any(wanted in types for wanted in _MAIL_TYPES) for types in change.changed.values())


def mail_change_types(change: StateChange) -> list[str]:
    """The mail object types `change` touched, sorted and de-duplicated
    across accounts — the ``{"types": [...]}`` body of the browser-facing
    ``mail`` event.

    Only ``Email``/``Mailbox`` (`_MAIL_TYPES`) survive: anything else a
    server pushes is not something this UI re-renders for, and letting it
    through would make the client fire a needless refetch.
    """
    seen = {t for types in change.changed.values() for t in types if t in _MAIL_TYPES}
    return sorted(seen)


def latest_state(change: StateChange) -> str | None:
    """The JMAP state string to put on the ``mail`` event's SSE ``id:``
    line, or `None` if `change` carries no mail state at all.

    Prefers the ``Email`` state over the ``Mailbox`` one — `Email/changes`
    is what a reconnecting client would replay with (design spec §6.5), and
    a mailbox-count-only change is the weaker of the two signals. A user has
    exactly one Stalwart account here (`mailosh.security.exchange`), so the
    "first account that has one" scan below is not really a choice between
    several candidate states; it is written to tolerate the multi-account
    shape `StateChange` can carry rather than assuming a single key.
    """
    for wanted in _MAIL_TYPES:
        for types in change.changed.values():
            state = types.get(wanted)
            if state is not None:
                return state
    return None


def _client_is_closed(client: JmapClient) -> bool:
    """True once `client`'s underlying `httpx.AsyncClient` has been closed —
    i.e. the pooled client this listener runs on was dropped
    (`ClientPool.drop` at logout/session revoke, or its idle sweep).

    Reads `JmapClient._http` (that module's own private attribute) through
    `getattr` rather than a public property: `mailosh/jmap/client.py` is
    not this task's file to change, and the `getattr` defaults also keep the
    stub clients this module's unit tests inject — plain objects with just
    an `event_stream` method — working, treating "no `_http` at all" as "not
    closed" so their reconnect behaviour is unaffected.
    """
    http = getattr(client, "_http", None)
    return bool(getattr(http, "is_closed", False))


async def stalwart_listener(
    client: JmapClient, hub: SseHub, relay: ChangeRelay | None = None
) -> None:
    """Forever: stream JMAP push state changes from `client`, publishing
    `hub`'s ``mail`` event whenever one touches Email or Mailbox — the
    event name, ``{"types": [...]}`` body and ``id:`` state string design
    spec §6.5 specifies, and that
    `mailosh/web/static/js/sse.js` consumes.

    `relay`, when given (`HubRegistry` passes one only when a cross-process
    fan-out bus is configured — see `mailosh.db.notify`), is called with
    the same types/state *after* the local publish, so it can tell peer
    workers. Second, never first: a browser tab attached to this process
    must never wait on a database round trip to see its own mail, and
    `NotifyBus.publish` is non-blocking precisely so this stays true.

    Reconnects with exponential backoff (1, 2, 4, ... capped at 30s, reset
    once a StateChange comes through), since `JmapClient.event_stream`
    itself never retries (see its own docstring) — retry policy is entirely
    this function's job. The backoff sleep applies to a *clean* end of
    stream too, not just to a failure: a server that accepts the GET and
    immediately closes it again would otherwise send this loop spinning
    reconnect-after-reconnect with no delay at all, since a generator that
    simply returns raises nothing for the `except` leg to catch.

    Runs inside `mailosh.jmap.pool.streaming(client)` for its whole life,
    which is what stops the pool's idle sweep closing this connection out
    from under a tab that is still watching it: `ClientPool` measures use
    by `get` calls, and a browser parked on an open ``/events`` makes none
    of those (review finding 1). A *deliberate* `ClientPool.drop` — logout,
    session revoke, the reaper — is unaffected and still ends this
    listener, which is the correct behaviour: it was authorized by that
    session.

    Returns (rather than retrying) once `client` has been closed: that only
    happens when the pool dropped it — the user logged out — and every
    reconnect against a closed `httpx.AsyncClient` can only fail the same
    way forever. Returning is not the end of the story for the *user*,
    though: `HubRegistry` watches for a listener that stops, ends the
    hub's open browser streams so each tab re-dials `/events`, and arms a
    fresh listener on whatever live pooled client that request brings.

    Exits promptly on `asyncio.CancelledError`: this is spawned as a
    background task by `HubRegistry` and must unwind cleanly on shutdown
    rather than swallowing the cancellation it sends. `CancelledError`
    derives from `BaseException`, not `Exception` (since Python 3.8), so
    the broad `except Exception` below already can't catch it — no separate
    case needed to satisfy that requirement.
    """
    backoff = _BACKOFF_INITIAL
    with pool.streaming(client):
        while True:
            try:
                # `aclosing` rather than a bare `async for`: `event_stream`
                # holds an open `httpx` streamed response for as long as its
                # generator is alive, and a generator abandoned mid-iteration
                # (cancellation at shutdown, or a `break`) is only finalized
                # whenever the loop's async-generator hooks get round to it.
                # Closing it here releases that connection at the moment we
                # stop reading it.
                async with aclosing(client.event_stream()) as stream:
                    async for change in stream:
                        if is_mail_change(change):
                            types = mail_change_types(change)
                            state = latest_state(change)
                            hub.publish("mail", json.dumps({"types": types}), event_id=state)
                            if relay is not None:
                                relay(types, state)
                        backoff = _BACKOFF_INITIAL
            except Exception:
                if _client_is_closed(client):
                    logger.info("stalwart_listener: client closed, stopping")
                    return
                logger.exception(
                    "stalwart_listener: event stream failed, reconnecting in %ss", backoff
                )
            else:
                if _client_is_closed(client):
                    logger.info("stalwart_listener: client closed, stopping")
                    return
                logger.warning(
                    "stalwart_listener: event stream ended, reconnecting in %ss", backoff
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)


class HubRegistry:
    """``user_id -> (SseHub, upstream listener task)``, built once per app
    (`create_app`'s lifespan, as `app.state.hubs`).

    One hub and at most one listener per user: every tab that user has open
    subscribes to the same hub, so N tabs cost one upstream JMAP
    EventSource connection, not N. Listeners start lazily — the first
    `GET /events` after login — and stop again from `stop_idle`, run by the
    app's periodic maintenance task, so a user who closed the browser three
    hours ago is not still holding a connection open against Stalwart.

    Nothing here takes a lock. Every mutating method below runs its whole
    dict read-modify-write between `await` points (`asyncio.create_task`
    schedules, it does not yield), and asyncio's single-threaded
    cooperative scheduling means no two of them can interleave mid-update —
    the same reasoning `SseHub.subscribe`/`publish` already document. That
    is what makes `ensure_listener` safe against the realistic race here:
    two tabs of the same user hitting `/events` in the same tick still
    produce exactly one listener.

    `bus` is the optional cross-process half (`mailosh.db.notify`). Given
    one, this registry both *publishes* (every change its own listeners see
    becomes a `ChangeNote` for peer workers, via the `relay` it hands
    `stalwart_listener`) and *receives* (`_deliver_remote` fans a peer's
    note out to the local hub for that user, and only that user — routing
    is by `ChangeNote.user_id` into `self._hubs`, so there is no path by
    which one user's note can reach another's subscribers, in this process
    or any other). `None`, the default, is the single-process behaviour
    this class had before and still has: nothing is published, nothing is
    received, and no bus method is ever called.
    """

    def __init__(self, bus: NotifyBus | None = None) -> None:
        self._hubs: dict[int, SseHub] = {}
        self._listeners: dict[int, asyncio.Task[None]] = {}
        #: The cross-process fan-out transport, or `None` for the default,
        #: single-process behaviour — in which case nothing below changes
        #: at all: no relay is handed to a listener, no note is ever
        #: published, and this class is exactly what it was.
        self._bus = bus
        if bus is not None:
            bus.subscribe(self._deliver_remote, self._fanout_lost)

    async def start(self) -> None:
        """Start the fan-out bus, if there is one. A no-op otherwise, so
        `create_app`'s lifespan can call it unconditionally.
        """
        if self._bus is not None:
            await self._bus.start()

    def hub_for(self, user_id: int) -> SseHub:
        """`user_id`'s hub, created on first use."""
        hub = self._hubs.get(user_id)
        if hub is None:
            hub = self._hubs[user_id] = SseHub()
        return hub

    async def ensure_listener(self, user_id: int, client: JmapClient) -> None:
        """Make sure exactly one `stalwart_listener` is running for
        `user_id`, starting one on `client` if there isn't.

        `client` must be that user's *pooled* `JmapClient`
        (`mailosh.web.deps.client_for`), never a freshly connected one: a
        listener holding a private connection would outlive the session it
        was authorized by, and keep streaming a logged-out user's mail
        events. A pooled client is closed the moment the session goes away,
        which `stalwart_listener` treats as "stop".

        Idempotent, including for a listener that has already finished on
        its own (its client was closed under it): a finished task is
        replaced rather than mistaken for a live one, so another still-live
        session of the same user gets a working listener back.

        `upstream_ready` runs before the task is even created, and
        therefore before the `hub.subscribe()` the same `GET /events`
        request is about to make: a hub whose previous listener died is
        open for business again the moment a new one is armed, rather than
        cutting off the very stream that just revived it.
        """
        hub = self.hub_for(user_id)
        hub.touch()
        existing = self._listeners.get(user_id)
        if existing is not None and not existing.done():
            return
        hub.upstream_ready()
        relay = None if self._bus is None else partial(self._relay, user_id)
        task = asyncio.create_task(
            stalwart_listener(client, hub, relay), name=f"stalwart-listener-user-{user_id}"
        )
        task.add_done_callback(partial(self._listener_finished, user_id))
        self._listeners[user_id] = task

    def notify(self, user_id: int, types: list[str]) -> None:
        """Tell `user_id`'s open tabs that something changed, from *this*
        process rather than from a Stalwart push — the same ``mail`` event
        `stalwart_listener` publishes, with the same ``{"types": [...]}``
        body, so the browser reacts exactly as it does to live mail
        (`static/js/sse.js` fires ``mail:changed`` and the list re-GETs).

        For the server-side discoveries a push cannot announce: the
        maintenance sweep in `mailosh.services.outbound` learns a message
        bounced by *polling* ``EmailSubmission/get``, and the tab showing
        Sent has to redraw that row's pill without a reload.

        Publishes only into a hub that already exists — a user with no tab
        open has nobody to tell, and creating a hub for them would only
        give `stop_idle` something to sweep. Relayed to peer workers when a
        fan-out bus is configured, same as a listener's own publish.
        """
        hub = self._hubs.get(user_id)
        if hub is not None:
            hub.publish("mail", json.dumps({"types": list(types)}))
        if self._bus is not None:
            self._relay(user_id, list(types), None)

    def _relay(self, user_id: int, types: list[str], state: str | None) -> None:
        """Tell peer workers about a change `user_id`'s local listener just
        published — `stalwart_listener`'s `relay` hook.

        Carries a routing hint, never the change: see `mailosh.db.notify`
        for why (``NOTIFY``'s 8000-byte payload cap, and the fact that a
        peer's browsers re-fetch anyway). Non-blocking and non-raising by
        `NotifyBus.publish`'s contract, so a database that is slow or gone
        costs peers their copy and nothing else.
        """
        bus = self._bus
        if bus is None:
            return
        bus.publish(ChangeNote(user_id=user_id, types=tuple(types), state=state))

    def _deliver_remote(self, note: ChangeNote) -> None:
        """Fan a peer worker's change note out to *this* process's
        subscribers for that user — `NotifyBus`'s `on_note` callback.

        Two deliberate refusals, both about not letting a remote note do
        more than a local one would:

        - `self._hubs.get`, never `hub_for`: a note for a user with no hub
          here is for a user with no tab here, and creating one would
          resurrect a hub `stop_idle` just collected, for a browser that
          does not exist, on every worker in the fleet.
        - nothing at all when that hub has no subscriber right now. A
          `publish` into a subscriberless hub delivers to nobody anyway
          (there are no queues) — its only effect would be `touch`, and
          that is exactly the effect to avoid: peers' notes would keep an
          idle hub's `last_activity` fresh forever, so `stop_idle` would
          never reclaim a user's upstream JMAP connection on a worker they
          stopped using hours ago.
        """
        hub = self._hubs.get(note.user_id)
        if hub is None or hub.subscriber_count == 0:
            return
        hub.publish("mail", json.dumps({"types": list(note.types)}), event_id=note.state)

    def _fanout_lost(self) -> None:
        """End every open stream when the fan-out connection drops —
        `NotifyBus`'s `on_lost` callback.

        The same answer `_listener_finished` gives to a dead JMAP listener,
        for the same reason: a stream that will not carry the events it
        promises must end, so the browser re-dials and re-fetches, rather
        than look healthy while going quiet (`SseHub.upstream_lost`). What
        differs is the latch — `end_streams`, not `upstream_lost`. The
        local listener is still alive and still feeding this hub, so the
        re-dial that follows must *succeed*; latching would end it on
        arrival instead, in a loop, because `ensure_listener` returns
        early (and so never calls `upstream_ready`) while a listener runs.
        """
        ended = sum(hub.subscriber_count for hub in self._hubs.values())
        logger.warning(
            "SSE fan-out connection lost; ending %d open stream(s) so they re-dial", ended
        )
        for hub in self._hubs.values():
            hub.end_streams()

    def _listener_finished(self, user_id: int, task: asyncio.Task[None]) -> None:
        """Done-callback for `user_id`'s listener: deregister it and tell
        the hub its feed is gone.

        A listener that stops used to be terminal. It holds *one session's*
        pooled client while the hub is per **user**, so a logout in one tab
        (`ClientPool.drop`) — or, before `pool.streaming`, the pool's own
        30-minute idle sweep — closed that client under it and it returned
        for good, while the user's other tabs kept a `/events` stream that
        looked perfectly healthy and would never carry another event: with
        a subscriber still attached, `stop_idle` would never reclaim the
        hub either (review findings 1 and 2). Dropping the dead task here
        means the next `GET /events` arms a fresh listener rather than
        finding a corpse registered; `hub.upstream_lost` ends the open
        streams so that request actually happens, seconds later, instead of
        waiting on a browser that has no way to know anything is wrong.

        Skipped when the task is no longer the registered one: `stop_idle`
        and `close` pop before they cancel, so a deliberate cancellation
        lands here with nothing to do — the hub is being torn down, not
        recovered.
        """
        _log_listener_result(task)
        if self._listeners.get(user_id) is not task:
            return
        del self._listeners[user_id]
        hub = self._hubs.get(user_id)
        if hub is None:
            return
        logger.info(
            "SSE listener %s stopped; ending %d open stream(s) so they re-dial",
            task.get_name(),
            hub.subscriber_count,
        )
        hub.upstream_lost()

    async def stop_idle(self, idle_seconds: int = 1800) -> None:
        """Cancel the listener of every user whose hub has no subscribers
        and has seen no activity for `idle_seconds`, and forget that hub
        (design spec §6.5: "started on first request after login, stopped
        after 30 min idle").

        Called every ~5 minutes by `create_app`'s maintenance task. Without
        it a listener would outlive its last tab forever — nothing else in
        the system ever tells the registry that a user went away, since a
        browser closing its `EventSource` only ends one subscription, not
        the upstream connection that feeds it. The next `GET /events` from
        that user simply builds a fresh hub and listener.

        `idle_seconds=0` collects every hub that has no subscriber right
        now — used by tests, and the same "cutoff of zero" convention
        `ClientPool.stop_idle` already offers.

        The idle list is built up front but re-checked per user immediately
        before that user's hub is popped, because the loop `await`s each
        cancellation and a `GET /events` can land in any of those gaps
        (review finding 3): the arriving request sees a live listener, and
        would then have had its hub popped and that listener cancelled out
        from under it — a tab subscribed to an orphaned hub nothing feeds
        and, since its connection stays open, nothing tells. Both halves of
        the condition are re-read: `subscriber_count` catches the new
        stream, `last_activity` catches a request that touched the hub
        without (yet) subscribing.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=idle_seconds)
        idle = [
            user_id
            for user_id, hub in self._hubs.items()
            if hub.subscriber_count == 0 and hub.last_activity <= cutoff
        ]
        for user_id in idle:
            hub = self._hubs.get(user_id)
            if hub is None or hub.subscriber_count != 0 or hub.last_activity > cutoff:
                continue
            self._hubs.pop(user_id, None)
            await _cancel(self._listeners.pop(user_id, None))

    async def close(self) -> None:
        """Cancel *and await* every listener, and forget every hub — called
        once, from `create_app`'s lifespan shutdown, before the pool closes
        the clients those listeners are streaming from.

        Awaiting (not just `cancel()`-ing) is the point: a merely-cancelled
        task is still pending when the event loop closes, which is exactly
        what makes Python print "Task was destroyed but it is pending!".
        Idempotent — a second call has nothing left to cancel.

        Every open browser stream is ended first (`upstream_lost`), for the
        same reason it is ended when a single listener dies: without it, a
        subscriber's `subscribe()` generator has no way to learn its feed
        is gone and sits parked on `queue.get()` indefinitely. That is not
        what makes process shutdown itself graceful — Uvicorn only runs
        lifespan shutdown (where this method runs) after it has already
        finished waiting on in-flight connections, and sse-starlette 3.4.8
        separately monkey-patches `uvicorn.Server.handle_exit` to set
        `AppStatus.should_exit`, which drains every open
        `EventSourceResponse` at signal time — before that wait, and so
        before this method ever runs. Calling `upstream_lost` here anyway
        keeps each `SseHub`'s own bookkeeping correct on its own terms:
        every subscriber generator gets to run its normal `finally`
        cleanup rather than being torn down however the ASGI server ends
        an in-flight response, and that no longer depends on a monkey-patch
        this module doesn't own. Each browser sees its connection close and
        re-dials the restarted server on its own.

        The fan-out bus, if there is one, is closed last — after the
        listeners that feed it are gone, so nothing is still trying to
        publish into a transport that is shutting down.
        """
        hubs = list(self._hubs.values())
        tasks = list(self._listeners.values())
        self._listeners.clear()
        self._hubs.clear()
        for hub in hubs:
            hub.upstream_lost()
        for task in tasks:
            await _cancel(task)
        if self._bus is not None:
            # Last, and by this registry rather than the lifespan: the bus
            # exists only to serve these hubs, so it outlives none of them,
            # and a caller that built a registry with one can never leak
            # its tasks by forgetting a second close call.
            await self._bus.close()


def _log_listener_result(task: asyncio.Task[None]) -> None:
    """Logs an unexpected listener failure and, just as importantly,
    *retrieves* the exception so asyncio doesn't report "Task exception was
    never retrieved" when the task is garbage collected
    (`HubRegistry._listener_finished`, the done-callback that calls this,
    drops finished tasks on the floor).
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("SSE listener %s stopped: %r", task.get_name(), exc)


async def _cancel(task: asyncio.Task[None] | None) -> None:
    """Cancel `task` and wait for it to actually finish unwinding.

    A listener that had already stopped by itself is awaited too — that is
    how its result/exception gets retrieved, and `await`ing an
    already-finished task returns immediately. Any exception it ended with
    is logged rather than propagated: `close`/`stop_idle` are cleanup paths,
    and one broken listener must not abort the sweep (or app shutdown) for
    every other user.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("SSE listener %s failed", task.get_name())
