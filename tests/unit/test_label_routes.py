"""HTTP-level tests for `mailosh.web.labels` (design spec §10) — the picker
and menu fragments, the six mutating routes, and the client half in
`static/js/labels.js` that has to agree with them.

Same rig as `tests/unit/test_mail_routes.py`: a real `create_app` (real
routers, real Jinja, real session/CSRF over file-backed aiosqlite) with
exactly two things faked — `verify_password` (no Stalwart) and
`deps.client_for` (an in-memory `FakeClient` in place of the pooled
`JmapClient`). No network call is ever made.

The properties pinned here, over and above the service-level ones in
`tests/unit/test_labels.py`:

* every mutating route is CSRF-protected, and a `GET`-shaped read is not a
  back door into one;
* the picker's checkboxes carry the tri-state the selection actually has;
* deleting a label drops its `LabelMeta` row, and *only* on success;
* an orphaned `LabelMeta` row is cleaned up on a path that already holds a
  full mailbox list, and never on the nav's own render path;
* a refusal reaches the browser as a readable `om:error` toast, never a raw
  JMAP error string and never a bare 500.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_labels import FakeClient, _mailbox

from mailosh.db.models import AppUser, LabelMeta
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps
from mailosh.web.app import create_app

REPO = pathlib.Path(__file__).resolve().parents[2]
LABELS_JS = REPO / "mailosh/web/static/js/labels.js"
NAV_HTML = REPO / "mailosh/web/templates/shell/nav.html"
PICKER_HTML = REPO / "mailosh/web/templates/labels/picker.html"
MENU_HTML = REPO / "mailosh/web/templates/labels/menu.html"

ME = "d@x"


class FakeAdmin:
    """`StalwartAdmin` stand-in — see `tests/unit/test_auth_routes.py`."""

    def __init__(self) -> None:
        self._minted = 0

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        self._minted += 1
        return ApiKey(id=f"k{self._minted}", secret=f"API_secret_{self._minted}")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient(
        placement={
            "e1": {"mb-inbox", "m-work"},
            "e2": {"mb-inbox"},
            "e3": {"mb-inbox", "m-work", "m-receipts"},
        }
    )


@pytest.fixture
def app(monkeypatch, sqlite_url, fake):
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


def _csrf(client: TestClient) -> str:
    """This session's CSRF token, fetched once and remembered.

    Cached deliberately: reading it costs a `GET /mail/inbox`, which is a
    `Mailbox/get` and a `query_page` against the fake — and several tests
    below count exactly how many JMAP calls a route makes.
    """
    cached = getattr(client, "_csrf_token", None)
    if cached is not None:
        return cached
    body = client.get("/mail/inbox").text
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', body)
    assert match is not None
    client._csrf_token = match.group(1)  # type: ignore[attr-defined]
    return client._csrf_token  # type: ignore[attr-defined,no-any-return]


def _post(client: TestClient, url: str, data=None):
    return client.post(url, data=data or {}, headers={"X-CSRF-Token": _csrf(client)})


def _trigger(response, name: str):
    raw = response.headers.get("HX-Trigger")
    assert raw, response.headers
    return json.loads(raw).get(name)


def _meta_rows(sqlite_url: str) -> list[LabelMeta]:
    async def go() -> list[LabelMeta]:
        engine = create_async_engine(sqlite_url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            rows = list((await db.execute(select(LabelMeta))).scalars())
        await engine.dispose()
        return rows

    return asyncio.run(go())


def _seed_meta(sqlite_url: str, rows: list[dict]) -> None:
    async def go() -> None:
        engine = create_async_engine(sqlite_url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = (await db.execute(select(AppUser))).scalars().one()
            for row in rows:
                db.add(LabelMeta(user_id=user.id, account_id="acct-labels", **row))
            await db.commit()
        await engine.dispose()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Authentication and CSRF
# ---------------------------------------------------------------------------


MUTATIONS = [
    ("/labels", {"name": "New"}),
    ("/labels/m-work/rename", {"name": "New"}),
    ("/labels/m-work/nest", {"parent_id": ""}),
    ("/labels/m-work/meta", {"color": "teal"}),
    ("/labels/m-work/color/clear", {}),
    ("/labels/m-work/delete", {}),
    ("/labels/apply", {"ids": "e1", "add": "m-work"}),
    ("/labels/move", {"ids": "e1", "to": "m-work"}),
    ("/labels/picker", {"ids": "e1"}),
    ("/labels/menu/m-work", {}),
    ("/labels/new", {}),
]


@pytest.mark.parametrize("url, data", MUTATIONS)
def test_every_label_route_needs_a_session(app, url, data):
    r = TestClient(app, follow_redirects=False).post(url, data=data)
    assert r.status_code in (303, 401), (url, r.status_code)


@pytest.mark.parametrize("url, data", MUTATIONS)
def test_every_label_route_needs_a_csrf_token(app, url, data, fake):
    """Including the two that only *read*: they are POSTs, so the router's
    own `csrf_protect` applies to them, and a token-less POST must be
    refused before it reaches a mailbox at all.
    """
    client = _login(app)
    before = len(fake.calls)
    r = client.post(url, data=data)
    assert r.status_code == 403, (url, r.status_code)
    assert len(fake.calls) == before, f"{url} reached JMAP without a token"


# ---------------------------------------------------------------------------
# The picker fragment
# ---------------------------------------------------------------------------


def test_the_picker_carries_the_selections_real_tri_state(app):
    """Two conversations, one of them filed under Work: the box has to say
    "mixed", not "on". A plain checked box would claim something untrue and
    clicking it would do something surprising.
    """
    client = _login(app)
    r = _post(client, "/labels/picker", {"ids": ["e1", "e2"], "mode": "label"})
    assert r.status_code == 200
    work = re.search(r'<input type="checkbox"[^>]*value="m-work"[^>]*>', r.text)
    assert work is not None, r.text
    assert 'data-state="mixed"' in work.group(0)
    assert "checked" not in work.group(0)

    # ...and a label every selected conversation carries reads "on".
    r = _post(client, "/labels/picker", {"ids": ["e1", "e3"], "mode": "label"})
    work = re.search(r'<input type="checkbox"[^>]*value="m-work"[^>]*>', r.text)
    assert work is not None
    assert 'data-state="on"' in work.group(0)
    assert "checked" in work.group(0)

    receipts = re.search(r'<input type="checkbox"[^>]*value="m-receipts"[^>]*>', r.text)
    assert receipts is not None
    assert 'data-state="mixed"' in receipts.group(0)


def test_the_picker_costs_one_mailbox_get_and_one_email_get(app, fake):
    """However big the selection, the whole tri-state comes out of one
    snapshot — the picker must not become a per-label question, and the
    orphan sweep must not become a second `Mailbox/get` on every open.
    """
    client = _login(app)
    _csrf(client)  # warm the token cache, so its own GET is not counted below
    fake.calls.clear()
    _post(client, "/labels/picker", {"ids": ["e1", "e2", "e3"], "mode": "label"})
    made = [call.name for call in fake.calls]
    assert made.count("get_email_states") == 1
    assert made.count("get_mailboxes") == 1
    assert made.count("query_search") == 0


def test_the_orphan_sweep_only_re_reads_the_mailboxes_when_it_has_to(app, fake, sqlite_url):
    """A `LabelMeta` row for a label the nav knows about cannot be orphaned,
    so the pre-check answers without asking the server. A row naming an id
    the nav has never heard of is what buys the authoritative second
    `Mailbox/get` — never the filtered nav, which drops hidden labels and
    would delete exactly their metadata.
    """
    client = _login(app)
    _csrf(client)
    _seed_meta(sqlite_url, [{"mailbox_id": "m-work", "color": "teal"}])
    fake.calls.clear()
    _post(client, "/labels/picker", {"ids": ["e1"], "mode": "label"})
    assert [c.name for c in fake.calls].count("get_mailboxes") == 1

    _seed_meta(sqlite_url, [{"mailbox_id": "m-vanished", "color": "rose"}])
    fake.calls.clear()
    _post(client, "/labels/picker", {"ids": ["e1"], "mode": "label"})
    assert [c.name for c in fake.calls].count("get_mailboxes") == 2
    assert {row.mailbox_id for row in _meta_rows(sqlite_url)} == {"m-work"}


def test_a_hidden_labels_metadata_survives_the_orphan_sweep(app, fake, sqlite_url):
    """`build_nav` drops a `hide` label from the tree entirely, so a sweep
    that trusted the nav would delete the colour and the visibility of every
    label the reader deliberately hid — and the label would come back.
    """
    client = _login(app)
    _seed_meta(
        sqlite_url,
        [
            {"mailbox_id": "m-receipts", "color": "rose", "visibility": "hide"},
            {"mailbox_id": "m-vanished", "color": "amber"},
        ],
    )
    _post(client, "/labels/picker", {"ids": ["e1"], "mode": "label"})
    rows = {row.mailbox_id: row for row in _meta_rows(sqlite_url)}
    assert set(rows) == {"m-receipts"}
    assert rows["m-receipts"].visibility == "hide"


def test_move_mode_offers_folders_and_never_type_to_create(app):
    """Move is a single choice into somewhere that already exists — spec
    §10's "Move (`v`) = single choice". Creating a folder mid-move is two
    gestures wearing one coat.
    """
    client = _login(app)
    r = _post(client, "/labels/picker", {"ids": ["e1"], "mode": "move"})
    assert 'data-mode="move"' in r.text
    assert 'type="radio"' in r.text
    assert 'type="checkbox"' not in r.text
    assert 'value="inbox"' in r.text and 'value="trash"' in r.text
    # The create row is served (the fragment is one template) but the client
    # only ever reveals it outside move mode — pinned in labels.js below.
    assert 'data-role="lp-create"' in r.text


def test_the_picker_keeps_a_show_if_unread_label_that_has_no_nav_row(app, sqlite_url):
    """A label the reader configured to hide itself while quiet is still a
    label they can file under. Dropping it from the picker would make the
    one control that reaches it unreachable.
    """
    client = _login(app)
    _seed_meta(sqlite_url, [{"mailbox_id": "m-receipts", "visibility": "show_if_unread"}])
    body = client.get("/mail/inbox").text
    assert 'href="/mail/m-receipts"' not in body  # no nav row: nothing unread

    r = _post(client, "/labels/picker", {"ids": ["e1"], "mode": "label"})
    assert 'value="m-receipts"' in r.text
    assert "is-muted" in r.text


def test_a_hidden_label_is_offered_nowhere(app, sqlite_url):
    """`hide` is structural: `build_nav` never puts such a label in the
    tree, so it produces no nav row, no chip and no picker entry.
    """
    client = _login(app)
    _seed_meta(sqlite_url, [{"mailbox_id": "m-receipts", "visibility": "hide"}])
    r = _post(client, "/labels/picker", {"ids": ["e1"], "mode": "label"})
    assert 'value="m-receipts"' not in r.text


# ---------------------------------------------------------------------------
# Create / rename / nest
# ---------------------------------------------------------------------------


def test_create_makes_a_label_and_says_what_it_is_called(app, fake):
    client = _login(app)
    r = _post(client, "/labels", {"name": "  Receipts  2026 "})
    assert r.status_code == 204
    assert _trigger(r, "om:done")["toast"] == "Created “Receipts 2026”"
    assert _trigger(r, "om:labels") == {"changed": True}
    assert "Receipts 2026" in {m.name for m in fake.mailboxes}


def test_create_nested_sets_the_parent(app, fake):
    client = _login(app)
    r = _post(client, "/labels", {"name": "Design", "parent_id": "m-work"})
    assert r.status_code == 204
    created = next(m for m in fake.mailboxes if m.name == "Design")
    assert created.parent_id == "m-work"


def test_a_duplicate_name_is_a_readable_toast_not_a_500(app):
    client = _login(app)
    r = _post(client, "/labels", {"name": "Work"})
    assert r.status_code == 200
    error = _trigger(r, "om:error")
    assert error["toast"] == "There's already a label called “Work” here."
    assert r.headers["HX-Reswap"] == "none"


def test_an_empty_name_never_reaches_the_server(app, fake):
    client = _login(app)
    before = len(fake.named("create_mailbox"))
    r = _post(client, "/labels", {"name": "   "})
    assert _trigger(r, "om:error")["toast"] == "Give the label a name."
    assert len(fake.named("create_mailbox")) == before


def test_rename_reports_the_cleaned_name(app, fake):
    client = _login(app)
    r = _post(client, "/labels/m-work/rename", {"name": " Work   things "})
    assert r.status_code == 204
    assert _trigger(r, "om:done")["toast"] == "Renamed to “Work things”"
    assert next(m for m in fake.mailboxes if m.id == "m-work").name == "Work things"


def test_a_cycle_is_refused_in_english(app):
    """The one the brief calls out: `parentId` pointing into a label's own
    subtree is the server's refusal (RFC 8621 §2), and it must reach the
    reader as a sentence rather than a JMAP error string.
    """
    client = _login(app)
    r = _post(client, "/labels/m-work/nest", {"parent_id": "m-work"})
    assert r.status_code == 200
    toast = _trigger(r, "om:error")["toast"]
    assert toast == "A label can't be nested inside itself."
    for leak in ("invalidProperties", "Mailbox/set", "parentId", "{"):
        assert leak not in toast


def test_nesting_back_to_the_top_level_is_an_empty_parent(app, fake):
    fake.mailboxes.append(_mailbox("m-design", "Design", parent_id="m-work"))
    client = _login(app)
    r = _post(client, "/labels/m-design/nest", {"parent_id": ""})
    assert r.status_code == 204
    assert next(m for m in fake.mailboxes if m.id == "m-design").parent_id is None


def test_a_system_folder_cannot_be_renamed_through_the_label_routes(app, fake):
    client = _login(app)
    r = _post(client, "/labels/mb-trash/rename", {"name": "Bin"})
    assert "system folder" in _trigger(r, "om:error")["toast"]
    assert next(m for m in fake.mailboxes if m.id == "mb-trash").name == "Trash"


# ---------------------------------------------------------------------------
# Colour and visibility
# ---------------------------------------------------------------------------


def test_colour_and_visibility_persist_and_do_not_overwrite_each_other(app, sqlite_url):
    client = _login(app)
    assert _post(client, "/labels/m-work/meta", {"visibility": "show_if_unread"}).status_code == 204
    assert _post(client, "/labels/m-work/meta", {"color": "teal"}).status_code == 204

    rows = _meta_rows(sqlite_url)
    assert len(rows) == 1
    assert (rows[0].mailbox_id, rows[0].color, rows[0].visibility) == (
        "m-work",
        "teal",
        "show_if_unread",
    )
    # ...and the nav renders the stored colour on the next load.
    assert "var(--label-teal)" in client.get("/mail/inbox").text


def test_a_colour_outside_the_palette_is_refused(app, sqlite_url):
    client = _login(app)
    r = _post(client, "/labels/m-work/meta", {"color": "url(javascript:alert(1))"})
    assert _trigger(r, "om:error")["toast"] == "That isn't a label colour."
    assert _meta_rows(sqlite_url) == []


def test_an_unknown_visibility_is_refused_rather_than_parsed_to_hide(app, sqlite_url):
    """`Visibility.parse` resolves an unrecognised token to `hide` — the
    right call for a row that already exists, and exactly the wrong thing
    to let a *write* do: a typo would silently hide the label.
    """
    client = _login(app)
    r = _post(client, "/labels/m-work/meta", {"visibility": "hidden"})
    assert _trigger(r, "om:error")["toast"] == "That isn't a visibility setting."
    assert _meta_rows(sqlite_url) == []


def test_clearing_a_colour_is_its_own_route(app, sqlite_url):
    client = _login(app)
    _post(client, "/labels/m-work/meta", {"color": "teal"})
    assert _post(client, "/labels/m-work/color/clear", {}).status_code == 204
    rows = _meta_rows(sqlite_url)
    assert len(rows) == 1 and rows[0].color is None


def test_metadata_cannot_be_written_for_a_system_folder(app, sqlite_url):
    client = _login(app)
    r = _post(client, "/labels/mb-inbox/meta", {"color": "teal"})
    assert "system folder" in _trigger(r, "om:error")["toast"]
    assert _meta_rows(sqlite_url) == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_keeps_the_mail_drops_the_row_and_says_what_moved(app, fake, sqlite_url):
    client = _login(app)
    _seed_meta(sqlite_url, [{"mailbox_id": "m-work", "color": "teal"}])
    # A message that lives *only* in the label, so the Archive branch runs.
    fake.placement["e4"] = {"m-work"}
    fake.threads["e4"] = "t-e4"

    r = _post(client, "/labels/m-work/delete", {})
    assert r.status_code == 204
    toast = _trigger(r, "om:done")["toast"]
    assert toast == "Deleted “Work” — 1 message moved to Archive"

    # The mail survives, with its other labels.
    assert fake.placement["e1"] == {"mb-inbox"}
    assert fake.placement["e3"] == {"mb-inbox", "m-receipts"}
    assert fake.placement["e4"] == {"mb-archive"}
    # ...and the display metadata goes with the label.
    assert _meta_rows(sqlite_url) == []
    # ...and the destroy was never the destructive one.
    assert all(not c.kwargs["on_destroy_remove_emails"] for c in fake.named("destroy_mailbox"))


def test_a_failed_delete_keeps_the_label_and_its_metadata(app, fake, sqlite_url):
    """The row goes last and only on success: a delete refused part-way must
    not leave a live label that has silently lost its colour.
    """
    from mailosh.jmap.errors import JmapError

    client = _login(app)
    _seed_meta(sqlite_url, [{"mailbox_id": "m-work", "color": "teal"}])
    fake.destroy_error = JmapError(
        "Mailbox/set destroy failed: {'type': 'forbidden', 'description': 'no'}"
    )
    r = _post(client, "/labels/m-work/delete", {})
    assert r.status_code == 200
    assert "permission" in _trigger(r, "om:error")["toast"]
    assert _trigger(r, "om:error")["refresh"] is True
    rows = _meta_rows(sqlite_url)
    assert len(rows) == 1 and rows[0].color == "teal"


def test_the_menu_says_what_deleting_will_do_to_the_mail(app):
    """The brief's own requirement: the confirmation has to say what happens
    to messages that are only in that label.
    """
    client = _login(app)
    r = _post(client, "/labels/menu/m-work", {})
    assert r.status_code == 200
    assert "they keep their other labels" in r.text
    assert "Any message left with none moves to Archive" in r.text
    assert "No mail is deleted" in r.text
    assert 'data-role="lm-delete"' in r.text


def test_the_menu_preselects_the_parent_a_label_is_actually_nested_under(app, fake):
    """A chooser that opens on "Top level" for a nested label is a control
    that lies about the state it is showing — and the next thing the reader
    does with it un-nests the label by accident.
    """
    fake.mailboxes.append(_mailbox("m-design", "Design", parent_id="m-work"))
    client = _login(app)

    nested = _post(client, "/labels/menu/m-design", {}).text
    chosen = re.search(r'<option value="([^"]*)" selected>', nested)
    assert chosen is not None and chosen.group(1) == "m-work"

    top = _post(client, "/labels/menu/m-receipts", {}).text
    chosen = re.search(r'<option value="([^"]*)" selected>', top)
    assert chosen is not None and chosen.group(1) == ""
    # ...and a label is never offered itself as its own parent.
    assert 'value="m-receipts"' not in top


def test_the_menu_refuses_to_offer_delete_for_a_parent_label(app, fake):
    fake.mailboxes.append(_mailbox("m-design", "Design", parent_id="m-work"))
    client = _login(app)
    r = _post(client, "/labels/menu/m-work", {})
    assert "nested label" in r.text
    assert 'data-role="lm-delete"' not in r.text


def test_the_menu_for_a_label_deleted_elsewhere_explains_itself(app, fake):
    """An orphan the other way round: the nav was rendered, then the label
    went away. The popover must say so rather than swap in emptiness.
    """
    client = _login(app)
    fake.mailboxes = [m for m in fake.mailboxes if m.id != "m-work"]
    r = _post(client, "/labels/menu/m-work", {})
    assert r.status_code == 200
    assert "no longer exists" in r.text
    assert 'data-role="lm-close"' in r.text


# ---------------------------------------------------------------------------
# Apply and move
# ---------------------------------------------------------------------------


def test_apply_adds_and_removes_in_one_request_and_offers_undo(app, fake):
    client = _login(app)
    fake.calls.clear()
    r = _post(
        client,
        "/labels/apply",
        {"ids": ["e1", "e2"], "add": ["m-receipts"], "remove": ["m-work"]},
    )
    assert r.status_code == 204
    done = _trigger(r, "om:done")
    assert done["toast"] == "Labels updated"
    assert done["undo"], "a label change has to be undoable"
    assert done["removed"] == []

    writes = [c for c in fake.calls if c.name == "set_mailboxes_patch"]
    assert len(writes) == 1
    assert fake.placement["e1"] == {"mb-inbox", "m-receipts"}
    assert fake.placement["e2"] == {"mb-inbox", "m-receipts"}


def test_the_undo_token_reverses_through_the_existing_undo_route(app, fake):
    """One undo implementation, not two: the token this route signs is the
    same `UndoSpec` `POST /a/undo` verifies.
    """
    client = _login(app)
    r = _post(client, "/labels/apply", {"ids": ["e2"], "add": ["m-work"]})
    token = _trigger(r, "om:done")["undo"]
    assert fake.placement["e2"] == {"mb-inbox", "m-work"}

    undone = _post(client, "/a/undo", {"token": token})
    assert undone.status_code == 204
    assert fake.placement["e2"] == {"mb-inbox"}


def test_a_partial_apply_never_reports_success(app, fake):
    client = _login(app)
    fake.partial_after = ["e1"]
    r = _post(client, "/labels/apply", {"ids": ["e1", "e2", "e3"], "add": ["m-receipts"]})
    assert r.status_code == 200
    error = _trigger(r, "om:error")
    assert error["toast"] == "Only 1 of 2 messages could be updated."
    assert error["refresh"] is True
    assert _trigger(r, "om:done") is None


def test_applying_a_label_that_no_longer_exists_is_refused_before_any_write(app, fake):
    client = _login(app)
    before = len(fake.named("set_mailboxes_patch"))
    r = _post(client, "/labels/apply", {"ids": ["e1"], "add": ["m-gone"]})
    assert _trigger(r, "om:error")["toast"] == "One of those labels no longer exists."
    assert len(fake.named("set_mailboxes_patch")) == before


def test_move_replaces_membership_and_names_the_rows_that_leave(app, fake):
    client = _login(app)
    r = _post(client, "/labels/move", {"ids": ["e3"], "to": "m-work"})
    assert r.status_code == 204
    done = _trigger(r, "om:done")
    assert done["toast"] == "Moved to “Work”"
    assert done["removed"] == ["t-e3"]
    assert fake.placement["e3"] == {"m-work"}


def test_move_accepts_a_reserved_nav_key(app, fake):
    client = _login(app)
    r = _post(client, "/labels/move", {"ids": ["e1"], "to": "archive"})
    assert _trigger(r, "om:done")["toast"] == "Moved to “Archive”"
    assert fake.placement["e1"] == {"mb-archive"}


def test_move_refuses_a_key_this_account_has_no_mailbox_for(app, fake):
    client = _login(app)
    before = len(fake.named("set_mailboxes_patch"))
    r = _post(client, "/labels/move", {"ids": ["e1"], "to": "starred"})
    assert "isn't available" in _trigger(r, "om:error")["toast"]
    assert len(fake.named("set_mailboxes_patch")) == before


# ---------------------------------------------------------------------------
# Orphaned LabelMeta rows
# ---------------------------------------------------------------------------


def test_an_orphaned_row_renders_no_ghost_and_is_cleaned_up_by_the_picker(app, sqlite_url, fake):
    """A label deleted in another client leaves a row keyed on an id that no
    longer resolves. The nav must be unaffected (it walks mailboxes, not
    rows), and the row must not linger to be inherited by whatever mailbox
    the server gives that id to next.
    """
    client = _login(app)
    _seed_meta(
        sqlite_url,
        [
            {"mailbox_id": "m-work", "color": "teal"},
            {"mailbox_id": "m-deleted-in-thunderbird", "color": "rose", "visibility": "hide"},
        ],
    )

    body = client.get("/mail/inbox").text
    assert body.count("nav-item") > 0
    assert "m-deleted-in-thunderbird" not in body
    # The nav render is the hot path and must not have written anything.
    assert len(_meta_rows(sqlite_url)) == 2

    _post(client, "/labels/picker", {"ids": ["e1"], "mode": "label"})
    assert {row.mailbox_id for row in _meta_rows(sqlite_url)} == {"m-work"}


# ---------------------------------------------------------------------------
# The client half
# ---------------------------------------------------------------------------


def _labels_js() -> str:
    return LABELS_JS.read_text()


def test_the_nav_offers_a_create_button_and_a_menu_per_label(app):
    body = _login(app).get("/mail/inbox").text
    assert body.count("data-label-new") == 1
    # One per rendered label row, and the nav renders both labels.
    assert body.count("data-label-menu=") == 2
    assert 'data-label-menu="m-work"' in body


def test_the_labels_module_is_loaded_by_the_app_layout(app):
    body = _login(app).get("/mail/inbox").text
    assert re.search(r'<script src="/static/js/labels\.js\?v=\w+" type="module">', body), body


def test_the_client_reaches_the_keyboard_registry_by_import_not_by_tag():
    """`l` and `v` have to open the same two popovers the toolbar buttons do.

    The hazard this guards is not importing the registry — four other entry
    points already do — it is *tagging* a file that is also imported. A
    `<script>`'s versioned `?v=` URL and an `import`'s unversioned one are
    two module records for the same file, so the registry would exist twice
    and every handler would fire twice. `keys.js` therefore has no tag of
    its own, and that is what this asserts.
    """
    source = _labels_js()
    assert 'from "./keys.js"' in source
    assert "registerLabels(" in source
    layout = (
        pathlib.Path(__file__).resolve().parents[2] / "mailosh/web/templates/layouts/app.html"
    ).read_text()
    assert "js/keys.js'" not in layout and 'js/keys.js"' not in layout


def test_the_client_posts_a_csrf_token_and_declares_itself_to_htmx():
    source = _labels_js()
    assert '"X-CSRF-Token": csrfToken()' in source
    assert '"HX-Request": "true"' in source


def test_the_client_carries_no_alpine_or_htmx_expression_attribute():
    """The app's CSP is `script-src 'self'` with no `'unsafe-eval'`, which
    rules out `hx-on:`, `hx-vals='js:…'` and `hx-trigger` filters — all
    three compile attribute text with `new Function` — as well as every
    Alpine directive that would need the evaluator.
    """
    for template in (PICKER_HTML, MENU_HTML, NAV_HTML):
        # Jinja comments are stripped first: this file's own header says
        # which three attribute forms the CSP forbids, and a test that
        # matched its own explanation would pass for the wrong reason.
        markup = re.sub(r"\{#.*?#\}", "", template.read_text(), flags=re.S)
        for banned in ("hx-on:", "hx-vals=", "x-data", "x-show", "onclick="):
            assert banned not in markup, (template.name, banned)


def test_the_create_row_is_offered_outside_move_mode_only():
    source = _labels_js()
    assert 'panel.dataset.mode !== "move"' in source


def test_the_client_hands_a_completed_mutation_to_the_existing_toast_machinery():
    """One answer to "what does a finished mutation look like": `actions.js`
    already draws the toast, wires Undo to `/a/undo`, removes the rows the
    server named and moves the nav badges.
    """
    source = _labels_js()
    assert 'window.htmx?.trigger?.(document.body, "om:done", done)' in source
    assert 'trigger(response, "om:error")' in source


def test_the_client_refreshes_rather_than_guessing_after_a_failure():
    source = _labels_js()
    failure = source[source.index("async function send(") :]
    failure = failure[: failure.index("\n// ---")]
    assert failure.count("refresh()") >= 2
