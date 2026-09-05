"""Per-session `JmapClient` pool (Task 5, design spec §9: "a `JmapClient`
per session (LRU, idle-evicted) built from the decrypted API key").

The browser only ever holds an opaque session cookie; every request that
needs to talk JMAP resolves its `SessionRow` (`mailosh.web.deps.
client_for`) and asks this pool for a connected client. Connecting is not
free — `JmapClient.connect_bearer` does a real HTTP round trip to discover
the JMAP session — so a client is built once per session id and reused
across requests, not reconnected every time; `stop_idle`, called from
`create_app`'s lifespan background task alongside the session reaper (Task 5
controller ruling #2), closes and evicts entries nobody has used in a while
so a long-running process doesn't accumulate one open connection pool per
session forever.

`streaming()` (below) is the one thing that keeps a client out of that
idle sweep: a reader holding an open stream on a client — the per-user SSE
listener in `mailosh.sse` — makes no `get` calls of its own, so without it
the sweep would close the connection out from under a tab that is very much
still using it.

Deliberately per-*session*, not per-*user*: unlike the Stalwart `x:ApiKey`
credential itself (one per user, ruling #1 — the 5-key-per-account quota
forces that), an in-memory `JmapClient`/its underlying `httpx.AsyncClient`
connection pool costs nothing to keep one of per browser tab/device and
sidesteps any need to share one `httpx.AsyncClient` across concurrent
requests from different sessions.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from mailosh.config import Settings
from mailosh.db.models import SessionRow
from mailosh.jmap.client import JmapClient
from mailosh.security import sessions

logger = logging.getLogger(__name__)


def _log_id(session_id: str) -> str:
    """A stable, non-reversible tag for a session, safe to write to logs.

    `SessionRow.id` is not an opaque database key: `mailosh/web/auth.py`
    sets it as the session cookie's *value*, so it is the live bearer
    credential itself. Logging it verbatim would put working session
    tokens into application logs, which are routinely shipped to
    aggregators with far broader read access than the database — anyone
    who could read them could replay the cookie and take over the
    session.

    Twelve hex characters of SHA-256 is plenty to correlate log lines for
    one session while being useless as a credential.
    """
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


#: Clients some long-lived reader is holding an open stream on right now —
#: in practice `mailosh.sse.stalwart_listener`, whose whole life is one
#: `event_stream()` against its session's pooled client. `stop_idle` skips
#: these (see `streaming` below for why they'd otherwise look idle).
#:
#: Module-level and keyed by client *identity* rather than a `ClientPool`
#: attribute keyed by session id, because the two ends of this don't share
#: a reference: a listener is handed a `JmapClient` and never the pool that
#: built it (nor the session id it was filed under), and neither
#: `mailosh.sse.HubRegistry` nor `create_app`'s `/events` wiring passes
#: one down. Identity keying also means several pools in one process (every
#: test that builds its own) can't confuse each other's entries, which a
#: session-id-keyed global would. `WeakSet`, so a client dropped while a
#: listener still held it is still collectable, and a listener that somehow
#: died without unwinding its `with` block leaks nothing.
_streaming: weakref.WeakSet[JmapClient] = weakref.WeakSet()


@contextmanager
def streaming(client: JmapClient) -> Iterator[None]:
    """Mark `client` as carrying a live stream for the duration of the
    block, exempting it from `ClientPool.stop_idle`.

    `ClientPool.get` refreshes an entry's `last_used` only when someone
    asks for the client — i.e. on an ordinary HTTP request. A browser tab
    sitting on an open ``GET /events`` makes none of those for as long as
    the user doesn't click anything, so ~30 minutes after their last click
    the sweep below would close the very client that tab's upstream
    listener is streaming on (review finding 1). Holding an open stream
    *is* use of a client; this is how the reader says so.

    Deliberately not honoured by `drop` or `close_all`: a logout, a session
    revoke or the reaper closing a session's client must still kill the
    listener bound to it (that listener was authorized by that session),
    and shutdown closes everything. This exempts the *idle* sweep only.
    """
    _streaming.add(client)
    try:
        yield
    finally:
        _streaming.discard(client)


def is_streaming(client: JmapClient) -> bool:
    """True while some reader is inside `streaming(client)`."""
    return client in _streaming


class ClientPool:
    """`session_id -> (JmapClient, last_used)`, guarded per-session so two
    concurrent first-loads of the *same* session can never both connect
    (wasting a round trip and leaking one of the two clients) while
    concurrent loads of *different* sessions never block each other.
    """

    def __init__(self) -> None:
        self._clients: dict[str, tuple[JmapClient, datetime]] = {}
        #: One `asyncio.Lock` per session id, created lazily. `dict.
        #: setdefault` has no `await` inside it, so it can't be interleaved
        #: by the event loop — safe to call from `get` with no separate
        #: guard lock of its own.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def get(self, session: SessionRow, settings: Settings) -> JmapClient:
        """Return the connected `JmapClient` for `session`, building (and
        caching) one on first use. Refreshes the entry's "last used" clock
        on every call, including a cache hit, so an active session's client
        never looks idle to `stop_idle` while it's actually in use.
        """
        lock = self._lock_for(session.id)
        async with lock:
            entry = self._clients.get(session.id)
            if entry is not None:
                client, _ = entry
                self._clients[session.id] = (client, datetime.now(UTC))
                return client
            secret = sessions.api_key_secret(session, settings)
            client = await JmapClient.connect_bearer(settings.stalwart_url, secret)
            self._clients[session.id] = (client, datetime.now(UTC))
            return client

    async def drop(self, session_id: str) -> None:
        """Evict and close `session_id`'s client, if any — called on
        logout/session-revoke so a closed session's pooled connection
        doesn't linger. A no-op for an id with no pooled client (already
        evicted, or never connected).

        A `close()` failure is logged (`logger.warning`) and swallowed,
        never propagated (fix round 2, reviewer finding): every caller of
        this method (`logout`, `logout_all`, `reap_expired_sessions`) is
        mid-way through cleaning up several things at once, and a broken
        close on one session's connection must not abort logout's own
        cookie-clearing/audit tail, or a reaper sweep's remaining rows and
        its stale-login-attempt prune, over what is ultimately a
        best-effort resource release.
        """
        entry = self._clients.pop(session_id, None)
        self._locks.pop(session_id, None)
        if entry is not None:
            try:
                await entry[0].close()
            except Exception:
                logger.warning(
                    "failed to close pooled JMAP client for session %s",
                    _log_id(session_id),
                    exc_info=True,
                )

    async def stop_idle(self, idle_seconds: int = 1800) -> None:
        """Close and evict every client not used within the last
        `idle_seconds` — the pool's half of `create_app`'s periodic
        maintenance task (every ~5 minutes; design spec §9's "idle-
        evicted"). `idle_seconds=0` evicts every client with no open stream
        on it — a "collect everything idle right now" cutoff used by tests
        (no production call site passes 0).

        Not equivalent to `close_all`, because of the streaming exemption
        below: a client currently pinned by `streaming()` survives even
        `idle_seconds=0`, where `close_all` closes every client
        unconditionally.

        With one exception: a client inside `streaming()` is skipped
        however long ago it was last `get`-ted, because it has an open
        reader on it right now that no `last_used` timestamp can see
        (review finding 1 — see `streaming` for the whole story). Nothing
        is pinned forever, though: that mark is released the moment the
        reader stops, so a listener `HubRegistry.stop_idle` cancels (the
        user closed the browser hours ago) leaves its client collectable by
        the very next sweep.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=idle_seconds)
        stale = [
            sid
            for sid, (client, last_used) in self._clients.items()
            if last_used <= cutoff and not is_streaming(client)
        ]
        for sid in stale:
            entry = self._clients.pop(sid, None)
            self._locks.pop(sid, None)
            if entry is not None:
                try:
                    await entry[0].close()
                except Exception:
                    logger.warning(
                        "failed to close pooled JMAP client for session %s",
                        _log_id(sid),
                        exc_info=True,
                    )

    async def close_all(self) -> None:
        """Close every pooled client — called once, from `create_app`'s
        lifespan shutdown.
        """
        entries = list(self._clients.values())
        self._clients.clear()
        self._locks.clear()
        for client, _ in entries:
            try:
                await client.close()
            except Exception:
                logger.exception("failed to close a pooled JMAP client during shutdown")
