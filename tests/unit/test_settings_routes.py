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

import json
import pathlib
import re

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from test_labels import FakeClient

from mailosh.jmap.models import Identity
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


class SettingsClient(FakeClient):
    """`FakeClient` plus the one method the settings pages need that the
    label tests never did: `Identity/get`, which the Compose page's
    signature editors are built from.
    """

    identities = (
        Identity(id="i1", email=ME, name="Dee"),
        Identity(id="i2", email="alt@x", name=None),
    )

    async def get_identities(self) -> list[Identity]:
        return list(self.identities)


@pytest.fixture
def fake() -> FakeClient:
    return SettingsClient(placement={"e1": {"mb-inbox", "m-work"}})


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


# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------


def _checked(body: str, name: str) -> str | None:
    """The checked radio's value for `name`, or None."""
    for tag in re.findall(rf'<input type="radio" name="{name}"[^>]*>', body):
        if "checked" in tag:
            return re.search(r'value="([^"]+)"', tag).group(1)
    return None


def test_appearance_renders_the_stored_values_checked(app):
    client = _login(app)
    body = client.get("/settings/appearance").text
    assert _checked(body, "theme") == "system"
    assert _checked(body, "density") == "comfortable"
    assert _checked(body, "reading_pane") == "none"
    assert _checked(body, "font_size") == "md"
    # The document itself carries the font size for the stylesheet.
    assert 'data-font-size="md"' in body


def test_appearance_save_persists_and_echoes_every_field(app):
    client = _login(app)
    r = _post(
        client,
        "/settings/appearance",
        {"theme": "dark", "density": "compact", "reading_pane": "right", "font_size": "lg"},
    )
    assert r.status_code == 204, r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["om:prefs"] == {
        "theme": "dark",
        "density": "compact",
        "reading_pane": "right",
        "font_size": "lg",
    }
    assert trigger["om:done"]["toast"] == "Saved"
    assert trigger["om:done"]["undo"] is None

    body = client.get("/settings/appearance").text
    assert _checked(body, "theme") == "dark"
    assert _checked(body, "font_size") == "lg"
    assert 'data-theme="dark"' in body and 'data-font-size="lg"' in body
    # The quick-settings popover on the same shell agrees.
    quick = body[body.index('id="quick-settings"') :]
    assert re.search(r'name="theme" value="dark" data-pref="theme"\s+checked', quick)


@pytest.mark.parametrize(
    "data",
    [
        {"theme": "sepia", "density": "compact", "reading_pane": "none", "font_size": "md"},
        {"theme": "dark", "density": "compact", "reading_pane": "none", "font_size": "xl"},
        {"theme": "dark", "density": "compact", "reading_pane": "left", "font_size": "md"},
        {"theme": "dark", "density": "compact"},
    ],
)
def test_appearance_refuses_a_value_outside_the_set(app, data):
    client = _login(app)
    before = client.get("/settings/appearance").text
    assert _post(client, "/settings/appearance", data).status_code == 422
    assert client.get("/settings/appearance").text == before


def test_appearance_save_needs_a_csrf_token(app):
    client = _login(app)
    r = client.post(
        "/settings/appearance",
        data={"theme": "dark", "density": "compact", "reading_pane": "none", "font_size": "md"},
    )
    assert r.status_code == 403


def test_settings_js_is_on_the_shell_and_imports_nothing():
    layout = REPO / "mailosh/web/templates/layouts/app.html"
    assert "static('js/settings.js')" in layout.read_text()
    source = (REPO / "mailosh/web/static/js/settings.js").read_text()
    assert "import " not in source
    assert "font_size" in source and "dataset.fontSize" in source
    css = (REPO / "styles/settings.css").read_text()
    assert "[data-font-size=lg]" in css and "[data-font-size=sm]" in css


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_reading_renders_the_stored_values_checked(app):
    client = _login(app)
    body = client.get("/settings/reading").text
    assert _checked(body, "conversation_view") == "true"
    assert _checked(body, "mark_read_delay") == "0"
    assert _checked(body, "auto_advance") == "older"
    assert _checked(body, "remote_images") == "ask"
    assert _checked(body, "dark_restyle") == "true"


def test_reading_save_persists_at_the_columns_own_types(app, sqlite_url):
    client = _login(app)
    r = _post(
        client,
        "/settings/reading",
        {
            "conversation_view": "false",
            "mark_read_delay": "3",
            "auto_advance": "list",
            "remote_images": "contacts",
            "dark_restyle": "false",
        },
    )
    assert r.status_code == 204, r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["om:prefs"] == {
        "conversation_view": False,
        "mark_read_delay": 3,
        "auto_advance": "list",
        "remote_images": "contacts",
        "dark_restyle": False,
    }
    body = client.get("/settings/reading").text
    assert _checked(body, "conversation_view") == "false"
    assert _checked(body, "mark_read_delay") == "3"
    assert _checked(body, "dark_restyle") == "false"
    # And the popover, on the same shell, was rendered from the same row.
    quick = body[body.index('id="quick-settings"') :]
    assert re.search(r'name="mark_read_delay" value="3"[^>]*\s+checked', quick)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("conversation_view", "yes"),
        ("mark_read_delay", "7"),
        ("auto_advance", "sideways"),
        ("remote_images", "never"),
        ("dark_restyle", "1"),
    ],
)
def test_reading_refuses_a_value_outside_the_set(app, field, bad):
    client = _login(app)
    data = {
        "conversation_view": "true",
        "mark_read_delay": "0",
        "auto_advance": "older",
        "remote_images": "ask",
        "dark_restyle": "true",
        field: bad,
    }
    assert _post(client, "/settings/reading", data).status_code == 422


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def test_compose_renders_the_stored_values_and_one_editor_per_identity(app):
    client = _login(app)
    body = client.get("/settings/compose").text
    assert _checked(body, "undo_send_seconds") == "10"
    assert _checked(body, "default_reply") == "reply"
    # One signature form per send-as address, each carrying its own id.
    for identity in SettingsClient.identities:
        assert f'value="{identity.id}"' in body
        assert f'id="sig-{identity.id}"' in body


def test_compose_save_persists_and_echoes_both_fields(app):
    client = _login(app)
    data = {"undo_send_seconds": "30", "default_reply": "reply_all"}
    r = _post(client, "/settings/compose", data)
    assert r.status_code == 204, r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["om:prefs"] == {"undo_send_seconds": 30, "default_reply": "reply_all"}
    body = client.get("/settings/compose").text
    assert _checked(body, "undo_send_seconds") == "30"
    assert _checked(body, "default_reply") == "reply_all"


@pytest.mark.parametrize(
    "data",
    [
        {"undo_send_seconds": "15", "default_reply": "reply"},
        {"undo_send_seconds": "10", "default_reply": "reply_none"},
        {"undo_send_seconds": "10"},
    ],
)
def test_compose_refuses_a_value_outside_the_set(app, data):
    client = _login(app)
    assert _post(client, "/settings/compose", data).status_code == 422


def test_a_signature_is_stored_sanitised_and_comes_back_in_the_editor(app):
    client = _login(app)
    r = _post(
        client,
        "/settings/compose/signature",
        {"identity_id": "i1", "html": "<p>Dee<script>alert(1)</script></p>"},
    )
    assert r.status_code == 204, r.text
    assert json.loads(r.headers["HX-Trigger"])["om:done"]["toast"] == "Signature saved"
    body = client.get("/settings/compose").text
    editor = body[body.index('id="sig-i1"') :]
    editor = editor[: editor.index("</textarea>")]
    assert "Dee" in editor
    assert "script" not in editor.lower()


def test_a_signature_for_an_identity_this_account_does_not_have_is_refused(app):
    client = _login(app)
    r = _post(client, "/settings/compose/signature", {"identity_id": "nope", "html": "<p>hi</p>"})
    assert r.status_code == 200
    assert "isn't one of your addresses" in r.headers["HX-Trigger"]
    assert "nope" not in client.get("/settings/compose").text


def test_a_signature_past_the_length_ceiling_is_refused(app):
    from mailosh.web.settings import MAX_SIGNATURE_CHARS

    client = _login(app)
    r = _post(
        client,
        "/settings/compose/signature",
        {"identity_id": "i1", "html": "x" * (MAX_SIGNATURE_CHARS + 1)},
    )
    assert r.status_code == 200
    assert "too long" in r.headers["HX-Trigger"]


def test_the_undo_send_window_reaches_the_dock_as_a_data_attribute(app):
    """`compose.js` reads the window off the form rather than holding a
    constant, because the value is a preference and the CSP forbids handing
    it over in an inline script."""
    client = _login(app)
    _post(client, "/settings/compose", {"undo_send_seconds": "20", "default_reply": "reply"})
    dock = client.get("/compose", headers={"HX-Request": "true"}).text
    assert 'data-undo-send-ms="20000"' in dock
    source = (REPO / "mailosh/web/static/js/compose.js").read_text()
    assert "undoSendMs" in source
    assert "}, undoWindowMs(root));" in source


def test_a_new_message_opens_with_the_signature_below_the_caret(app):
    client = _login(app)
    _post(client, "/settings/compose/signature", {"identity_id": "i1", "html": "<p>Ada L</p>"})
    dock = client.get("/compose", headers={"HX-Request": "true"}).text
    assert "Ada L" in dock


def test_a_signature_goes_above_the_quote_not_below_it():
    """The one placement rule, tested where it lives: replying above your
    own sign-off is the failure this ordering exists to prevent."""
    from mailosh.services.compose import with_signature

    reply = "<div><br></div><blockquote>old mail</blockquote>"
    signed = with_signature(reply, "<p>Dee</p>")
    assert signed.index("<p>Dee</p>") < signed.index("<blockquote>")
    assert signed.startswith("<div><br></div>")
    # A new message is just the spacer plus the signature.
    assert with_signature("", "<p>Dee</p>") == "<div><br></div><p>Dee</p>"
    # No signature changes nothing at all.
    assert with_signature(reply, "") == reply


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("reply", "reply"), ("reply_all", "reply_all")],
)
def test_a_default_reply_resolves_from_the_preference(stored, expected):
    """`"default"` never reaches `build_reply` — one place resolves it."""
    from types import SimpleNamespace

    from mailosh.web.compose import _reply_mode

    prefs = SimpleNamespace(default_reply=stored)
    assert _reply_mode("default", prefs) == expected
    # An explicit mode is never overridden by the preference.
    assert _reply_mode("forward", prefs) == "forward"
    assert _reply_mode("reply_all", SimpleNamespace(default_reply="reply")) == "reply_all"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_labels_lists_every_label_and_posts_to_the_existing_routes(app):
    """The page owns no label logic: every control targets a route
    `mailosh/web/labels.py` already serves, so renaming here and renaming
    from the sidebar's hover menu are one code path."""
    client = _login(app)
    body = client.get("/settings/labels").text
    assert 'value="Work"' in body and 'value="Receipts"' in body
    # System mailboxes are not labels and must not be editable here.
    assert 'value="Inbox"' not in body and 'value="Trash"' not in body
    for suffix in ("rename", "meta", "nest", "delete", "color/clear"):
        assert f'hx-post="/labels/m-work/{suffix}"' in body
    # And it refreshes itself off the trigger those routes already emit.
    assert 'hx-trigger="om:labels from:body"' in body


def test_a_hidden_label_is_still_listed_so_it_can_be_un_hidden(app):
    """`build_nav` drops `hide` labels structurally — right for the nav,
    wrong for the only page that can bring one back."""
    client = _login(app)
    r = _post(client, "/labels/m-work/meta", {"visibility": "hide"})
    assert r.status_code == 204, r.text
    body = client.get("/settings/labels").text
    assert 'value="Work"' in body
    picked = re.search(r'<select[^>]*id="vis-m-work".*?</select>', body, re.S)
    assert picked is not None
    assert re.search(r'value="hide" selected', picked.group(0))


def test_renaming_from_the_settings_page_goes_through_the_label_route(app, fake):
    client = _login(app)
    r = _post(client, "/labels/m-work/rename", {"name": "Client work"})
    assert r.status_code == 204, r.text
    assert "Renamed" in r.headers["HX-Trigger"]
    assert 'value="Client work"' in client.get("/settings/labels").text


def test_the_labels_page_refuses_a_colour_that_is_not_one(app):
    client = _login(app)
    r = _post(client, "/labels/m-work/meta", {"color": "chartreuse"})
    assert r.status_code == 200
    assert "isn't a label colour" in r.headers["HX-Trigger"]
    assert "chartreuse" not in client.get("/settings/labels").text


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def test_account_shows_the_address_as_text_not_as_a_field(app):
    client = _login(app)
    body = client.get("/settings/account").text
    assert ME in body
    assert 'name="display_name"' in body
    assert 'name="email"' not in body
    for field in ("current_password", "new_password", "confirm_password"):
        assert f'name="{field}"' in body


def test_a_display_name_reaches_stalwart_before_the_local_row(app):
    client = _login(app)
    r = _post(client, "/settings/account/name", {"display_name": "Dee Ess"})
    assert r.status_code == 204, r.text
    assert app.state.admin.names == [(ME, "Dee Ess")]
    assert "Dee Ess" in client.get("/settings/account").text


def test_a_display_name_past_the_ceiling_is_refused_and_never_sent(app):
    client = _login(app)
    r = _post(client, "/settings/account/name", {"display_name": "x" * 200})
    assert r.status_code == 200
    assert "too long" in r.headers["HX-Trigger"]
    assert app.state.admin.names == []


def test_a_password_change_verifies_the_current_one_against_stalwart(app):
    """A session proves somebody logged in once; it does not prove the
    person at the keyboard knows the password."""
    client = _login(app)
    r = _post(
        client,
        "/settings/account/password",
        {
            "current_password": "wrong",
            "new_password": "a-longer-secret",
            "confirm_password": "a-longer-secret",
        },
    )
    assert r.status_code == 200
    assert "isn't your current password" in r.headers["HX-Trigger"]
    assert app.state.admin.passwords == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {"new_password": "a-longer-secret", "confirm_password": "different-secret"},
            "don't match",
        ),
        ({"new_password": "short", "confirm_password": "short"}, "at least"),
        ({"new_password": "right", "confirm_password": "right"}, "already have"),
    ],
)
def test_a_password_change_is_refused_before_anything_is_sent(app, data, expected):
    client = _login(app)
    r = _post(client, "/settings/account/password", {"current_password": "right", **data})
    assert r.status_code == 200
    assert expected in r.headers["HX-Trigger"]
    assert app.state.admin.passwords == []


def test_a_password_change_signs_every_other_session_out_and_says_so(app):
    first = _login(app)
    second = _login(app)
    assert second.get("/settings/account").status_code == 200

    r = _post(
        first,
        "/settings/account/password",
        {
            "current_password": "right",
            "new_password": "a-longer-secret",
            "confirm_password": "a-longer-secret",
        },
    )
    assert r.status_code == 204, r.text
    assert app.state.admin.passwords == [(ME, "a-longer-secret")]
    toast = json.loads(r.headers["HX-Trigger"])["om:done"]["toast"]
    assert "1 other session signed out" in toast
    # The other browser is gone; this one is still signed in.
    assert second.get("/settings/account", follow_redirects=False).status_code == 303
    assert first.get("/settings/account").status_code == 200


def test_a_mail_server_that_refuses_the_change_leaves_every_session_alone(app):
    first = _login(app)
    second = _login(app)
    app.state.admin.fail_password = True
    r = _post(
        first,
        "/settings/account/password",
        {
            "current_password": "right",
            "new_password": "a-longer-secret",
            "confirm_password": "a-longer-secret",
        },
    )
    assert r.status_code == 200
    assert "wouldn't accept" in r.headers["HX-Trigger"]
    assert second.get("/settings/account").status_code == 200


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def _session_ids(body: str) -> list[str]:
    return re.findall(r'hx-post="/settings/security/sessions/([^/]+)/revoke"', body)


def test_security_lists_every_live_session_and_marks_this_one(app):
    first = _login(app)
    _login(app)
    body = first.get(
        "/settings/security", headers={"User-Agent": "Mozilla/5.0 (Macintosh) Firefox/1"}
    ).text
    assert "Signed in on 2 devices" in body
    assert body.count("This device") == 1
    # The current row has no Sign out button; the other one does.
    assert len(_session_ids(body)) == 1


def test_a_row_signs_that_session_out_and_leaves_this_one_alone(app):
    first = _login(app)
    second = _login(app)
    other = _session_ids(first.get("/settings/security").text)[0]

    r = _post(first, f"/settings/security/sessions/{other}/revoke")
    assert r.status_code == 204, r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["om:done"]["toast"] == "Signed out"
    assert trigger["om:sessions"] == {"changed": True}

    assert second.get("/settings/security", follow_redirects=False).status_code == 303
    assert "Signed in on 1 device" in first.get("/settings/security").text


def test_this_session_cannot_be_revoked_from_a_row(app):
    client = _login(app)
    body = client.get("/settings/security").text
    match = re.search(r"<td>\s*[^<]*<span class=\"settings-pill\">This device", body)
    assert match is not None
    # Reach for it anyway, the way a forged form would.
    current = client.cookies["sid"]
    r = _post(client, f"/settings/security/sessions/{current}/revoke")
    assert r.status_code == 200
    assert "account menu" in r.headers["HX-Trigger"]
    assert client.get("/settings/security").status_code == 200


def test_one_user_cannot_sign_another_out(app, monkeypatch):
    """The row is looked up by id *and* by `user_id`: a session id alone is
    enough to end a session, and this route must not be a way to end
    somebody else's."""
    mine = _login(app)
    theirs = _login(app, username="other@x")
    stolen = theirs.cookies["sid"]
    r = _post(mine, f"/settings/security/sessions/{stolen}/revoke")
    assert r.status_code == 200
    assert "already signed out" in r.headers["HX-Trigger"]
    assert theirs.get("/settings/security").status_code == 200


def test_the_user_agent_column_is_readable_and_never_empty(app):
    from mailosh.web.settings import _describe_agent

    assert _describe_agent("Mozilla/5.0 (Macintosh) Firefox/141.0") == "Firefox on Mac"
    edge = "Mozilla/5.0 (Windows NT) Chrome/9 Safari/537 Edg/9"
    assert _describe_agent(edge) == "Edge on Windows"
    assert _describe_agent(None) == "Unknown device"
    # Unrecognised is shown, not swallowed — a strange session is exactly
    # the one worth reading.
    assert _describe_agent("curl/8.7.1") == "curl/8.7.1"


# ---------------------------------------------------------------------------
# Wire-up
# ---------------------------------------------------------------------------


def test_the_account_menu_links_to_settings(app):
    """`/settings/*` existed with no control anywhere pointing at it. The
    gear beside this menu opens the quick-settings popover, which is a
    subset; everything else about the account lives on these pages."""
    client = _login(app)
    body = client.get("/settings/appearance").text
    panel = body[body.index("account-panel") :]
    panel = panel[: panel.index("</details>")]
    assert 'href="/settings/appearance"' in panel
    assert 'hx-get="/settings/appearance"' in panel
    assert 'hx-target="#main"' in panel


def test_the_palette_offers_every_settings_page(app):
    client = _login(app)
    payload = client.get("/palette/index").json()
    pages = [s for s in payload["settings"] if s["href"] is not None]
    assert [s["href"] for s in pages] == [f"/settings/{key}" for key, _label in PAGES]
    source = (REPO / "mailosh/web/static/js/palette.js").read_text()
    assert "setting.href ? go(setting.href)" in source
