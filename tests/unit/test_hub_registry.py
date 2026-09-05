"""Task 7 (per-user live updates): `mailosh.sse.HubRegistry` — the
per-user `SseHub` + per-user Stalwart listener registry `create_app`'s
lifespan owns (`app.state.hubs`) and `GET /events` drives.

Pure in-process asyncio, like `tests/unit/test_sse_hub.py`: every test here
injects a stub client (a plain class with an `event_stream`
async-generator method) rather than a real `JmapClient`, so nothing in this
file opens a socket or needs a Stalwart. What *is* exercised for real is
the lifecycle design spec §6.5 asks for and that a long-running server gets
wrong easily: exactly one listener task per user no matter how many tabs
connect at once, idle listeners cancelled (and their hubs forgotten) rather
than leaked forever, and a shutdown that cancels *and awaits* every task so
Python never prints "Task was destroyed but it is pending!" on the way out.

`registry._listeners` is read directly in a few assertions below — the same
"a unit test may look at its own module's privates" convention
`test_sse_hub.py` already uses for `hub._subscribers`, and preferable to
widening `HubRegistry`'s public surface past the four methods the plan's
Interfaces block locks down (`hub_for`, `ensure_listener`, `stop_idle`,
`close`) purely for test introspection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mailosh.jmap.models import StateChange
from mailosh.sse import HubRegistry

CHANGE = StateChange(changed={"acc1": {"Email": "s9"}})


async def _until(predicate, *, attempts: int = 1000) -> None:
    """Poll `predicate()` across up to `attempts` bare event-loop turns —
    same helper (and rationale) as `tests/unit/test_sse_hub.py`'s: wait for
    a just-`create_task`-ed coroutine to reach its first real `await`
    without a wall-clock sleep or a fixed guess at how many turns it takes.
    """
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never became true")


async def _settle(turns: int = 20) -> None:
    """Yield the event loop `turns` times, so a task that *would* have done
    something more (a second listener connecting, say) has had every chance
    to before a "it didn't" assertion runs.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


class StubClient:
    """A `JmapClient` stand-in whose event stream yields one `StateChange`
    and then blocks forever, exactly like a real idle EventSource
    connection that has already reported one change. `streams` counts how
    many times `stalwart_listener` (re)connected, which is what the
    "exactly one listener task per user" assertions actually measure.
    """

    def __init__(self) -> None:
        self.streams = 0

    async def event_stream(self):
        self.streams += 1
        yield CHANGE
        await asyncio.Event().wait()


class ClosedClient:
    """A pooled client that has already been closed — what a user's
    `JmapClient` becomes the moment they log out (`ClientPool.drop`). Its
    stream ends immediately and its underlying httpx client reports closed,
    which is the signal `stalwart_listener` must treat as "stop", not
    "reconnect in a second, forever".
    """

    class _Http:
        is_closed = True

    def __init__(self) -> None:
        self._http = self._Http()

    async def event_stream(self):
        return
        yield  # pragma: no cover - unreachable; keeps this an async generator


class DyingClient:
    """A pooled client that starts healthy and is then closed *under* the
    listener — `ClientPool.drop` when the session that owns it logs out.
    `close()` flips the `_http.is_closed` flag `mailosh.sse.
    _client_is_closed` reads and fails the in-flight stream the way httpx
    does once its transport is gone, so the listener stops for good exactly
    as it does in production.
    """

    class _Http:
        def __init__(self) -> None:
            self.is_closed = False

    def __init__(self) -> None:
        self._http = self._Http()
        self.streams = 0
        self._closed = asyncio.Event()

    async def event_stream(self):
        self.streams += 1
        yield CHANGE
        await self._closed.wait()
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    async def close(self) -> None:
        self._http.is_closed = True
        self._closed.set()


class SlowCancelClient:
    """A stub whose stream takes a *controllable* number of event-loop
    turns to finish unwinding once cancelled: `cancelling` is set the
    moment cancellation reaches it, and it then parks in its `finally`
    until the test sets `release`.

    That holds `stop_idle`'s own `await` on one user's cancellation open
    for as long as a test wants, so the test can do something in the middle
    of a sweep (subscribe to *another* user's hub) deterministically,
    without a wall-clock sleep or a guess at how many loop turns the
    cancellation takes.
    """

    def __init__(self) -> None:
        self.streams = 0
        self.cancelling = asyncio.Event()
        self.release = asyncio.Event()

    async def event_stream(self):
        self.streams += 1
        try:
            await asyncio.Event().wait()
            yield CHANGE  # pragma: no cover - unreachable; keeps this a generator
        finally:
            self.cancelling.set()
            await self.release.wait()


async def _drain(stream) -> list:
    """Consume an `SseHub.subscribe()` generator to completion.

    A task running this stands in for one open `/events` response: it ends
    exactly when a real browser's connection would — when the hub stops
    feeding that stream — which is what "the tab notices and re-dials" is
    made of.
    """
    return [event async for event in stream]


# ---------------------------------------------------------------------------
# hub_for
# ---------------------------------------------------------------------------


def test_hub_for_returns_the_same_hub_for_the_same_user():
    registry = HubRegistry()
    assert registry.hub_for(7) is registry.hub_for(7)


def test_hub_for_returns_a_distinct_hub_per_user():
    registry = HubRegistry()
    assert registry.hub_for(7) is not registry.hub_for(8)


# ---------------------------------------------------------------------------
# ensure_listener
# ---------------------------------------------------------------------------


async def test_ensure_listener_starts_exactly_one_task_per_user():
    registry = HubRegistry()
    client = StubClient()
    try:
        await registry.ensure_listener(1, client)
        await registry.ensure_listener(1, client)
        await registry.ensure_listener(1, client)
        await _until(lambda: client.streams == 1)
        await _settle()
        assert client.streams == 1
        assert len(registry._listeners) == 1
    finally:
        await registry.close()


async def test_ensure_listener_is_idempotent_under_concurrent_calls():
    """Two tabs hitting `/events` in the same tick must not each start
    their own listener for the same user."""
    registry = HubRegistry()
    client = StubClient()
    try:
        await asyncio.gather(*(registry.ensure_listener(1, client) for _ in range(5)))
        await _until(lambda: client.streams == 1)
        await _settle()
        assert client.streams == 1
        assert len(registry._listeners) == 1
    finally:
        await registry.close()


async def test_ensure_listener_starts_one_listener_per_user():
    registry = HubRegistry()
    one, two = StubClient(), StubClient()
    try:
        await registry.ensure_listener(1, one)
        await registry.ensure_listener(2, two)
        await _until(lambda: one.streams == 1 and two.streams == 1)
        assert len(registry._listeners) == 2
    finally:
        await registry.close()


async def test_listener_publishes_into_its_own_users_hub_only():
    registry = HubRegistry()
    mine, theirs = registry.hub_for(1), registry.hub_for(2)
    sub = mine.subscribe()
    sub_task = asyncio.create_task(anext(sub))
    await _until(lambda: mine.subscriber_count == 1)
    try:
        await registry.ensure_listener(1, StubClient())
        event = await asyncio.wait_for(sub_task, timeout=2)
        assert event.event == "mail"
        assert theirs.subscriber_count == 0
    finally:
        await sub.aclose()
        await registry.close()


async def test_ensure_listener_restarts_a_listener_that_already_finished():
    """A listener whose client died (the user logged out, so the pooled
    `JmapClient` was closed) exits on its own; the next `/events` request —
    a different, still-live session of the same user — must get a fresh
    listener rather than be told one already exists.
    """
    registry = HubRegistry()
    live = StubClient()
    try:
        await registry.ensure_listener(1, ClosedClient())
        await _until(lambda: registry._listeners[1].done())

        await registry.ensure_listener(1, live)
        await _until(lambda: live.streams == 1)
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# A dead listener is recoverable, not terminal (fix round 3, review
# findings 1 and 2)
# ---------------------------------------------------------------------------


async def test_a_dead_listener_ends_the_hubs_open_streams():
    """Review findings 1 and 2: a dead listener used to be terminal *and*
    silent.

    The listener holds *one session's* pooled client while the hub is per
    **user**, so a logout in one tab (`ClientPool.drop`) — or, before fix
    round 3, the pool's 30-minute idle sweep — closed that client under it
    and it returned for good. Every other tab kept a healthy-looking
    `/events` stream that would never carry another event: sse-starlette's
    pings mean the browser's `EventSource` never errors, so nothing
    re-dialled and `sse.js` never set `offline` either. The registry has to
    notice and end those streams, which is what makes the browser reconnect
    into a freshly armed listener.
    """
    registry = HubRegistry()
    client = DyingClient()
    hub = registry.hub_for(1)
    stream_gen = hub.subscribe()
    stream = asyncio.create_task(_drain(stream_gen))
    await _until(lambda: hub.subscriber_count == 1)
    try:
        await registry.ensure_listener(1, client)
        await _until(lambda: client.streams == 1)

        await client.close()  # what ClientPool.drop does at logout

        await _until(lambda: stream.done())
        assert hub.subscriber_count == 0
        # …and nothing dead is left registered, so the very next `/events`
        # arms a new listener on whatever live client it brings.
        assert 1 not in registry._listeners
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
        await stream_gen.aclose()
        await registry.close()


async def test_a_stream_opened_after_the_listener_died_ends_immediately():
    """The same race one tick later. A second tab's `/events` can pass
    `ensure_listener` (a live task) and only reach `hub.subscribe()` after
    that task has died — it must not settle into a permanently silent
    stream either. Ending it right away costs one reconnect and gets that
    tab a working listener; leaving it open costs the user every future
    email.
    """
    registry = HubRegistry()
    client = DyingClient()
    try:
        await registry.ensure_listener(1, client)
        await _until(lambda: client.streams == 1)
        await client.close()
        await _until(lambda: 1 not in registry._listeners)

        hub = registry.hub_for(1)
        assert await _drain(hub.subscribe()) == []
    finally:
        await registry.close()


async def test_ensure_listener_reopens_a_hub_whose_listener_had_died():
    """…and the reconnect that follows works: `ensure_listener` arming a
    new listener puts the hub back in business for the subscribe() call the
    same `/events` request is about to make.
    """
    registry = HubRegistry()
    dead, live = DyingClient(), StubClient()
    try:
        await registry.ensure_listener(1, dead)
        await _until(lambda: dead.streams == 1)
        await dead.close()
        await _until(lambda: 1 not in registry._listeners)

        await registry.ensure_listener(1, live)
        hub = registry.hub_for(1)
        stream_gen = hub.subscribe()
        stream = asyncio.create_task(anext(stream_gen))
        # It stayed open (a hub still marked feedless would have ended it
        # without ever registering) and it carries events again.
        await _until(lambda: hub.subscriber_count == 1)
        hub.publish("mail", '{"types": ["Email"]}')
        event = await asyncio.wait_for(stream, timeout=2)
        assert event.event == "mail"
        await stream_gen.aclose()
    finally:
        await registry.close()


async def test_close_ends_every_open_stream_so_shutdown_does_not_hang_on_them():
    """Shutdown's half of the same contract: an SSE response that never
    ends is exactly what makes a graceful shutdown sit out its timeout.
    Ending the streams lets each browser see the connection close and
    re-dial the (restarted) server on its own.
    """
    registry = HubRegistry()
    await registry.ensure_listener(1, StubClient())
    hub = registry.hub_for(1)
    stream_gen = hub.subscribe()
    stream = asyncio.create_task(_drain(stream_gen))
    await _until(lambda: hub.subscriber_count == 1)

    await registry.close()

    await _until(lambda: stream.done())
    assert hub.subscriber_count == 0
    await stream_gen.aclose()


# ---------------------------------------------------------------------------
# stop_idle
# ---------------------------------------------------------------------------


async def test_stop_idle_cancels_idle_listeners_and_forgets_their_hubs():
    registry = HubRegistry()
    client = StubClient()
    await registry.ensure_listener(1, client)
    await _until(lambda: client.streams == 1)
    hub = registry.hub_for(1)
    task = registry._listeners[1]

    await registry.stop_idle(0)

    # Cancelled *and* awaited: a merely-`cancel()`-ed task would still be
    # pending here, and would print "Task was destroyed but it is pending!"
    # when the loop closes.
    assert task.done()
    assert task.cancelled()
    assert registry._listeners == {}
    assert registry.hub_for(1) is not hub  # forgotten, so this is a fresh one

    await registry.close()


async def test_stop_idle_keeps_a_hub_that_still_has_a_subscriber():
    registry = HubRegistry()
    await registry.ensure_listener(1, StubClient())
    hub = registry.hub_for(1)
    sub = hub.subscribe()
    sub_task = asyncio.create_task(anext(sub))
    await _until(lambda: hub.subscriber_count == 1)
    try:
        await registry.stop_idle(0)

        assert registry.hub_for(1) is hub
        assert len(registry._listeners) == 1
    finally:
        sub_task.cancel()
        await asyncio.gather(sub_task, return_exceptions=True)
        await sub.aclose()
        await registry.close()


async def test_stop_idle_keeps_a_recently_active_hub():
    registry = HubRegistry()
    try:
        await registry.ensure_listener(1, StubClient())
        hub = registry.hub_for(1)

        await registry.stop_idle(1800)

        assert registry.hub_for(1) is hub
        assert len(registry._listeners) == 1
    finally:
        await registry.close()


async def test_stop_idle_spares_a_hub_that_gained_a_subscriber_mid_sweep():
    """Review finding 3: the sweep's idle list is computed up front and the
    loop then `await`s each cancellation, so a `/events` request that
    arrives for a user *later* in that list — sees a live listener,
    subscribes — used to have its hub popped and its listener cancelled
    anyway, leaving a browser attached to an orphaned hub with nothing
    feeding it and no reason to reconnect.

    `SlowCancelClient` parks user 1's cancellation so the sweep is
    provably inside that window when user 2's tab arrives; no wall-clock
    sleep is involved.
    """
    registry = HubRegistry()
    slow, live = SlowCancelClient(), StubClient()
    await registry.ensure_listener(1, slow)
    await registry.ensure_listener(2, live)
    await _until(lambda: slow.streams == 1 and live.streams == 1)
    stale = datetime.now(UTC) - timedelta(hours=1)
    registry.hub_for(1).last_activity = stale
    hub2 = registry.hub_for(2)
    hub2.last_activity = stale

    sweep = asyncio.create_task(registry.stop_idle(1800))
    await _until(slow.cancelling.is_set)  # the sweep is now parked on user 1

    # user 2's tab reconnects mid-sweep: GET /events ensures the (still
    # live) listener, then subscribes.
    await registry.ensure_listener(2, live)
    stream_gen = hub2.subscribe()
    stream = asyncio.create_task(_drain(stream_gen))
    await _until(lambda: hub2.subscriber_count == 1)

    slow.release.set()
    await sweep
    try:
        assert 1 not in registry._listeners  # user 1 was still collected
        assert 2 in registry._listeners
        assert not registry._listeners[2].done()
        assert registry.hub_for(2) is hub2
        assert hub2.subscriber_count == 1
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
        await stream_gen.aclose()
        await registry.close()


async def test_stop_idle_measures_the_hubs_last_activity():
    registry = HubRegistry()
    try:
        await registry.ensure_listener(1, StubClient())
        registry.hub_for(1).last_activity = datetime.now(UTC) - timedelta(seconds=3600)

        await registry.stop_idle(1800)

        assert registry._listeners == {}
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_cancels_and_awaits_every_listener():
    registry = HubRegistry()
    one, two = StubClient(), StubClient()
    await registry.ensure_listener(1, one)
    await registry.ensure_listener(2, two)
    await _until(lambda: one.streams == 1 and two.streams == 1)
    tasks = list(registry._listeners.values())

    await registry.close()

    assert all(t.done() and t.cancelled() for t in tasks)
    assert registry._listeners == {}


async def test_close_is_idempotent():
    registry = HubRegistry()
    await registry.ensure_listener(1, StubClient())
    await registry.close()
    await registry.close()  # must not raise
    assert registry._listeners == {}


async def test_close_on_a_registry_that_never_started_anything():
    await HubRegistry().close()
