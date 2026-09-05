"""Cross-process fan-out for live updates: Postgres ``LISTEN``/``NOTIFY``
(design spec §12, "Postgres LISTEN/NOTIFY fan-out is the Phase 2 path to
multiple workers").

``mailosh.sse`` keeps its ``user_id -> (SseHub, listener task)`` registry in
*process* memory. With one uvicorn worker that is the whole system; with
several, each worker only ever sees the JMAP state changes its own
``stalwart_listener`` tasks received. This module is the wire that lets one
worker tell the others, so a hub can fan out changes it never saw itself.

Three pieces, mirroring `mailosh.sse`'s own shape:

- `ChangeNote` + `encode`/`decode`: the payload. Deliberately a **routing
  hint and nothing else** — a user id, which JMAP object types moved, and
  the state string to put on the SSE ``id:`` line. Never the change itself:
  ``NOTIFY`` payloads are capped at 8000 bytes by PostgreSQL (``NOTIFY``'s
  own documented limit, enforced by the server, not by us), and a mail
  UI's job on a change is to *re-fetch* anyway (``sse.js`` turns every
  ``mail`` frame into one coalesced ``mail:changed`` the list answers with
  its own request). Nothing about a message body, a subject, or an address
  ever crosses this channel.

- `PostgresNotifyBus`: one dedicated asyncpg connection per process,
  ``LISTEN``ing on `CHANNEL` and ``NOTIFY``ing on it. Publishing is
  fire-and-forget through a bounded outbox (`publish` never blocks and
  never raises — the same rule `mailosh.sse.SseHub.publish` follows, for
  the same reason: the caller is a listener loop that must not stall
  behind a slow database).

- `NotifyBus`: the protocol `mailosh.sse.HubRegistry` actually depends on,
  so the registry has no idea Postgres exists and the default (no bus at
  all) is exactly today's in-process behaviour.

**The connection is a silent-death surface, and is treated as one.** A
dropped-but-not-noticed ``LISTEN`` connection is the fan-out equivalent of
the dead JMAP listener `mailosh.sse.SseHub.upstream_lost` exists for: no
error surfaces anywhere, notifications simply stop arriving, and every tab
attached to this worker quietly stops seeing what its peers do. So the
supervisor here does not merely wait for an exception — it *probes*
(``SELECT 1`` every `health_poll_seconds`, which is what turns a half-open
TCP connection into a detected failure), reports the loss to its subscriber
exactly once per outage, and reconnects with the same 1/2/4/…/30 s backoff
`stalwart_listener` uses.

**What this does not make safe, and must not be read as making safe.**
Fan-out is the transport a multi-worker deployment needs; it is not the
whole of what multi-worker needs. Three things are still process-local, and
each is its own piece of work:

1. *No listener ownership.* ``GET /events`` arms a listener on whichever
   worker serves it, so a user with tabs on three workers has three upstream
   JMAP EventSource connections to Stalwart, not the one design spec §6.5
   promises. Deciding which worker *owns* a user's upstream — a lease, with
   an expiry, and a story for the lease-holder dying — is the next step, and
   this channel is what it would be built on. Nothing here does it.

2. *Logout is process-local.* ``mailosh.web.auth.logout`` revokes the
   session row (shared, so every worker's next *request* 401s) and calls
   ``ClientPool.drop`` (this process only). Another worker's already-open
   ``/events`` for that session keeps streaming on the pooled client it
   still holds — and ``pool.streaming`` deliberately exempts that client
   from the idle sweep, so "until it goes idle" is not a bound. That is the
   most valuable next use of this bus: a revoke note whose peers answer with
   their own ``ClientPool.drop``.

3. *Each worker sweeps on its own clock.* The maintenance loop
   (``mailosh.web.app``) runs per process, so the session reaper and the
   idle sweeps do N times the work at N times the moments. Wasteful rather
   than wrong, but it is not nothing at scale.

Security note: the payload crosses a trust boundary. Anything that can
connect to the database can ``NOTIFY`` on this channel, so `decode` treats
every field as hostile — the user id must be a real ``int``, object types
are filtered against the same allow-list `mailosh.sse.mail_change_types`
applies, and a state string carrying a control character (which would let a
peer inject extra fields into the browser's SSE frame through the ``id:``
line) is rejected outright. Outbound, the payload is bound as a query
parameter via ``pg_notify($1, $2)`` rather than interpolated into a
``NOTIFY`` statement, so a state string can never be SQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: The one ``LISTEN``/``NOTIFY`` channel this app uses. A constant rather
#: than a setting: notification channels are scoped to a database, and two
#: Mailosh deployments sharing one database would already be sharing
#: `app_user`/`session` rows — there is no configuration that makes that
#: work, so there is none to expose here.
CHANNEL = "mailosh_sse"

#: Payload budget. PostgreSQL caps a ``NOTIFY`` payload at 8000 bytes and
#: raises if you exceed it; the margin below leaves room for that limit
#: being counted with the terminating NUL and for a future field, so a
#: too-long JMAP state string degrades (see `encode`) instead of turning
#: every notification for that user into an error.
MAX_PAYLOAD_BYTES = 7900

#: Payload schema version. `decode` refuses anything else rather than
#: guessing: during a rolling restart, an old worker reading a new
#: worker's payload must drop it, not misread it.
_PAYLOAD_VERSION = 1

#: The JMAP object types allowed to cross the channel — the same allow-list
#: `mailosh.sse` applies to what Stalwart pushes, re-applied on the way in
#: so a peer (or anything else with database access) cannot make this
#: worker emit an arbitrary event body to a browser.
_MAIL_TYPES = ("Email", "Mailbox")

#: Cap on a state string. Real JMAP states are short opaque tokens; this
#: only exists so one absurd value cannot consume the whole payload budget.
_MAX_STATE_CHARS = 256

#: Outbox depth. Notes are dropped (with a warning) rather than allowed to
#: back up without limit if the database stops accepting them — a fan-out
#: hint is worthless by the time it is minutes old, and `publish`'s caller
#: is a listener loop that must never block.
_OUTBOX_MAXSIZE = 1000

#: Reconnect backoff, seconds: 1, 2, 4, ... capped at 30 — deliberately the
#: same curve `mailosh.sse.stalwart_listener` uses for its own upstream.
_BACKOFF_INITIAL = 1
_BACKOFF_CAP = 30

#: How often the idle connection is probed with ``SELECT 1``. See the
#: module docstring: this is the only thing that detects a connection that
#: is gone but was never closed.
_HEALTH_POLL_SECONDS = 30.0

#: Deadline on any single statement this module sends. A half-open TCP
#: connection does not *fail* a query, it swallows it: without this the
#: probe below would block on the kernel's retransmit timer for minutes
#: while the fan-out sat silently dead, which is the exact failure the
#: probe exists to catch. Generous enough that a merely busy database is
#: never mistaken for a dead one.
_STATEMENT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ChangeNote:
    """One "something moved for this user" hint, as it crosses processes.

    `types` is the same sorted, de-duplicated list `mailosh.sse.
    mail_change_types` builds for the browser-facing event body, and
    `state` the same JMAP state string that becomes its SSE ``id:`` line —
    so a peer worker can rebuild a byte-identical ``mail`` frame without
    having seen the `StateChange` itself.
    """

    user_id: int
    types: tuple[str, ...]
    state: str | None = None


def encode(origin: str, note: ChangeNote) -> str | None:
    """Serialize `note` for a ``NOTIFY`` payload, or `None` if it cannot be
    made to fit `MAX_PAYLOAD_BYTES`.

    `origin` identifies the *bus* that published it, and is what stops a
    process reacting to its own notification: PostgreSQL delivers a
    ``NOTIFY`` to every listener on the channel including the connection
    that sent it, and the publishing worker's hubs were already fed
    directly by their own `stalwart_listener`. Without this every local
    event would be delivered twice.

    Degrades rather than failing when the budget is tight: the state string
    is the only unbounded field (it comes from the mail server), and it is
    an *optimization* — the browser echoes it back as ``Last-Event-ID`` —
    while the user id and types are what makes the note mean anything. So
    an over-budget note is re-encoded without the state, and only a note
    that is still too large without it (nothing a real `StateChange`
    produces) is dropped.
    """
    payload = _dump(origin, note.user_id, list(note.types), note.state)
    if len(payload.encode()) <= MAX_PAYLOAD_BYTES:
        return payload
    payload = _dump(origin, note.user_id, list(note.types), None)
    if len(payload.encode()) <= MAX_PAYLOAD_BYTES:
        logger.warning(
            "SSE fan-out: change note for user %s exceeded the NOTIFY payload cap; "
            "sent without its state id",
            note.user_id,
        )
        return payload
    logger.error(
        "SSE fan-out: change note for user %s does not fit a NOTIFY payload; dropped",
        note.user_id,
    )
    return None


def _dump(origin: str, user_id: int, types: list[str], state: str | None) -> str:
    """The wire form: compact JSON with one-character keys, because every
    byte of it counts against `MAX_PAYLOAD_BYTES`.
    """
    body: dict[str, Any] = {"v": _PAYLOAD_VERSION, "o": origin, "u": user_id, "t": types}
    if state is not None:
        body["s"] = state
    return json.dumps(body, separators=(",", ":"))


def decode(payload: str) -> tuple[str, ChangeNote] | None:
    """Parse a received ``NOTIFY`` payload into ``(origin, note)``, or
    `None` if it is anything this app did not write.

    Every branch that returns `None` is a *drop*, never an exception: this
    runs inside asyncpg's notification callback, where raising would take
    out the connection's protocol over one bad message, and the message may
    have come from anything with database access — see the module
    docstring. `_MAIL_TYPES` filtering and the control-character check on
    the state string are the two that matter most, because those two fields
    reach a browser: the types as the event body, the state as the SSE
    ``id:`` line.
    """
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        logger.warning("SSE fan-out: ignoring an unparseable notification payload")
        return None
    if not isinstance(body, dict) or body.get("v") != _PAYLOAD_VERSION:
        logger.warning("SSE fan-out: ignoring a notification with an unknown payload version")
        return None
    origin = body.get("o")
    user_id = body.get("u")
    raw_types = body.get("t")
    state = body.get("s")
    if not isinstance(origin, str) or not origin:
        return None
    # `type(...) is int` rather than `isinstance`: `bool` is an `int`
    # subclass, and a `true` here is malformed input, not user 1.
    if type(user_id) is not int:
        return None
    if not isinstance(raw_types, list):
        return None
    types = tuple(t for t in raw_types if isinstance(t, str) and t in _MAIL_TYPES)
    if not types:
        return None
    if state is not None and not _is_safe_state(state):
        return None
    return origin, ChangeNote(user_id=user_id, types=types, state=state)


def _is_safe_state(state: Any) -> bool:
    """Whether `state` is usable as an SSE ``id:`` value.

    A newline (or any other control character) in an ``id:`` line would end
    the field early and let the rest be read as further SSE fields by the
    browser — event injection through a channel a peer controls. Length is
    capped for the payload budget's sake.
    """
    return (
        isinstance(state, str)
        and 0 < len(state) <= _MAX_STATE_CHARS
        and state.isprintable()
        and "\n" not in state
    )


def asyncpg_dsn(database_url: str) -> str:
    """Turn `Settings.database_url` into the DSN asyncpg's own
    ``connect()`` takes.

    SQLAlchemy spells the driver in the scheme (``postgresql+asyncpg://``);
    asyncpg wants a plain ``postgresql://``. Anything that is not a
    Postgres URL raises here rather than later, because there is exactly
    one way to reach this function — an operator set the fan-out to
    ``postgres`` — and "your database is SQLite, so there is nothing to
    fan out over" is a startup-time configuration error worth saying out
    loud instead of a connection that quietly never works.
    """
    scheme, sep, rest = database_url.partition("://")
    driver = scheme.split("+", 1)[0]
    if not sep or driver not in ("postgresql", "postgres"):
        raise ValueError(
            f"SSE fan-out needs a PostgreSQL database_url, got {scheme!r}. "
            "Set MAILOSH_SSE_FANOUT=memory, or point MAILOSH_DATABASE_URL at Postgres."
        )
    return f"postgresql://{rest}"


class NotifyConnection(Protocol):
    """The slice of ``asyncpg.Connection`` this module uses.

    Spelled out as a protocol so `PostgresNotifyBus` can be driven by a
    fake connection in tests — the *whole* bus (encode/decode, the origin
    filter, the reconnect supervisor, the outbox writer) is then the code
    actually under test, rather than a second implementation written to
    mirror it.
    """

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None: ...

    def add_termination_listener(self, callback: Callable[..., None]) -> None: ...

    async def execute(self, query: str, *args: Any) -> Any: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    def is_closed(self) -> bool: ...

    def terminate(self) -> None: ...


class NotifyBus(Protocol):
    """What `mailosh.sse.HubRegistry` depends on — deliberately four
    methods and no mention of Postgres.
    """

    def subscribe(
        self, on_note: Callable[[ChangeNote], None], on_lost: Callable[[], None]
    ) -> None: ...

    def publish(self, note: ChangeNote) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


async def _default_connect(dsn: str) -> NotifyConnection:
    """Open the dedicated ``LISTEN`` connection.

    asyncpg is imported here rather than at module scope so importing
    `mailosh.sse` (which needs `ChangeNote`/`NotifyBus` for typing alone)
    does not drag in a C extension it will never call on the default,
    in-process path.
    """
    import asyncpg

    return await asyncpg.connect(dsn)  # type: ignore[return-value]


class PostgresNotifyBus:
    """One process's end of the ``LISTEN``/``NOTIFY`` channel.

    Two tasks and one connection, each with a single job, which is what
    keeps this reasonable to reason about:

    - the **supervisor** (`_run`) owns the connection's *lifecycle*:
      connect, ``LISTEN``, wait for it to die, tell the subscriber, close,
      back off, repeat.
    - the **writer** (`_write`) owns the connection's *query path*: it is
      the only thing that ever issues a statement, which is why no lock is
      needed (an ``asyncpg.Connection`` cannot run two queries at once).
      It drains the outbox and, whenever the outbox is quiet for
      `health_poll_seconds`, probes with ``SELECT 1``.

    Incoming notifications need neither: asyncpg dispatches them on the
    event loop straight into `_on_notify`.

    Fan-out is deliberately dumb — every worker with a listener for a user
    publishes every change it sees, and every worker receives it. With *W*
    workers each holding a listener for the same user that is W² messages
    per change, which is fine at the handful-of-workers scale this is for
    (each message is well under 200 bytes) and is the price of having no
    cross-process ownership protocol. Establishing which worker *owns* a
    user's upstream listener is the next step past this one, not part of
    it.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[], Awaitable[NotifyConnection]] | None = None,
        channel: str = CHANNEL,
        health_poll_seconds: float = _HEALTH_POLL_SECONDS,
        statement_timeout_seconds: float = _STATEMENT_TIMEOUT_SECONDS,
        backoff_initial: float = _BACKOFF_INITIAL,
        backoff_cap: float = _BACKOFF_CAP,
    ) -> None:
        self._dsn = dsn
        self._connect = connect if connect is not None else lambda: _default_connect(dsn)
        self._channel = channel
        self._health_poll_seconds = health_poll_seconds
        self._statement_timeout_seconds = statement_timeout_seconds
        self._backoff_initial = backoff_initial
        self._backoff_cap = backoff_cap
        #: This bus's identity on the wire — see `encode`. Per *instance*,
        #: not per process: two buses in one process (every test that
        #: builds a pair) must be able to hear each other.
        self._origin = uuid.uuid4().hex
        self._outbox: asyncio.Queue[ChangeNote] = asyncio.Queue(maxsize=_OUTBOX_MAXSIZE)
        self._on_note: Callable[[ChangeNote], None] | None = None
        self._on_lost: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._down = asyncio.Event()
        self._connected = False
        #: Set by `close` before it cancels, so `_run`'s teardown can tell
        #: a deliberate shutdown from a connection that died under it and
        #: not report the second as if it were the first.
        self._closing = False

    @property
    def origin(self) -> str:
        """This bus's wire identity (`encode`'s ``origin``)."""
        return self._origin

    @property
    def connected(self) -> bool:
        """Whether a ``LISTEN`` connection is established right now."""
        return self._connected

    def subscribe(self, on_note: Callable[[ChangeNote], None], on_lost: Callable[[], None]) -> None:
        """Register the two callbacks `mailosh.sse.HubRegistry` provides:
        one per note arriving from a peer, one for "this connection is
        gone" — which the registry answers the way it answers a dead JMAP
        listener, by ending open browser streams so each tab re-dials.
        """
        self._on_note = on_note
        self._on_lost = on_lost

    async def start(self) -> None:
        """Spawn the supervisor. Idempotent.

        Returns as soon as the task exists rather than waiting for a first
        successful connect: a database blip must not stop the app booting,
        and every hub still works from its own local listener while the
        fan-out is down. A failed connect is logged at ``exception`` level
        on every attempt, so an operator who sets the fan-out on and has it
        never work is told, repeatedly, in the logs.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="sse-fanout-postgres")

    def publish(self, note: ChangeNote) -> None:
        """Queue `note` for the peers. Never blocks, never raises.

        Called from inside `mailosh.sse.stalwart_listener`'s read loop,
        immediately after the local `SseHub.publish` — so a database that
        is slow, full or gone can only ever cost peers their copy, never
        delay the browser tabs attached to *this* worker.
        """
        try:
            self._outbox.put_nowait(note)
        except asyncio.QueueFull:
            logger.warning(
                "SSE fan-out: outbox full; dropping change note for user %s", note.user_id
            )

    async def close(self) -> None:
        """Cancel *and await* the supervisor (which cancels and awaits the
        writer, and closes the connection). Idempotent, and leaves no
        pending task behind — the same contract
        `mailosh.sse.HubRegistry.close` keeps for its listeners, and for
        the same reason: a merely-cancelled task still pending when the
        loop closes is what prints "Task was destroyed but it is pending!".
        """
        task, self._task = self._task, None
        if task is None:
            return
        self._closing = True
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # -- internals ---------------------------------------------------

    async def _run(self) -> None:
        """Connect, ``LISTEN``, serve until the connection dies, report the
        loss, back off, repeat — forever, until cancelled by `close`.

        `on_lost` fires only on the *transition* from working to broken,
        never on a failed reconnect attempt while already broken. That
        matters: the subscriber's response is to end every open browser
        stream, and firing it on each attempt through a long outage would
        turn a database outage into a reconnect storm, cutting every tab's
        connection every backoff cycle for as long as it lasted.
        """
        backoff = self._backoff_initial
        while True:
            conn: NotifyConnection | None = None
            try:
                conn = await self._connect()
                self._down.clear()
                conn.add_termination_listener(self._on_terminated)
                await conn.add_listener(self._channel, self._on_notify)
                self._connected = True
                backoff = self._backoff_initial
                logger.info("SSE fan-out: listening on %r", self._channel)
                await self._serve(conn)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SSE fan-out: LISTEN connection failed; retrying in %ss", backoff)
            finally:
                was_connected = self._connected
                self._connected = False
                if conn is not None:
                    # `terminate()`, not `await close()`: this runs on the
                    # cancellation path too, where awaiting a graceful
                    # round trip against a database that may be the thing
                    # that died is exactly how shutdown hangs. Dropping a
                    # LISTEN connection loses nothing.
                    with suppress(Exception):
                        conn.terminate()
                if was_connected and not self._closing:
                    self._report_lost()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_cap)

    async def _serve(self, conn: NotifyConnection) -> None:
        """Run the writer and wait for the connection to go down.

        Returns (rather than raising) on a clean detection of the loss; the
        writer's own failures reach here through `_down`, so the supervisor
        has exactly one shape of "it's over" to handle.
        """
        writer = asyncio.create_task(self._write(conn), name="sse-fanout-writer")
        try:
            await self._down.wait()
        finally:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer

    async def _write(self, conn: NotifyConnection) -> None:
        """The connection's only query path: drain the outbox, and probe
        with ``SELECT 1`` whenever it has been quiet for
        `health_poll_seconds`.

        The probe is the half-open-connection detector described in the
        module docstring — without it, a connection killed by a NAT table,
        a load balancer or a firewall stays "open" here forever and simply
        stops delivering notifications, which is precisely the silent death
        this whole module is careful about. Any failure (probe or publish)
        sets `_down`, which is the supervisor's cue to reconnect.

        Both statements run under `_statement_timeout_seconds`, and that is
        not belt-and-braces: a half-open connection answers a query by
        *hanging*, not by failing, so an untimed probe would park here
        forever and detect exactly nothing — the probe would have been
        theatre.
        """
        try:
            while True:
                try:
                    note = await asyncio.wait_for(
                        self._outbox.get(), timeout=self._health_poll_seconds
                    )
                except TimeoutError:
                    await self._statement(conn.fetchval("SELECT 1"))
                    continue
                payload = encode(self._origin, note)
                if payload is None:
                    continue
                # Bound parameters, not an interpolated `NOTIFY` statement:
                # the payload carries a state string this process did not
                # author. `pg_notify` is the function form of `NOTIFY` and
                # is the only spelling that accepts parameters at all.
                await self._statement(
                    conn.execute("SELECT pg_notify($1, $2)", self._channel, payload)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SSE fan-out: NOTIFY connection failed")
            self._down.set()

    async def _statement(self, awaitable: Awaitable[Any]) -> Any:
        """Await one statement under `_statement_timeout_seconds`,
        converting the timeout into the same failure any other broken
        connection raises — `_write`'s caller cannot usefully tell "gone"
        from "not answering", and both mean reconnect.
        """
        try:
            return await asyncio.wait_for(awaitable, timeout=self._statement_timeout_seconds)
        except TimeoutError as exc:
            raise ConnectionError("SSE fan-out: statement timed out") from exc

    def _on_terminated(self, conn: object) -> None:
        """asyncpg's termination callback: the connection is gone. Wakes
        the supervisor immediately rather than waiting for the next probe.
        """
        self._down.set()

    def _on_notify(self, conn: object, pid: int, channel: str, payload: str) -> None:
        """asyncpg's notification callback (invoked on the event loop, so
        no thread hand-off is needed).

        Everything here is defensive: this runs inside the connection's
        protocol, so an exception escaping would take the connection down
        over one malformed message — from a peer mid-rollout, or from
        anything else able to ``NOTIFY`` on this channel.
        """
        decoded = decode(payload)
        if decoded is None:
            return
        origin, note = decoded
        if origin == self._origin:
            # Our own notification, echoed back by the server. The hub it
            # belongs to was fed directly by the listener that published
            # it; delivering it again would double every local event.
            return
        sink = self._on_note
        if sink is None:
            return
        try:
            sink(note)
        except Exception:
            logger.exception("SSE fan-out: delivering a peer change note failed")

    def _report_lost(self) -> None:
        """Tell the subscriber the fan-out is down, without letting its
        failure break the reconnect loop.
        """
        logger.warning("SSE fan-out: LISTEN connection lost")
        sink = self._on_lost
        if sink is None:
            return
        try:
            sink()
        except Exception:
            logger.exception("SSE fan-out: reporting the lost connection failed")
