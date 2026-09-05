"""Task 8 (real-time): the in-process SSE hub (`mailosh.sse.SseHub`), the
JMAP-push-to-hub bridge (`mailosh.sse.stalwart_listener`), and the raw SSE
frame parser the JMAP client's event stream is built on
(`mailosh.jmap.client.parse_sse_stream`).

Every test here is pure-Python/in-process — no HTTP, no Docker, no real
Stalwart — matching `mailosh.jmap.client.event_stream`'s own "no reconnect
logic, no network here" scoping: `stalwart_listener`'s test injects a stub
client object (a plain class with an `event_stream` async-generator method)
rather than a real `JmapClient`, and `parse_sse_stream`'s tests feed it a
canned async line iterator rather than a streamed httpx response. The live,
end-to-end proof (a real `curl -N /events` catching a real Stalwart mail
delivery) is a manual step, not part of this suite — see the Task 8 report.

Task 5 (design spec §9, controller ruling #3 / the plan's own Interfaces
block: "`start_listener` param removed (listeners are per-user, Task 7)")
retires the single shared-demo-account listener `mailosh.web.app.
create_app(start_listener=...)` used to spawn — this file's own two
lifecycle tests for that exact wiring go with it. `mailosh.sse.SseHub`/
`stalwart_listener`/`is_mail_change` (and `mailosh.jmap.client.
parse_sse_stream`) are untouched: Task 7's `HubRegistry` builds on them
directly (plan file structure: "keep SseHub/stalwart_listener"), so every
test below that exercises those pure functions/classes stays exactly as it
was.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from mailosh.jmap.client import SseFrame, parse_sse_stream
from mailosh.jmap.models import StateChange
from mailosh.jmap.pool import ClientPool
from mailosh.sse import (
    SseHub,
    is_mail_change,
    latest_state,
    mail_change_types,
    stalwart_listener,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _alines(lines: list[str]):
    """Wrap a plain list of strings as the async line iterator
    `parse_sse_stream` expects (what `httpx.Response.aiter_lines()` would
    hand it against a real stream)."""
    for line in lines:
        yield line


async def _until(predicate, *, attempts: int = 1000) -> None:
    """Poll `predicate()` across up to `attempts` bare event-loop turns.

    Used to wait for a just-`create_task`-ed coroutine to run its
    synchronous prefix (e.g. `SseHub.subscribe` registering its queue
    before blocking on `queue.get()`) without a real `asyncio.sleep` delay
    or a fixed guess at how many turns it takes.
    """
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never became true")


# ---------------------------------------------------------------------------
# SseHub
# ---------------------------------------------------------------------------


async def test_publish_reaches_two_concurrent_subscribers():
    hub = SseHub()
    gen1 = hub.subscribe()
    gen2 = hub.subscribe()
    task1 = asyncio.create_task(anext(gen1))
    task2 = asyncio.create_task(anext(gen2))
    try:
        await _until(lambda: len(hub._subscribers) == 2)

        hub.publish("new-mail", "hello")

        event1 = await asyncio.wait_for(task1, timeout=1)
        event2 = await asyncio.wait_for(task2, timeout=1)
        assert event1.event == "new-mail"
        assert event1.data == "hello"
        assert event2.event == "new-mail"
        assert event2.data == "hello"
    finally:
        await gen1.aclose()
        await gen2.aclose()


async def test_publish_drops_for_a_full_subscriber_without_blocking_the_healthy_one():
    hub = SseHub()
    slow = hub.subscribe()
    slow_task = asyncio.create_task(anext(slow))
    await _until(lambda: len(hub._subscribers) == 1)

    # Prime `slow`: resolve its registration-time `queue.get()` with a
    # throwaway event so the generator pauses at its `yield` (idle, not
    # mid-`await`) — the fill loop below relies on nothing draining the
    # queue until the test explicitly resumes it.
    hub.publish("prime", "")
    await asyncio.wait_for(slow_task, timeout=1)

    # `slow` is the only subscriber right now, so every one of these lands
    # in its queue uncontested, filling it to the documented cap.
    for i in range(100):
        hub.publish("fill", str(i))

    healthy = hub.subscribe()
    healthy_task = asyncio.create_task(anext(healthy))
    await _until(lambda: len(hub._subscribers) == 2)

    # `slow`'s queue is now full. This publish must be dropped for `slow`
    # (silently — `publish` itself must not raise or block) while still
    # reaching `healthy`, whose queue has room.
    hub.publish("new-mail", "overflow")

    healthy_event = await asyncio.wait_for(healthy_task, timeout=1)
    assert healthy_event.event == "new-mail"
    assert healthy_event.data == "overflow"

    # `slow`'s backlog stayed capped at maxsize rather than growing past it
    # — proof the overflow publish was dropped, not queued anyway. `healthy`
    # already drained its one item above, so this sum attributes cleanly to
    # `slow` alone without needing to identify queues by object identity.
    assert sum(q.qsize() for q in hub._subscribers) == 100

    await healthy.aclose()
    await slow.aclose()


async def test_unsubscribe_cleanup_no_queue_leak():
    hub = SseHub()
    gen = hub.subscribe()
    task = asyncio.create_task(anext(gen))
    await _until(lambda: len(hub._subscribers) == 1)

    hub.publish("prime", "")
    await asyncio.wait_for(task, timeout=1)  # drain so `gen` is idle at `yield`, not mid-`await`
    assert len(hub._subscribers) == 1

    await gen.aclose()

    assert len(hub._subscribers) == 0


async def test_publish_carries_the_event_id():
    hub = SseHub()
    gen = hub.subscribe()
    task = asyncio.create_task(anext(gen))
    await _until(lambda: len(hub._subscribers) == 1)

    hub.publish("mail", '{"types": ["Email"]}', event_id="s42")

    event = await asyncio.wait_for(task, timeout=1)
    assert event.id == "s42"
    await gen.aclose()


async def test_subscriber_count_tracks_live_subscribers():
    hub = SseHub()
    assert hub.subscriber_count == 0
    gen = hub.subscribe()
    task = asyncio.create_task(anext(gen))
    await _until(lambda: hub.subscriber_count == 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await gen.aclose()
    assert hub.subscriber_count == 0


async def test_last_activity_advances_on_publish_and_on_subscriber_churn():
    """`HubRegistry.stop_idle` reads exactly this clock, so it has to move
    both when the hub is doing something (a publish) and when a subscriber
    comes or goes — otherwise a tab that stayed connected for hours with no
    mail would be collected the moment it closed."""
    hub = SseHub()
    stale = datetime.now(UTC) - timedelta(hours=1)

    hub.last_activity = stale
    hub.publish("mail", "{}")
    assert hub.last_activity > stale

    hub.last_activity = stale
    gen = hub.subscribe()
    task = asyncio.create_task(anext(gen))
    await _until(lambda: hub.subscriber_count == 1)
    assert hub.last_activity > stale

    hub.last_activity = stale
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await gen.aclose()
    assert hub.last_activity > stale


# ---------------------------------------------------------------------------
# parse_sse_stream (mailosh.jmap.client)
# ---------------------------------------------------------------------------


async def test_parse_sse_stream_yields_event_and_data():
    frames = [
        f
        async for f in parse_sse_stream(
            _alines(["event: state", 'data: {"changed": {"a1": {"Email": "s1"}}}', ""])
        )
    ]
    assert frames == [SseFrame(event="state", data='{"changed": {"a1": {"Email": "s1"}}}')]


async def test_parse_sse_stream_joins_multiline_data_with_newlines():
    frames = [
        f
        async for f in parse_sse_stream(_alines(["event: state", "data: line1", "data: line2", ""]))
    ]
    assert frames == [SseFrame(event="state", data="line1\nline2")]


async def test_parse_sse_stream_ignores_comment_lines():
    frames = [
        f
        async for f in parse_sse_stream(
            _alines([": this is a keepalive comment, not a field", "event: ping", "data: 30", ""])
        )
    ]
    # The comment line contributed nothing to the dispatched frame — only
    # the two real fields did.
    assert frames == [SseFrame(event="ping", data="30")]


async def test_parse_sse_stream_comment_only_block_yields_no_frame():
    frames = [
        f
        async for f in parse_sse_stream(
            _alines(
                [
                    ": just a keepalive, no event/data fields at all",
                    "",
                    "event: state",
                    "data: {}",
                    "",
                ]
            )
        )
    ]
    assert len(frames) == 1
    assert frames[0].event == "state"


async def test_parse_sse_stream_yields_non_state_frames_for_the_caller_to_filter():
    # parse_sse_stream itself doesn't know about JMAP/"state" — it dispatches
    # whatever event name the stream sent; filtering to "state" only is
    # event_stream()'s job, not this parser's.
    frames = [
        f
        async for f in parse_sse_stream(
            _alines(["event: ping", "data: 30", "", "event: state", 'data: {"changed": {}}', ""])
        )
    ]
    assert [f.event for f in frames] == ["ping", "state"]


# ---------------------------------------------------------------------------
# is_mail_change (mailosh.sse)
# ---------------------------------------------------------------------------


def test_is_mail_change_true_for_email_type():
    assert is_mail_change(StateChange(changed={"a1": {"Email": "s1"}})) is True


def test_is_mail_change_true_for_mailbox_type():
    assert is_mail_change(StateChange(changed={"a1": {"Mailbox": "s1"}})) is True


def test_is_mail_change_false_when_no_account_touches_mail():
    assert is_mail_change(StateChange(changed={"a1": {"Thread": "s1"}})) is False


def test_is_mail_change_true_if_any_account_touches_mail():
    change = StateChange(changed={"a1": {"Thread": "s1"}, "a2": {"Email": "s2"}})
    assert is_mail_change(change) is True


# ---------------------------------------------------------------------------
# mail_change_types / latest_state (the `mail` event's payload and id)
# ---------------------------------------------------------------------------


def test_mail_change_types_is_sorted_and_deduplicated():
    change = StateChange(changed={"a1": {"Mailbox": "s1", "Email": "s2"}, "a2": {"Email": "s3"}})
    assert mail_change_types(change) == ["Email", "Mailbox"]


def test_mail_change_types_ignores_non_mail_types():
    change = StateChange(changed={"a1": {"Thread": "s1", "Email": "s2"}})
    assert mail_change_types(change) == ["Email"]


def test_latest_state_prefers_the_email_state():
    change = StateChange(changed={"a1": {"Mailbox": "m1", "Email": "e1"}})
    assert latest_state(change) == "e1"


def test_latest_state_falls_back_to_the_mailbox_state():
    assert latest_state(StateChange(changed={"a1": {"Mailbox": "m1"}})) == "m1"


def test_latest_state_is_none_when_nothing_mail_related_changed():
    assert latest_state(StateChange(changed={"a1": {"Thread": "t1"}})) is None


# ---------------------------------------------------------------------------
# stalwart_listener (mailosh.sse)
# ---------------------------------------------------------------------------


async def test_stalwart_listener_publishes_a_mail_event_with_types_and_state_id():
    """Task 7 wire format (design spec §6.5): event name `mail`, a JSON
    object body naming the changed types, and `id:` carrying the JMAP state
    string so a reconnecting browser can hand it back as
    `Last-Event-ID` for an `Email/changes` replay.
    """
    hub = SseHub()
    change = StateChange(changed={"a1": {"Email": "s9"}})

    class StubClient:
        async def event_stream(self):
            yield change
            await asyncio.Event().wait()  # then hang, like a real idle connection

    sub = hub.subscribe()
    sub_task = asyncio.create_task(anext(sub))
    await _until(lambda: len(hub._subscribers) == 1)

    listener_task = asyncio.create_task(stalwart_listener(StubClient(), hub))
    try:
        event = await asyncio.wait_for(sub_task, timeout=2)
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task

    assert event.event == "mail"
    assert json.loads(event.data) == {"types": ["Email"]}
    assert event.id == "s9"

    await sub.aclose()


async def test_stalwart_listener_publishes_nothing_for_a_non_mail_change():
    hub = SseHub()

    class StubClient:
        async def event_stream(self):
            yield StateChange(changed={"a1": {"Thread": "s1"}})
            await asyncio.Event().wait()

    sub = hub.subscribe()
    sub_task = asyncio.create_task(anext(sub))
    await _until(lambda: len(hub._subscribers) == 1)

    listener_task = asyncio.create_task(stalwart_listener(StubClient(), hub))
    try:
        for _ in range(50):
            await asyncio.sleep(0)
        assert not sub_task.done()
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task
        sub_task.cancel()
        await asyncio.gather(sub_task, return_exceptions=True)
        await sub.aclose()


async def test_stalwart_listener_backs_off_when_the_stream_ends_cleanly():
    """A server that accepts the EventSource GET and immediately closes it
    (no error, no frames) must not spin: the reconnect sleep applies to a
    clean end-of-stream exactly as it does to a failure, or this loop would
    reconnect thousands of times a second.
    """
    hub = SseHub()

    class EofClient:
        def __init__(self) -> None:
            self.streams = 0

        async def event_stream(self):
            self.streams += 1
            return
            yield  # pragma: no cover - unreachable; keeps this an async generator

    client = EofClient()
    task = asyncio.create_task(stalwart_listener(client, hub))
    try:
        await _until(lambda: client.streams == 1)
        for _ in range(50):
            await asyncio.sleep(0)
        assert client.streams == 1  # still sleeping off the backoff, not spinning
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_stalwart_listener_stops_when_its_client_has_been_closed():
    """The listener runs on the *pooled* client, which `ClientPool.drop`
    closes at logout. Retrying against a closed `httpx.AsyncClient` can only
    ever fail again, so the listener returns instead of looping until the
    idle sweep eventually cancels it.
    """
    hub = SseHub()

    class ClosedClient:
        class _Http:
            is_closed = True

        def __init__(self) -> None:
            self._http = self._Http()
            self.streams = 0

        async def event_stream(self):
            self.streams += 1
            raise RuntimeError("Cannot send a request, as the client has been closed.")
            yield  # pragma: no cover - unreachable; keeps this an async generator

    client = ClosedClient()
    await asyncio.wait_for(stalwart_listener(client, hub), timeout=1)
    assert client.streams == 1


async def test_stalwart_listener_exits_promptly_on_cancellation():
    hub = SseHub()

    class HangingClient:
        async def event_stream(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover - unreachable; keeps this an async generator

    task = asyncio.create_task(stalwart_listener(HangingClient(), hub))
    await asyncio.sleep(0)  # let it start and reach the inner await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


# ---------------------------------------------------------------------------
# stalwart_listener x ClientPool: an open event stream *is* use of the
# pooled client (fix round 3, review finding 1)
# ---------------------------------------------------------------------------


class PooledStubClient:
    """A pooled-`JmapClient` stand-in for the pool-interaction tests below.

    Two things a plain stub doesn't have and these need: an
    `_http.is_closed` flag (what `mailosh.sse._client_is_closed` reads to
    decide "the pool dropped this client, stop") and a `close()` that both
    sets it and fails the *in-flight* `event_stream` the way httpx does
    once its transport is gone — which is exactly what `ClientPool.drop`
    (logout) and `ClientPool.stop_idle` (the idle sweep) do to a client a
    listener is streaming on.
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
        yield StateChange(changed={"a1": {"Email": "s1"}})
        await self._closed.wait()
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    async def close(self) -> None:
        self._http.is_closed = True
        self._closed.set()


def _pool_with(client: object, *, idle_for: int) -> ClientPool:
    """A `ClientPool` holding `client` under session id ``"s1"``, last used
    `idle_for` seconds ago.

    Written straight into `pool._clients` rather than through
    `ClientPool.get`, which would need a real `SessionRow`, a `Settings`
    with a decryptable API key in it, and a Stalwart to connect to — none
    of which this in-process file has or wants. The tuple shape is the
    pool's own (`(client, last_used)`), read/written under the same "a unit
    test may look at its own module's privates" convention `hub.
    _subscribers` already uses here.
    """
    pool = ClientPool()
    pool._clients["s1"] = (client, datetime.now(UTC) - timedelta(seconds=idle_for))
    return pool


async def test_listener_keeps_its_pooled_client_out_of_the_pools_idle_sweep():
    """Review finding 1: an idle tab used to lose live updates for good.

    `ClientPool.get` refreshes `last_used` only on an ordinary HTTP
    request, and an open `/events` stream makes none — so ~30 minutes after
    the user's last click the pool's own sweep closed the very client the
    listener was streaming on, the listener returned permanently, and the
    tab went silent while still looking healthy (sse-starlette's pings keep
    the browser's `EventSource` from ever erroring). A live event stream is
    *use* of that client: the idle sweep must leave it alone.
    """
    hub = SseHub()
    client = PooledStubClient()
    pool = _pool_with(client, idle_for=3600)

    task = asyncio.create_task(stalwart_listener(client, hub))
    try:
        await _until(lambda: client.streams == 1)

        await pool.stop_idle(1800)

        assert "s1" in pool._clients
        assert client._http.is_closed is False
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_pool_drop_still_kills_the_listener_streaming_on_that_client():
    """The exemption covers the *idle* sweep only. `ClientPool.drop` —
    logout, session revoke, the session reaper — is a deliberate close, and
    the listener bound to that session's client must still die with it
    (getting the user's *other* live sessions a working listener back is
    `HubRegistry`'s job, not this one's).
    """
    hub = SseHub()
    client = PooledStubClient()
    pool = _pool_with(client, idle_for=0)
    task = asyncio.create_task(stalwart_listener(client, hub))
    await _until(lambda: client.streams == 1)

    await pool.drop("s1")

    await _until(lambda: task.done())
    assert "s1" not in pool._clients
    assert task.result() is None  # returned of its own accord, didn't raise


async def test_pool_can_evict_the_client_again_once_the_listener_stops():
    """The exemption is scoped to the listener's lifetime, not the
    client's: a listener cancelled by `HubRegistry.stop_idle` (the user
    closed the browser hours ago) must not leave its pooled client pinned
    open forever — the pool's next sweep collects it normally.
    """
    hub = SseHub()
    client = PooledStubClient()
    pool = _pool_with(client, idle_for=3600)
    task = asyncio.create_task(stalwart_listener(client, hub))
    await _until(lambda: client.streams == 1)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    await pool.stop_idle(1800)

    assert pool._clients == {}
    assert client._http.is_closed is True
