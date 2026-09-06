"""The routes that put a stranger's markup, images and attachments in
front of a reader (`mailosh.web.frames`), plus the parent half of the
resize handshake (`static/js/frame.js`).

Everything runs against a real `create_app` — real routers, real Jinja
environment, real session plumbing over a file-backed aiosqlite database —
with exactly two things faked: `mailosh.web.auth.verify_password` (no
Stalwart) and `deps.client_for` (a `FakeClient` in place of the pooled
`JmapClient`, answering `Email/get` with wire-shaped rows so
`EmailBody`'s own validators do the parsing they do in production). No
network call is ever made: DNS is patched at `fetch_guard`'s single
resolution point wherever a fetch is expected to succeed, and every other
proxy test uses a URL the guard refuses before a socket could exist.

`frame.js` is tested by **running it**, under node, against a stub window
and a pair of stub frames — not by looking for substrings in its source. A
source-order check ("the origin comparison appears before the assignment")
passes on a file where every guard has been commented out and re-added
inside a dead branch; only executing the listener proves that a message
from another window, or one claiming a height of 99 999 px, actually does
nothing. The node tests skip where node is absent, so the two cheap
structural invariants that *are* worth pinning — no `allow-same-origin`
anywhere in the served tree, exactly one `message` listener app-wide —
are asserted separately and always run.

The blob routes (`/m/{id}/cid/{cid}`, `/m/{id}/att/{blob}`,
`/m/{id}/source`) are all scoped the same way, and the tests below assert
that scoping *positively*: every "another message's part is a 404" case
also fetches the same id through the message that really owns it and
expects a 200, so a route that 404'd everything would not pass. The three
allow-lists are asserted as whole sets rather than one sampled type, since
what matters is the boundary, not the example.

Fixtures are deliberately local to this module rather than shared: two
other tasks are editing the conversation view at the same time, and a
fixture in `tests/conftest.py` is a file three agents would be writing at
once. `authed` carries no `X-CSRF-Token` of its own, which is what makes
it the "no token" client for `POST /m/{id}/restyle` — the one mutating
route here; the `csrf` fixture supplies the header where a test means to
succeed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import pathlib
import re
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
import pytest
import tinycss2
from conftest import make_settings
from helpers import parse_attrs
from sqlalchemy import select

from mailosh.db import repo
from mailosh.db.models import AppUser, Contact, ImageSenderAllow, SessionRow
from mailosh.jmap.errors import TransportError
from mailosh.render.frame_document import FRAME_SCRIPT_HASH, csp_header
from mailosh.render.image_policy import (
    CID_URL_TTL,
    IMAGE_URL_TTL,
    sign_cid_url,
    sign_remote_url,
    verify_cid_token,
    verify_image_token,
)
from mailosh.security import sessions
from mailosh.security.exchange import VerifiedAccount
from mailosh.security.signing import sign_payload
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps, frames
from mailosh.web.app import create_app

STATIC = pathlib.Path("mailosh/web/static")
TEMPLATES = pathlib.Path("mailosh/web/templates")
FRAME_JS = STATIC / "js/frame.js"

ME = "d@x"
ACCOUNT = "acct-1"
PUBLIC = "93.184.216.34"
PNG = {"content-type": "image/png"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdmin:
    """`StalwartAdmin` stand-in — see `tests/unit/test_auth_routes.py`."""

    def __init__(self) -> None:
        self._minted = 0

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        self._minted += 1
        return ApiKey(id=f"k{self._minted}", secret=f"API_secret_{self._minted}")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None


class FakeClient:
    """The pooled `JmapClient`, reduced to the two calls a frame makes.

    `_call` answers with the raw JMAP shape rather than a ready-made
    `EmailBody`, so `mailosh.web.frames._load_message` really does drive
    `EmailBody.model_validate` over `textBody`/`htmlBody`/`bodyValues` the
    way it does against Stalwart. A fake that handed back finished objects
    would hide a route that asked for the wrong properties.

    `stream_blob` yields a real `httpx.Response`, so the blob routes drive
    httpx's own `aiter_bytes()` chunking rather than a generator written to
    suit them — a route that forgot to bound what it forwards would look
    fine against a fake that only ever produced one chunk.
    """

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.get_calls: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.blob_calls: list[tuple[str, str, str]] = []

    @property
    def account_id(self) -> str:
        return ACCOUNT

    def blob(self, blob_id: str, data: bytes) -> None:
        self.blobs[blob_id] = data

    @asynccontextmanager
    async def stream_blob(
        self, blob_id: str, *, mime_type: str, name: str
    ) -> AsyncIterator[httpx.Response]:
        self.blob_calls.append((blob_id, mime_type, name))
        if blob_id not in self.blobs:
            raise TransportError(f"no such blob: {blob_id}")
        yield httpx.Response(200, content=self.blobs[blob_id])

    def message(
        self,
        email_id: str,
        *,
        html: str | None = None,
        text: str | None = None,
        sender: str = "a@x.test",
        subject: str = "s",
        seen: bool = True,
        blob_id: str | None = None,
        attachments: tuple[dict, ...] = (),
    ) -> dict:
        body_values: dict[str, dict] = {}
        row: dict = {
            "id": email_id,
            "threadId": "T1",
            "mailboxIds": {"mb-inbox": True},
            "keywords": {"$seen": True} if seen else {},
            "from": [{"name": None, "email": sender}],
            "to": [],
            "subject": subject,
            "receivedAt": "2026-09-03T10:00:00Z",
            "preview": "",
            "hasAttachment": bool(attachments),
            "blobId": blob_id,
            "attachments": [dict(part) for part in attachments],
        }
        if text is not None:
            row["textBody"] = [{"partId": "t"}]
            body_values["t"] = {"value": text, "isTruncated": False}
        if html is not None:
            row["htmlBody"] = [{"partId": "h"}]
            body_values["h"] = {"value": html, "isTruncated": False}
        row["bodyValues"] = body_values
        self.messages[email_id] = row
        return row

    async def _call(self, method_calls: list[tuple[str, dict, str]]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, args, call_id in method_calls:
            assert name == "Email/get", name
            self.get_calls.append(args)
            rows = [self.messages[i] for i in args["ids"] if i in self.messages]
            out[call_id] = {"list": rows, "notFound": [i for i in args["ids"] if i not in rows]}
        return out


def _fake_addrinfo(addresses: list[str]):
    """An async stand-in for `getaddrinfo` answering exactly `addresses`,
    shaped like the real thing (5-tuples whose `sockaddr[0]` is the
    address).
    """
    infos = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 443, 0, 0))
        if ":" in a
        else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443))
        for a in addresses
    ]

    async def fake(host: str, port: int) -> list:
        return infos

    return fake


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def pooled() -> dict[int, FakeClient]:
    """`user id -> FakeClient` for the routes that resolve a client from a
    *token* rather than from the request's own session.

    `GET /m/{id}/cid/{cid}` carries no cookie (see `mailosh.web.frames`), so
    it cannot go through `deps.client_for` and the override above does not
    reach it: it looks up one of the token's reader's own sessions and asks
    `mailosh.jmap.pool.ClientPool` for a client. Faking the *pool* rather
    than the route's dependency keeps that lookup — session row, expiry,
    per-reader scoping — inside the test instead of stubbed over, which is
    the part a cross-account bug would hide in. An id with no entry falls
    back to the shared `fake`, so every existing single-reader test is
    unaffected.
    """
    return {}


@pytest.fixture
def app(monkeypatch, sqlite_url, fake, pooled):
    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    async def pool_get(self, session, settings):
        return pooled.get(session.user_id, fake)

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    monkeypatch.setattr("mailosh.jmap.pool.ClientPool.get", pool_get)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = lambda: fake
    return application


@pytest.fixture
async def running(app):
    """The app with its lifespan entered on *this test's* event loop.

    Not `TestClient`: that drives the lifespan from a private portal
    thread, and the SQLAlchemy async engine it builds there cannot then be
    read from a coroutine running here — which `token_for` needs to do to
    learn the logged-in user's id.
    """
    async with app.router.lifespan_context(app):
        yield app


def _asgi(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def app_client(running):
    """An unauthenticated client. Redirects are not followed, so a route
    that demands a session answers 303 rather than the login page."""
    async with _asgi(running) as client:
        yield client


@pytest.fixture
async def authed(running):
    """A client holding a live session cookie. A second client from
    `app_client` stays anonymous, so the "requires a session" tests are not
    testing the same object from a different angle."""
    async with _asgi(running) as client:
        response = await client.post("/login", data={"username": ME, "password": "right"})
        assert response.status_code == 303, response.text
        yield client


@pytest.fixture
async def reader(running, authed) -> tuple[str, int]:
    """`(secret key, user id)` for the logged-in reader — everything needed
    to mint or verify one of this app's own image tokens."""
    async with running.state.sessionmaker() as db:
        user_id = (await db.execute(select(AppUser))).scalars().one().id
    return running.state.settings.secret_key, user_id


@pytest.fixture
def token_for(reader):
    """`sign_remote_url` bound to the logged-in reader and this app's key —
    exactly what `GET /m/{id}/html` mints for a remote `<img>`."""
    secret_key, user_id = reader

    def sign(url: str) -> str:
        return sign_remote_url(url, secret_key=secret_key, user_id=user_id)

    return sign


@pytest.fixture
def cid_url(reader):
    """`GET /m/{id}/cid/{cid}?u=…` as the sanitiser writes it — with every
    part of the capability separately overridable.

    `signed_message`, `signed_cid` and `signed_user` are what goes *into*
    the token; `email_id` and `content_id` are what goes into the path. They
    are separate parameters precisely so a test can mint a real, unexpired,
    correctly-signed token for one resource and present it for another,
    which is the attack the binding exists to stop and the one a fixture
    that derived the token from the path could not express.
    """
    secret_key, user_id = reader

    def url(
        email_id: str,
        content_id: str,
        *,
        signed_message: str | None = None,
        signed_cid: str | None = None,
        signed_user: int | None = None,
        now: float | None = None,
    ) -> str:
        token = sign_cid_url(
            email_id if signed_message is None else signed_message,
            content_id if signed_cid is None else signed_cid,
            secret_key=secret_key,
            user_id=user_id if signed_user is None else signed_user,
            now=now,
        )
        path = f"/m/{quote(email_id, safe='')}/cid/{quote(content_id, safe='')}"
        return f"{path}?u={quote(token, safe='')}"

    return url


@pytest.fixture
async def csrf(running, authed) -> dict[str, str]:
    """The logged-in session's own CSRF token, as the header htmx sends.

    Read out of the session row rather than scraped from a rendered page:
    the meta tag that carries it lives in a template two other agents are
    editing, and a fixture that broke when they moved it would be testing
    their layout, not this module's one mutating route.
    """
    async with running.state.sessionmaker() as db:
        token = (await db.execute(select(SessionRow))).scalars().one().csrf_token
    return {"X-CSRF-Token": token}


@pytest.fixture
def set_prefs(running, reader):
    """Write straight to the logged-in reader's `UiPref` row.

    `POST /prefs` would do the same thing through a router this module does
    not own; going through `repo` keeps the restyle tests below independent
    of whichever controls that panel happens to expose today.
    """
    _, user_id = reader

    async def apply(**fields: object) -> None:
        async with running.state.sessionmaker() as db:
            await repo.set_prefs(db, user_id, **fields)

    return apply


@pytest.fixture
def resolves_public(monkeypatch):
    """Every name resolves to one public address. Opt-in, not autouse: the
    tests that expect a refusal must reach `check_url`'s real judgement."""
    monkeypatch.setattr("mailosh.render.fetch_guard._getaddrinfo", _fake_addrinfo([PUBLIC]))


# ---------------------------------------------------------------------------
# The invariant that outranks everything else in this file
# ---------------------------------------------------------------------------


_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
#: Whole-line `//` comments only. A blanket `//` strip would also eat the
#: middle of every `https://` URL in the file.
_JS_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _code_of(path: pathlib.Path) -> str:
    """`path` with its comments removed.

    The scans below are about what the browser is *given*, and the files
    that get this most right are exactly the ones whose comments explain
    why `allow-same-origin` is absent. Scanning raw text would make
    documenting the rule a violation of it.
    """
    text = path.read_text()
    if path.suffix == ".js":
        return _JS_LINE_COMMENT.sub("", _JS_BLOCK_COMMENT.sub("", text))
    return _JINJA_COMMENT.sub("", _HTML_COMMENT.sub("", text))


def _iframes() -> list[tuple[pathlib.Path, dict[str, str]]]:
    """Every `<iframe>` element in every template, with its attributes."""
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for attrs in parse_attrs(_code_of(path)).get("iframe", []):
            found.append((path, attrs))
    return found


def test_no_template_ever_frames_a_message_with_same_origin_access():
    """`allow-same-origin` alongside `allow-scripts` hands a hostile message
    the reader's origin: its cookies, its storage, `parent.document`, and a
    fetch carrying the session. It is the single omission the whole design
    rests on, so this is asserted over *every* iframe in the tree — not
    just the one this task renders — and on the parsed token list rather
    than on raw text.
    """
    iframes = _iframes()
    assert iframes, "no iframe found — the template scan is broken, not clean"
    for path, attrs in iframes:
        assert "sandbox" in attrs, path
        assert "allow-same-origin" not in attrs["sandbox"].split(), path
        assert "allow-scripts" in attrs["sandbox"].split(), path
        assert attrs.get("referrerpolicy") == "no-referrer", path


def test_allow_same_origin_is_named_by_no_served_code():
    """The behavioural check above cannot see a `sandbox` value assembled
    by JS, or one spliced into an attribute by a template expression. This
    one catches the string itself, anywhere in the code the app serves.
    """
    served = [*TEMPLATES.rglob("*.html"), *STATIC.rglob("*.js")]
    assert served, "nothing scanned — the paths above are wrong"
    assert [p for p in served if "allow-same-origin" in _code_of(p)] == []


def test_frame_js_registers_exactly_one_message_listener_app_wide():
    """A `message` listener is a door open to every window on the internet.
    The guards in `frame.js` are only as good as the *narrowest* listener,
    so a second one anywhere in the app is a second, unreviewed door.
    """
    total = sum(_code_of(p).count('addEventListener("message"') for p in STATIC.rglob("*.js"))
    assert total == 1
    assert _code_of(FRAME_JS).count('addEventListener("message"') == 1


# ---------------------------------------------------------------------------
# frame.js, executed
# ---------------------------------------------------------------------------

#: Drives the real `frame.js` against a stub window and two stub frames,
#: then reports what each synthetic message did to their heights. Every
#: case resets both frames to the sentinel `"7px"` first, so "ignored"
#: means "still the sentinel" rather than "never set at all".
_HARNESS = """
const frames = [];
const make = () => { const f = { contentWindow: {}, style: {} }; frames.push(f); return f; };
const a = make(), b = make();
const listeners = [];
globalThis.window = { addEventListener: (type, fn) => listeners.push([type, fn]) };
const out = { listeners: 0, selector: null, cases: {} };
globalThis.document = {
  querySelectorAll: (sel) => {
    out.selector = sel;
    return frames;
  },
};

await import(process.argv[2]);

const handlers = listeners.filter(([t]) => t === "message").map(([, fn]) => fn);
out.listeners = handlers.length;

function run(name, event) {
  a.style.height = "7px";
  b.style.height = "7px";
  for (const fn of handlers) fn(event);
  out.cases[name] = { a: a.style.height, b: b.style.height };
}

const T = "mailosh:frame-height";
run("ok", { origin: "null", source: a.contentWindow, data: { type: T, height: 640 } });
run("second_frame", { origin: "null", source: b.contentWindow, data: { type: T, height: 512 } });
run("foreign_window", { origin: "null", source: {}, data: { type: T, height: 640 } });
run("named_origin", {
  origin: "https://evil.test", source: a.contentWindow, data: { type: T, height: 640 },
});
run("app_origin", {
  origin: "http://testserver", source: a.contentWindow, data: { type: T, height: 640 },
});
run("absurd_height", { origin: "null", source: a.contentWindow, data: { type: T, height: 99999 } });
run("negative_height", { origin: "null", source: a.contentWindow, data: { type: T, height: -1 } });
run("tiny_height", { origin: "null", source: a.contentWindow, data: { type: T, height: 10 } });
run("string_height", {
  origin: "null", source: a.contentWindow, data: { type: T, height: "tall" },
});
run("wrong_type", {
  origin: "null", source: a.contentWindow, data: { type: "other", height: 640 },
});
run("no_data", { origin: "null", source: a.contentWindow, data: null });

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def frame_js_run(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment-dependent
        pytest.skip("node is not installed; frame.js behaviour cannot be executed")
    harness = tmp_path_factory.mktemp("frame-js") / "harness.mjs"
    harness.write_text(_HARNESS)
    proc = subprocess.run(
        [node, str(harness), FRAME_JS.resolve().as_uri()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_frame_js_binds_exactly_one_listener_when_it_runs(frame_js_run):
    assert frame_js_run["listeners"] == 1


def test_frame_js_sizes_the_frame_the_message_actually_came_from(frame_js_run):
    assert frame_js_run["cases"]["ok"] == {"a": "640px", "b": "7px"}
    assert frame_js_run["cases"]["second_frame"] == {"a": "7px", "b": "512px"}


@pytest.mark.parametrize("case", ["foreign_window", "named_origin", "app_origin"])
def test_frame_js_ignores_a_message_from_any_window_that_is_not_one_of_our_frames(
    frame_js_run, case
):
    """`event.origin === "null"` rejects every page with a real origin —
    including the app's own — and the `contentWindow === event.source`
    identity check rejects a sandboxed window that is not one of ours.
    Neither check alone is sufficient, so both are proved separately.
    """
    assert frame_js_run["cases"][case] == {"a": "7px", "b": "7px"}


@pytest.mark.parametrize(
    ("case", "height"),
    [("absurd_height", "20000px"), ("negative_height", "200px"), ("tiny_height", "200px")],
)
def test_frame_js_clamps_every_height_into_the_documented_range(frame_js_run, case, height):
    """An unclamped height is a layout denial of service: 99 999 px is a
    scrollbar the reader can never reach the end of, and 0 hides the
    message entirely.
    """
    assert frame_js_run["cases"][case]["a"] == height


@pytest.mark.parametrize("case", ["string_height", "wrong_type", "no_data"])
def test_frame_js_ignores_a_message_it_cannot_read_a_height_out_of(frame_js_run, case):
    """A frame that cannot say how tall it is keeps the height it had —
    `Number("tall")` is NaN, and NaN must not clamp to the minimum and
    collapse a long message to 200 px.
    """
    assert frame_js_run["cases"][case]["a"] == "7px"


# ---------------------------------------------------------------------------
# GET /m/{id}/html
# ---------------------------------------------------------------------------


def _csp_of(response: httpx.Response) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for chunk in response.headers["content-security-policy"].split(";"):
        parts = chunk.split()
        if parts:
            out[parts[0]] = parts[1:]
    return out


async def test_html_route_carries_the_exact_csp_and_the_script_hash_matches_the_body(authed, fake):
    fake.message("E1", html="<p>hello</p>")
    r = await authed.get("/m/E1/html?remote=0&theme=light")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == csp_header()

    body = r.text
    assert body.count("<script>") == 1
    served = body[body.index("<script>") + len("<script>") : body.index("</script>")]
    digest = hashlib.sha256(served.encode("utf-8")).digest()
    assert _csp_of(r)["script-src"] == [f"'sha256-{base64.b64encode(digest).decode()}'"]
    assert FRAME_SCRIPT_HASH == "sha256-" + base64.b64encode(digest).decode()


async def test_html_route_headers_are_the_locked_set(authed, fake):
    """`no-referrer` keeps the URL of the message being read out of every
    request the frame makes; `no-store` keeps a message body out of a shared
    browser's disk cache after the reader logs out. Both would otherwise be
    silently replaced by the app-wide middleware's laxer defaults.
    """
    fake.message("E1", html="<p>hello</p>")
    r = await authed.get("/m/E1/html")
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-type"].startswith("text/html")


async def test_html_route_sandbox_never_grants_same_origin_and_always_grants_scripts(authed, fake):
    fake.message("E1", html="<p>hello</p>")
    sandbox = _csp_of(await authed.get("/m/E1/html"))["sandbox"]
    assert "allow-same-origin" not in sandbox
    assert "allow-scripts" in sandbox


async def test_html_route_csp_is_identical_with_and_without_remote_images(authed, fake):
    """The header must not widen when the reader says "show images": every
    permitted remote image is rewritten to this app's own proxy, so a
    sanitiser miss still cannot reach the sender's host — the browser is
    refused the request either way.
    """
    fake.message("E1", html='<img src="https://track.test/a.gif">')
    off = await authed.get("/m/E1/html?remote=0")
    on = await authed.get("/m/E1/html?remote=1")
    assert off.headers["content-security-policy"] == on.headers["content-security-policy"]
    for response in (off, on):
        directives = _csp_of(response)
        assert directives["img-src"] == ["'self'", "data:"]
        for sources in directives.values():
            assert not any(s.startswith(("http:", "https:", "//")) for s in sources)


async def test_html_route_requires_a_session(app_client, fake):
    fake.message("E1", html="<p>x</p>")
    assert (await app_client.get("/m/E1/html")).status_code in (303, 401)


async def test_html_route_404s_when_there_is_no_html_to_frame(authed, fake):
    """The conversation view renders a text/plain body inline and never asks
    for a frame, so a request for one is a stale URL — not a message to
    render as an empty document.
    """
    fake.message("E1", text="plain only")
    assert (await authed.get("/m/E1/html")).status_code == 404
    assert (await authed.get("/m/nosuch/html")).status_code == 404


async def test_remote_images_become_this_readers_signed_same_origin_proxy_urls(
    authed, fake, reader
):
    """The wiring Task 6 could not land: `sign_image` in the frame's
    sanitize context. The document must carry a token that verifies back to
    the original URL for *this* reader, and the sender's host must appear
    nowhere in it.
    """
    secret_key, user_id = reader
    fake.message("E1", html='<img src="https://track.test/a.gif">')
    body = (await authed.get("/m/E1/html?remote=1")).text
    images = parse_attrs(body)["img"]
    assert len(images) == 1
    assert "track.test" not in body

    parts = urlsplit(images[0]["src"])
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "testserver", "/img")
    token = unquote(parse_qs(parts.query)["u"][0])
    # Both halves of what the sanitizer signed: the original URL, and *this*
    # reader — the id `GET /img` reads back off the token to key its
    # per-reader fan-out budget.
    assert verify_image_token(token, secret_key=secret_key) == (
        "https://track.test/a.gif",
        user_id,
    )


async def test_remote_images_are_dropped_entirely_when_the_reader_has_not_asked(authed, fake):
    fake.message("E1", html='<img src="https://track.test/a.gif"><p>body</p>')
    body = (await authed.get("/m/E1/html?remote=0")).text
    images = parse_attrs(body)["img"]
    assert len(images) == 1
    # The image element survives (it is the blocked-image placeholder the
    # base stylesheet styles with `img:not([src])`) — but with no src there
    # is nothing for the browser to fetch.
    assert "src" not in images[0]
    assert "track.test" not in body


async def test_an_inline_cid_image_resolves_against_this_messages_own_parts(authed, fake, reader):
    """`cid_parts` is a membership test, not a lookup table for the whole
    account: a message naming a Content-ID it does not itself carry gets no
    `src` at all, so one mail cannot address another's attachments by
    guessing.
    """
    fake.message(
        "E1",
        html='<img src="cid:logo@x"><img src="cid:someone-elses@x">',
        attachments=({"partId": "3", "blobId": "B1", "cid": "logo@x", "type": "image/png"},),
    )
    images = parse_attrs((await authed.get("/m/E1/html")).text)["img"]
    assert len(images) == 2
    parts = urlsplit(images[0]["src"])
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "testserver", "/m/E1/cid/logo%40x")
    # And the src carries the capability the route is served against, bound
    # to this message, this part and this reader.
    secret_key, user_id = reader
    token = unquote(parse_qs(parts.query)["u"][0])
    assert (
        verify_cid_token(token, secret_key=secret_key, email_id="E1", content_id="logo@x")
        == user_id
    )
    assert "src" not in images[1]


async def test_the_quoted_history_is_hidden_behind_the_frames_own_toggle(authed, fake):
    fake.message(
        "E1",
        html='<div>reply</div><div class="gmail_quote">older</div>',
    )
    tags = parse_attrs((await authed.get("/m/E1/html")).text)
    assert len([a for a in tags["button"] if "data-mailosh-quote-toggle" in a]) == 1
    assert len([a for a in tags["div"] if "data-mailosh-quote" in a]) == 1


# ---------------------------------------------------------------------------
# GET /m/{id}/frame
# ---------------------------------------------------------------------------


async def test_iframe_never_requests_same_origin(authed, fake):
    fake.message("E1", html="<p>x</p>")
    frag = (await authed.get("/m/E1/frame?thread=T1&remote=0")).text
    iframes = parse_attrs(frag)["iframe"]
    assert len(iframes) == 1
    attrs = iframes[0]
    assert attrs["sandbox"].split() == [
        "allow-scripts",
        "allow-popups",
        "allow-popups-to-escape-sandbox",
    ]
    assert "allow-same-origin" not in attrs["sandbox"]
    assert attrs["referrerpolicy"] == "no-referrer"
    assert attrs["loading"] == "lazy"
    assert attrs["class"] == "mail-frame"


async def test_the_frame_partial_points_at_the_html_route_with_the_readers_own_flag(authed, fake):
    fake.message("E1", html="<p>x</p>")
    for flag in ("0", "1"):
        frag = (await authed.get(f"/m/E1/frame?thread=T1&remote={flag}")).text
        assert parse_attrs(frag)["iframe"][0]["src"] == f"/m/E1/html?remote={flag}"

    # The wrapper Task 7's banner buttons swap, carrying the thread the
    # frame belongs to so that swap can re-render in context.
    wrapper = parse_attrs(frag)["div"][0]
    assert wrapper["id"] == "frame-E1"
    assert wrapper["data-thread"] == "T1"


async def test_the_frame_partial_requires_a_session(app_client):
    r = await app_client.get("/m/E1/frame?thread=T1")
    assert r.status_code in (303, 401)


# ---------------------------------------------------------------------------
# The remote-image gate, where the reader can actually see it
#
# The count and the host list are properties of the sanitising pass and of
# nothing else, so this route is the only place they can be read — which is
# what these tests pin: not that a banner *can* render (that is
# `test_reading_banners.py`, against the template), but that the numbers
# reaching it came from this message's own sanitise, and that the reader's
# stored policy is what decides whether they are taken at all.
# ---------------------------------------------------------------------------


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_text(html: str) -> str:
    """Everything a reader would see in `html`, whitespace-collapsed."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _banners(html: str) -> list[dict[str, str]]:
    """Every `.frame-banner` element in `html`, as its attributes."""
    return [
        attrs
        for attrs in parse_attrs(html).get("div", [])
        if "frame-banner" in attrs.get("class", "").split()
    ]


def _src(html: str) -> str:
    (iframe,) = parse_attrs(html)["iframe"]
    return iframe["src"]


TRACKED = '<img src="https://a.test/1.gif"><img src="https://a.test/2.gif"><img src="https://b.test/3.gif">'


async def test_a_blocked_message_carries_one_banner_counting_its_own_images(authed, fake):
    """Three images over two hosts: the count is images, the hosts are
    deduplicated, and both come out of this message's sanitise rather than
    from anything the request said.
    """
    fake.message("E1", html=TRACKED, sender="news@t.test")
    frag = (await authed.get("/m/E1/frame?thread=T1")).text

    assert len(_banners(frag)) == 1
    text = _visible_text(frag)
    assert "3 remote images" in text
    assert "a.test, b.test" in text
    assert _src(frag) == "/m/E1/html?remote=0"


async def test_a_message_with_nothing_blocked_gets_no_banner(authed, fake):
    fake.message("E1", html="<p>text only</p>", sender="news@t.test")
    frag = (await authed.get("/m/E1/frame?thread=T1")).text
    assert _banners(frag) == []
    assert len(parse_attrs(frag)["iframe"]) == 1


async def test_the_banner_offers_both_controls_aimed_at_this_frame(authed, fake):
    fake.message("E1", html=TRACKED, sender="News@T.test")
    frag = (await authed.get("/m/E1/frame?thread=T%201")).text

    (button,) = [attrs for attrs in parse_attrs(frag)["button"] if "hx-get" in attrs]
    assert button["hx-get"] == "/m/E1/frame?thread=T%201&remote=1"
    assert button["hx-target"] == "#frame-E1"

    (form,) = parse_attrs(frag)["form"]
    assert form["hx-post"] == "/m/E1/images/allow"
    assert form["hx-target"] == "#frame-E1"
    # The folded address the allow route will check the post against, not
    # whatever case the `From` header happened to use.
    (field,) = parse_attrs(frag)["input"]
    assert field["value"] == "news@t.test"


async def test_the_readers_policy_decides_when_they_have_not_answered(authed, fake, set_prefs):
    """The preference is shipped and stored; until something reads it,
    choosing "always show" does nothing at all.
    """
    fake.message("E1", html=TRACKED, sender="news@t.test")
    assert _banners((await authed.get("/m/E1/frame?thread=T1")).text) != []

    await set_prefs(remote_images="always")
    frag = (await authed.get("/m/E1/frame?thread=T1")).text
    assert _banners(frag) == []
    assert _src(frag) == "/m/E1/html?remote=1"


async def test_an_unknown_policy_blocks_rather_than_shows(authed, fake, set_prefs):
    """A stale row or a policy a later build wrote must fail towards "the
    reader is not tracked".
    """
    fake.message("E1", html=TRACKED, sender="news@t.test")
    await set_prefs(remote_images="whatever-comes-next")
    frag = (await authed.get("/m/E1/frame?thread=T1")).text
    assert len(_banners(frag)) == 1
    assert _src(frag) == "/m/E1/html?remote=0"


@pytest.mark.parametrize(
    "policy,override,shown",
    [("ask", "1", True), ("always", "0", False), ("ask", None, False), ("always", None, True)],
)
async def test_the_per_message_override_beats_the_policy_in_both_directions(
    authed, fake, set_prefs, policy, override, shown
):
    fake.message("E1", html=TRACKED, sender="news@t.test")
    await set_prefs(remote_images=policy)
    url = "/m/E1/frame?thread=T1" + ("" if override is None else f"&remote={override}")
    frag = (await authed.get(url)).text

    assert _src(frag) == f"/m/E1/html?remote={1 if shown else 0}"
    assert (_banners(frag) == []) is shown


async def test_a_contact_is_trusted_only_under_the_contacts_policy(
    running, authed, fake, set_prefs, reader
):
    """Someone the reader has written to has already been handed their
    address — but only when the reader asked for that rule.
    """
    _, user_id = reader
    fake.message("E1", html=TRACKED, sender="priya@t.test")
    await set_prefs(remote_images="ask")

    async with running.state.sessionmaker() as db:
        db.add(Contact(user_id=user_id, email="Priya@T.test", count=1))
        await db.commit()

    assert _src((await authed.get("/m/E1/frame?thread=T1")).text) == "/m/E1/html?remote=0"
    await set_prefs(remote_images="contacts")
    assert _src((await authed.get("/m/E1/frame?thread=T1")).text) == "/m/E1/html?remote=1"


# --- POST /m/{id}/images/allow ---------------------------------------------


async def test_allowing_a_sender_writes_the_row_and_re_renders_with_images_on(
    running, authed, fake, csrf
):
    fake.message("E1", html=TRACKED, sender="News@T.test")
    response = await authed.post("/m/E1/images/allow", data={"sender": "news@t.test"}, headers=csrf)

    assert response.status_code == 200
    assert _banners(response.text) == []
    assert _src(response.text) == "/m/E1/html?remote=1"
    # The row is the reader's judgement about a sender, so it is keyed on
    # the folded address and outlives this message.
    async with running.state.sessionmaker() as db:
        rows = (await db.execute(select(ImageSenderAllow))).scalars().all()
    assert [row.sender_email for row in rows] == ["news@t.test"]


async def test_an_allow_listed_sender_shows_images_on_a_later_message(authed, fake, csrf):
    fake.message("E1", html=TRACKED, sender="news@t.test")
    await authed.post("/m/E1/images/allow", data={"sender": "news@t.test"}, headers=csrf)

    fake.message("E2", html=TRACKED, sender="NEWS@t.test")
    assert _src((await authed.get("/m/E2/frame?thread=T1")).text) == "/m/E2/html?remote=1"
    fake.message("E3", html=TRACKED, sender="other@t.test")
    assert _src((await authed.get("/m/E3/frame?thread=T1")).text) == "/m/E3/html?remote=0"


async def test_the_allow_route_refuses_an_address_that_is_not_this_messages_own(
    running, authed, fake, csrf
):
    """The field is a form value and what it writes is a durable row that
    permits remote loads. Without the check, one message would be a
    tracking-consent primitive for every address in the reader's mail.
    """
    fake.message("E1", html=TRACKED, sender="news@t.test")
    response = await authed.post(
        "/m/E1/images/allow", data={"sender": "attacker@evil.test"}, headers=csrf
    )

    assert response.status_code == 403
    async with running.state.sessionmaker() as db:
        assert (await db.execute(select(ImageSenderAllow))).scalars().all() == []


async def test_the_allow_route_needs_a_csrf_token(running, authed, fake):
    fake.message("E1", html=TRACKED, sender="news@t.test")
    response = await authed.post("/m/E1/images/allow", data={"sender": "news@t.test"})

    assert response.status_code == 403
    async with running.state.sessionmaker() as db:
        assert (await db.execute(select(ImageSenderAllow))).scalars().all() == []


async def test_the_allow_route_requires_a_session(app_client, fake):
    fake.message("E1", html=TRACKED, sender="news@t.test")
    response = await app_client.post("/m/E1/images/allow", data={"sender": "news@t.test"})
    assert response.status_code in (303, 401, 403)


async def test_the_allow_route_404s_for_a_message_this_account_does_not_have(authed, fake, csrf):
    response = await authed.post(
        "/m/nosuch/images/allow", data={"sender": "news@t.test"}, headers=csrf
    )
    assert response.status_code == 404


async def test_a_sender_mismatch_is_not_reported_as_a_stale_csrf_token(authed, fake, csrf):
    """The app translates a CSRF 403 on an HX request into the "signed out
    elsewhere, reload" toast. That translation used to key on the status
    alone, so *this* 403 -- a valid token, a sender that is not the
    message's own -- told the reader to reload a page that was fine."""
    fake.message("E1", html=TRACKED, sender="news@t.test")
    response = await authed.post(
        "/m/E1/images/allow",
        data={"sender": "someone-else@t.test"},
        headers={**csrf, "HX-Request": "true"},
    )
    assert response.status_code == 403
    assert "hx-trigger" not in response.headers
    assert "hx-reswap" not in response.headers


# ---------------------------------------------------------------------------
# GET /img — the four route tests Task 6 could not write
# ---------------------------------------------------------------------------


async def test_proxy_serves_only_allowlisted_types_with_locked_headers(
    authed, respx_mock, token_for, resolves_public
):
    respx_mock.get("https://cdn.test/a.png").respond(
        200,
        headers={"content-type": "image/png", "set-cookie": "a=b", "x-upstream": "leak"},
        content=b"PNG",
    )
    r = await authed.get("/img?u=" + token_for("https://cdn.test/a.png"))
    assert r.status_code == 200
    assert r.content == b"PNG"
    assert r.headers["content-type"] == "image/png"
    assert "set-cookie" not in r.headers
    assert "x-upstream" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["cache-control"] == "private, max-age=86400"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"


async def test_proxy_refuses_a_type_the_guard_does_not_allow(
    authed, respx_mock, token_for, resolves_public
):
    """`image/svg+xml` is a scripting context, not a picture. Served from
    this origin it would be an XSS, so it is a 502 like any other refusal.
    """
    respx_mock.get("https://cdn.test/x.svg").respond(
        200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>"
    )
    r = await authed.get("/img?u=" + token_for("https://cdn.test/x.svg"))
    assert r.status_code == 502
    assert r.content == b""


async def test_proxy_rejects_an_unsigned_or_foreign_token(authed, token_for):
    assert (await authed.get("/img?u=nonsense")).status_code == 403
    assert (await authed.get("/img")).status_code == 422
    # A token this app minted, for somebody else.
    other = sign_remote_url("https://cdn.test/a.png", secret_key="k" * 40, user_id=9999)
    assert (await authed.get("/img?u=" + other)).status_code == 403


async def test_proxy_needs_no_session_because_the_frame_that_fetches_it_has_none(
    app_client, respx_mock, token_for, resolves_public
):
    """The defect this route had, asserted as its fix.

    A `UserDep` here was not a second line of defence — it was a `303 ->
    /login` for every proxied image in the app, because the `<img>` making
    the request lives in an opaque-origin document that attaches no
    `SameSite=Lax` cookie. `app_client` holds no session at all, which is
    precisely the shape of the real request.

    What that costs, stated rather than glossed: the token is now a bearer
    capability. It is still unforgeable, still names exactly one URL, still
    dies in an hour, and `fetch_guard` still decides what may be fetched —
    so what a leaked token buys is one image, once, for an hour, and no
    access to any mailbox.
    """
    respx_mock.get("https://cdn.test/a.png").respond(200, headers=PNG, content=b"PNG")
    r = await app_client.get("/img?u=" + token_for("https://cdn.test/a.png"))
    assert r.status_code == 200
    assert r.content == b"PNG"


async def test_proxy_still_refuses_a_token_this_app_did_not_mint(app_client):
    """Dropping the session did not drop the signature. Everything the
    token check refused before, it refuses with no cookie in play."""
    assert (await app_client.get("/img?u=nonsense")).status_code == 403
    forged = sign_remote_url("https://cdn.test/a.png", secret_key="k" * 40, user_id=1)
    assert (await app_client.get("/img?u=" + forged)).status_code == 403


async def test_proxy_answers_502_and_no_body_on_a_blocked_target(authed, token_for):
    """`BlockedUrl` *is* a `ValueError`, so a single `except ValueError`
    around both the token check and the fetch would answer 403 here —
    telling whoever sent the email that their token was fine and their
    target was the problem. The status is the assertion, and so is the
    empty body: this response is read by an `<img>` in the sender's own
    document, and a reason string there is a probe result.
    """
    r = await authed.get("/img?u=" + token_for("http://127.0.0.1/x"))
    assert r.status_code == 502
    assert r.content == b""


async def test_the_proxy_caps_one_readers_outbound_fetches(authed, monkeypatch, token_for):
    """One message can carry two hundred remote images and the browser will
    ask for all of them at once. Without the semaphore that is two hundred
    concurrent sockets from one reader.
    """
    live = 0
    peak = 0

    async def slow(url: str) -> tuple[str, bytes]:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
        finally:
            live -= 1
        return "image/png", b"PNG"

    monkeypatch.setattr(frames, "fetch_image", slow)
    responses = await asyncio.gather(
        *(authed.get("/img?u=" + token_for(f"https://cdn.test/{i}.png")) for i in range(20))
    )
    assert [r.status_code for r in responses] == [200] * 20
    assert peak == frames.FANOUT_LIMIT


async def test_the_fan_out_registry_holds_one_reusable_slot_per_reader(running, authed, token_for):
    """Per user, not global: one reader's newsletter must not stall another
    reader's inbox. And one slot per reader, not one per request — a
    semaphore rebuilt on every hit is a cap that never binds.
    """
    assert not hasattr(running.state, "image_fanout")
    await authed.get("/img?u=" + token_for("http://127.0.0.1/x"))
    registry = running.state.image_fanout
    assert len(registry) == 1
    (slot,) = registry.values()

    await authed.get("/img?u=" + token_for("http://127.0.0.1/y"))
    assert len(running.state.image_fanout) == 1
    assert next(iter(running.state.image_fanout.values())) is slot
    # Released after each fetch, not leaked.
    assert slot._value == frames.FANOUT_LIMIT


# ---------------------------------------------------------------------------
# Source-shape guards that survive a missing node
# ---------------------------------------------------------------------------


def test_frame_js_names_the_same_bounds_the_server_does():
    """The two halves of the handshake are in different languages, so the
    clamp is the one number that cannot be shared by import. It can still
    be pinned.
    """
    src = FRAME_JS.read_text()
    assert re.search(r"MIN_FRAME_HEIGHT\s*=\s*200\b", src)
    assert re.search(r"MAX_FRAME_HEIGHT\s*=\s*20000\b", src)


def test_frame_js_looks_for_every_class_the_app_actually_frames_with(frame_js_run):
    """A frame the selector misses is a frame that never resizes. Two
    spellings is one too many — `thread/frame.html`'s `.mail-frame` and the
    conversation view's inline `.msg-frame` should collapse to one — but
    until they do, both must be found. Widening the selector is not a
    security decision: what admits a message is the window-identity check,
    not the class.
    """
    selector = {s.strip() for s in frame_js_run["selector"].split(",")}
    assert selector == {"iframe.mail-frame", "iframe.msg-frame"}

    # `.att-preview-frame` (thread/preview.html) is the one iframe in the
    # tree that is deliberately outside this rule: it holds an *attachment*,
    # not one of our own rendered documents, so nothing inside it will ever
    # post a height and a selector that matched it would be claiming
    # otherwise. Its size is CSS's (styles/thread.css), which is why it can
    # be left out without leaving a frame stuck at 200px. Named here rather
    # than filtered by a pattern so a third class cannot join it silently.
    framed = {attrs.get("class") for _, attrs in _iframes()} - {"att-preview-frame"}
    assert framed <= {s.removeprefix("iframe.") for s in selector}


# ---------------------------------------------------------------------------
# GET /m/{id}/cid/{content_id} — inline parts
# ---------------------------------------------------------------------------


def _part(**overrides: object) -> dict:
    """One wire-shaped `EmailBodyPart`, defaulted to a small inline PNG."""
    return {
        "partId": "3",
        "blobId": "B3",
        "type": "image/png",
        "cid": "logo@mail",
        "disposition": "inline",
        "size": 3,
        "name": "logo.png",
        **overrides,
    }


async def test_cid_part_streams_with_locked_down_headers(app_client, fake, cid_url):
    """Fetched by `app_client` — the client with **no session cookie** —
    because that is the only client that reproduces the request a browser
    actually makes. The frame document has an opaque origin, so its `<img>`
    sends no cookie; a test that fetched this through `authed` would keep
    passing however broken the capability was, which is exactly how the
    original defect shipped green.
    """
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    r = await app_client.get(cid_url("E1", "logo@mail"))
    assert r.status_code == 200
    assert r.content == b"PNG"
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"


async def test_cid_matching_strips_the_angle_brackets_rfc2392_leaves_off_the_url(
    app_client, fake, cid_url
):
    """A Content-ID header carries `<...>`; a `cid:` URL never does. The
    route and `html_sanitize`'s rewrite must strip identically, or an
    ordinary newsletter's logo 404s.
    """
    fake.message("E1", attachments=(_part(cid="<logo@mail>"),))
    fake.blob("B3", b"PNG")
    assert (await app_client.get(cid_url("E1", "logo@mail"))).status_code == 200


async def test_cid_matching_is_case_sensitive_as_rfc2392_requires(app_client, fake, cid_url):
    """Only the `cid:` *scheme* is case-insensitive; the id is not. A
    case-folding lookup would let one part answer for another — in the
    token's resource check as much as in the part lookup, so both spellings
    are minted honestly here and only the right one is served."""
    fake.message("E1", attachments=(_part(cid="Logo@Mail"),))
    fake.blob("B3", b"PNG")
    assert (await app_client.get(cid_url("E1", "Logo@Mail"))).status_code == 200
    assert (await app_client.get(cid_url("E1", "logo@mail"))).status_code == 404


@pytest.mark.parametrize(
    "mime",
    [
        "image/svg+xml",
        "text/html",
        "application/pdf",
        "application/octet-stream",
        "image/x-icon+xml",
    ],
)
async def test_non_image_cid_parts_are_404_not_served(app_client, fake, cid_url, mime):
    """`image/svg+xml` is a scripting context, not a picture: served from
    this origin under an `<img>` the frame already permits, it would be the
    one XSS the whole sandbox exists to prevent. The allow-list is
    positive, so every other type is refused by construction.
    """
    fake.message("E1", attachments=(_part(type=mime, cid="x@m"),))
    fake.blob("B3", b"<svg onload=alert(1)>")
    assert (await app_client.get(cid_url("E1", "x@m"))).status_code == 404


async def test_a_declared_type_with_parameters_is_normalised_before_the_allow_list(
    app_client, fake, cid_url
):
    fake.message("E1", attachments=(_part(type="IMAGE/PNG; name=a.png"),))
    fake.blob("B3", b"PNG")
    r = await app_client.get(cid_url("E1", "logo@mail"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


async def test_an_unknown_cid_and_an_oversized_part_are_both_404(app_client, fake, cid_url):
    fake.message("E1", attachments=(_part(cid="x@m", size=99 * 1024 * 1024),))
    fake.blob("B3", b"PNG")
    assert (await app_client.get(cid_url("E1", "nope@m"))).status_code == 404
    assert (await app_client.get(cid_url("E1", "x@m"))).status_code == 404


async def test_a_part_declaring_a_small_size_cannot_stream_past_the_ceiling(
    app_client, fake, cid_url
):
    """`size` is metadata a sender writes. A part that claims three bytes
    and serves six megabytes must still be cut off at the cap, or the
    declared size is the only thing bounding what this server forwards.
    """
    fake.message("E1", attachments=(_part(size=3),))
    fake.blob("B3", b"P" * (frames.MAX_INLINE_BYTES + 4096))
    r = await app_client.get(cid_url("E1", "logo@mail"))
    assert r.status_code == 200
    assert len(r.content) == frames.MAX_INLINE_BYTES


async def test_a_cid_from_another_message_is_404_even_though_the_blob_exists(
    app_client, fake, cid_url
):
    """A Content-ID is not a capability. `html_sanitize` already refuses a
    `cid:` that is not one of *this* message's parts; the route repeats the
    check rather than trusting that, because the URL is guessable and
    nothing stops a reader (or a script in some other tab) requesting it
    directly.
    """
    fake.message("E1", attachments=(_part(cid="mine@m"),))
    fake.message("E2", attachments=(_part(blobId="B9", cid="theirs@m"),))
    fake.blob("B3", b"PNG")
    fake.blob("B9", b"OTHER")
    assert (await app_client.get(cid_url("E1", "theirs@m"))).status_code == 404
    assert (await app_client.get(cid_url("E2", "theirs@m"))).status_code == 200


async def test_cid_route_404s_for_a_message_this_account_does_not_have(app_client, cid_url):
    assert (await app_client.get(cid_url("nosuch", "logo@mail"))).status_code == 404


async def test_an_inline_image_in_a_rendered_frame_resolves_against_this_route(
    authed, app_client, fake
):
    """The end-to-end seam, and the one this whole change exists for: what
    the sanitiser writes into the document is a URL this router answers
    **without a cookie**, because the document that fetches it has an opaque
    origin and cannot send one.

    Two clients on purpose. `authed` renders the frame (that request is a
    navigation and does carry the session); `app_client` fetches the image
    exactly as the framed `<img>` does, with nothing but the URL.
    """
    fake.message("E1", html='<img src="cid:logo@mail">', attachments=(_part(),))
    fake.blob("B3", b"PNG")
    doc = (await authed.get("/m/E1/html")).text
    images = parse_attrs(doc)["img"]
    assert len(images) == 1
    src = images[0]["src"]
    assert src.startswith("http://testserver/m/E1/cid/logo%40mail?u=")
    served = await app_client.get(src.removeprefix("http://testserver"))
    assert served.status_code == 200
    assert served.content == b"PNG"


# ---------------------------------------------------------------------------
# The cid capability: what the token has to be bound to, and what happens
# when each binding is broken
# ---------------------------------------------------------------------------
#
# This block is the price of authorising `/m/{id}/cid/{cid}` by a token
# rather than by a session. The route reads a part of somebody's mail with
# no cookie in play, so the token is the *whole* admission decision, and a
# binding left out is not a missing feature — it is a mailbox disclosure.
# Every test below mints a real, correctly signed, unexpired token and then
# changes exactly one thing about it.


@pytest.fixture
async def second_reader(running, authed, pooled) -> int:
    """A second logged-in reader with a mailbox of their own, and the id
    their capabilities are minted under.

    Depends on `authed` so the first reader exists and is logged in first —
    the point of the fixture is two readers at once, each with a live
    session and a distinct `FakeClient` behind the pool.
    """
    other = FakeClient()
    other.message("E1", attachments=(_part(),))
    other.blob("B3", b"THEIRS")
    async with _asgi(running) as client:
        response = await client.post("/login", data={"username": "e@x", "password": "right"})
        assert response.status_code == 303, response.text
    async with running.state.sessionmaker() as db:
        rows = (await db.execute(select(AppUser).order_by(AppUser.id))).scalars().all()
    user_id = next(row.id for row in rows if row.stalwart_username == "e@x")
    pooled[user_id] = other
    return user_id


async def test_a_cid_token_reads_only_the_mailbox_of_the_reader_it_names(
    app_client, fake, cid_url, second_reader
):
    """The binding that matters most, shown as a difference rather than as
    a refusal: the *same* URL path, two tokens, two mailboxes.

    Both readers hold a message called `E1` carrying a part called
    `logo@mail` — which is the realistic case, since JMAP ids are per
    account and two accounts collide freely. If the route resolved the
    message anywhere but through the token's own reader, one of these two
    fetches would return the other's bytes.
    """
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"MINE")

    mine = await app_client.get(cid_url("E1", "logo@mail"))
    theirs = await app_client.get(cid_url("E1", "logo@mail", signed_user=second_reader))
    assert (mine.status_code, mine.content) == (200, b"MINE")
    assert (theirs.status_code, theirs.content) == (200, b"THEIRS")


async def test_a_cid_token_cannot_reach_a_message_its_own_reader_does_not_hold(
    app_client, fake, cid_url, second_reader
):
    """The same property with the collision removed: a token naming the
    second reader is refused for a message only the first reader has, even
    though the URL is one the first reader's own token serves.
    """
    fake.message("E9", attachments=(_part(),))
    fake.blob("B3", b"MINE")

    assert (await app_client.get(cid_url("E9", "logo@mail"))).status_code == 200
    assert (
        await app_client.get(cid_url("E9", "logo@mail", signed_user=second_reader))
    ).status_code == 404


async def test_a_cid_token_for_one_message_does_not_read_another(app_client, fake, cid_url):
    """The token names the message. Without that it would be a capability
    for `logo@mail` in *any* message this reader holds — and a signature
    logo has the same Content-ID across every message a correspondent ever
    sent, so one leaked URL would read the lot.
    """
    fake.message("E1", attachments=(_part(),))
    fake.message("E2", attachments=(_part(blobId="B7"),))
    fake.blob("B3", b"ONE")
    fake.blob("B7", b"TWO")

    assert (await app_client.get(cid_url("E2", "logo@mail"))).status_code == 200
    # A real token for E1's part, presented on E2's path.
    assert (
        await app_client.get(cid_url("E2", "logo@mail", signed_message="E1"))
    ).status_code == 404


async def test_a_cid_token_for_one_part_does_not_read_another(app_client, fake, cid_url):
    """The token names the Content-ID. Without that it would be a
    capability for the whole message: every inline part it carries, read
    with one URL and a guess.
    """
    fake.message(
        "E1",
        attachments=(_part(), _part(partId="4", blobId="B4", cid="secret@mail", name="s.png")),
    )
    fake.blob("B3", b"LOGO")
    fake.blob("B4", b"SECRET")

    assert (await app_client.get(cid_url("E1", "secret@mail"))).status_code == 200
    # A real token for the logo, presented on the other part's path.
    assert (
        await app_client.get(cid_url("E1", "secret@mail", signed_cid="logo@mail"))
    ).status_code == 404


async def test_a_cid_token_expires(app_client, fake, cid_url):
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    fresh = time.time()
    assert (await app_client.get(cid_url("E1", "logo@mail", now=fresh))).status_code == 200
    stale = fresh - CID_URL_TTL - 1
    assert (await app_client.get(cid_url("E1", "logo@mail", now=stale))).status_code == 404


async def test_the_cid_ttl_matches_the_image_tokens_hour():
    """One number, stated in two places because they are two decisions —
    but a capability into the reader's own mailbox must never outlive the
    one into a stranger's CDN."""
    assert CID_URL_TTL == 3600
    assert CID_URL_TTL <= IMAGE_URL_TTL


@pytest.mark.parametrize("where", ["payload", "signature"])
async def test_a_tampered_cid_token_is_refused(app_client, fake, cid_url, where):
    """Fails closed, not "logged and served". The flip is in the base64 of
    one half or the other, so the payload's own fields stay well-formed and
    only the MAC disagrees.
    """
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    url = cid_url("E1", "logo@mail")
    head, query = url.split("?u=")
    payload, signature = unquote(query).split(".")
    if where == "payload":
        payload = payload[:-2] + ("AB" if payload[-2:] != "AB" else "CD")
    else:
        signature = signature[:-2] + ("AB" if signature[-2:] != "AB" else "CD")
    assert (
        await app_client.get(f"{head}?u={quote(payload + '.' + signature, safe='')}")
    ).status_code == 404


async def test_a_forged_user_id_in_a_cid_token_is_refused(app_client, fake, reader):
    """`True == 1` in Python, so a payload carrying `{"s": true}` would read
    user 1's mail under a naive comparison. The app cannot mint such a
    payload; `sign_payload` under the real key can, which is the only way to
    ask the question.
    """
    secret_key, _ = reader
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    forged = sign_payload(
        {"m": "E1", "c": "logo@mail", "s": True},
        secret_key=secret_key,
        purpose="cid",
        ttl=CID_URL_TTL,
    )
    r = await app_client.get(f"/m/E1/cid/logo%40mail?u={quote(forged, safe='')}")
    assert r.status_code == 404


async def test_an_image_token_and_a_cid_token_cannot_be_spent_as_each_other(
    app_client, fake, reader, token_for, cid_url
):
    """Two purposes, two derived keys. The payloads look alike enough — both
    carry `"s"` — that only the key separation stops a proxy token from
    being a mailbox read, so it is asserted in both directions.
    """
    secret_key, user_id = reader
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")

    proxy = token_for("https://cdn.test/a.png")
    assert (
        await app_client.get(f"/m/E1/cid/logo%40mail?u={quote(proxy, safe='')}")
    ).status_code == 404

    inline = sign_cid_url("E1", "logo@mail", secret_key=secret_key, user_id=user_id)
    assert (await app_client.get("/img?u=" + quote(inline, safe=""))).status_code == 403


async def test_a_missing_token_is_the_same_404_as_a_wrong_one(app_client, fake, cid_url):
    """One status *and one body* for every refusal. Two distinguishable
    ones would answer "does this message carry this Content-ID?" for anyone
    willing to ask twice — and this route answers without a session.
    """
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    bodies = {
        (await app_client.get("/m/E1/cid/logo%40mail")).text,
        (await app_client.get("/m/E1/cid/logo%40mail?u=")).text,
        (await app_client.get("/m/E1/cid/logo%40mail?u=nonsense")).text,
        (await app_client.get(cid_url("E1", "nope@mail"))).text,
        (await app_client.get(cid_url("nosuch", "logo@mail"))).text,
        (await app_client.get(cid_url("E1", "logo@mail", signed_message="E2"))).text,
    }
    assert len(bodies) == 1, bodies


async def test_a_cid_capability_dies_with_the_readers_last_session(
    running, app_client, fake, cid_url, reader
):
    """The token names a reader; acting as that reader needs one of their
    sessions, because the per-session Stalwart API key is the only
    credential this app holds for them. So "sign out everywhere" revokes
    outstanding image URLs at that moment rather than at their expiry —
    which is the answer to the one thing a bearer capability would
    otherwise be bad at.
    """
    _, user_id = reader
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    url = cid_url("E1", "logo@mail")
    assert (await app_client.get(url)).status_code == 200

    async with running.state.sessionmaker() as db:
        await sessions.revoke_all(db, user_id)
    assert (await app_client.get(url)).status_code == 404


async def test_an_expired_session_does_not_serve_a_live_capability(
    running, app_client, fake, cid_url
):
    """Not only deletion: a session still in the table but past its
    absolute expiry is not a credential either. `live_session_for_user`
    applies the same two expirations `load_session` does, which is the
    property that stops a capability from outliving the login it was minted
    under.
    """
    fake.message("E1", attachments=(_part(),))
    fake.blob("B3", b"PNG")
    url = cid_url("E1", "logo@mail")
    assert (await app_client.get(url)).status_code == 200

    async with running.state.sessionmaker() as db:
        rows = (await db.execute(select(SessionRow))).scalars().all()
        for row in rows:
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert (await app_client.get(url)).status_code == 404


# ---------------------------------------------------------------------------
# GET /m/{id}/att/{blob_id} — the download half of Task 11
# ---------------------------------------------------------------------------


def _attachment(**overrides: object) -> dict:
    return {
        "partId": "2",
        "blobId": "B2",
        "type": "application/pdf",
        "cid": None,
        "disposition": "attachment",
        "size": 9,
        "name": "spec.pdf",
        **overrides,
    }


def _disposition(header: str) -> tuple[str, dict[str, str]]:
    """`(type, {param: value})` for a Content-Disposition header value."""
    kind, _, rest = header.partition(";")
    params: dict[str, str] = {}
    for chunk in rest.split(";"):
        name, _, value = chunk.partition("=")
        if name.strip():
            params[name.strip().lower()] = value.strip().strip('"')
    return kind.strip(), params


async def test_download_is_octet_stream_with_both_filename_forms(authed, fake):
    """RFC 5987: the ASCII-folded `filename=` is what an old client reads,
    `filename*=UTF-8''` is what everything else reads. Emitting only the
    second loses the name on the first; only the first loses the accents.
    """
    fake.message("E1", attachments=(_attachment(name="rapport été.pdf"),))
    fake.blob("B2", b"%PDF-1.7 ")
    r = await authed.get("/m/E1/att/B2")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.7 "
    assert r.headers["content-type"] == "application/octet-stream"
    kind, params = _disposition(r.headers["content-disposition"])
    assert kind == "attachment"
    assert params["filename*"] == "UTF-8''rapport%20%C3%A9t%C3%A9.pdf"
    assert params["filename"].isascii()
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("mime", "served"),
    [
        ("image/png", "image/png"),
        ("image/gif", "image/gif"),
        ("image/jpeg", "image/jpeg"),
        ("image/webp", "image/webp"),
        ("image/bmp", "image/bmp"),
        ("application/pdf", "application/pdf"),
        ("text/plain", "text/plain"),
    ],
)
async def test_previewable_types_are_served_inline_with_their_real_type(authed, fake, mime, served):
    fake.message("E1", attachments=(_attachment(type=mime, name="f"),))
    fake.blob("B2", b"abc")
    r = await authed.get("/m/E1/att/B2?inline=1")
    assert r.headers["content-type"].split(";")[0] == served
    assert _disposition(r.headers["content-disposition"])[0] == "inline"


@pytest.mark.parametrize(
    "mime",
    [
        "text/html",
        "image/svg+xml",
        "application/xhtml+xml",
        "application/x-msdownload",
        "text/xml",
        "application/zip",
    ],
)
async def test_dangerous_types_are_never_inline_even_with_the_flag(authed, fake, mime):
    """`inline=1` is a request, not an instruction. Anything outside
    `PREVIEW_TYPES` falls back to the download shape, so a `.html`
    attachment cannot be rendered as a page on this app's own origin no
    matter what the URL asks for.
    """
    fake.message("E1", attachments=(_attachment(type=mime, name="f"),))
    fake.blob("B2", b"<script>alert(1)</script>")
    r = await authed.get("/m/E1/att/B2?inline=1")
    assert r.headers["content-type"] == "application/octet-stream"
    assert _disposition(r.headers["content-disposition"])[0] == "attachment"


async def test_every_preview_type_is_one_a_browser_cannot_script(authed, fake):
    """The allow-list itself, not one sample from it: a type added here
    later must be inert in a document context.
    """
    assert frames.PREVIEW_TYPES == {
        "image/png",
        "image/gif",
        "image/jpeg",
        "image/webp",
        "image/bmp",
        "application/pdf",
        "text/plain",
    }
    assert frames.INLINE_IMAGE_TYPES == {
        "image/png",
        "image/gif",
        "image/jpeg",
        "image/webp",
        "image/bmp",
        "image/x-icon",
    }
    assert "image/svg+xml" not in frames.PREVIEW_TYPES | frames.INLINE_IMAGE_TYPES


async def test_a_blob_id_from_another_message_is_404(authed, fake):
    """A blob id is not a capability either. Scoping is by *message*, not
    by account: two messages in one mailbox must not be able to serve each
    other's parts, because the id is the only thing the URL carries.
    """
    fake.message("E1", attachments=(_attachment(blobId="B2"),))
    fake.message("E2", attachments=(_attachment(blobId="B9"),))
    fake.blob("B2", b"mine")
    fake.blob("B9", b"theirs")
    assert (await authed.get("/m/E1/att/B9")).status_code == 404
    assert (await authed.get("/m/E2/att/B9")).status_code == 200


async def test_the_whole_message_blob_is_not_reachable_through_the_attachment_route(authed, fake):
    """`blobId` on the Email itself is the raw RFC 5322 source. It has its
    own route, with its own cap and its own `text/plain`; this one serves
    parts, and must not become a second, uncapped way to the same bytes.
    """
    fake.message("E1", blob_id="B0", attachments=(_attachment(),))
    fake.blob("B0", b"From: a@x\r\n\r\nbody")
    fake.blob("B2", b"%PDF-1.7 ")
    assert (await authed.get("/m/E1/att/B0")).status_code == 404


async def test_a_filename_cannot_inject_a_response_header(authed, fake):
    """CR/LF in an attachment name is a header-injection attempt, not a
    formatting quirk — the name comes straight off the wire.
    """
    fake.message("E1", attachments=(_attachment(name='a"\r\nSet-Cookie: x=y\r\n.pdf'),))
    fake.blob("B2", b"%PDF-1.7 ")
    r = await authed.get("/m/E1/att/B2")
    assert "set-cookie" not in r.headers
    header = r.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header
    kind, params = _disposition(header)
    assert kind == "attachment"
    assert params["filename"].count('"') == 0


async def test_a_nameless_part_still_downloads(authed, fake):
    fake.message("E1", attachments=(_attachment(name=None),))
    fake.blob("B2", b"%PDF-1.7 ")
    r = await authed.get("/m/E1/att/B2")
    assert r.status_code == 200
    assert _disposition(r.headers["content-disposition"])[0] == "attachment"


async def test_attachment_headers_are_the_locked_set(authed, fake):
    """The CSP is the route's own and nothing else's: `create_app`'s
    app-wide header permits `script-src 'self'`, and only this response's
    stricter one reduces a blob to `default-src 'none'; sandbox`. `nosniff`
    is guaranteed twice over — here and by that same middleware — which is
    the intent, not an accident: see `_BLOB_HEADERS`.
    """
    fake.message("E1", attachments=(_attachment(),))
    fake.blob("B2", b"%PDF-1.7 ")
    for query in ("", "?inline=1"):
        r = await authed.get(f"/m/E1/att/B2{query}")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "private, max-age=3600"


async def test_attachment_route_requires_a_session(app_client, fake):
    fake.message("E1", attachments=(_attachment(),))
    fake.blob("B2", b"%PDF-1.7 ")
    assert (await app_client.get("/m/E1/att/B2")).status_code in (303, 401)


# ---------------------------------------------------------------------------
# GET /m/{id}/source — the raw message, which two links already point at
# ---------------------------------------------------------------------------


async def test_source_route_is_plain_text_and_capped(authed, fake):
    fake.message("E1", blob_id="B0")
    fake.blob("B0", b"From: a@x\r\n\r\nbody")
    r = await authed.get("/m/E1/source")
    assert r.status_code == 200
    assert r.content == b"From: a@x\r\n\r\nbody"
    assert r.headers["content-type"].startswith("text/plain")
    assert "charset=utf-8" in r.headers["content-type"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert frames.MAX_SOURCE_BYTES == 2 * 1024 * 1024


async def test_source_is_cut_off_at_the_cap_rather_than_streamed_whole(authed, fake):
    """The one reader who reaches this route is the one looking at a body
    Stalwart already truncated — i.e. a message big enough that forwarding
    it whole is the failure mode, not the feature.
    """
    fake.message("E1", blob_id="B0")
    fake.blob("B0", b"H" * (frames.MAX_SOURCE_BYTES + 1024))
    r = await authed.get("/m/E1/source")
    assert len(r.content) == frames.MAX_SOURCE_BYTES


async def test_source_is_never_served_as_something_a_browser_would_render(authed, fake):
    """The raw source of a message *is* the sender's HTML. Serving it as
    anything but `text/plain` with `nosniff` would render it — on this
    app's own origin, outside the frame and outside its CSP.
    """
    fake.message("E1", blob_id="B0")
    fake.blob("B0", b"Content-Type: text/html\r\n\r\n<script>alert(1)</script>")
    r = await authed.get("/m/E1/source")
    assert r.headers["content-type"].split(";")[0] == "text/plain"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_source_404s_when_the_message_has_no_blob_of_its_own(authed, fake):
    fake.message("E1", html="<p>x</p>")
    assert (await authed.get("/m/E1/source")).status_code == 404
    assert (await authed.get("/m/nosuch/source")).status_code == 404


async def test_source_route_requires_a_session(app_client, fake):
    fake.message("E1", blob_id="B0")
    fake.blob("B0", b"raw")
    assert (await app_client.get("/m/E1/source")).status_code in (303, 401)


# ---------------------------------------------------------------------------
# The dark restyle, wired
# ---------------------------------------------------------------------------

#: A newsletter with no colour information at all: `background_is_light`
#: reads it as white paper, which is the case that gets inverted.
LIGHT_MAIL = "<p>plain white newsletter</p>"


def _declarations(doc: str) -> dict[tuple[str, str, str], str]:
    """`{(media, selector, property): value}` for the frame's *own*
    stylesheet — the first `<style>` element, never the message's.

    A real CSS parse, descending into at-rules, because the two things
    worth asserting about a restyle are which selectors carry the filter
    and whether they sit inside a `prefers-color-scheme` query. A
    substring check answers neither, and would pass on a rule that had been
    moved into a media query that never matches.
    """
    out: dict[tuple[str, str, str], str] = {}

    def walk(rules: list, media: str) -> None:
        for rule in rules:
            if rule.type == "qualified-rule":
                selector = tinycss2.serialize(rule.prelude).strip()
                for decl in tinycss2.parse_blocks_contents(
                    rule.content, skip_comments=True, skip_whitespace=True
                ):
                    if decl.type == "declaration":
                        out[(media, selector, decl.lower_name)] = tinycss2.serialize(
                            decl.value
                        ).strip()
            elif rule.type == "at-rule" and rule.content is not None:
                walk(
                    tinycss2.parse_rule_list(
                        rule.content, skip_comments=True, skip_whitespace=True
                    ),
                    tinycss2.serialize(rule.prelude).strip(),
                )

    base = re.findall(r"<style>(.*?)</style>", doc, re.DOTALL)[0]
    walk(tinycss2.parse_stylesheet(base, skip_comments=True, skip_whitespace=True), "")
    return out


def _filters(doc: str) -> dict[tuple[str, str], str]:
    return {
        (media, selector): value
        for (media, selector, prop), value in _declarations(doc).items()
        if prop == "filter"
    }


def _scheme(doc: str) -> str:
    return _declarations(doc)[("", "html", "color-scheme")]


async def test_a_light_mail_is_inverted_for_a_dark_reader(authed, fake, set_prefs):
    """Both rules or neither: inverting `html` alone turns every photograph
    in the message into its own negative, so the counter-invert on `img` is
    part of the feature, not a refinement of it.
    """
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL)
    filters = _filters((await authed.get("/m/E1/html")).text)
    assert filters == {
        ("", "html"): "invert(1) hue-rotate(180deg)",
        ("", 'img,[style*="background-image"]'): "invert(1) hue-rotate(180deg)",
    }


async def test_a_system_theme_reader_gets_the_inversion_behind_a_media_query(
    authed, fake, set_prefs
):
    """The server cannot know how a "system" reader's OS resolves, and an
    unconditional invert would hand a system-*light* reader a photographic
    negative of every message.
    """
    await set_prefs(theme="system")
    fake.message("E1", html=LIGHT_MAIL)
    media = {media for media, _ in _filters((await authed.get("/m/E1/html")).text)}
    assert media == {"(prefers-color-scheme: dark)"}


async def test_a_light_theme_reader_is_never_handed_an_inverted_message(authed, fake, set_prefs):
    await set_prefs(theme="light")
    fake.message("E1", html=LIGHT_MAIL)
    assert _filters((await authed.get("/m/E1/html")).text) == {}


async def test_a_mail_declaring_its_own_scheme_is_trusted_and_left_alone(authed, fake, set_prefs):
    """`<meta name="color-scheme">` is in nh3's `CLEAN_CONTENT_TAGS`, so by
    sanitisation time the evidence is gone. Reading it off the *raw* body
    is the whole of this case: get that wrong and a mail that already knows
    how to be dark is inverted into a mess.
    """
    await set_prefs(theme="dark")
    fake.message("E1", html='<meta name="color-scheme" content="dark light"><p>hi</p>')
    doc = (await authed.get("/m/E1/html")).text
    assert _filters(doc) == {}
    assert _scheme(doc) == "light dark"


async def test_a_scheme_declared_in_the_mails_own_css_is_trusted_too(authed, fake, set_prefs):
    """`color-scheme` is not in `css_sanitize.ALLOWED_PROPERTIES`, so this
    evidence only survives in the raw `<style>` blocks either.
    """
    await set_prefs(theme="dark")
    fake.message("E1", html="<style>:root{color-scheme:light dark}</style><p>hi</p>")
    doc = (await authed.get("/m/E1/html")).text
    assert _filters(doc) == {}
    assert _scheme(doc) == "light dark"


async def test_an_already_dark_mail_is_left_alone(authed, fake, set_prefs):
    """Inverting a dark mail turns it light — exactly backwards."""
    await set_prefs(theme="dark")
    fake.message("E1", html='<body bgcolor="#0d1015"><p>dark newsletter</p></body>')
    assert _filters((await authed.get("/m/E1/html")).text) == {}


async def test_the_background_is_read_from_the_sanitised_stylesheet(authed, fake, set_prefs):
    """`background-color` *does* survive `css_sanitize`, so the colour this
    reads is the colour the reader actually sees."""
    await set_prefs(theme="dark")
    fake.message("E1", html="<style>body{background-color:#0d1015}</style><p>x</p>")
    assert _filters((await authed.get("/m/E1/html")).text) == {}
    fake.message("E2", html="<style>body{background-color:#ffffff}</style><p>x</p>")
    assert len(_filters((await authed.get("/m/E2/html")).text)) == 2


async def test_the_appearance_pref_turns_the_whole_feature_off(authed, fake, set_prefs):
    await set_prefs(theme="dark", dark_restyle=False)
    fake.message("E1", html=LIGHT_MAIL)
    assert _filters((await authed.get("/m/E1/html")).text) == {}


async def test_restyle_0_in_the_url_turns_the_inversion_off(authed, fake, set_prefs):
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL)
    assert len(_filters((await authed.get("/m/E1/html")).text)) == 2
    assert _filters((await authed.get("/m/E1/html?restyle=0")).text) == {}


async def test_the_theme_query_parameter_overrides_the_stored_pref(authed, fake, set_prefs):
    """Task 13's print view renders a message at a theme of its choosing;
    absent the parameter the reader's own stored theme is what counts, not
    a hardcoded default.
    """
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL)

    # Asserted on the outcome, not on the `color-scheme` string. A light
    # mail read at a dark theme is *inverted*, and inversion renders from a
    # light base on purpose -- pinning `color-scheme` to the reader's theme
    # there cancels the two out and the message renders as an empty box.
    # So "the stored theme counts" shows up as the inversion being applied,
    # and `?theme=light` as it not being.
    stored = await authed.get("/m/E1/html")
    assert len(_filters(stored.text)) == 2

    overridden = await authed.get("/m/E1/html?theme=light")
    assert _scheme(overridden.text) == "light"
    assert _filters(overridden.text) == {}


async def test_show_original_is_remembered_per_sender(authed, fake, csrf, set_prefs):
    """The point of the override: it outlives the message it was clicked
    on, and it is scoped to the sender it was clicked for.
    """
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL, sender="news@t.test")
    posted = await authed.post("/m/E1/restyle", data={"sender": "news@t.test"}, headers=csrf)
    assert posted.status_code == 200

    fake.message("E2", html=LIGHT_MAIL, sender="news@t.test")
    assert _filters((await authed.get("/m/E2/html")).text) == {}
    frag = (await authed.get("/m/E2/frame?thread=T1")).text
    assert parse_attrs(frag)["iframe"][0]["src"] == "/m/E2/html?remote=0&restyle=0"

    fake.message("E3", html=LIGHT_MAIL, sender="other@t.test")
    assert len(_filters((await authed.get("/m/E3/html")).text)) == 2
    other = (await authed.get("/m/E3/frame?thread=T1")).text
    assert parse_attrs(other)["iframe"][0]["src"] == "/m/E3/html?remote=0"


async def test_the_override_is_matched_case_insensitively(authed, fake, csrf, set_prefs):
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL, sender="News@T.Test")
    assert (
        await authed.post("/m/E1/restyle", data={"sender": "news@t.test"}, headers=csrf)
    ).status_code == 200
    fake.message("E2", html=LIGHT_MAIL, sender="NEWS@t.test")
    assert _filters((await authed.get("/m/E2/html")).text) == {}


async def test_the_restyle_route_answers_with_a_frame_that_asks_for_no_restyle(
    authed, fake, csrf, set_prefs
):
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL, sender="news@t.test")
    r = await authed.post(
        "/m/E1/restyle", data={"sender": "news@t.test", "remote": "1"}, headers=csrf
    )
    iframes = parse_attrs(r.text)["iframe"]
    assert len(iframes) == 1
    assert iframes[0]["src"] == "/m/E1/html?remote=1&restyle=0"
    assert "allow-same-origin" not in iframes[0]["sandbox"].split()
    assert parse_attrs(r.text)["div"][0]["data-thread"] == "T1"


async def test_restyle_route_rejects_a_sender_that_is_not_this_messages_own(authed, fake, csrf):
    """The address is a form field, and it becomes a durable per-sender
    row. Without this check one message would be a write primitive for
    every sender in the reader's mail.
    """
    fake.message("E1", html=LIGHT_MAIL, sender="news@t.test")
    r = await authed.post("/m/E1/restyle", data={"sender": "x@evil.test"}, headers=csrf)
    assert r.status_code == 403


async def test_restyle_route_needs_a_csrf_token(authed, fake):
    fake.message("E1", html=LIGHT_MAIL, sender="news@t.test")
    r = await authed.post("/m/E1/restyle", data={"sender": "news@t.test"})
    assert r.status_code == 403


async def test_restyle_route_writes_nothing_when_the_sender_does_not_match(
    running, authed, fake, csrf, set_prefs
):
    """A rejected post must leave no row behind — a 403 that still recorded
    the override would be the bug the 403 exists to prevent.
    """
    from mailosh.db.models import SenderPref

    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL, sender="news@t.test")
    await authed.post("/m/E1/restyle", data={"sender": "x@evil.test"}, headers=csrf)
    async with running.state.sessionmaker() as db:
        assert (await db.execute(select(SenderPref))).scalars().all() == []


async def test_restyle_route_requires_a_session(app_client, fake):
    fake.message("E1", html=LIGHT_MAIL, sender="news@t.test")
    r = await app_client.post("/m/E1/restyle", data={"sender": "news@t.test"})
    assert r.status_code in (303, 401, 403)


async def test_restyle_route_404s_for_a_message_this_account_does_not_have(authed, fake, csrf):
    r = await authed.post("/m/nosuch/restyle", data={"sender": "news@t.test"}, headers=csrf)
    assert r.status_code == 404


async def test_a_restyled_frame_still_carries_the_unchanged_csp(authed, fake, set_prefs):
    """Nothing about a restyle decision may widen the policy: the header is
    the same string for an inverted message as for an untouched one.
    """
    await set_prefs(theme="dark")
    fake.message("E1", html=LIGHT_MAIL)
    inverted = await authed.get("/m/E1/html")
    plain = await authed.get("/m/E1/html?restyle=0")
    assert inverted.headers["content-security-policy"] == csp_header()
    assert plain.headers["content-security-policy"] == inverted.headers["content-security-policy"]
