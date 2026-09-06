"""Unit tests for the ⌘K command palette's data endpoint (Task 11, design
spec §6.1/§6.2): `mailosh.web.palette`'s `ACTIONS`/`SETTINGS` constants, the
`_goto_entries`/`_label_entries` nav-shaping helpers, and `GET /palette/index`
mounted on a bare `FastAPI()` (this router is not registered in
`mailosh.web.app.create_app` — a later dispatch does that for every new
Task 11/12 router at once).

Two layers, deliberately: `_goto_entries`/`_label_entries` are pure functions
of a `NavModel`, so the interesting hide/show-if-unread/nesting logic is
covered there directly (fast, no HTTP, no database) against a `NavModel`
built the same way `tests/unit/test_mailbox_tree.py` builds one — a real
`build_nav` over a duck-typed fake `JmapClient`. The route-level tests then
only have to prove the wiring: a real database-backed `label_meta_map`
lookup feeding a real `build_nav` feeding these same helpers, end to end.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.db.base import Base
from mailosh.db.models import LabelMeta
from mailosh.jmap.models import Mailbox
from mailosh.services.mailbox_tree import build_nav
from mailosh.ui.format import LABEL_COLORS
from mailosh.web import deps, palette

ACCOUNT = "acct-palette"

# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


def _mailbox(
    mailbox_id: str,
    name: str,
    *,
    role: str | None = None,
    parent_id: str | None = None,
    sort_order: int = 0,
    total: int = 0,
    unread: int = 0,
) -> Mailbox:
    return Mailbox(
        id=mailbox_id,
        name=name,
        parent_id=parent_id,
        role=role,
        sort_order=sort_order,
        total_emails=total,
        unread_emails=unread,
    )


#: The six role mailboxes `build_nav`'s system/more spec names, so every
#: goto entry has something to resolve against.
_ROLE_MAILBOXES = [
    _mailbox("mb-inbox", "Inbox", role="inbox", sort_order=10, total=9, unread=3),
    _mailbox("mb-sent", "Sent", role="sent", sort_order=20, total=4),
    _mailbox("mb-drafts", "Drafts", role="drafts", sort_order=30, total=2),
    _mailbox("mb-archive", "Archive", role="archive", sort_order=50, total=7),
    _mailbox("mb-junk", "Spam", role="junk", sort_order=60),
    _mailbox("mb-trash", "Trash", role="trash", sort_order=70, total=1),
]

#: A label tree covering every visibility case: "Work" (shown, coloured) has
#: a nested unshown-metadata child ("Design", visible by default); "Quiet"
#: is `show_if_unread` with zero unread (hidden); "Secret" is `hide`
#: (structurally dropped by `build_nav` itself, never even reaching
#: `hidden_in_nav`).
_LABEL_MAILBOXES = [
    _mailbox("m-work", "Work", sort_order=100, total=27, unread=3),
    _mailbox("m-work-design", "Design", parent_id="m-work", sort_order=110, total=9, unread=0),
    _mailbox("m-quiet", "Newsletters Quiet", sort_order=120, total=5, unread=0),
    _mailbox("m-secret", "Secret", sort_order=130, total=3, unread=1),
]

_LABEL_META = {
    "m-work": LabelMeta(color="indigo", visibility="show"),
    "m-quiet": LabelMeta(color="amber", visibility="show_if_unread"),
    "m-secret": LabelMeta(color="rose", visibility="hide"),
}


class FakeClient:
    """Stands in for `JmapClient`: `build_nav` only ever calls
    `get_mailboxes()`, and the route only additionally reads `account_id`.
    """

    def __init__(self, mailboxes: list[Mailbox]) -> None:
        self._mailboxes = mailboxes

    @property
    def account_id(self) -> str:
        return ACCOUNT

    async def get_mailboxes(self) -> list[Mailbox]:
        return self._mailboxes


async def _nav(mailboxes: list[Mailbox], label_meta: dict[str, LabelMeta] | None = None):
    return await build_nav(FakeClient(mailboxes), active_key="", label_meta=label_meta or {})


# ---------------------------------------------------------------------------
# ACTIONS / SETTINGS — the fixed, Python-owned constants
# ---------------------------------------------------------------------------


def test_actions_are_exactly_the_six_implemented_mutations_and_never_snooze():
    # Cardinality *and* membership, not a substring check: deleting the
    # guard against a stray "snooze" entry must fail this even if some
    # other, unrelated action still happens to be present.
    assert {a.id for a in palette.ACTIONS} == {
        "archive",
        "delete",
        "spam",
        "star",
        "mark-read",
        "mark-unread",
    }
    blob = json.dumps([a.model_dump() for a in palette.ACTIONS]).lower()
    assert "snooze" not in blob
    assert "important" not in blob


def test_every_action_needs_a_selection_kind_and_at_least_one_key():
    assert len(palette.ACTIONS) == 6
    for action in palette.ACTIONS:
        assert action.kind == "action"
        assert action.needs == "selection"
        assert len(action.keys) >= 1
    # ids are unique -- two commands sharing an id would collide in any
    # client-side keyed list/index.
    assert len({a.id for a in palette.ACTIONS}) == len(palette.ACTIONS)


def test_read_and_unread_are_distinct_commands_with_distinct_keys():
    by_id = {a.id for a in palette.ACTIONS}
    assert {"mark-read", "mark-unread"} <= by_id
    read = next(a for a in palette.ACTIONS if a.id == "mark-read")
    unread = next(a for a in palette.ACTIONS if a.id == "mark-unread")
    assert read.keys != unread.keys


def test_settings_cover_every_legal_prefs_value_exactly_once():
    # Task 12's own `POST /prefs` interface: theme (system|light|dark),
    # density (compact|standard|comfortable), shortcuts (true|false).
    by_id = {s.id: s for s in palette.SETTINGS}
    assert len(by_id) == len(palette.SETTINGS)  # every id unique

    toggles = [s for s in palette.SETTINGS if s.href is None]
    assert len(toggles) == 8

    themes = {s.values["theme"] for s in toggles if "theme" in s.values}
    assert themes == {"system", "light", "dark"}

    densities = {s.values["density"] for s in toggles if "density" in s.values}
    assert densities == {"compact", "standard", "comfortable"}

    shortcuts = {s.values["shortcuts"] for s in toggles if "shortcuts" in s.values}
    assert shortcuts == {True, False}

    assert all(s.post == "/prefs" for s in toggles)
    # Every value dict is single-field: one POST flips exactly one toggle.
    assert all(len(s.values) == 1 for s in toggles)


def test_every_settings_page_is_reachable_from_the_palette():
    """The Settings group holds two shapes: the quick toggles above, and
    one entry per full page. Built from `mailosh.web.settings.PAGES`, so a
    seventh page cannot exist without being findable from Cmd+K."""
    from mailosh.web.settings import PAGES

    pages = [s for s in palette.SETTINGS if s.href is not None]
    assert [s.href for s in pages] == [f"/settings/{key}" for key, _label in PAGES]
    assert [s.label for s in pages] == [f"Settings: {label}" for _key, label in PAGES]
    # A page navigates; it never posts a preference.
    assert all(s.post == "" and s.values == {} for s in pages)


# ---------------------------------------------------------------------------
# _goto_entries / _label_entries — pure functions of a NavModel
# ---------------------------------------------------------------------------


async def test_goto_lists_all_eight_system_and_more_items_with_the_five_g_sequences():
    nav = await _nav(_ROLE_MAILBOXES)
    entries = palette._goto_entries(nav)
    by_id = {e.id: e for e in entries}

    assert {e.id for e in entries} == {
        "goto:inbox",
        "goto:starred",
        "goto:sent",
        "goto:drafts",
        "goto:all",
        "goto:archive",
        "goto:spam",
        "goto:trash",
    }
    assert by_id["goto:inbox"].keys == ["g", "i"]
    assert by_id["goto:inbox"].href == "/mail/inbox"
    assert by_id["goto:starred"].keys == ["g", "s"]
    assert by_id["goto:sent"].keys == ["g", "t"]
    assert by_id["goto:drafts"].keys == ["g", "d"]
    assert by_id["goto:all"].keys == ["g", "a"]
    # No documented sequence for these three -- still present, just unkeyed.
    assert by_id["goto:archive"].keys == []
    assert by_id["goto:spam"].keys == []
    assert by_id["goto:trash"].keys == []


async def test_goto_and_labels_exclude_hidden_labels_but_keep_visible_and_nested_ones():
    nav = await _nav(_ROLE_MAILBOXES + _LABEL_MAILBOXES, _LABEL_META)

    goto_ids = {e.id for e in palette._goto_entries(nav)}
    label_ids = {e.id for e in palette._label_entries(nav)}

    # "Secret" (hide) never reaches the tree at all; "Quiet" (show_if_unread,
    # zero unread) reaches it but `hidden_in_nav` skips its row -- both must
    # be absent from *both* groups.
    for hidden_id in ("m-secret", "m-quiet"):
        assert f"goto:{hidden_id}" not in goto_ids
        assert hidden_id not in label_ids

    # "Work" (visible) and its child "Design" (visible, no LabelMeta row of
    # its own -- defaults to shown) are both present, independent of any
    # parent/child relationship.
    assert {"goto:m-work", "goto:m-work-design"} <= goto_ids
    assert {"m-work", "m-work-design"} <= label_ids


async def test_label_entries_use_the_bare_mailbox_id_not_a_goto_prefixed_one():
    nav = await _nav(_ROLE_MAILBOXES + _LABEL_MAILBOXES, _LABEL_META)
    labels = {entry.id: entry for entry in palette._label_entries(nav)}
    assert labels["m-work"].label == "Work"
    assert labels["m-work"].color == "indigo"
    assert "goto:m-work" not in labels


async def test_label_color_is_narrowed_and_falls_back_to_a_seeded_palette_name():
    hostile = {"m-work": LabelMeta(color="javascript:alert(1)", visibility="show")}
    nav = await _nav([*_ROLE_MAILBOXES, _LABEL_MAILBOXES[0]], hostile)
    (entry,) = palette._label_entries(nav)
    # The hostile string never reaches the client -- it's narrowed to one of
    # the 12 known palette names, deterministically (same seed every time).
    assert entry.color in LABEL_COLORS
    assert entry.color != "javascript:alert(1)"
    again = palette._label_entries(await _nav([*_ROLE_MAILBOXES, _LABEL_MAILBOXES[0]], hostile))
    assert again[0].color == entry.color


# ---------------------------------------------------------------------------
# GET /palette/index -- the router mounted on a bare FastAPI()
# ---------------------------------------------------------------------------


def _prepare_db(sqlite_url: str, rows: list[LabelMeta] | None = None) -> None:
    """Create the schema (and, optionally, seed `LabelMeta` rows) on
    `sqlite_url` via a throwaway engine that is fully closed before any
    `TestClient` request runs -- the same "separate engine on the same
    file" shape `tests/unit/test_mail_routes.py::_seed_label_meta` uses, for
    the same reason: nothing here may share a connection with whatever loop
    a later `TestClient` request ends up running on.
    """

    async def go() -> None:
        engine = create_async_engine(sqlite_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if rows:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                session.add_all(rows)
                await session.commit()
        await engine.dispose()

    asyncio.run(go())


def _db_override(sqlite_url: str):
    """A `deps.get_db` replacement that opens a brand new engine/session on
    every call, scoped entirely to whatever loop is calling it -- so it is
    safe regardless of which loop `TestClient` happens to dispatch a given
    request on, with no engine/connection ever shared across one.
    """

    async def get_db():
        engine = create_async_engine(sqlite_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            yield session
        await engine.dispose()

    return get_db


def _make_app(fake: FakeClient, sqlite_url: str, *, user_id: int = 1) -> FastAPI:
    app = FastAPI()
    app.include_router(palette.router)
    app.dependency_overrides[deps.client_for] = lambda: fake
    app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id=user_id)
    app.dependency_overrides[deps.get_db] = _db_override(sqlite_url)
    return app


def test_index_route_returns_the_four_groups(sqlite_url):
    _prepare_db(sqlite_url)
    fake = FakeClient([*_ROLE_MAILBOXES, _LABEL_MAILBOXES[0]])  # role boxes + "Work"

    response = TestClient(_make_app(fake, sqlite_url)).get("/palette/index")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body.keys()) == {"actions", "goto", "labels", "settings"}
    assert body["actions"] == [a.model_dump() for a in palette.ACTIONS]
    assert body["settings"] == [s.model_dump() for s in palette.SETTINGS]
    assert {g["id"] for g in body["goto"]} >= {"goto:inbox", "goto:m-work"}
    # No `LabelMeta` row was seeded for "Work" here (that scenario is
    # `test_index_route_excludes_a_really_hidden_label_end_to_end`'s job) --
    # this only checks that an uncoloured label still gets *some* narrowed,
    # non-null palette colour rather than `None` reaching the client.
    assert len(body["labels"]) == 1
    assert body["labels"][0]["id"] == "m-work"
    assert body["labels"][0]["label"] == "Work"
    assert body["labels"][0]["color"] in LABEL_COLORS


def test_index_route_excludes_a_really_hidden_label_end_to_end(sqlite_url):
    """The one test that goes through a *real* database row rather than a
    hand-built `label_meta` dict: `LabelMeta.visibility="hide"` persisted,
    read back by the real `repo.label_meta_map`, and filtered by the real
    `build_nav` + `hidden_in_nav` -- proving the whole chain, not just the
    pure-function half of it.
    """
    secret_row = LabelMeta(
        user_id=1, account_id=ACCOUNT, mailbox_id="m-secret", visibility="hide", color="rose"
    )
    visible_row = LabelMeta(
        user_id=1, account_id=ACCOUNT, mailbox_id="m-work", visibility="show", color="indigo"
    )
    _prepare_db(sqlite_url, [secret_row, visible_row])
    fake = FakeClient(_ROLE_MAILBOXES + _LABEL_MAILBOXES)

    response = TestClient(_make_app(fake, sqlite_url)).get("/palette/index")

    assert response.status_code == 200
    raw = response.text
    assert "Secret" not in raw
    assert "m-secret" not in raw
    assert "Work" in raw and "m-work" in raw


def test_index_route_is_the_only_route_and_get_only(sqlite_url):
    paths = {route.path: route.methods for route in palette.router.routes}
    assert paths == {"/palette/index": {"GET"}}

    _prepare_db(sqlite_url)
    fake = FakeClient(_ROLE_MAILBOXES)
    assert TestClient(_make_app(fake, sqlite_url)).post("/palette/index").status_code == 405
