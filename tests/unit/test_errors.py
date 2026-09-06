"""HTTP-level tests for Task 13's backend half: the global error surface
(design spec §9's last bullet, §6.3/§13's error taxonomy) and the two
routers that were deliberately left unmounted until this task
(`mailosh.web.palette`, `mailosh.web.prefs`).

Covers:

- `mailosh.web.palette`/`mailosh.web.prefs` are reachable through the
  *real* `create_app` — behind the same session/CSRF chain every other
  route uses — not just the bare `FastAPI()` each router's own test module
  (`test_palette.py`, `test_prefs.py`) mounts it on today.
- `JmapError`/`TransportError` never reach a caller as a bare 500: `200` +
  `HX-Reswap: none` + the exact `om:error` trigger for an HTMX request, a
  502/500 Retry page otherwise — rendered from
  `fragments/error_page.html`, one template for both statuses, with the
  URL it offers to retry escaped by Jinja rather than by hand.
- Design spec §5.4's offline banner: present in the served shell, hidden,
  and bound to the `$store.ui.offline` flag `static/js/sse.js` sets — and
  absent from the fragment layout, so a swap can never leave two of them
  in the document.
- `RequestValidationError` -> 422, plus the same toast trigger for HTMX,
  with FastAPI's own JSON body preserved underneath it.
- The already-existing `SessionRequired` -> login-redirect shapes (401 +
  `HX-Redirect` for HTMX, 303 otherwise) — asserted here, not reimplemented
  (this task's own brief: "already exists from the login work").
- The security-headers middleware: present with their exact spec values on
  both an authenticated page and the (unauthenticated) login page, and
  structured (`setdefault`, not an unconditional overwrite) so a more
  specific route — the `/m/*` message frames, 1B — can override the CSP
  without this middleware itself changing.

Everything runs against a real `create_app` (real routers, real session/
CSRF/DB plumbing over a file-backed aiosqlite db), the same shape
`tests/unit/test_mail_routes.py`/`test_auth_routes.py` already use:
`mailosh.web.auth.verify_password` and `deps.client_for` are the only two
fakes. `FakeClient.raises`, once armed, makes its `get_mailboxes` call
raise whatever it's given — the first JMAP call any authenticated page in
this app makes (`mailosh.web.mail._nav_for` -> `build_nav` ->
`get_mailboxes`) — so arming it after login is enough to exercise the
global handlers from a real route, with no route needing to know anything
about this test suite.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import make_settings
from fastapi.responses import Response
from fastapi.testclient import TestClient

from mailosh.jmap.client import QueryPage
from mailosh.jmap.errors import JmapError, TransportError
from mailosh.jmap.models import Mailbox
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps
from mailosh.web.app import (
    _JMAP_ERROR_TOAST,
    _STALE_CSRF_TOAST,
    _VALIDATION_TOAST,
    create_app,
)

ME = "d@x"

#: Design spec §9's last bullet, copied verbatim (not re-imported from
#: `mailosh.web.app`) so this test proves the header the *spec* mandates is
#: actually sent, rather than merely agreeing with whatever the module
#: happens to define.
_EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; frame-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


class FakeAdmin:
    """`StalwartAdmin` stand-in — see `tests/unit/test_auth_routes.py`."""

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        return ApiKey(id="k1", secret="API_secret_1")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None


class FakeClient:
    """Stands in for the pooled `JmapClient`. `get_mailboxes` serves six
    role mailboxes — enough for `build_nav` to render a real nav, nothing
    this module's tests read past "the page rendered" — unless `raises`
    has been armed, in which case it raises that instead of returning.
    Every test below logs in (and, where needed, extracts a CSRF token)
    *before* arming this, so one instance covers both the success path
    (login, CSRF, the security-header assertions) and the failure path.
    """

    def __init__(self) -> None:
        self.raises: Exception | None = None

    @property
    def account_id(self) -> str:
        return "acct-1"

    async def get_mailboxes(self) -> list[Mailbox]:
        if self.raises is not None:
            raise self.raises
        return [
            Mailbox(
                id="mb-inbox",
                name="Inbox",
                role="inbox",
                sort_order=10,
                total_emails=0,
                unread_emails=0,
            ),
            Mailbox(
                id="mb-sent",
                name="Sent",
                role="sent",
                sort_order=20,
                total_emails=0,
                unread_emails=0,
            ),
            Mailbox(
                id="mb-drafts",
                name="Drafts",
                role="drafts",
                sort_order=30,
                total_emails=0,
                unread_emails=0,
            ),
            Mailbox(
                id="mb-archive",
                name="Archive",
                role="archive",
                sort_order=50,
                total_emails=0,
                unread_emails=0,
            ),
            Mailbox(
                id="mb-junk",
                name="Spam",
                role="junk",
                sort_order=60,
                total_emails=0,
                unread_emails=0,
            ),
            Mailbox(
                id="mb-trash",
                name="Trash",
                role="trash",
                sort_order=70,
                total_emails=0,
                unread_emails=0,
            ),
        ]

    async def query_page(self, **kwargs: object) -> QueryPage:
        return QueryPage(
            thread_order=[], total=0, emails_by_thread={}, position=int(kwargs["position"])
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def app(monkeypatch, sqlite_url, fake):
    """A real `create_app` with its lifespan running — see
    `tests/unit/test_auth_routes.py::app` for why the throwaway `with
    TestClient(...)` is what actually drives startup.
    """

    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = lambda: fake
    with TestClient(application):
        yield application


def _login(app) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    r = client.post("/login", data={"username": ME, "password": "right"})
    assert r.status_code == 303, r.text
    return client


def _extract_csrf(text: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]*)"', text)
    assert m, f"csrf meta tag not found in: {text[:200]!r}"
    return m.group(1)


def _csrf_for(client: TestClient) -> str:
    """The current session's own CSRF token, read off the meta tag a full
    page (never an `HX-Request` one — that answers the chrome-less
    fragment) renders. Same helper `test_auth_routes.py` uses.
    """
    page = client.get("/mail/inbox")
    return _extract_csrf(page.text)


# ---------------------------------------------------------------------------
# Job 1: palette/prefs are reachable through the real app
# ---------------------------------------------------------------------------


def test_palette_index_is_reachable_through_the_real_app_and_gated_like_any_route(app):
    anon = TestClient(app, follow_redirects=False)
    full = anon.get("/palette/index")
    assert full.status_code == 303
    assert full.headers["location"].startswith("/login")
    hx = anon.get("/palette/index", headers={"HX-Request": "true"})
    assert hx.status_code == 401
    assert hx.headers["HX-Redirect"].startswith("/login")

    r = _login(app).get("/palette/index")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"actions", "goto", "labels", "settings"}
    assert {item["id"] for item in body["goto"]} >= {"goto:inbox"}


def test_prefs_post_is_reachable_through_the_real_app_and_persists(app):
    anon = TestClient(app, follow_redirects=False)
    assert anon.post("/prefs", data={"theme": "dark"}).status_code == 303

    client = _login(app)
    token = _csrf_for(client)

    r = client.post("/prefs", data={"theme": "dark"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 204
    assert json.loads(r.headers["HX-Trigger"]) == {"om:prefs": {"theme": "dark"}}

    # Persisted for real, not just acknowledged: the next page load reads it
    # straight back out of the database via `deps.prefs_for`, the same
    # dependency every other page in this app renders `data-theme` from.
    page = client.get("/mail/inbox")
    assert 'data-theme="dark"' in page.text


def test_prefs_post_without_csrf_token_is_403_through_the_real_app(app):
    client = _login(app)
    r = client.post("/prefs", data={"theme": "dark"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Job 2: JmapError/TransportError never reach a caller as a bare 500
# ---------------------------------------------------------------------------


def test_transport_error_hx_request_gets_200_reswap_none_and_the_exact_om_error_trigger(app, fake):
    client = _login(app)
    fake.raises = TransportError("connection refused")

    r = client.get("/mail/inbox", headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert r.headers["HX-Reswap"] == "none"
    assert json.loads(r.headers["HX-Trigger"]) == {
        "om:error": {
            "toast": "Couldn't reach the mail server. Try again.",
            "retry": True,
        }
    }
    # The brief's own exact string, not just this module's idea of it.
    assert _JMAP_ERROR_TOAST == "Couldn't reach the mail server. Try again."


@pytest.mark.parametrize("exc", [TransportError("down"), JmapError("bad")])
def test_a_failed_swap_never_rewrites_the_address_bar(app, fake, exc):
    """The `200` that keeps htmx from treating this as a load failure also
    makes it a *success* as far as history is concerned: a boosted link's
    URL is pushed on any successful response. So a failed click on Starred
    left the inbox on screen with `/mail/starred` in the address bar — a
    URL naming a mailbox that was never rendered, which a reload or a Back
    would then act on. `HX-Reswap: none` and `HX-Push-Url: false` are the
    same statement about the same response: nothing was swapped, so
    nothing is pushed.
    """
    client = _login(app)
    fake.raises = exc

    r = client.get("/mail/starred", headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert r.headers["HX-Reswap"] == "none"
    assert r.headers["HX-Push-Url"] == "false"


def test_transport_error_full_page_gets_a_502_retry_page(app, fake):
    client = _login(app)
    fake.raises = TransportError("connection refused")

    r = client.get("/mail/inbox")

    assert r.status_code == 502
    assert r.headers["content-type"].startswith("text/html")
    assert "Retry" in r.text
    assert "<a href=" in r.text


def test_jmap_error_hx_request_gets_the_same_contract_as_transport_error(app, fake):
    client = _login(app)
    fake.raises = JmapError("unknownMethod")

    r = client.get("/mail/inbox", headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert r.headers["HX-Reswap"] == "none"
    assert json.loads(r.headers["HX-Trigger"]) == {
        "om:error": {"toast": "Couldn't reach the mail server. Try again.", "retry": True}
    }


def test_the_error_page_is_a_rendered_template_whose_retry_link_comes_back_here(app, fake):
    """`_error_page` renders `fragments/error_page.html` rather than
    concatenating markup in Python. Two things that buys, both asserted
    here rather than by looking for the template's name in the source: a
    real, styled document (the reader is looking at a page, not a wall of
    unstyled text), and Jinja's autoescaping on the one piece of the page
    that is not a constant — the URL it offers to retry.
    """
    client = _login(app)
    fake.raises = TransportError("down")

    r = client.get("/mail/inbox?after=%22%3E%3Cscript%3E")

    assert r.status_code == 502
    # A whole document, and a styled one: the served stylesheet is what
    # separates this from the bare string it replaced.
    assert r.text.lstrip().startswith("<!doctype html>")
    assert re.search(r'<link rel="stylesheet" href="/static/app\.css\?v=[0-9a-f]{8}">', r.text)

    # The Retry link is this same request's own address, escaped into the
    # attribute rather than closing it.
    href = re.search(r'<a href="([^"]*)"', r.text)
    assert href is not None
    assert href.group(1).endswith("/mail/inbox?after=%22%3E%3Cscript%3E")
    assert "<script>" not in r.text

    # And it carries no theme of its own: an exception handler runs outside
    # every route's dependencies, so there is no session and no `prefs` to
    # read one from. An unset `data-theme` is exactly "follow the OS" in
    # styles/input.css, which is the right answer rather than a guess.
    assert "data-theme" not in r.text


def test_the_error_page_renders_the_same_way_for_both_handlers(app, fake):
    """One template, two statuses. The 502/500 split is the *handler's*
    (`TransportError` never got a JMAP-shaped answer; a plain `JmapError`
    got an error one), and nothing about it reaches the page — so the two
    bodies are byte-identical for the same URL.
    """
    client = _login(app)

    fake.raises = TransportError("down")
    transport = client.get("/mail/inbox")
    fake.raises = JmapError("unknownMethod")
    jmap = client.get("/mail/inbox")

    assert (transport.status_code, jmap.status_code) == (502, 500)
    assert transport.text == jmap.text


def test_jmap_error_full_page_gets_500_not_502(app, fake):
    """`TransportError` (a request that never got a JMAP-shaped answer at
    all) reads as a `502`; a plain `JmapError` (one that *did* reach the
    server and *did* get an answer, just an error one — `MethodError` and
    friends) reads as a `500` instead. Same handler pair, different full
    page status, proven side by side so the two can't quietly collapse to
    one code.
    """
    client = _login(app)
    fake.raises = JmapError("invalidArguments")

    r = client.get("/mail/inbox")

    assert r.status_code == 500
    assert "Retry" in r.text


@pytest.mark.parametrize(
    ("exc", "full_page_status"),
    [(TransportError("down"), 502), (JmapError("bad"), 500)],
)
def test_a_jmap_failure_never_answers_an_hx_request_with_a_bare_500(
    app, fake, exc, full_page_status
):
    client = _login(app)
    fake.raises = exc

    hx = client.get("/mail/inbox", headers={"HX-Request": "true"})
    assert hx.status_code == 200

    full = client.get("/mail/inbox")
    assert full.status_code == full_page_status


# ---------------------------------------------------------------------------
# The other half of the connection surface: spec §5.4's offline banner
# ---------------------------------------------------------------------------


def test_the_shell_renders_the_offline_banner_hidden_and_bound_to_the_store(app):
    """The banner spec §5.4 promises, and `sse.js`/`mailosh/sse.py` both
    describe, has to actually be in the served page — the flag behind it
    (`$store.ui.offline`) and its 120 s fallback polling have worked since
    live updates landed, but nothing rendered them.

    Asserted on the real response, not on the template file: an `{% include
    %}` in a layout nobody renders would satisfy the source but ship
    nothing. `hidden` on the served markup is what keeps the strip out of a
    page whose JS never boots.
    """
    page = _login(app).get("/mail/inbox").text

    assert page.count('id="offline"') == 1
    banner = re.search(r'<div[^>]*id="offline"[^>]*>', page)
    assert banner is not None
    assert re.search(r"\shidden[\s>]", banner.group(0))
    # An Alpine root: a standalone directive on an element with no
    # `x-data`/`x-init` above it is never evaluated at all.
    assert re.search(r"\sx-data[\s>=]", banner.group(0))
    assert "$store.ui.offline" in banner.group(0)
    assert "Reconnecting to your mailbox" in page


def test_the_fragment_swap_never_ships_a_second_offline_banner(app):
    """`layouts/fragment.html` answers every in-place navigation. A banner
    in there would put a second `id="offline"` into the document on the
    first mailbox switch, and Alpine would drive whichever came first.
    """
    page = _login(app).get("/mail/inbox", headers={"HX-Request": "true"}).text

    assert 'id="offline"' not in page


# ---------------------------------------------------------------------------
# SessionRequired -> login redirect: already implemented (Task 5); asserted
# here per this task's own brief, not reimplemented.
# ---------------------------------------------------------------------------


def test_expired_session_redirect_shapes_are_unchanged(app):
    anon = TestClient(app, follow_redirects=False)

    full = anon.get("/mail/inbox")
    assert full.status_code == 303
    assert full.headers["location"].startswith("/login")

    hx = anon.get("/mail/inbox", headers={"HX-Request": "true"})
    assert hx.status_code == 401
    assert hx.headers["HX-Redirect"].startswith("/login")


# ---------------------------------------------------------------------------
# RequestValidationError -> 422, plus a toast for HTMX
# ---------------------------------------------------------------------------


def test_invalid_prefs_value_is_422_with_a_toast_for_hx(app):
    client = _login(app)
    token = _csrf_for(client)

    r = client.post(
        "/prefs",
        data={"theme": "blue"},
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
    )

    assert r.status_code == 422
    assert json.loads(r.headers["HX-Trigger"]) == {
        "om:error": {"toast": _VALIDATION_TOAST, "retry": False}
    }
    # FastAPI's own body shape survives underneath the toast trigger.
    locations = {tuple(err["loc"]) for err in r.json()["detail"]}
    assert ("body", "theme") in locations


def test_invalid_prefs_value_without_hx_gets_plain_422_and_no_trigger_header(app):
    client = _login(app)
    token = _csrf_for(client)

    r = client.post("/prefs", data={"theme": "blue"}, headers={"X-CSRF-Token": token})

    assert r.status_code == 422
    assert "HX-Trigger" not in r.headers
    locations = {tuple(err["loc"]) for err in r.json()["detail"]}
    assert ("body", "theme") in locations


# ---------------------------------------------------------------------------
# Job 3: security headers, present with their exact spec values, and
# structured so a later, more specific route can override the CSP.
# ---------------------------------------------------------------------------


def _assert_security_headers(response) -> None:
    assert response.headers["Content-Security-Policy"] == _EXPECTED_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    # The load-bearing constraint the brief names explicitly: this is a
    # mail client rendering attacker-controlled HTML, and the Alpine CSP
    # build exists specifically so this can hold.
    assert "unsafe-eval" not in response.headers["Content-Security-Policy"]


def test_security_headers_present_on_an_authenticated_page(app):
    _assert_security_headers(_login(app).get("/mail/inbox"))


def test_security_headers_present_on_the_login_page(app):
    _assert_security_headers(TestClient(app).get("/login"))


def test_security_headers_still_present_on_a_jmap_error_response(app, fake):
    client = _login(app)
    fake.raises = TransportError("down")

    _assert_security_headers(client.get("/mail/inbox"))
    _assert_security_headers(client.get("/mail/inbox", headers={"HX-Request": "true"}))


def test_setdefault_lets_a_more_specific_route_override_the_csp(app):
    """The `/m/*` message-frame routes (1B) will need a stricter CSP than
    the app's own. The middleware already applies every header with
    `response.headers.setdefault`, not an unconditional overwrite — proving
    that here, on a throwaway route added straight to this fixture's app,
    means it doesn't have to wait for 1B to exist to prove the contract:
    a route that sets its own `Content-Security-Policy` keeps it, and the
    other two headers still arrive as usual right alongside it.
    """

    @app.get("/__test_only_csp_override")
    def _frame_stub():
        return Response(content="ok", headers={"Content-Security-Policy": "sandbox"})

    r = TestClient(app).get("/__test_only_csp_override")

    assert r.headers["Content-Security-Policy"] == "sandbox"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"


# ---------------------------------------------------------------------------
# Where `next` points after a session expires under htmx
# ---------------------------------------------------------------------------


def test_an_expired_fragment_poll_sends_the_reader_back_to_the_page_not_the_fragment(app):
    """`next` comes from the address bar, not from the request that 401'd.

    Every htmx request has a route of its own and `deps._next_url` reads
    `request.url.path`, so when a session expired, `sse.js`'s 120 s poll
    refetched `/mail/inbox/rows` and the login page inherited
    `next=/mail/inbox/rows?...`. Signing in then rendered a bare row
    fragment with no shell around it -- an app that looks broken at the
    exact moment the user has just proved who they are.
    """
    anon = TestClient(app, follow_redirects=False)

    r = anon.get(
        "/mail/inbox/rows?position=0&limit=50",
        headers={"HX-Request": "true", "HX-Current-URL": "http://localhost:8000/mail/inbox"},
    )

    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/login?next=%2Fmail%2Finbox"
    assert "rows" not in r.headers["HX-Redirect"]


def test_the_fragment_path_is_still_used_when_htmx_sends_no_current_url(app):
    """A fallback, not a hard dependency: without the header the previous
    behaviour stands rather than `next` being lost altogether."""
    anon = TestClient(app, follow_redirects=False)

    r = anon.get("/mail/inbox/rows?position=0&limit=50", headers={"HX-Request": "true"})

    assert r.status_code == 401
    assert r.headers["HX-Redirect"].startswith("/login?next=")


def test_a_hostile_current_url_cannot_become_an_open_redirect(app):
    """`HX-Current-URL` is client-supplied, so only its path and query may
    be used -- never its scheme or host."""
    anon = TestClient(app, follow_redirects=False)

    r = anon.get(
        "/mail/inbox/rows",
        headers={
            "HX-Request": "true",
            "HX-Current-URL": "https://evil.example.com/mail/inbox",
        },
    )

    assert r.headers["HX-Redirect"] == "/login?next=%2Fmail%2Finbox"
    assert "evil.example.com" not in r.headers["HX-Redirect"]


# ---------------------------------------------------------------------------
# A live session holding another session's CSRF token
#
# Signing out and back in mints a new session with a new token. Any tab still
# showing the old page keeps sending the old one, and every mutation from it
# answers 403 while the session cookie is perfectly valid — so nothing
# redirects to login and nothing explains itself. The reader sees a Send
# button that does nothing at all. Reported from a real session; reproduced
# by signing out in one client and posting from another.
# ---------------------------------------------------------------------------


def test_a_stale_csrf_token_tells_the_reader_what_happened(app):
    """The `om:error` contract, not a bare 403.

    htmx treats a 4xx as a load failure: no swap, and nothing a listener can
    turn into a toast. The whole reason `_error_toast` answers 200 is so the
    reader is told something.
    """
    client = _login(app)
    token = _extract_csrf(client.get("/mail/inbox").text)

    stale = "x" * len(token)
    assert stale != token
    r = client.post(
        "/a/read",
        data={"ids": "E1", "on": "1"},
        headers={"X-CSRF-Token": stale, "HX-Request": "true"},
    )
    assert r.status_code == 200
    assert r.headers.get("hx-reswap") == "none"
    payload = json.loads(r.headers["hx-trigger"])
    assert payload["om:error"]["toast"] == _STALE_CSRF_TOAST
    # Not worth retrying: the token will be just as wrong next time.
    assert payload["om:error"]["retry"] is False


def test_the_toast_does_not_tell_them_to_reload_first(app):
    """Reloading fixes the token and destroys whatever is being written --
    and compose autosave is failing for the same reason, so the DOM holds
    the only copy. The copy has to rescue the text before the fix."""
    assert "reload" in _STALE_CSRF_TOAST.lower()
    before = _STALE_CSRF_TOAST.lower().index("copy")
    assert before < _STALE_CSRF_TOAST.lower().index("reload")


def test_a_non_htmx_caller_still_gets_a_real_403(app):
    """An API client or a test asserting the status directly must not be
    handed a 200 because a browser needed a toast."""
    client = _login(app)
    r = client.post("/a/read", data={"ids": "E1", "on": "1"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


def test_other_http_errors_keep_fastapis_own_behaviour(app):
    """The handler translates 403 and re-raises everything else. A 404 must
    not come back as a 200 with a toast about signing out."""
    client = _login(app)
    r = client.get("/mail/no-such-mailbox", headers={"HX-Request": "true"})
    assert r.status_code in (303, 404), r.status_code
    assert "om:error" not in r.headers.get("hx-trigger", "")
