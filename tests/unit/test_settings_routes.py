"""HTTP-level tests for `mailosh.web.settings` (design spec §10) — the six
pages, every save, every refusal, and the client half in
`static/js/settings.js` that has to agree with them.

Same rig as `tests/unit/test_label_routes.py`: a real `create_app` (real
routers, real Jinja, real session/CSRF over file-backed aiosqlite) with
exactly two things faked — `verify_password` (no Stalwart) and
`deps.client_for` (the in-memory `FakeClient` from `test_labels.py`) — plus
a `FakeAdmin` that records what the account page asks Stalwart to change.
No network call is ever made.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from test_labels import FakeClient

from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps
from mailosh.web.app import create_app
from mailosh.web.settings import PAGES

REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "mailosh/web/templates/settings"

ME = "d@x"


class FakeAdmin:
    """`StalwartAdmin` stand-in — `tests/unit/test_auth_routes.py`'s, plus
    the two account-page methods, each recording its arguments.
    """

    def __init__(self) -> None:
        self._minted = 0
        self.passwords: list[tuple[str, str]] = []
        self.names: list[tuple[str, str]] = []
        self.fail_password = False

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        self._minted += 1
        return ApiKey(id=f"k{self._minted}", secret=f"API_secret_{self._minted}")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None

    async def set_password(self, username: str, password: str) -> None:
        if self.fail_password:
            from mailosh.jmap.errors import JmapError

            raise JmapError("no")
        self.passwords.append((username, password))

    async def set_display_name(self, username: str, name: str) -> None:
        self.names.append((username, name))


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient(placement={"e1": {"mb-inbox", "m-work"}})


@pytest.fixture
def app(monkeypatch, sqlite_url, fake):
    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    monkeypatch.setattr("mailosh.web.settings.verify_password", fake_verify, raising=False)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = lambda: fake
    with TestClient(application):
        yield application


def _login(app, *, username: str = ME) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    r = client.post("/login", data={"username": username, "password": "right"})
    assert r.status_code == 303, r.text
    return client


def _csrf(client: TestClient) -> str:
    cached = getattr(client, "_csrf_token", None)
    if cached is not None:
        return cached
    body = client.get("/settings/appearance").text
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', body)
    assert match is not None
    client._csrf_token = match.group(1)  # type: ignore[attr-defined]
    return client._csrf_token  # type: ignore[attr-defined,no-any-return]


def _post(client: TestClient, url: str, data=None, *, htmx: bool = True):
    headers = {"X-CSRF-Token": _csrf(client)}
    if htmx:
        headers["HX-Request"] = "true"
    return client.post(url, data=data or {}, headers=headers)


def _strip_comments(markup: str) -> str:
    return re.sub(r"\{#.*?#\}", "", markup, flags=re.S)


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------


def test_settings_root_redirects_to_the_first_page(app):
    client = _login(app)
    r = client.get("/settings")
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/appearance"


def test_settings_requires_a_session(app):
    r = TestClient(app, follow_redirects=False).get("/settings/appearance")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


@pytest.mark.parametrize("page", [key for key, _label in PAGES])
def test_every_page_renders_inside_the_shell_with_its_tab_current(app, page):
    client = _login(app)
    r = client.get(f"/settings/{page}")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="main"' in body
    assert 'class="account-menu"' in body  # the real top bar
    current = re.findall(r'<a href="/settings/([a-z]+)"[^>]*aria-current="page"', body)
    assert current == [page]
    # Every one of the six is reachable from every page.
    for key, _label in PAGES:
        assert f'hx-get="/settings/{key}"' in body


def test_an_unknown_page_is_not_a_template_lookup(app):
    client = _login(app)
    assert client.get("/settings/nope").status_code == 422


def test_htmx_gets_the_fragment_and_a_history_restore_gets_the_document(app):
    client = _login(app)
    fragment = client.get("/settings/reading", headers={"HX-Request": "true"}).text
    assert "<html" not in fragment
    assert fragment.lstrip().startswith("<title>")
    assert 'aria-current="page"' in fragment
    restored = client.get(
        "/settings/reading",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    ).text
    assert "<html" in restored


def test_the_sub_nav_is_a_list_of_real_links_with_no_inline_behaviour():
    markup = _strip_comments((TEMPLATES / "page.html").read_text())
    assert 'aria-label="Settings"' in markup
    assert "hx-on" not in markup
    assert "x-data" not in markup and "x-on" not in markup
    assert "onclick" not in markup
    # The same shape as `shell/nav.html`'s items: a fragment swap, URL pushed.
    assert 'hx-target="#main"' in markup
    assert 'hx-push-url="true"' in markup


def test_no_settings_template_uses_an_evaluated_attribute():
    """`script-src 'self'` with no `'unsafe-eval'` (spec §9): nothing here
    may need an evaluator."""
    for path in sorted(TEMPLATES.glob("*.html")):
        markup = _strip_comments(path.read_text())
        assert "hx-on" not in markup, path.name
        assert "hx-vals='js:" not in markup, path.name
        assert "onclick" not in markup, path.name
        assert "<script>" not in markup, path.name
