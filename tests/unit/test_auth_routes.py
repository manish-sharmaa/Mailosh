"""HTTP-level tests for login/logout/session middleware (Task 5, design spec
§9): the full password -> per-user Stalwart API key -> DB session -> cookie
lifecycle, indistinguishable failure copy, rate limiting, CSRF-protected
mutations, and the "one Stalwart API key per user, not per session"
controller ruling (#1) -- Task 4's live finding that Stalwart caps API keys
at 5 per account.

Every test builds a fresh `create_app(settings=make_settings(sqlite_url))`
(a real app, real Postgres-shaped tables via `Base.metadata.create_all` on a
file-backed aiosqlite db — see `tests/conftest.py`) and replaces
`app.state.admin` with `FakeAdmin` — no real Stalwart/network call, ever.
`mailosh.web.auth.verify_password` is monkeypatched per test/fixture rather
than hitting a real server; `tests/integration/test_live_auth_flow.py`
already proves the real Stalwart exchange works end to end.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from sqlalchemy import select

from mailosh.db.models import AppUser, AuditLog
from mailosh.jmap.client import QueryPage
from mailosh.jmap.errors import TransportError
from mailosh.jmap.models import Mailbox
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps
from mailosh.web.app import create_app
from mailosh.web.auth import _safe_next


class FakeAdmin:
    """Stands in for `StalwartAdmin`. `destroy_api_key` takes `(username,
    key_id)` -- the real, live-tested two-argument shape `StalwartAdmin`
    actually has (Task 4's live finding: `x:ApiKey` is a per-account
    resource, so a destroy call needs to know which account's namespace to
    search — see `docs/spikes/p1a-findings.md`, "Auth
    exchange", point 3) — not the task brief's own stub, which still shows
    the plan's superseded one-argument signature and would TypeError
    against the real call site in `mailosh.web.auth`.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.destroyed: list[tuple[str, str]] = []
        self._minted = 0

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        self._minted += 1
        self.created.append(username)
        return ApiKey(id=f"k{self._minted}", secret=f"API_secret_{self._minted}")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        self.destroyed.append((username, key_id))


class FakeMailClient:
    """The bare minimum `mailosh.web.mail` needs from a `JmapClient` — an
    empty inbox.

    Task 8 replaced the `/mail/{key}` placeholder these tests used as "any
    authenticated page" with the real list view, which reaches for the
    session's pooled JMAP client. Overriding `deps.client_for` with this keeps
    that page renderable (it is still only ever used here to read the CSRF
    meta tag off an authenticated response) without a single network call —
    the list's own behaviour is `tests/unit/test_mail_routes.py`'s subject.
    """

    account_id = "acc1"

    async def get_mailboxes(self) -> list[Mailbox]:
        return [
            Mailbox(
                id="mb-inbox",
                name="Inbox",
                role="inbox",
                sort_order=10,
                total_emails=0,
                unread_emails=0,
            )
        ]

    async def query_page(self, **kwargs) -> QueryPage:
        return QueryPage(
            thread_order=[], total=0, emails_by_thread={}, position=int(kwargs["position"])
        )


@pytest.fixture
def app(monkeypatch, sqlite_url):
    """A `create_app` with its lifespan actually running.

    `Starlette`'s `TestClient` only runs an app's lifespan (which is where
    `create_app` builds `app.state.settings`/`sessionmaker`/`pool` — every
    route in this file needs at least one of those) while a `TestClient`
    instance is open as a context manager — a bare, never-`with`-entered
    `TestClient(app).get(...)` still makes a real request (it spins up its
    own throwaway portal per call) but never triggers startup, leaving
    `app.state` half-built. This fixture enters exactly one throwaway
    client for the whole test's duration purely to drive that startup/
    shutdown — every test below builds its own separate, un-entered
    `TestClient(app, ...)` instances against the same `app` object instead
    (e.g. two independent cookie jars for a "two devices, one account"
    test) — those share the one already-populated `app.state` (`request.
    app` is the same singleton object no matter which `TestClient`
    connection made the request), without each re-running the lifespan.
    """

    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = FakeMailClient
    with TestClient(application):
        yield application


def _login(client: TestClient, *, username: str = "d@x") -> None:
    r = client.post("/login", data={"username": username, "password": "right"})
    assert r.status_code == 303, r.text


def _extract_csrf(text: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]*)"', text)
    assert m, f"csrf meta tag not found in: {text[:200]!r}"
    return m.group(1)


def _csrf_for(client: TestClient) -> str:
    """The current session's own CSRF token, read off the meta tag every
    authenticated *full page* renders (here, the inbox). Deliberately not an
    `HX-Request`: since Task 8 that answers with the `#main` fragment, which
    has no `<head>` and therefore no meta tag.
    """
    page = client.get("/mail/inbox")
    return _extract_csrf(page.text)


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


def test_login_page_renders(app):
    r = TestClient(app).get("/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert 'name="username"' in r.text


# ---------------------------------------------------------------------------
# POST /login: success
# ---------------------------------------------------------------------------


def test_login_success_sets_cookie_and_redirects(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": "d@x", "password": "right", "next": "/mail/inbox"})
    assert r.status_code == 303
    assert r.headers["location"] == "/mail/inbox"
    assert "sid=" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"]
    assert app.state.admin.created == ["d@x"]


def test_login_defaults_next_to_the_inbox(app):
    # Not "/": that is only a redirect *to* the inbox, and relying on the
    # indirection is what left every default login on a bare 404 before the
    # `/` route existed (Task 8).
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": "d@x", "password": "right"})
    assert r.headers["location"] == "/mail/inbox"


def test_login_rejects_unsafe_next_as_open_redirect_guard(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post(
        "/login", data={"username": "d@x", "password": "right", "next": "https://evil.example/"}
    )
    assert r.headers["location"] == "/mail/inbox"
    c2 = TestClient(app, follow_redirects=False)
    r2 = c2.post("/login", data={"username": "d2@x", "password": "right", "next": "//evil.example"})
    assert r2.headers["location"] == "/mail/inbox"


# ---------------------------------------------------------------------------
# _safe_next: fix round 1, Finding 3 -- a backslash (or mixed-slash) variant
# of the `//host` trick bypassed the original guard, which only ever matched
# a literal `//` prefix. A browser's URL parser treats a leading backslash
# the same as a forward slash when resolving the authority component (e.g.
# `new URL("/\\evil.example/", "https://mailosh.example/").host` is
# `"evil.example"`), so `/\evil`/`/\\evil` are exactly as dangerous as
# `//evil` and must be rejected the same way -- it was inert only because
# `RedirectResponse` percent-encodes a literal backslash in the `Location`
# header, not because the guard itself was correct on its own terms.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("next_value", "expected"),
    [
        ("//evil", "/mail/inbox"),
        ("/\\evil", "/mail/inbox"),
        ("/\\\\evil", "/mail/inbox"),
        ("https://evil", "/mail/inbox"),
        ("/mail/inbox", "/mail/inbox"),
        ("/t/thread-1", "/t/thread-1"),
    ],
)
def test_safe_next_rejects_every_open_redirect_shape(next_value, expected):
    assert _safe_next(next_value) == expected


def test_login_rejects_cross_site_post(app):
    # "Login CSRF": a cross-site page auto-submitting a form to /login with
    # attacker-controlled credentials, trying to force a victim's browser
    # into an attacker-owned session, must be rejected outright regardless
    # of whether the credentials happen to be valid — login has no CSRF
    # token to check (no session exists yet), so this Fetch Metadata check
    # is the only thing standing in the way (module docstring / design
    # spec §9).
    c = TestClient(app, follow_redirects=False)
    r = c.post(
        "/login",
        data={"username": "d@x", "password": "right"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert app.state.admin.created == []  # never even reached credential verification


def test_remember_me_sets_persistent_cookie_max_age(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": "d@x", "password": "right", "remember": "1"})
    assert "Max-Age=" in r.headers["set-cookie"]


def test_without_remember_cookie_has_no_max_age(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": "d@x", "password": "right"})
    assert "Max-Age=" not in r.headers["set-cookie"]


async def test_login_ok_is_audited(app):
    c = TestClient(app)
    c.post("/login", data={"username": "d@x", "password": "right"})
    async with app.state.sessionmaker() as db:
        result = await db.execute(select(AuditLog).where(AuditLog.action == "login.ok"))
        assert result.scalars().first() is not None


async def test_ip_ignores_x_forwarded_for_by_default(monkeypatch, sqlite_url):
    # trust_proxy defaults to False -- a spoofable client-supplied header
    # must never override request.client.host unless explicitly trusted.
    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = FakeMailClient
    with TestClient(application) as c:
        c.post(
            "/login",
            data={"username": "d@x", "password": "right"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        async with application.state.sessionmaker() as db:
            result = await db.execute(select(AuditLog).where(AuditLog.action == "login.ok"))
            row = result.scalars().first()
            assert row is not None
            assert row.ip != "203.0.113.7"


async def test_ip_uses_x_forwarded_for_when_trust_proxy_enabled(monkeypatch, sqlite_url):
    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    application = create_app(settings=make_settings(sqlite_url, trust_proxy=True))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = FakeMailClient
    with TestClient(application) as c:
        c.post(
            "/login",
            data={"username": "d@x", "password": "right"},
            # Multiple hops: only the first (the real client, prepended by
            # the nearest trusted proxy) is used.
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
        )
        async with application.state.sessionmaker() as db:
            result = await db.execute(select(AuditLog).where(AuditLog.action == "login.ok"))
            row = result.scalars().first()
            assert row is not None
            assert row.ip == "203.0.113.7"


# ---------------------------------------------------------------------------
# POST /login: failures are indistinguishable, and rate-limited
# ---------------------------------------------------------------------------


def test_login_failure_is_generic_and_rate_limited(app):
    c = TestClient(app)
    for _ in range(5):
        r = c.post("/login", data={"username": "d@x", "password": "wrong"})
        assert r.status_code == 200
        assert "Wrong email or password" in r.text
    r = c.post("/login", data={"username": "d@x", "password": "right"})
    assert r.status_code == 429
    assert "Try again in" in r.text


def test_login_unknown_account_gets_identical_generic_message(app):
    # Same copy, same status code as a known account + wrong password (the
    # fake_verify fixture returns None for ANY password on an unrecognized
    # username, exactly like the real verify_password would) — and,
    # crucially, never creates an AppUser row or touches the admin client
    # for a username that was never actually verified.
    c = TestClient(app)
    r = c.post("/login", data={"username": "nobody@x", "password": "whatever"})
    assert r.status_code == 200
    assert "Wrong email or password" in r.text
    assert app.state.admin.created == []


async def test_login_fail_is_audited_without_creating_a_user(app):
    c = TestClient(app)
    c.post("/login", data={"username": "nobody@x", "password": "whatever"})
    async with app.state.sessionmaker() as db:
        result = await db.execute(select(AuditLog).where(AuditLog.action == "login.fail"))
        assert result.scalars().first() is not None
        users = await db.execute(select(AppUser))
        assert users.scalars().first() is None


def test_login_transport_error_renders_unreachable_message_not_generic(app, monkeypatch):
    async def raising_verify(url, u, p):
        raise TransportError("simulated: mail server unreachable", status_code=None)

    monkeypatch.setattr("mailosh.web.auth.verify_password", raising_verify)
    c = TestClient(app)
    r = c.post("/login", data={"username": "d@x", "password": "right"})
    assert r.status_code == 200
    assert "reach the mail server right now" in r.text
    assert "Wrong email or password" not in r.text


def test_login_transport_error_does_not_count_as_a_failed_attempt(app, monkeypatch):
    calls = {"n": 0}

    async def flaky_verify(url, u, p):
        calls["n"] += 1
        if calls["n"] <= 5:
            raise TransportError("simulated outage", status_code=None)
        return VerifiedAccount(u, "acc1", u)

    monkeypatch.setattr("mailosh.web.auth.verify_password", flaky_verify)
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        r = c.post("/login", data={"username": "d@x", "password": "right"})
        assert r.status_code == 200
        assert "reach the mail server right now" in r.text
    # A 6th attempt, now succeeding, must not be rate-limited by the 5
    # transport failures above — proves they were never recorded as
    # failures against the login limiter.
    r = c.post("/login", data={"username": "d@x", "password": "right"})
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# require_session: redirect shapes for full-page vs. HX requests
# ---------------------------------------------------------------------------


def test_protected_route_redirects_html_and_hx(app):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/mail/inbox").status_code == 303
    r = c.get("/mail/inbox", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["HX-Redirect"].startswith("/login")


def test_authenticated_request_reaches_the_mail_shell(app):
    c = TestClient(app, follow_redirects=False)
    _login(c)
    r = c.get("/mail/inbox")
    assert r.status_code == 200
    assert "<html" in r.text


# ---------------------------------------------------------------------------
# POST /logout, /logout/all: CSRF-gated, destroy-on-last-session (ruling #1)
# ---------------------------------------------------------------------------


def test_logout_destroys_key_and_clears_cookie(app):
    c = TestClient(app, follow_redirects=False)
    _login(c)
    token = _csrf_for(c)

    r = c.post("/logout", headers={"X-CSRF-Token": token})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert app.state.admin.destroyed == [("d@x", "k1")]
    # The cleared cookie is re-sent (Max-Age=0 / expired), not simply omitted.
    assert "sid=" in r.headers["set-cookie"]

    # And the session is genuinely gone: the (stale) cookie no longer works.
    assert c.get("/mail/inbox").status_code == 303


def test_logout_wrong_csrf_token_is_403_and_destroys_nothing(app):
    c = TestClient(app, follow_redirects=False)
    _login(c)
    r = c.post("/logout", headers={"X-CSRF-Token": "not-the-right-token"})
    assert r.status_code == 403
    assert app.state.admin.destroyed == []


def test_post_without_csrf_is_403(app):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "d@x", "password": "right"})
    assert c.post("/logout").status_code == 403


async def test_logout_is_audited(app):
    c = TestClient(app, follow_redirects=False)
    _login(c)
    token = _csrf_for(c)
    c.post("/logout", headers={"X-CSRF-Token": token})
    async with app.state.sessionmaker() as db:
        result = await db.execute(select(AuditLog).where(AuditLog.action == "logout"))
        assert result.scalars().first() is not None


def test_logout_keeps_key_alive_while_another_session_remains(app):
    # Two "devices" (two independent cookie jars), same account.
    c1 = TestClient(app, follow_redirects=False)
    c2 = TestClient(app, follow_redirects=False)
    _login(c1)
    _login(c2)
    assert app.state.admin.created == ["d@x"]  # ruling #1: minted once, reused

    token1 = _csrf_for(c1)
    r = c1.post("/logout", headers={"X-CSRF-Token": token1})
    assert r.status_code == 303
    assert app.state.admin.destroyed == []  # c2's session still needs it

    token2 = _csrf_for(c2)
    r2 = c2.post("/logout", headers={"X-CSRF-Token": token2})
    assert r2.status_code == 303
    assert app.state.admin.destroyed == [("d@x", "k1")]  # last one out destroys it


def test_logout_all_destroys_key_once_and_revokes_every_session(app):
    c1 = TestClient(app, follow_redirects=False)
    c2 = TestClient(app, follow_redirects=False)
    _login(c1)
    _login(c2)

    token = _csrf_for(c1)
    r = c1.post("/logout/all", headers={"X-CSRF-Token": token})
    assert r.status_code == 303
    assert app.state.admin.destroyed == [("d@x", "k1")]

    # c2's cookie now names a revoked session id.
    assert c2.get("/mail/inbox").status_code == 303


async def test_logout_all_is_audited(app):
    c = TestClient(app, follow_redirects=False)
    _login(c)
    token = _csrf_for(c)
    c.post("/logout/all", headers={"X-CSRF-Token": token})
    async with app.state.sessionmaker() as db:
        result = await db.execute(select(AuditLog).where(AuditLog.action == "logout.all"))
        assert result.scalars().first() is not None


# ---------------------------------------------------------------------------
# Controller ruling #1: one Stalwart API key per USER, not per session.
# ---------------------------------------------------------------------------


def test_second_login_for_same_user_reuses_the_existing_key(app):
    c1 = TestClient(app, follow_redirects=False)
    c2 = TestClient(app, follow_redirects=False)
    c3 = TestClient(app, follow_redirects=False)
    _login(c1)
    _login(c2)
    _login(c3)
    # Three logins, one account -> exactly one Stalwart mint, not three
    # (Task 4's live finding: Stalwart caps API keys at 5 per account).
    assert app.state.admin.created == ["d@x"]


def test_different_users_each_get_their_own_key(app):
    c1 = TestClient(app, follow_redirects=False)
    c2 = TestClient(app, follow_redirects=False)
    _login(c1, username="alice@x")
    _login(c2, username="bob@x")
    assert app.state.admin.created == ["alice@x", "bob@x"]


# ---------------------------------------------------------------------------
# Security headers (design spec §9) — applied to every response.
# ---------------------------------------------------------------------------


def test_security_headers_present_on_every_response(app):
    r = TestClient(app).get("/login")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


# ---------------------------------------------------------------------------
# The control that reaches the routes above
#
# `POST /logout` and `POST /logout/all` were implemented in Phase 1A, down to
# revoking the session row, destroying the Stalwart API key and writing an
# audit row — and until Phase 1E nothing in any template or module called
# either one. Every test above passed against a feature no reader could use.
# These pin the way in, not the route.
# ---------------------------------------------------------------------------

TOPBAR = pathlib.Path(__file__).resolve().parents[2] / "mailosh/web/templates/shell/topbar.html"


def _shell(app) -> str:
    """The signed-in shell, which is where the account menu lives."""
    client = TestClient(app, follow_redirects=False)
    _login(client)
    return client.get("/mail/inbox").text


def test_the_account_menu_reaches_both_logout_routes(app):
    body = _shell(app)
    assert 'action="/logout"' in body
    assert 'action="/logout/all"' in body


def test_signing_out_is_a_navigation_not_a_swap(app):
    """A plain `<form method="post">`, deliberately.

    Both routes answer 303 to `/login`. An `hx-post` would swap that into a
    fragment and leave the shell of a signed-out session on screen around
    it; the whole document has to be replaced.
    """
    markup = TOPBAR.read_text()
    menu = markup[markup.index('class="account-menu"') :]
    assert 'method="post"' in menu
    assert "hx-post" not in menu


def test_both_sign_out_forms_carry_a_csrf_token(app):
    """`deps.csrf_protect` reads the `csrf_token` *field* for a non-htmx
    POST — the `hx-headers` token on `<body>` never reaches a plain form
    submit, so a missing hidden input is a 403 at the moment someone tries
    to leave."""
    body = _shell(app)
    forms = re.findall(r'<form[^>]*action="/logout(?:/all)?"[^>]*>.*?</form>', body, re.S)
    assert len(forms) == 2, forms
    for form in forms:
        assert 'name="csrf_token"' in form, form


def test_the_menu_names_the_account_being_left(app):
    """The first question someone opening this menu has is which account
    they are about to sign out of."""
    body = _shell(app)
    menu = body[body.index('class="account-menu"') :]
    assert "d@x" in menu[: menu.index("</details>")]
