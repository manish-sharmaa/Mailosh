"""Task 7 (per-user live updates): `GET /events` (`mailosh.web.events`).

Why this file drives the ASGI app by hand instead of using
`fastapi.testclient.TestClient` like every other route test here:
`TestClient` (and `httpx.ASGITransport`, which it is built on) *buffers*
the whole response — `starlette/testclient.py` runs `await
app(scope, receive, send)` to completion and only then hands httpx a
`ByteStream` of the collected body. That is fine for every ordinary route,
and fatal for this one: an SSE response never completes, so a
`TestClient.stream("GET", "/events")` would hang the suite forever rather
than yield its first frame. `_first_sse_frame` below therefore calls the
ASGI callable directly, reads the `http.response.start` message plus the
first non-empty `http.response.body` chunk off a queue under
`asyncio.wait_for`, and then cancels and awaits the request task — a hard
upper bound on how long any assertion here can block.

Consequences of that choice, both deliberate:

- The app's lifespan is entered via `app.router.lifespan_context(app)`
  rather than by `with TestClient(app)`, so `app.state` (engine,
  sessionmaker, pool, hubs) is built on *this test's* event loop. Driving
  both from one loop is the whole point: a SQLAlchemy async engine created
  inside `TestClient`'s private portal thread cannot be used from here.
- Logging in is done with a plain `httpx.AsyncClient` over
  `httpx.ASGITransport` (buffering is irrelevant for `POST /login`), and
  its session cookie is then replayed as a raw `cookie` header on the
  hand-built `/events` scope.

No network and no Stalwart: `verify_password` is monkeypatched, the
`StalwartAdmin` is a fake, and `mailosh.web.events.stream_client` is
dependency-overridden with a stub JMAP client whose `event_stream` keeps
yielding `StateChange`s, so a `mail` frame reaches the subscriber promptly
instead of the test waiting out `ping=25`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest
from conftest import make_settings

from mailosh.jmap.models import StateChange
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.web import events
from mailosh.web.app import create_app


class FakeAdmin:
    """`StalwartAdmin` stand-in — same shape `tests/unit/test_auth_routes.py`
    uses (`destroy_api_key(username, key_id)` is the real two-argument
    signature, not the plan's superseded one)."""

    def __init__(self) -> None:
        self._minted = 0

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        self._minted += 1
        return ApiKey(id=f"k{self._minted}", secret=f"API_secret_{self._minted}")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None


class ChattyClient:
    """A pooled-`JmapClient` stand-in that reports an Email state change
    every few milliseconds, so whenever `/events` subscribes it sees a real
    `mail` frame right away. A single-shot stub would race the subscription
    (the listener starts before `EventSourceResponse` begins iterating
    `hub.subscribe()`, and `publish` has nowhere to put an event that
    arrives first — see `SseHub.publish`).
    """

    async def event_stream(self):
        n = 0
        while True:
            n += 1
            yield StateChange(changed={"acc1": {"Email": f"s{n}"}})
            await asyncio.sleep(0.005)


class DeadClient:
    """A pooled-`JmapClient` stand-in that has already been closed — what
    `ClientPool.drop` leaves behind when the session that owns it logs out.
    Its stream fails the way httpx does with a closed transport, and
    `_http.is_closed` is the flag `mailosh.sse._client_is_closed` reads to
    tell that apart from a reconnectable blip.
    """

    class _Http:
        is_closed = True

    def __init__(self) -> None:
        self._http = self._Http()

    async def event_stream(self):
        raise RuntimeError("Cannot send a request, as the client has been closed.")
        yield  # pragma: no cover - unreachable; keeps this an async generator


@pytest.fixture
def app(monkeypatch, sqlite_url):
    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    return application


@contextlib.asynccontextmanager
async def _running(app):
    """The app with its lifespan started, on this test's own event loop."""
    async with app.router.lifespan_context(app):
        yield app


def _asgi_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _login(client: httpx.AsyncClient) -> str:
    """Log in and return the resulting `Cookie:` header value."""
    r = await client.post("/login", data={"username": "d@x", "password": "right"})
    assert r.status_code == 303, r.text
    return "; ".join(f"{name}={value}" for name, value in client.cookies.items())


def _events_scope(cookie: str) -> dict[str, Any]:
    """A hand-built ASGI scope for ``GET /events`` carrying `cookie`."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"accept", b"text/event-stream"),
            (b"cookie", cookie.encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }


async def _connected_receive() -> dict[str, Any]:
    """An ASGI `receive` for a browser that stays connected: it never sends
    `http.disconnect`, so anything that ends the response has to be the
    server's own doing."""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")  # pragma: no cover


async def _first_sse_frame(app, cookie: str, *, timeout: float = 5.0) -> tuple[dict, bytes]:
    """Open `/events` against `app`'s raw ASGI callable and return
    ``(http.response.start message, first non-empty body chunk)``, then
    cancel the request the way a browser closing its tab would.
    """
    scope = _events_scope(cookie)
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(message: dict[str, Any]) -> None:
        await messages.put(message)

    task = asyncio.create_task(app(scope, _connected_receive, send))
    try:
        start = await asyncio.wait_for(messages.get(), timeout=timeout)
        assert start["type"] == "http.response.start"
        while True:
            body = await asyncio.wait_for(messages.get(), timeout=timeout)
            if body.get("body"):
                return start, body["body"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _headers(start: dict[str, Any]) -> dict[str, str]:
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


async def test_events_without_a_session_is_401(app):
    async with _running(app), _asgi_client(app) as client:
        r = await client.get("/events", headers={"accept": "text/event-stream"})
    assert r.status_code == 401


async def test_events_401_even_without_the_eventsource_accept_header(app):
    """An `EventSource` can't follow the HTML login redirect every other
    protected route answers with, so `/events` must be a plain 401 for any
    caller — not just one that announced `Accept: text/event-stream`.
    """
    async with _running(app), _asgi_client(app) as client:
        r = await client.get("/events")
    assert r.status_code == 401


async def test_events_with_an_unknown_session_cookie_is_401(app):
    async with _running(app), _asgi_client(app) as client:
        r = await client.get("/events", headers={"cookie": "sid=not-a-real-session"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Authenticated
# ---------------------------------------------------------------------------


async def test_events_streams_mail_frames_for_a_logged_in_user(app):
    app.dependency_overrides[events.stream_client] = lambda: ChattyClient()
    async with _running(app):
        async with _asgi_client(app) as client:
            cookie = await _login(client)
        start, chunk = await _first_sse_frame(app, cookie)

    assert start["status"] == 200
    assert _headers(start)["content-type"].startswith("text/event-stream")

    text = chunk.decode()
    assert "event: mail" in text
    assert "id: s" in text
    payload = json.loads(next(line[6:] for line in text.splitlines() if line.startswith("data: ")))
    assert payload == {"types": ["Email"]}


async def test_events_starts_one_listener_and_shutdown_cancels_it(app):
    """The lifespan half of the contract: `/events` lazily starts this
    user's upstream listener, two tabs share it, and `create_app`'s
    shutdown cancels *and awaits* it — a still-pending task here is exactly
    what makes Python print "Task was destroyed but it is pending!" once
    the loop closes.
    """
    app.dependency_overrides[events.stream_client] = lambda: ChattyClient()
    async with _running(app):
        async with _asgi_client(app) as client:
            cookie = await _login(client)
        await _first_sse_frame(app, cookie)
        await _first_sse_frame(app, cookie)  # a second tab
        hubs = app.state.hubs
        assert len(hubs._listeners) == 1
        listener = next(iter(hubs._listeners.values()))

    assert listener.done()
    assert listener.cancelled()
    assert hubs._listeners == {}


async def test_events_response_ends_when_the_upstream_listener_dies(app):
    """Fix round 3, review findings 1 and 2, end to end.

    A listener whose pooled client was closed under it (a logout in
    another tab, or — before `pool.streaming` — the pool's idle sweep)
    stops for good. `HubRegistry` answers by ending the hub's streams, and
    this is the assertion that makes that worth anything: the real
    `EventSourceResponse` actually *finishes* when its iterator does,
    rather than sitting there pinging. A finished response is what a
    browser sees as a dropped connection, which is what makes it re-dial
    `/events` — and, if it can't, what makes `sse.js` show the offline
    banner (design spec §5.4). Nothing sends `http.disconnect` here, so
    the only thing that can end this request is the server itself.
    """
    app.dependency_overrides[events.stream_client] = lambda: DeadClient()
    async with _running(app):
        async with _asgi_client(app) as client:
            cookie = await _login(client)

        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        request = asyncio.create_task(app(_events_scope(cookie), _connected_receive, send))
        try:
            await asyncio.wait_for(asyncio.shield(request), timeout=5)
        finally:
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request

        assert messages[0]["type"] == "http.response.start"
        assert messages[0]["status"] == 200
        assert messages[-1]["type"] == "http.response.body"
        assert messages[-1].get("more_body", False) is False
