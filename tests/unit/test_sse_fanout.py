"""Cross-process live-update fan-out: `mailosh.db.notify` and the
`mailosh.sse.HubRegistry` wiring that uses it.

Everything here is in-process asyncio, like `tests/unit/test_sse_hub.py`
and `tests/unit/test_hub_registry.py` — but the thing standing in for
PostgreSQL is a fake *connection*, not a fake bus. `FakePostgres` below
implements the `NotifyConnection` protocol `PostgresNotifyBus` actually
talks to and routes a ``pg_notify`` to every connection ``LISTEN``ing on
that channel, exactly as one database does for its clients. So the code
under test in every "two workers" test below is the shipping bus: its own
`encode`/`decode`, its own origin filter, its own reconnect supervisor and
outbox writer. A second implementation written to mirror the real one
would prove only that the mirror works.

"Two independent registries sharing one database" is therefore literal:
each `HubRegistry` gets its own `PostgresNotifyBus`, each bus opens its own
connection, and both connections belong to one `FakePostgres`. That is the
shape of two uvicorn workers, minus the socket. The socket itself — that
asyncpg's real API is used correctly — is covered by
`tests/unit/test_sse_fanout_postgres.py`, which is skipped unless a real
database is pointed at it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from mailosh.config import Settings
from mailosh.db.notify import (
    MAX_PAYLOAD_BYTES,
    ChangeNote,
    PostgresNotifyBus,
    asyncpg_dsn,
    decode,
    encode,
)
from mailosh.jmap.models import StateChange
from mailosh.sse import HubRegistry
from mailosh.web.app import _notify_bus

CHANGE = StateChange(changed={"acc1": {"Email": "s9"}})


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _until(predicate, *, attempts: int = 2000) -> None:
    """Poll `predicate()` across up to `attempts` bare event-loop turns —
    the same helper (and rationale) `tests/unit/test_hub_registry.py` uses.
    """
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never became true")


async def _settle(turns: int = 60) -> None:
    """Yield the loop `turns` times, so anything that *would* have
    delivered a second event has had every chance to before a cardinality
    assertion runs.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


class StubClient:
    """A `JmapClient` stand-in whose stream reports one Email change and
    then blocks forever — one publish, so every count below is exact.
    """

    def __init__(self) -> None:
        self.streams = 0

    async def event_stream(self):
        self.streams += 1
        yield CHANGE
        await asyncio.Event().wait()


class FakeConnection:
    """One client connection to `FakePostgres`, implementing the slice of
    ``asyncpg.Connection`` that `mailosh.db.notify.NotifyConnection`
    declares.
    """

    def __init__(self, server: FakePostgres) -> None:
        self._server = server
        self.listeners: dict[str, object] = {}
        self.termination_listeners: list[object] = []
        self.queries: list[tuple[str, tuple]] = []
        self.closed = False
        #: Set to make every statement hang forever without ever failing —
        #: what a half-open TCP connection does.
        self.hangs = False

    async def add_listener(self, channel: str, callback) -> None:
        self.listeners[channel] = callback
        self._server.listening.append((self, channel))

    def add_termination_listener(self, callback) -> None:
        self.termination_listeners.append(callback)

    async def execute(self, query: str, *args):
        await self._statement(query, args)
        if "pg_notify" in query:
            channel, payload = args
            self._server.sent.append((channel, payload))
            self._server.deliver(channel, payload)

    async def fetchval(self, query: str, *args):
        await self._statement(query, args)
        return 1

    async def _statement(self, query: str, args: tuple) -> None:
        self.queries.append((query, args))
        if self.hangs:
            await asyncio.Event().wait()
        if self.closed:
            raise ConnectionError("connection is closed")

    def is_closed(self) -> bool:
        return self.closed

    def terminate(self) -> None:
        self.closed = True

    def drop(self) -> None:
        """The database (or the network) going away: the connection closes
        under us and asyncpg fires its termination listeners.
        """
        self.closed = True
        for callback in self.termination_listeners:
            callback(self)


class FakePostgres:
    """One database's ``LISTEN``/``NOTIFY`` router.

    Delivery is `loop.call_soon`, not a direct call, so a notification
    lands on a later event-loop turn the way a real one does — no test
    below can accidentally depend on delivery happening inside the
    publisher's own `await`.
    """

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.listening: list[tuple[FakeConnection, str]] = []
        #: Every payload that reached the server, in order — the wire.
        self.sent: list[tuple[str, str]] = []

    async def connect(self) -> FakeConnection:
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn

    def deliver(self, channel: str, payload: str, pid: int = 42) -> None:
        loop = asyncio.get_running_loop()
        for conn, listening_on in list(self.listening):
            if conn.closed or listening_on != channel:
                continue
            loop.call_soon(conn.listeners[channel], conn, pid, channel, payload)


def _bus(server: FakePostgres, **overrides) -> PostgresNotifyBus:
    """A bus wired to `server`, with every timer wound down to test speed.

    `health_poll_seconds` defaults high enough never to fire: the probe is
    the subject of exactly one test below, and everywhere else its query
    traffic would only be noise.
    """
    kwargs = {
        "connect": server.connect,
        "health_poll_seconds": 30.0,
        "statement_timeout_seconds": 5.0,
        "backoff_initial": 0.001,
        "backoff_cap": 0.001,
    }
    kwargs.update(overrides)
    return PostgresNotifyBus("postgresql://fake/db", **kwargs)


async def _worker(server: FakePostgres, **overrides) -> tuple[HubRegistry, PostgresNotifyBus]:
    """One "uvicorn worker": a registry and its own connection to
    `server`, started and confirmed listening.
    """
    bus = _bus(server, **overrides)
    registry = HubRegistry(bus)
    await registry.start()
    await _until(lambda: bus.connected)
    return registry, bus


async def _collect(stream, out: list) -> None:
    """Drain an `SseHub.subscribe()` generator into `out` — one open
    ``GET /events`` response, and it ends when a real browser's would.
    """
    async for event in stream:
        out.append(event)


class Tab:
    """One open ``/events`` stream against a hub, plus the events it saw."""

    def __init__(self, hub) -> None:
        self.events: list = []
        self._stream = hub.subscribe()
        self._task = asyncio.create_task(_collect(self._stream, self.events))

    @property
    def ended(self) -> bool:
        return self._task.done()

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self._stream.aclose()


# ---------------------------------------------------------------------------
# The payload: a routing hint, inside NOTIFY's 8000-byte cap, from an
# untrusted peer
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip():
    note = ChangeNote(user_id=7, types=("Email", "Mailbox"), state="s42")
    payload = encode("origin-a", note)
    assert payload is not None
    assert decode(payload) == ("origin-a", note)


def test_a_payload_carries_no_mail_content():
    """The whole point of the design: a peer is told *that* something moved
    for a user, never what. Anything else would not fit `NOTIFY` anyway.
    """
    payload = encode("origin-a", ChangeNote(user_id=7, types=("Email",), state="s42"))
    assert payload is not None
    assert set(json.loads(payload)) == {"v", "o", "u", "t", "s"}


def test_an_oversized_state_is_dropped_rather_than_the_whole_note():
    """`NOTIFY` refuses a payload over 8000 bytes, and the state string is
    the only field a mail server controls the length of. Losing it costs a
    reconnecting browser its `Last-Event-ID`; losing the note costs that
    user every peer worker's updates.
    """
    payload = encode("origin-a", ChangeNote(user_id=7, types=("Email",), state="x" * 20000))
    assert payload is not None
    assert len(payload.encode()) <= MAX_PAYLOAD_BYTES
    origin, note = decode(payload)
    assert (origin, note.user_id, note.types, note.state) == ("origin-a", 7, ("Email",), None)


def test_a_note_that_cannot_fit_at_all_is_dropped():
    assert encode("o" * (MAX_PAYLOAD_BYTES + 100), ChangeNote(user_id=1, types=("Email",))) is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[]",
        '{"v":99,"o":"a","u":1,"t":["Email"]}',  # a version this worker cannot read
        '{"o":"a","u":1,"t":["Email"]}',  # no version
        '{"v":1,"u":1,"t":["Email"]}',  # no origin, so the echo filter cannot work
        '{"v":1,"o":"a","u":"1","t":["Email"]}',  # user id as a string
        '{"v":1,"o":"a","u":true,"t":["Email"]}',  # bool is an int subclass; not a user
        '{"v":1,"o":"a","u":1,"t":"Email"}',  # types not a list
        '{"v":1,"o":"a","u":1,"t":[]}',  # nothing a mail UI reacts to
        '{"v":1,"o":"a","u":1,"t":["Thread"]}',  # ...nor after filtering
    ],
)
def test_decode_drops_anything_this_app_did_not_write(payload):
    """Anything that can reach the database can `NOTIFY` on this channel,
    and `decode` runs inside asyncpg's callback where raising would take
    the connection down. Every rejection is a drop.
    """
    assert decode(payload) is None


def test_decode_filters_object_types_to_the_mail_allow_list():
    decoded = decode('{"v":1,"o":"a","u":1,"t":["Email","Thread","Mailbox"]}')
    assert decoded is not None
    assert decoded[1].types == ("Email", "Mailbox")


@pytest.mark.parametrize("state", ["a\nb", "a\rb", "a\x00b", "x" * 500])
def test_decode_rejects_a_state_that_could_forge_an_sse_frame(state):
    """The state string becomes the SSE ``id:`` line a browser reads. A
    newline in it ends that field early and lets whatever follows be parsed
    as further SSE fields — event forgery over a channel a peer controls.
    """
    assert decode(json.dumps({"v": 1, "o": "a", "u": 1, "t": ["Email"], "s": state})) is None


# ---------------------------------------------------------------------------
# Two workers, one database
# ---------------------------------------------------------------------------


async def test_a_change_on_one_worker_reaches_a_subscriber_on_the_other():
    """The headline. Worker A holds the user's upstream JMAP listener;
    worker B holds their open tab. Without the fan-out that tab sees
    nothing A's listener ever receives.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    b, _ = await _worker(server)
    hub = b.hub_for(1)
    tab = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        await a.ensure_listener(1, StubClient())

        await _until(lambda: len(tab.events) == 1)
        event = tab.events[0]
        assert event.event == "mail"
        assert json.loads(event.data) == {"types": ["Email"]}
        assert event.id == "s9"

        await _settle()
        assert len(tab.events) == 1  # exactly one, not one per hop
    finally:
        await tab.close()
        await a.close()
        await b.close()


async def test_a_users_event_never_reaches_a_different_users_subscriber():
    """The one guarantee that must hold across processes as absolutely as
    it does within one.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    b, _ = await _worker(server)
    mine, theirs = b.hub_for(1), b.hub_for(2)
    my_tab, their_tab = Tab(mine), Tab(theirs)
    await _until(lambda: mine.subscriber_count == 1 and theirs.subscriber_count == 1)
    try:
        await a.ensure_listener(1, StubClient())

        await _until(lambda: len(my_tab.events) == 1)
        await _settle()
        assert len(my_tab.events) == 1
        assert their_tab.events == []
        assert not their_tab.ended
    finally:
        await my_tab.close()
        await their_tab.close()
        await a.close()
        await b.close()


async def test_a_worker_does_not_redeliver_its_own_note_to_its_own_tabs():
    """PostgreSQL delivers a `NOTIFY` to every listener on the channel,
    including the connection that sent it. The publishing worker's hub was
    already fed directly by its own listener, so without the origin filter
    every local event would arrive twice — one refetch per change becomes
    two, on every worker, forever.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    hub = a.hub_for(1)
    tab = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        await a.ensure_listener(1, StubClient())

        await _until(lambda: len(server.sent) == 1)  # it really did go on the wire
        await _settle()
        assert len(tab.events) == 1
    finally:
        await tab.close()
        await a.close()


async def test_a_note_for_a_user_with_no_tab_here_creates_nothing():
    """A remote note must not conjure a hub. Every worker in the fleet
    would otherwise hold one per user in the fleet.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    b, _ = await _worker(server)
    try:
        await a.ensure_listener(5, StubClient())

        await _until(lambda: len(server.sent) == 1)
        await _settle()
        assert b._hubs == {}
    finally:
        await a.close()
        await b.close()


async def test_a_note_does_not_keep_a_subscriberless_hub_alive():
    """`stop_idle` measures a hub's `last_activity`, and a hub with no
    subscriber delivers a publish to nobody anyway — so touching it on a
    peer's note would buy nothing and cost the idle sweep: a worker the
    user closed their last tab on hours ago would hold its upstream JMAP
    connection open for as long as any *other* worker kept publishing.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    b, _ = await _worker(server)
    hub = b.hub_for(1)
    idle_since = datetime.now(UTC) - timedelta(hours=1)
    hub.last_activity = idle_since
    try:
        await a.ensure_listener(1, StubClient())

        await _until(lambda: len(server.sent) == 1)
        await _settle()
        assert hub.last_activity == idle_since

        await b.stop_idle(1800)
        assert b._hubs == {}
    finally:
        await a.close()
        await b.close()


async def test_without_a_bus_two_registries_stay_isolated():
    """The default, pinned: `HubRegistry()` with no bus is the
    single-process behaviour it always had. If this ever starts passing
    events between registries, something is fanning out that should not be.
    """
    a, b = HubRegistry(), HubRegistry()
    hub = b.hub_for(1)
    tab = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        await a.ensure_listener(1, StubClient())
        await _settle()
        assert tab.events == []
    finally:
        await tab.close()
        await a.close()
        await b.close()


# ---------------------------------------------------------------------------
# The LISTEN connection is a silent-death surface
# ---------------------------------------------------------------------------


async def test_a_lost_listen_connection_ends_open_streams_so_they_redial():
    """The fan-out's own `upstream_lost`. A dropped `LISTEN` connection
    raises nothing anywhere: notifications simply stop, and every tab on
    this worker keeps a perfectly healthy-looking stream that will never
    again carry what its peers see. Ending the streams converts that into
    the one thing a browser handles — a dropped connection it re-dials.
    """
    server = FakePostgres()
    b, bus = await _worker(server, backoff_initial=30, backoff_cap=30)
    hub = b.hub_for(1)
    tab = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        server.connections[0].drop()

        await _until(lambda: tab.ended)
        assert hub.subscriber_count == 0
        assert not bus.connected
    finally:
        await tab.close()
        await b.close()


async def test_a_stream_opened_after_a_fanout_loss_is_served_normally():
    """The other half of that, and the one that would hurt most if it were
    wrong: the loss must *not* latch.

    The local listener is still alive and still feeding this hub, so the
    re-dial the cut provokes has to succeed. `ensure_listener` returns
    early while a listener is running — and therefore never reaches
    `upstream_ready` — so latching here would end every re-dial on arrival,
    turning one lost database connection into an endless reconnect loop
    against a worker that was otherwise working fine.
    """
    server = FakePostgres()
    b, _ = await _worker(server, backoff_initial=30, backoff_cap=30)
    client = StubClient()
    await b.ensure_listener(1, client)
    hub = b.hub_for(1)
    first = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        server.connections[0].drop()
        await _until(lambda: first.ended)

        # the tab re-dials: GET /events runs ensure_listener, then subscribes
        await b.ensure_listener(1, client)
        second = Tab(hub)
        await _until(lambda: hub.subscriber_count == 1)
        assert not second.ended

        hub.publish("mail", '{"types": ["Email"]}')
        await _until(lambda: len(second.events) == 1)
        await second.close()
    finally:
        await first.close()
        await b.close()


async def test_the_bus_reconnects_and_fanout_resumes():
    server = FakePostgres()
    a, _ = await _worker(server)
    b, bus_b = await _worker(server)
    try:
        server.connections[1].drop()
        await _until(lambda: not bus_b.connected)

        await _until(lambda: bus_b.connected)  # reconnected on its own
        hub = b.hub_for(1)
        tab = Tab(hub)
        await _until(lambda: hub.subscriber_count == 1)

        await a.ensure_listener(1, StubClient())
        await _until(lambda: len(tab.events) == 1)
        await tab.close()
    finally:
        await a.close()
        await b.close()


async def test_a_half_open_connection_is_caught_by_the_health_probe():
    """The failure that has no error at all: the connection is gone but
    nothing closed it, so asyncpg fires no termination callback and
    statements hang instead of failing. Only the timed ``SELECT 1`` probe
    finds this, which is why it exists and why it has a deadline.
    """
    server = FakePostgres()
    b, bus = await _worker(
        server, health_poll_seconds=0.001, statement_timeout_seconds=0.02, backoff_initial=30
    )
    hub = b.hub_for(1)
    tab = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        conn = server.connections[0]
        conn.hangs = True  # no close, no exception, no termination callback

        await _until(lambda: tab.ended, attempts=20000)
        assert not bus.connected
        assert any("SELECT 1" in query for query, _ in conn.queries)
    finally:
        await tab.close()
        await b.close()


async def test_a_garbage_notification_is_ignored_and_the_bus_survives():
    """A foreign `NOTIFY` on this channel — anything else with database
    access, or a peer mid-rollout — must not take the connection down with
    it.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    b, bus = await _worker(server)
    hub = b.hub_for(1)
    tab = Tab(hub)
    await _until(lambda: hub.subscriber_count == 1)
    try:
        server.deliver("mailosh_sse", "}{ not a payload")
        await _settle()
        assert tab.events == []
        assert not tab.ended
        assert bus.connected

        await a.ensure_listener(1, StubClient())
        await _until(lambda: len(tab.events) == 1)
    finally:
        await tab.close()
        await a.close()
        await b.close()


# ---------------------------------------------------------------------------
# Publishing never costs the local tabs anything
# ---------------------------------------------------------------------------


async def test_publish_never_blocks_or_raises_when_the_outbox_is_full():
    """`publish` is called from inside the listener's read loop, right
    after the local `SseHub.publish`. A full outbox (a database that has
    stopped accepting) must cost peers their copy and nothing else.
    """
    bus = _bus(FakePostgres())
    for _ in range(2000):
        bus.publish(ChangeNote(user_id=1, types=("Email",), state="s1"))
    assert bus._outbox.full()


async def test_the_notify_payload_is_a_bound_parameter():
    """`pg_notify($1, $2)`, never an interpolated `NOTIFY` statement: the
    payload carries a state string this process did not author.
    """
    server = FakePostgres()
    a, _ = await _worker(server)
    try:
        await a.ensure_listener(1, StubClient())
        await _until(lambda: len(server.sent) == 1)

        notify = [q for q in server.connections[0].queries if "pg_notify" in q[0]]
        assert len(notify) == 1
        query, args = notify[0]
        assert query == "SELECT pg_notify($1, $2)"
        assert args[0] == "mailosh_sse"
        assert json.loads(args[1])["u"] == 1
    finally:
        await a.close()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_closing_the_registry_closes_the_bus_and_leaves_no_task():
    """`HubRegistry.close` owns the bus it was given, for the same reason
    it cancels *and awaits* its listeners: a task still pending when the
    loop closes is what prints "Task was destroyed but it is pending!".
    """
    server = FakePostgres()
    before = {t.get_name() for t in asyncio.all_tasks()}
    a, bus = await _worker(server)
    await a.ensure_listener(1, StubClient())
    await _until(lambda: len(server.sent) == 1)

    await a.close()

    await _settle()
    leaked = {t.get_name() for t in asyncio.all_tasks() if not t.done()} - before
    assert leaked == set()
    assert server.connections[0].closed
    assert not bus.connected


async def test_closing_the_registry_twice_is_harmless():
    server = FakePostgres()
    a, _ = await _worker(server)
    await a.close()
    await a.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_asyncpg_dsn_strips_the_sqlalchemy_driver():
    assert asyncpg_dsn("postgresql+asyncpg://u:p@host:5432/db") == "postgresql://u:p@host:5432/db"


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///./test.db", "mysql://h/db", "nonsense"])
def test_asyncpg_dsn_rejects_anything_that_is_not_postgres(url):
    """An operator who turns fan-out on against a non-Postgres database
    finds out at startup, not by wondering why nothing propagates.
    """
    with pytest.raises(ValueError, match="PostgreSQL"):
        asyncpg_dsn(url)


def _settings(**overrides) -> Settings:
    return Settings(
        stalwart_admin_secret="admin-secret-for-tests-only",
        secret_key="test-secret-key-not-for-production-use!!",
        _env_file=None,
        **overrides,
    )


def test_the_fanout_setting_defaults_to_the_single_process_behaviour():
    assert _settings().sse_fanout == "memory"
    assert _notify_bus(_settings()) is None


def test_the_fanout_setting_builds_a_postgres_bus_when_asked():
    bus = _notify_bus(_settings(sse_fanout="postgres"))
    assert isinstance(bus, PostgresNotifyBus)


def test_turning_fanout_on_against_sqlite_fails_at_startup():
    with pytest.raises(ValueError, match="PostgreSQL"):
        _notify_bus(_settings(sse_fanout="postgres", database_url="sqlite+aiosqlite:///./x.db"))
