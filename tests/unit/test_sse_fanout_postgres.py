"""The same fan-out, against a real PostgreSQL — the half
`tests/unit/test_sse_fanout.py` cannot cover.

That file drives `PostgresNotifyBus` through a fake `NotifyConnection`, so
it proves the bus's own logic (payload, origin filter, reconnect, routing)
but says nothing about whether asyncpg's actual API is being used
correctly: that `add_listener` takes the callback shape assumed here, that
``SELECT pg_notify($1, $2)`` really binds parameters, that a notification
comes back on the event loop. Exactly one round trip through a real server
answers all of that, and nothing else does.

Skipped unless ``MAILOSH_TEST_PG_DSN`` names a database to use, e.g.::

    MAILOSH_TEST_PG_DSN=postgresql://mailosh:mailosh@localhost:55432/mailosh \\
      .venv/bin/python -m pytest tests/unit/test_sse_fanout_postgres.py

Nothing here creates, reads or writes a table: ``LISTEN``/``NOTIFY`` is
server-side message passing, so this is safe to point at a live
development database.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from mailosh.db.notify import PostgresNotifyBus
from mailosh.jmap.models import StateChange
from mailosh.sse import HubRegistry

DSN = os.environ.get("MAILOSH_TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set MAILOSH_TEST_PG_DSN to run the fan-out against a real PostgreSQL"
)

#: A per-run channel, so this never collides with a Mailosh process that is
#: actually running against the same database.
CHANNEL = f"mailosh_sse_test_{uuid.uuid4().hex[:8]}"

CHANGE = StateChange(changed={"acc1": {"Email": "s9"}})

#: Real round trips, so these are wall-clock deadlines rather than
#: event-loop turns — generous enough not to flake, short enough that a
#: broken fan-out fails the suite rather than hanging it.
TIMEOUT = 10.0


class StubClient:
    """A `JmapClient` stand-in reporting one Email change, then idle."""

    async def event_stream(self):
        yield CHANGE
        await asyncio.Event().wait()


async def _worker() -> HubRegistry:
    bus = PostgresNotifyBus(DSN, channel=CHANNEL)
    registry = HubRegistry(bus)
    await registry.start()
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not bus.connected:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("the bus never connected")
        await asyncio.sleep(0.01)
    return registry


async def _subscribe(hub, out: list) -> tuple[asyncio.Task, object]:
    stream = hub.subscribe()

    async def drain() -> None:
        async for event in stream:
            out.append(event)

    task = asyncio.create_task(drain())
    while hub.subscriber_count == 0:
        await asyncio.sleep(0)
    return task, stream


async def _wait_for(predicate) -> None:
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition never became true")
        await asyncio.sleep(0.01)


async def test_a_real_notify_reaches_the_other_registrys_subscriber():
    """Two registries, two asyncpg connections, one database: worker A's
    listener publishes, worker B's tab receives.
    """
    a, b = await _worker(), await _worker()
    mine: list = []
    theirs: list = []
    my_task, my_stream = await _subscribe(b.hub_for(1), mine)
    their_task, their_stream = await _subscribe(b.hub_for(2), theirs)
    try:
        await a.ensure_listener(1, StubClient())

        await _wait_for(lambda: len(mine) == 1)
        assert mine[0].event == "mail"
        assert json.loads(mine[0].data) == {"types": ["Email"]}
        assert mine[0].id == "s9"

        # ...and user 2's tab, on that same worker, heard nothing.
        await asyncio.sleep(0.5)
        assert len(mine) == 1
        assert theirs == []
    finally:
        for task in (my_task, their_task):
            task.cancel()
        await asyncio.gather(my_task, their_task, return_exceptions=True)
        await my_stream.aclose()
        await their_stream.aclose()
        await a.close()
        await b.close()


async def test_a_worker_does_not_hear_its_own_real_notify_twice():
    """PostgreSQL delivers a notification to the connection that sent it
    too. Only the origin stamp stops that becoming a duplicate event.
    """
    a = await _worker()
    seen: list = []
    task, stream = await _subscribe(a.hub_for(1), seen)
    try:
        await a.ensure_listener(1, StubClient())

        await _wait_for(lambda: len(seen) == 1)
        await asyncio.sleep(0.5)
        assert len(seen) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await a.close()
