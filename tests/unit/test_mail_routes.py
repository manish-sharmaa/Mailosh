"""HTTP-level tests for the list-first mail shell (Task 8, design spec
§4.3/§5/§6.4): `GET /` -> `/mail/inbox`, the full page vs. the `#main`
fragment, the nav model rendered into `shell/nav.html`, the row contract
`list/row.html` owes every later task, the endless-scroll sentinel, and the
"unknown mailbox key never reaches a JMAP filter" guard.

Everything runs against a real `create_app` (real routers, real Jinja
environment, real session/CSRF plumbing over a file-backed aiosqlite db) with
exactly two things faked: `mailosh.web.auth.verify_password` (no Stalwart) and
`deps.client_for` (a `FakeClient` in place of the pooled `JmapClient`). No
network call is ever made, and `FakeClient` records every `query_page` call so
a test can assert one was *not* made.
"""

from __future__ import annotations

import asyncio
import collections
import inspect
import json
import pathlib
import re
import typing
from datetime import UTC, datetime

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from helpers import parse_attrs
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.db.models import AppUser, LabelMeta
from mailosh.jmap.client import QueryPage
from mailosh.jmap.models import Address, EmailBody, EmailHeader, Mailbox
from mailosh.security.exchange import VerifiedAccount
from mailosh.services import mailbox_tree
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps, palette, prefs
from mailosh.web.app import create_app

ACCOUNT = "acct-1"
ME = "d@x"


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


def _mailbox(
    mailbox_id: str,
    name: str,
    role: str | None,
    sort_order: int,
    *,
    total: int = 0,
    unread: int = 0,
) -> Mailbox:
    return Mailbox(
        id=mailbox_id,
        name=name,
        role=role,
        sort_order=sort_order,
        total_emails=total,
        unread_emails=unread,
    )


def _mailboxes() -> list[Mailbox]:
    """Six role mailboxes plus three user labels."""
    return [
        _mailbox("mb-inbox", "Inbox", "inbox", 10, total=9, unread=12),
        _mailbox("mb-sent", "Sent", "sent", 20, total=4),
        _mailbox("mb-drafts", "Drafts", "drafts", 30, total=2),
        _mailbox("mb-archive", "Archive", "archive", 50, total=7),
        _mailbox("mb-junk", "Spam", "junk", 60),
        _mailbox("mb-trash", "Trash", "trash", 70, total=1),
        _mailbox("m-work", "Work", None, 80, total=4, unread=3),
        _mailbox("m-quiet", "Quiet", None, 81, total=6),
        _mailbox("m-loud", "Loud", None, 82, total=6, unread=2),
    ]


def _header(
    email_id: str,
    thread_id: str,
    *,
    mailbox_ids: set[str] | None = None,
    subject: str = "Q3 roadmap review",
    keywords: set[str] | None = None,
    has_attachment: bool = False,
    minute: int = 0,
) -> EmailHeader:
    return EmailHeader(
        id=email_id,
        thread_id=thread_id,
        mailbox_ids=mailbox_ids or {"mb-inbox"},
        keywords=keywords if keywords is not None else set(),
        from_=[Address(name="Priya Natarajan", email="priya@example.com")],
        subject=subject,
        received_at=datetime(2026, 9, 2, 10, minute, tzinfo=UTC),
        preview="Attaching the deck we walked through; owners are tagged inline",
        has_attachment=has_attachment,
    )


#: One unread thread whose subject is deliberately raw HTML (the escaping
#: assertion below), one read thread with two *scoped* messages out of three
#: in the whole JMAP thread (so `count` != `len(email_ids)` has teeth) plus a
#: label the nav knows about, so it renders a chip.
def _default_threads() -> dict[str, list[EmailHeader]]:
    return {
        "t1": [_header("e1", "t1", subject="Spike <b>subject</b>", has_attachment=True)],
        "t2": [
            _header("e2", "t2", mailbox_ids={"mb-inbox", "m-work"}, keywords={"$seen"}, minute=1),
            _header("e3", "t2", mailbox_ids={"mb-inbox", "m-work"}, keywords={"$seen"}, minute=2),
            # Archived member of the same conversation: in `email_ids`, out of
            # the Inbox row's own `count`.
            _header("e4", "t2", mailbox_ids={"mb-archive"}, keywords={"$seen"}, minute=3),
        ],
    }


class FakeClient:
    """Stands in for the pooled `JmapClient`. Records every `query_page` call
    so "this route must not reach a JMAP filter" is assertable, and serves a
    fixed page of threads regardless of the filter it is handed (the filter
    itself is `tests/unit/test_thread_list.py`'s subject, not this module's).
    """

    def __init__(
        self,
        *,
        threads: dict[str, list[EmailHeader]] | None = None,
        total: int | None = None,
        mailboxes: list[Mailbox] | None = None,
    ) -> None:
        self.threads = _default_threads() if threads is None else threads
        self.mailboxes = _mailboxes() if mailboxes is None else mailboxes
        self.total = len(self.threads) if total is None else total
        self.queries: list[dict[str, object]] = []
        self.thread_calls: list[str] = []
        #: Whether `query_page` honours `position`/`limit` instead of serving
        #: every registered thread whatever was asked for. Off by default
        #: because most tests here set a `total` far larger than the handful
        #: of threads they register (a 120-row mailbox rendered from two),
        #: and slicing that would hand them an empty page. The routes that
        #: resolve a *position* to one conversation need the real thing, and
        #: turn it on.
        self.paged = False

    @property
    def account_id(self) -> str:
        return ACCOUNT

    async def get_mailboxes(self) -> list[Mailbox]:
        return self.mailboxes

    async def query_page(self, **kwargs: object) -> QueryPage:
        self.queries.append(kwargs)
        position = int(kwargs["position"])  # type: ignore[arg-type]
        order = list(self.threads)
        if self.paged:
            order = order[position : position + int(kwargs["limit"])]  # type: ignore[arg-type]
        return QueryPage(
            thread_order=order,
            total=self.total,
            emails_by_thread=self.threads,
            position=position,
        )

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
        self.thread_calls.append(thread_id)
        if thread_id not in self.threads:
            return []
        return [
            EmailBody(
                id=header.id,
                thread_id=header.thread_id,
                mailbox_ids=header.mailbox_ids,
                keywords=header.keywords,
                from_=header.from_,
                to=[Address(name="Demo", email=ME)],
                subject=header.subject,
                received_at=header.received_at,
                preview=header.preview,
                has_attachment=header.has_attachment,
                text_body="Sounds good.\n\n> earlier line\n> - Demo",
            )
            for header in self.threads[thread_id]
        ]


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


def _seed_label_meta(sqlite_url: str, rows: list[dict[str, object]]) -> None:
    """Insert `LabelMeta` rows for the (already logged-in) user, over a
    throwaway engine on the same sqlite file — the app's own engine belongs
    to the `TestClient` portal's event loop, so it cannot be borrowed from a
    synchronous test.
    """

    async def go() -> None:
        engine = create_async_engine(sqlite_url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = (await db.execute(select(AppUser))).scalars().one()
            for row in rows:
                db.add(LabelMeta(user_id=user.id, account_id=ACCOUNT, **row))
            await db.commit()
        await engine.dispose()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# GET / and the login landing target
# ---------------------------------------------------------------------------


def test_root_redirects_to_the_inbox(app):
    r = TestClient(app, follow_redirects=False).get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/mail/inbox"


def test_login_without_next_lands_on_the_inbox(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": ME, "password": "right"})
    assert r.headers["location"] == "/mail/inbox"


# ---------------------------------------------------------------------------
# GET /mail/{key}: full page vs. #main fragment
# ---------------------------------------------------------------------------


def test_full_page_renders_the_whole_shell(app):
    r = _login(app).get("/mail/inbox")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()
    assert 'id="main"' in r.text
    assert 'id="list"' in r.text
    assert 'name="csrf-token"' in r.text


def test_hx_request_returns_only_the_main_fragment(app):
    r = _login(app).get("/mail/inbox", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text
    assert 'id="list"' in r.text
    # htmx picks the new document title out of the fragment, and pushes the
    # URL from the header even for a caller that forgot `hx-push-url`.
    assert "<title>" in r.text
    assert r.headers["HX-Push-Url"] == "/mail/inbox"


def test_hx_history_restore_gets_a_full_page(app):
    r = _login(app).get(
        "/mail/inbox",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<html" in r.text


# ---------------------------------------------------------------------------
# shell/nav.html
# ---------------------------------------------------------------------------


def test_nav_renders_every_system_item_and_no_deferred_feature(app):
    body = _login(app).get("/mail/inbox").text
    for label in ("Inbox", "Starred", "Sent", "Drafts", "All mail", "Archive", "Spam", "Trash"):
        assert f">{label}<" in body or f">{label}" in body, label
    # Design spec §3: no control for a deferred feature, ever.
    assert "Snoozed" not in body
    assert "snooze" not in body.lower()


def _nav_link(body: str, href: str) -> str | None:
    """One sidebar row, by its href — `class="nav-item"` is what tells it
    apart from the wordmark, which also links to the inbox."""
    match = re.search(rf'<a href="{re.escape(href)}"\s+class="nav-item".*?</a>', body, re.S)
    return match.group(0) if match else None


def test_nav_marks_the_active_item_and_shows_counts(app):
    body = _login(app).get("/mail/inbox").text
    inbox_item = _nav_link(body, "/mail/inbox")
    assert inbox_item, body[:400]
    assert 'aria-current="page"' in inbox_item
    assert ">12<" in inbox_item  # Inbox unread badge
    # Drafts badges its *total*, not its unread count (spec §5.2).
    drafts_item = _nav_link(body, "/mail/drafts")
    assert drafts_item and ">2<" in drafts_item
    # ...and a mailbox with nothing to report carries no badge at all.
    assert "nav-count" not in (_nav_link(body, "/mail/sent") or "")


def test_nav_renders_label_colours_and_counts(app, sqlite_url):
    c = _login(app)
    _seed_label_meta(sqlite_url, [{"mailbox_id": "m-work", "color": "indigo"}])
    work = _nav_link(c.get("/mail/inbox").text, "/mail/m-work")
    assert work, "Work label row not rendered"
    assert "var(--label-indigo)" in work
    assert ">3<" in work


def test_nav_respects_hidden_in_nav(app, sqlite_url):
    """A `show_if_unread` label at zero unread keeps its chips but loses its
    sidebar row; the same preference on a label that *does* have unread mail
    still renders (`mailbox_tree.hidden_in_nav`).
    """
    c = _login(app)
    _seed_label_meta(
        sqlite_url,
        [
            {"mailbox_id": "m-quiet", "visibility": "show_if_unread"},
            {"mailbox_id": "m-loud", "visibility": "show_if_unread"},
        ],
    )
    body = c.get("/mail/inbox").text
    assert 'href="/mail/m-quiet"' not in body
    assert 'href="/mail/m-loud"' in body


# ---------------------------------------------------------------------------
# list/row.html — the contract every later task binds to
# ---------------------------------------------------------------------------


def _row_html(body: str, thread_id: str) -> str:
    """Everything between a row's opening `<div>` and the next `<div>` in the
    document — exact, because a row contains no nested `<div>` of its own.
    """
    marker = f'<div id="row-{thread_id}"'
    assert marker in body, f"row {thread_id} not found in: {body[:600]}"
    start = body.index(marker)
    following = re.search(r"<div\b", body[start + 1 :])
    end = start + 1 + following.start() if following else len(body)
    return body[start:end]


def test_rows_carry_the_contract_attributes(app):
    body = _login(app).get("/mail/inbox").text
    assert 'role="grid"' in body
    assert 'aria-multiselectable="true"' in body

    row = _row_html(body, "t1")
    assert 'role="row"' in row
    assert 'data-id="t1"' in row
    assert 'data-email-ids="e1"' in row
    assert 'aria-selected="false"' in row
    # The row's primary target is a real anchor (review finding B2), so
    # Cmd/middle-click, "open in new tab" and a JS-off click all work.
    # `hx-boost` rather than `hx-get`: htmx hands a modifier-click on a
    # *boosted* anchor back to the browser instead of preventing it.
    # ...and it carries where the row sat, which is the only thing that
    # knows: `/t/{id}` would otherwise have to search a mailbox for a
    # thread that genuinely lives in several.
    assert 'href="/t/t1?key=inbox&amp;pos=0"' in row
    assert 'hx-boost="true"' in row
    assert "hx-get=" not in row
    assert 'hx-target="#main"' in row
    assert 'preload="mousedown"' in row
    assert "row-check" in row
    assert 'aria-label="Select conversation"' in row
    assert "row-star" in row
    assert "aria-pressed" in row
    assert '<time class="row-date" datetime="2026-09-02T10:00:00+00:00"' in row
    # Hover actions carry their keyboard shortcut in the tooltip (spec §6.1).
    assert 'title="Archive (e)"' in row
    assert 'title="Delete (#)"' in row


def test_row_marks_unread_and_attachments(app):
    body = _login(app).get("/mail/inbox").text
    unread = _row_html(body, "t1")
    read = _row_html(body, "t2")
    assert "is-unread" in unread
    assert "is-unread" not in read
    # The paperclip is inlined SVG, so the icon's own accessible name is
    # what identifies it — not a class or a file name.
    assert 'aria-label="Has attachment"' in unread
    assert 'aria-label="Has attachment"' not in read


def test_row_subject_is_escaped(app):
    row = _row_html(_login(app).get("/mail/inbox").text, "t1")
    assert "&lt;b&gt;subject&lt;/b&gt;" in row
    assert "<b>subject</b>" not in row


def test_row_count_is_the_scoped_count_not_the_whole_thread(app):
    """`ThreadRow.count` counts the messages in the *viewed* mailbox;
    `email_ids` spans the whole conversation. t2 has three messages, only two
    of them in the Inbox — the row must say 2 and still carry all three ids.
    """
    row = _row_html(_login(app).get("/mail/inbox").text, "t2")
    assert 'data-count="2"' in row
    assert 'data-email-ids="e2,e3,e4"' in row
    assert "(2)" in row  # the sender list's own message count
    assert "(3)" not in row


def test_row_renders_label_chips(app, sqlite_url):
    c = _login(app)
    _seed_label_meta(sqlite_url, [{"mailbox_id": "m-work", "color": "emerald"}])
    row = _row_html(c.get("/mail/inbox").text, "t2")
    assert "var(--label-emerald)" in row
    assert ">Work<" in row


# ---------------------------------------------------------------------------
# GET /mail/{key}/rows and the endless-scroll sentinel
# ---------------------------------------------------------------------------


def test_rows_fragment_has_no_shell(app):
    r = _login(app).get("/mail/inbox/rows?position=0")
    assert r.status_code == 200
    assert "<html" not in r.text
    assert 'id="row-t1"' in r.text


def test_rows_fragment_carries_the_live_title_and_nav(app):
    """This fragment is what a live update arrives through, so it has to bring
    the unread badge and the document title with it (spec §6.5) — otherwise
    new mail grows the list while the sidebar still shows the old count.
    """
    body = _login(app).get("/mail/inbox/rows?position=0").text
    assert "<title>(12) Inbox — Mailosh</title>" in body
    assert '<div id="nav" class="flex flex-col gap-0.5" hx-swap-oob="true">' in body
    # ...and never when the same fragment is included by the full page, where
    # the layout already renders both.
    page = _login(app).get("/mail/inbox").text
    assert "hx-swap-oob" not in page
    assert page.count("<title>") == 1


def test_sentinel_present_only_while_more_pages_remain(app, fake):
    fake.total = 120
    body = _login(app).get("/mail/inbox/rows?position=0&limit=50").text
    assert 'hx-trigger="intersect once root:#list"' in body
    assert "position=50" in body


def test_sentinel_absent_at_the_end_of_the_list(app, fake):
    """The Phase 0 bug: a sentinel that never stops re-arming itself. It must
    not be rendered at all once `next_position` is `None`.
    """
    fake.total = 2
    body = _login(app).get("/mail/inbox/rows?position=0&limit=50").text
    assert "intersect once" not in body

    fake.total = 120
    last = _login(app).get("/mail/inbox/rows?position=100&limit=50").text
    assert "intersect once" not in last


def test_list_container_refreshes_itself_on_mail_changed(app):
    body = _login(app).get("/mail/inbox").text
    container = re.search(r'<div id="list"[^>]*>', body)
    assert container, body[:400]
    assert 'hx-trigger="mail:changed from:body"' in container.group(0)
    assert "/mail/inbox/rows?position=0" in container.group(0)
    assert 'hx-swap="morph:innerHTML show:none"' in container.group(0)


# ---------------------------------------------------------------------------
# Review finding B1: the roving tabindex was never initialised. `list.render()`
# in app.js only runs on `htmx:afterSettle`, which does not fire on a first
# full page load, so the served page contained ZERO `tabindex="0"`: Tab
# skipped the list entirely and stopped on every row's checkbox and star
# instead. The first row of a page render now carries the tab stop in the
# markup, so the list is reachable before (and without) any JS.
# ---------------------------------------------------------------------------


def _row_tags(body: str) -> list[str]:
    return re.findall(r'<div id="row-[^"]*"[^>]*>', body)


def test_rendered_list_has_exactly_one_roving_tab_stop(app):
    rows = _row_tags(_login(app).get("/mail/inbox").text)
    assert len(rows) == 2
    assert [r for r in rows if 'tabindex="0"' in r] == [rows[0]]
    assert all('tabindex="-1"' in r for r in rows[1:])


def test_main_fragment_also_opens_with_a_tab_stop(app):
    """The htmx nav swap replaces the whole list, so it owns the tab stop the
    same way the full page does."""
    rows = _row_tags(_login(app).get("/mail/inbox", headers={"HX-Request": "true"}).text)
    assert sum('tabindex="0"' in r for r in rows) == 1


def test_appended_rows_never_carry_a_second_tab_stop(app):
    """The `/rows` fragment is either the endless-scroll append — where a
    second `tabindex="0"` would be a second tab stop in one grid — or the
    `mail:changed` refresh, which cannot happen with JS off. app.js's roving
    pass owns the tab stop in both cases.
    """
    rows = _row_tags(_login(app).get("/mail/inbox/rows?position=50&limit=50").text)
    assert rows
    assert all('tabindex="-1"' in r for r in rows)


# ---------------------------------------------------------------------------
# Review finding B3: a live update truncated an infinitely-scrolled list.
# `#list` re-GETs itself with the page size it was served with, so after the
# sentinel had grown the list to 150 rows one `mail:changed` morphed it back
# to 50 — clamping scrollTop and re-arming the sentinel on every incoming
# message. app.js grows that `limit` to the number of rows actually rendered;
# these two guard the server contract it depends on. The rewrite itself is
# JS, and this repository has no JS test runner (no Node toolchain).
# ---------------------------------------------------------------------------


def _tags_containing(body: str, needle: str) -> list[str]:
    """Every element start-tag in `body` whose text contains `needle`. No
    attribute value in these templates contains a `>`, so the naive scan is
    exact here.
    """
    return [m.group(0) for m in re.finditer(r"<[a-z]+\b[^>]*>", body) if needle in m.group(0)]


def test_refresh_control_carries_the_limit_hook(app):
    """`syncListLimit` rewrites the `limit` of `document.querySelector(
    '[data-role="refresh"]')` — so the hook has to be on exactly one
    element, and that element has to be the one that actually re-GETs the
    list. The old assertion was `'data-role="refresh"' in body`, which
    stayed green with the hook moved onto the Select-all button (a control
    with no `hx-get` to rewrite at all).
    """
    body = _login(app).get("/mail/inbox").text
    tags = _tags_containing(body, 'data-role="refresh"')
    assert len(tags) == 1, tags
    refresh = tags[0]
    assert refresh.startswith("<button")
    assert 'hx-get="/mail/inbox/rows?position=0&limit=50"' in refresh
    assert 'hx-target="#list"' in refresh

    # One per toolbar — the list's and the selection's — and neither of them
    # re-GETs anything, which is the point: `syncListLimit` looks the
    # refresh hook up by `data-role` and would happily rewrite a `limit`
    # onto a control that has no request to rewrite.
    select_all = _tags_containing(body, 'data-role="select-all"')
    assert len(select_all) == 2, select_all
    assert all("hx-get" not in tag for tag in select_all)


def test_list_refresh_url_round_trips_the_page_it_was_served(app, fake):
    """`#list` re-GETs *itself* on `mail:changed`, so its `hx-get` has to
    name the page size it was served with: `syncListLimit` only *grows*
    that number once the sentinel has appended past it (`rendered <= limit`
    short-circuits), and the route falls back to `PAGE_SIZE` when `limit`
    is absent. Deleting `&limit={{ page.limit }}` from the attribute left
    every other test in the suite green, so this drives a non-default page
    size and then follows the URL the page actually printed.
    """
    client = _login(app)
    body = client.get("/mail/inbox?position=1&limit=7").text
    tags = _tags_containing(body, 'id="list"')
    assert len(tags) == 1, tags
    match = re.search(r'hx-get="([^"]+)"', tags[0])
    assert match is not None, tags[0]
    assert match.group(1) == "/mail/inbox/rows?position=1&limit=7"

    client.get(match.group(1))
    assert fake.queries[-1]["position"] == 1
    assert fake.queries[-1]["limit"] == 7


def test_rows_endpoint_honours_a_grown_limit_up_to_the_ceiling(app, fake):
    client = _login(app)
    client.get("/mail/inbox/rows?position=0&limit=150")
    assert fake.queries[-1]["limit"] == 100
    client.get("/mail/inbox/rows?position=0&limit=100")
    assert fake.queries[-1]["limit"] == 100
    client.get("/mail/inbox/rows?position=50&limit=150")
    assert fake.queries[-1]["position"] == 50


# ---------------------------------------------------------------------------
# Toolbar, empty states
# ---------------------------------------------------------------------------


def test_toolbar_counts_come_from_the_page_total(app, fake):
    fake.total = 1284
    body = _login(app).get("/mail/inbox").text
    assert "1\u20132 of 1,284" in body


def test_empty_inbox_is_all_caught_up(app, fake):
    fake.threads = {}
    fake.total = 0
    body = _login(app).get("/mail/inbox").text
    assert "You&#39;re all caught up" in body or "You're all caught up" in body


def test_other_empty_mailboxes_say_nothing_here(app, fake):
    fake.threads = {}
    fake.total = 0
    body = _login(app).get("/mail/archive").text
    assert "Nothing here" in body
    assert "all caught up" not in body


# ---------------------------------------------------------------------------
# Unknown keys never reach a JMAP filter
# ---------------------------------------------------------------------------


def test_unknown_mailbox_key_is_a_404_page_and_never_queries(app, fake):
    r = _login(app).get("/mail/definitely-not-a-mailbox")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert fake.queries == []


def test_unknown_mailbox_key_on_the_rows_endpoint_never_queries(app, fake):
    r = _login(app).get("/mail/definitely-not-a-mailbox/rows")
    assert r.status_code == 404
    assert fake.queries == []


def test_a_real_label_id_is_a_valid_key(app, fake):
    r = _login(app).get("/mail/m-work")
    assert r.status_code == 200
    assert fake.queries and fake.queries[0]["mailbox_id"] == "m-work"


# ---------------------------------------------------------------------------
# GET /t/{thread_id}
# ---------------------------------------------------------------------------


def test_thread_page_renders_under_the_new_shell(app):
    r = _login(app).get("/t/t2")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()
    # Back arrow with its `u` hint, and the action bar (spec §5.3/§6.1).
    assert 'class="kbd">u<' in r.text
    assert 'title="Archive (e)"' in r.text


def test_thread_back_link_returns_to_the_mailbox_you_came_from(app):
    r = _login(app).get(
        "/t/t2", headers={"HX-Request": "true", "HX-Current-URL": "http://t/mail/m-work"}
    )
    assert 'href="/mail/m-work"' in r.text
    assert "<html" not in r.text
    # ...and that mailbox stays highlighted in the sidebar while you read.
    work = _nav_link(r.text, "/mail/m-work")
    assert work and 'aria-current="page"' in work


def test_thread_falls_back_to_the_inbox_for_a_forged_current_url(app):
    r = _login(app).get(
        "/t/t2", headers={"HX-Request": "true", "HX-Current-URL": "http://t/mail/not-a-mailbox"}
    )
    assert 'href="/mail/inbox"' in r.text
    assert 'href="/mail/not-a-mailbox"' not in r.text
    inbox = _nav_link(r.text, "/mail/inbox")
    assert inbox and 'aria-current="page"' in inbox


def test_unknown_thread_is_a_404(app):
    assert _login(app).get("/t/nope").status_code == 404


def test_the_thread_page_carries_the_message_ids_its_action_bar_posts(app):
    """`/a/{archive,delete,read}` take *message* ids. The page used to carry
    only `data-thread-id`, which is not one — so static/js/actions.js found
    no target and three visibly live buttons did nothing at all.
    """
    body = _login(app).get("/t/t2").text
    scroll = re.search(r'<div class="thread-scroll"[^>]*>', body)
    assert scroll, body[:400]
    ids = re.search(r'data-email-ids="([^"]*)"', scroll.group(0))
    assert ids, scroll.group(0)
    # Exactly this conversation's messages, oldest first — the same set
    # `get_thread` returned, no more (the thread id) and no fewer.
    assert ids.group(1).split(",") == ["e2", "e3", "e4"]
    assert 'data-thread-id="t2"' in scroll.group(0)


def test_the_thread_page_offers_the_way_out_its_own_actions_need(app):
    # Archiving the conversation you are reading has no row to collapse, so
    # actions.js leaves via this control rather than blanking the page.
    body = _login(app).get("/t/t2").text
    back = re.search(r'<a [^>]*data-role="back"[^>]*>', body)
    assert back, body[:400]
    assert 'href="/mail/inbox"' in back.group(0)


# ---------------------------------------------------------------------------
# The toolbar's range readout. It lives outside `#list`, which is all a
# `mail:changed` swap replaces, so every fragment that changes the list has
# to bring it along.
# ---------------------------------------------------------------------------


def _range_span(markup: str):
    return re.search(r'<span id="list-range"[^>]*>([^<]*)</span>', markup)


def test_the_range_is_rendered_once_in_the_toolbar_and_not_out_of_band(app):
    page = _login(app).get("/mail/inbox").text
    assert page.count('id="list-range"') == 1
    span = _range_span(page)
    assert span, page[:400]
    assert span.group(1) == "1\u20132 of 2"
    # The page owns its own toolbar; only the fragment swaps one in.
    assert "hx-swap-oob" not in span.group(0)


def test_the_rows_fragment_brings_the_range_with_it(app, fake):
    """Without this the readout kept claiming "1-2 of 2" over one row after
    an archive, and over three after new mail arrived — the same staleness
    the out-of-band nav already existed to prevent, one element further out.
    """
    client = _login(app)
    assert "1\u20132 of 2" in client.get("/mail/inbox").text

    fake.threads = {"t1": _default_threads()["t1"]}
    fake.total = 1
    span = _range_span(client.get("/mail/inbox/rows?position=0&limit=50").text)
    assert span
    assert 'hx-swap-oob="true"' in span.group(0)
    assert span.group(1) == "1\u20131 of 1"


def test_an_appended_page_measures_its_range_from_the_top_of_the_list(app, fake):
    """The sentinel appends *beneath* rows that are still on screen, so the
    range it carries has to describe the whole list. Measured from the
    appended page's own position it would read "51-..." under a list whose
    first row is still row 1.
    """
    fake.total = 120
    client = _login(app)
    first = client.get("/mail/inbox/rows?position=0&limit=50").text
    sentinel = re.search(r'<div class="row-sentinel"[^>]*>', first)
    assert sentinel, first[:400]
    url = re.search(r'hx-get="([^"]*)"', sentinel.group(0))
    assert url and "start=0" in url.group(1)

    appended = _range_span(client.get(url.group(1)).text)
    assert appended
    # `first` is the start the sentinel carried; `last` is where the
    # appended page ends. (This FakeClient serves the same two threads for
    # every position, which is what makes the two halves independently
    # visible here.)
    assert appended.group(1) == "1\u201352 of 120"


def _row_links(html: str) -> list[str]:
    """Every row's own conversation link, in document order."""
    return [
        attrs["href"]
        for attrs in parse_attrs(html).get("a", [])
        if "row-link" in attrs.get("class", "").split()
    ]


def test_each_row_hands_over_where_it_sat(app, fake):
    """The mailbox and the absolute position, which is the pair `/t/{id}`
    needs before it will render a readout at all — and the only thing that
    knows them is the row that was clicked.
    """
    links = _row_links(_login(app).get("/mail/inbox").text)
    assert links == ["/t/t1?key=inbox&pos=0", "/t/t2?key=inbox&pos=1"]


def test_an_appended_page_numbers_its_rows_from_its_own_position(app, fake):
    """`page.position`, not `start`. The sentinel appends beneath rows that
    are still on screen, and `start` is where the *rendered list* begins —
    numbering from there would make the first appended row row 1 again, and
    every arrow in the conversation it opened would be off by a page.
    """
    fake.total = 120
    client = _login(app)
    appended = client.get("/mail/inbox/rows?position=50&limit=50&start=0").text

    assert _row_links(appended) == ["/t/t1?key=inbox&pos=50", "/t/t2?key=inbox&pos=51"]


def test_a_label_key_reaches_the_link_url_encoded(app, fake):
    """A label is addressed by its own mailbox id, which is opaque and
    server-chosen; nothing may assume it is URL-safe.
    """
    fake.mailboxes = [*fake.mailboxes, _mailbox("m sp ace", "Odd", None, 90)]
    fake.threads = {"t9": [_header("e9", "t9", mailbox_ids={"m sp ace"})]}

    links = _row_links(_login(app).get("/mail/m%20sp%20ace").text)

    assert links == ["/t/t9?key=m%20sp%20ace&pos=0"]


def test_a_page_with_no_start_reports_its_own_range(app, fake):
    # The default, and what the `#list` refresh relies on: it re-fetches
    # from its own position with a limit app.js has already grown to cover
    # everything appended, so the page it gets back IS the whole list.
    fake.total = 120
    span = _range_span(_login(app).get("/mail/inbox/rows?position=50&limit=50").text)
    assert span
    assert span.group(1) == "51\u201352 of 120"


# ---------------------------------------------------------------------------
# Wiring create_app owes the rest of Phase 1A
# ---------------------------------------------------------------------------


def test_actions_router_is_mounted(app):
    # 405 (wrong method), not 404 (route missing) — `mailosh.web.actions`
    # is reachable, which is all this test is proving.
    assert _login(app).get("/a/archive").status_code == 405


# ---------------------------------------------------------------------------
# Task 10: the keyboard registry, the selection model, the `?` overlay and
# the selection toolbar.
#
# There is no JS test runner in this repository (no Node toolchain, Global
# Constraints), so the JavaScript half is held from Python in the only way
# that is exact: `static/js/keys.js` writes its shortcut table as literal
# JSON — see that file's header — and everything below reads it. What these
# check is agreement across files that cannot see each other:
#
#   keys.js <-> palette.py        the six action ids the palette dispatches
#   keys.js <-> app.js            the store methods the runners drive
#   keys.js <-> the templates     `title="Archive (e)"`
#   keys.js <-> actions.js        `om.targets()`
#
# ...plus the grid itself, which is markup and therefore testable directly.
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parents[2]
KEYS_JS = REPO / "mailosh/web/static/js/keys.js"
LIST_APP_JS = REPO / "mailosh/web/static/js/app.js"
LIST_ACTIONS_JS = REPO / "mailosh/web/static/js/actions.js"
INPUT_CSS = REPO / "styles/input.css"
BUILT_CSS = REPO / "mailosh/web/static/app.css"
TEMPLATE_FILES = sorted((REPO / "mailosh/web/templates").rglob("*.html"))

#: Scopes `keys.js` recognises (spec §6.1: "global < list < thread < compose
#: < dialog").
SCOPES = {"global", "list", "thread", "compose", "dialog"}

#: `data-action` hook -> the registry id that owns its key. The two names
#: differ for read/unread on purpose: the hook is the *route direction*
#: (`read`/`unread`, folded into the kind by `actions.js`), the registry id
#: is the palette's (`mark-read`/`mark-unread`).
ACTION_KEY_IDS = {
    "archive": "archive",
    "delete": "delete",
    "spam": "spam",
    "star": "star",
    "read": "mark-read",
    "unread": "mark-unread",
    "select": "select",
    # Trash and Spam's readings of the same three keys (`keys.js` `inView`):
    # `e` restores, `#` deletes forever, `!` is "not spam".
    "restore": "archive",
    "destroy": "delete",
    "unspam": "spam",
}

_MODIFIER_NAMES = {"shift", "ctrl", "control", "alt", "meta", "cmd", "mod"}


def _registry() -> list[dict]:
    """`keys.js`'s `DEFAULTS` table, parsed as the JSON it is deliberately
    written as. A regex over JavaScript object literals would be a guess;
    this is exact, and it fails loudly the moment the table stops being
    machine-readable.
    """
    block = re.search(r"^const DEFAULTS = (\[.*?^\]);$", KEYS_JS.read_text(), re.M | re.S)
    assert block is not None, "keys.js no longer declares a DEFAULTS table"
    return json.loads(block.group(1))


def _by_id() -> dict[str, dict]:
    return {entry["id"]: entry for entry in _registry()}


def _js_block(source: str, header: str) -> str:
    """The body of a top-level `function`/`const` block, matched to the
    closing brace in column 0 — minus its comments, for the same reason
    `tests/unit/test_actions_frontend.py` strips `{# … #}` out of a
    template: these files explain themselves at length, and a rule about
    what the code *does* must not be satisfied (or broken) by prose
    describing it. Only whole-line `//` comments and `/* … */` blocks go,
    so a `//` inside a string literal is safe.
    """
    found = re.search(rf"^{header}\s*\{{$(.*?)^\}};?$", source, re.M | re.S)
    assert found is not None, header
    body = re.sub(r"/\*.*?\*/", "", found.group(1), flags=re.S)
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("//"))


def _as_binding(keys: list[str]) -> str:
    """`palette.py`'s key shape in `keys.js`'s notation. The two lists are
    told apart the same way `keys.js` tells a chord from a sequence: a
    leading modifier name means one keystroke (`["shift", "i"]` ->
    `"Shift+I"`), anything else means one keystroke per element
    (`["g", "i"]` -> `"g i"`).
    """
    if keys and keys[0].lower() in _MODIFIER_NAMES:
        return "+".join(part.capitalize() for part in keys)
    return " ".join(keys)


def _strip_comments(markup: str) -> str:
    return re.sub(r"\{#.*?#\}", "", markup, flags=re.S)


def _selection_toolbar(body: str) -> str:
    """The second `.list-toolbar` in a rendered list page. Neither toolbar
    contains a nested `<div>`, so slicing to the next `</div>` is exact.
    """
    first = body.index('class="list-toolbar"')
    at = body.index('class="list-toolbar"', first + 1)
    return body[at : body.index("</div>", at)]


# --- the registry itself ---------------------------------------------------


def test_the_registry_is_internally_consistent():
    entries = _registry()
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids)), "duplicate registry id"

    # Every entry has a runner and every runner has an entry: an id in one
    # and not the other is either a key that throws or dead code nothing
    # can reach.
    runners = _js_block(KEYS_JS.read_text(), "const RUNNERS =")
    named = set(re.findall(r'^  "?([\w-]+)"?:', runners, re.M))
    assert named == set(ids)

    assert {entry["scope"] for entry in entries} <= SCOPES
    order = re.search(r"const GROUP_ORDER = \[(.*?)\];", KEYS_JS.read_text(), re.S)
    assert order is not None
    assert {entry["group"] for entry in entries} <= set(re.findall(r'"([^"]+)"', order.group(1)))
    assert all(entry["keys"] for entry in entries), "an entry with no binding"


def test_a_binding_claimed_twice_is_resolved_by_scope_and_never_by_table_order():
    """Scopes stack, so `list` and `thread` are *both* live on a conversation
    page. A binding claimed by two entries at the *same* scope is genuinely
    ambiguous and forbidden; one claimed at two different scopes is the
    deliberate case — `Shift+U` marks a whole conversation unread from the
    list and marks it unread from this card down while reading one — and
    `lookup` has to settle it by specificity, because settling it by table
    order would make which one runs a property of where a line was pasted.
    """
    entries = _registry()
    for live in ({"global", "list"}, {"global", "list", "thread"}):
        claims: collections.Counter[tuple[str, str]] = collections.Counter()
        for entry in entries:
            if entry["scope"] not in live:
                continue
            for binding in entry["keys"]:
                claims[(binding, entry["scope"])] += 1
        assert [claim for claim, n in claims.items() if n > 1] == [], live

    # Exactly one binding is claimed twice at all, and the two claims are
    # the list/thread pair above. A third would be a design decision, not
    # an accident, and should have to change this number.
    across: collections.Counter[str] = collections.Counter()
    for entry in entries:
        for binding in entry["keys"]:
            across[binding] += 1
    doubled = {binding for binding, n in across.items() if n > 1}
    assert doubled == {"Shift+U"}
    assert {entry["scope"] for entry in entries if "Shift+U" in entry["keys"]} == {"list", "thread"}

    # ...and dispatch keeps the narrowest live scope rather than the first
    # match, which is the only thing that makes the pairing well defined.
    source = KEYS_JS.read_text()
    ranks = re.search(r"const SCOPE_RANK = \{([^}]*)\}", source)
    assert ranks is not None, "keys.js no longer ranks its scopes"
    assert re.findall(r"(\w+): (\d+)", ranks.group(1)) == [
        ("global", "0"),
        ("list", "1"),
        ("thread", "2"),
        # 1C's compose scope, the narrowest of the four. Live only while
        # focus is actually inside a compose window (`activeScopes`), so a
        # dock open in the corner cannot take `Mod+K` away from the
        # palette or `e` away from the list behind it.
        ("compose", "3"),
    ]
    lookup = _js_block(source, r"function lookup\(event, prefix, scopes\)")
    assert "SCOPE_RANK[entry.scope] > SCOPE_RANK[best.entry.scope]" in lookup
    # No early `return` on the first live hit — that is what table order
    # would look like.
    assert "return { entry: entry, active: true }" not in lookup


def test_the_registry_covers_the_map_the_spec_writes_down():
    """Spec §6.1's map, as bindings rather than as ids — an id may be
    renamed, a key may not quietly go missing.
    """
    bound = {binding for entry in _registry() for binding in entry["keys"]}
    assert bound == {
        # list
        "j", "k", "o", "Enter", "u", "x", "Shift+J", "Shift+K",
        "e", "#", "!", "s", "Shift+I", "Shift+U", "l", "v", "[", "]", ".",
        # sequences
        "g i", "g s", "g t", "g d", "g a", "g l",
        "* a", "* n", "* r", "* u", "* s", "* t",
        # global
        "c", "/", "z", "?", "Mod+K", "Escape",
        # thread
        "n", "p", ";", ":", "r", "a", "f",
        # compose (1C). `⌘↵` send, `⌘⇧C`/`⌘⇧B` Cc/Bcc. Spec §6.1's two
        # other compose entries are deliberately absent from the table:
        # `⌘K` (link) and `Esc` (close) are the *same* bindings the global
        # scope already claims, narrowed inside their own runners rather
        # than claimed a second time — see `escape`/`palette` in RUNNERS —
        # so the `?` overlay still describes each key exactly once, and
        # `⌘B/I/U` are Squire's own and never reach dispatch.
        "Mod+Enter", "Mod+Shift+C", "Mod+Shift+B",
    }  # fmt: skip


def test_a_key_that_has_not_shipped_is_hidden_and_silent():
    """An unavailable entry has to mean *nothing happens*: no overlay row
    promising a key that does not work, and no rejection shake suggesting
    the reader nearly did something.
    """
    reserved = {entry["id"] for entry in _registry() if not entry["available"]}
    assert reserved == {
        "more",
    }
    # `compose`, `reply`, `reply-all` and `forward` left this set with 1C
    # — the compose dock and the inline reply card are what made the four
    # keys mean something (`static/js/compose.js`, `mailosh/web/compose.py`).

    source = KEYS_JS.read_text()
    runners = _js_block(source, "const RUNNERS =")
    inert = set(re.findall(r'^  "?([\w-]+)"?: \(\) => undefined,$', runners, re.M))
    assert inert == reserved

    # ...and dispatch leaves before it can shake for one of them.
    fire = _js_block(source, r"function fire\(found, event\)")
    assert "if (!found.entry.available) return;" in fire
    assert fire.index("available") < fire.index("shake")

    # The overlay is drawn from the registry, filtered on availability.
    fill = _js_block(source, r"function fillShortcuts\(dialog\)")
    filters = re.findall(r"registry\.filter\(\(entry\) => ([^)]*)\)", fill)
    assert len(filters) == 1, filters
    assert "entry.available" in filters[0]


def test_the_registry_owns_the_ids_the_palette_dispatches():
    """`palette.py` landed first and its `PaletteAction.id` is what the
    palette will call `om.act()` with, so the registry has to answer to the
    same names — and to the same keys, or the palette's shortcut column
    would advertise a binding dispatch does not have.
    """
    entries = _by_id()
    for action in palette.ACTIONS:
        assert action.id in entries, action.id
        assert _as_binding(action.keys) in entries[action.id]["keys"], action.id

    # The palette's five fixed `g` sequences, matched through the mailbox
    # key each one names.
    for key, keys in palette._GOTO_KEYS.items():
        entry = entries.get("goto-" + key)
        assert entry is not None, key
        assert _as_binding(keys) in entry["keys"], key


def test_every_store_method_the_registry_drives_exists_in_the_store():
    """The registry's runners are the only callers of half the selection
    model. A method renamed in app.js and not here is a key that throws in
    the browser and passes every other test in this suite.
    """
    called = set(re.findall(r"\blist\.(\w+)\(", KEYS_JS.read_text()))
    block = _js_block(LIST_APP_JS.read_text(), "const list =")
    defined = set(re.findall(r"^  (\w+)\(", block, re.M))
    assert {"move", "toggle", "extend", "selectMatching", "clear", "ids"} <= called
    assert called <= defined, called - defined


def test_the_registry_asks_the_action_layer_what_it_would_act_on():
    """One definition of "the current conversation". keys.js needs it to
    reject a key with nothing to act on and to decide which way `s`
    toggles; recomputing it would be a second definition free to disagree
    with the one that actually posts.
    """
    assert "window.om?.targets" in KEYS_JS.read_text()
    surface = _js_block(LIST_ACTIONS_JS.read_text(), "const om =")
    assert re.search(r"^  targets\(\) \{\n    return defaultTargets\(\);", surface, re.M)


# --- the grid: one tab stop, and Enter opens it ----------------------------


def test_the_list_is_one_tab_stop_and_its_row_controls_are_none(app):
    """The ARIA grid pattern, and the review finding it answers. Every row
    control was natively focusable and `.row:focus-within` revealed the
    hover actions part-way through a traversal, so a tab-walk stopped five
    times per row — about 251 stops on a 50-row page. The row is the tab
    stop; arrows reach its cells.
    """
    body = _login(app).get("/mail/inbox").text
    assert body.count('tabindex="0"') == 1

    rows = _row_tags(body)
    assert 'tabindex="0"' in rows[0]

    for thread_id in ("t1", "t2"):
        markup = _row_html(body, thread_id)
        controls = re.findall(r"<(?:a|button|input|select|textarea)\b[^>]*>", markup)
        # the stretched anchor, the checkbox, the star, and the four hover
        # actions (both directions of read/unread ship; CSS shows one)
        assert len(controls) == 7, controls
        assert all('tabindex="-1"' in tag for tag in controls), controls


def test_enter_opens_the_focused_row(app):
    """Without this the list was reachable and inert — a focusable
    `role="row"` that did nothing, which is worse than an unreachable one.
    """
    entry = _by_id()["open"]
    assert entry["available"] is True
    assert entry["scope"] == "list"
    assert entry["keys"] == ["o", "Enter"]

    opener = _js_block(KEYS_JS.read_text(), r"function openFocused\(\)")
    assert '".row-link"' in opener
    assert ".click()" in opener

    # ...and the thing it clicks is on every row.
    row = _row_html(_login(app).get("/mail/inbox").text, "t1")
    assert row.count('class="row-link"') == 1


def test_the_grid_arrow_keys_are_scoped_to_the_list_not_the_window():
    """A global ArrowDown would take scrolling away from the rest of the
    page. These are grid mechanics, not shortcuts, which is also why they
    are not registry entries and never appear in the `?` overlay.
    """
    source = LIST_APP_JS.read_text()
    handler = _js_block(source, r"function onGridKeydown\(event\)")
    assert "closest?.(ROW_SELECTOR)" in handler
    for key in ("ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in handler, key
    assert 'document.body.addEventListener("keydown", onGridKeydown)' in source

    bound = {binding for entry in _registry() for binding in entry["keys"]}
    assert not any(binding.startswith("Arrow") for binding in bound)


def test_shift_click_selects_a_range_from_the_checkbox_not_the_row():
    """The collision, and the call. Shift-click on the row's stretched
    anchor is the browser's "open in a new window", restored on purpose
    after htmx had swallowed it; Gmail's range gesture wants Shift-click
    too. The range goes on the *checkbox*, which has no native modifier
    meaning to lose and is what "selection" means on a row — so the row
    keeps every modifier click and the keyboard gets `Shift+J`/`Shift+K`.
    """
    select_case = re.search(
        r'if \(kind === "select"\) \{(.*?)\n    return;', LIST_ACTIONS_JS.read_text(), re.S
    )
    assert select_case is not None, "actions.js no longer handles the select hook"
    assert "event.shiftKey" in select_case.group(1)
    assert "list.range(" in select_case.group(1)

    # The anchor keeps its native gestures: app.js's capture listener still
    # hands Shift and Alt back to the browser, and still prevents nothing.
    handler = _js_block(LIST_APP_JS.read_text(), r"function onClick\(event\)")
    assert "event.shiftKey || event.altKey" in handler
    assert "preventDefault" not in handler


# --- the toolbar and the overlay -------------------------------------------


def test_the_toolbar_ships_both_states_and_shows_the_empty_one(app):
    body = _login(app).get("/mail/inbox").text
    bars = _tags_containing(body, 'class="list-toolbar"')
    assert len(bars) == 2, bars

    # The default bar is visible before Alpine starts, and on a page whose
    # Alpine never starts at all; the selection bar is cloaked until there
    # is something to show it for.
    assert 'x-show="$store.list.count === 0"' in bars[0]
    assert "x-cloak" not in bars[0]
    # Exact complements, so no state of the selection can show both bars or
    # neither.
    assert 'x-show="$store.list.count !== 0"' in bars[1]
    assert "x-cloak" in bars[1]

    # Both carry their own `x-data` root. Alpine initializes trees rooted at
    # `[x-data]`/`[x-init]` and nothing else, so a lone `x-show` is never
    # evaluated — silently, with no console error: the cloaked bar would
    # simply never appear. Verified in a browser, where it was failing
    # exactly that quietly before this attribute landed.
    assert all(re.search(r"\bx-data\b", bar) for bar in bars), bars


def test_alpine_starts_after_the_modules_that_register_its_stores():
    """Load order, pinned because it is invisible when it breaks.

    A `defer` script and a non-async module script share one in-order
    execution list, so document position decides who runs first — and
    Alpine queues `start()` in a microtask that drains the instant its own
    script finishes. With Alpine ahead of `app.js`, `start()` fired
    `alpine:init` with nobody listening and then walked the DOM while
    `$store.list` did not exist: every directive reading it threw once,
    invisibly, and an effect that throws before touching a reactive
    property never subscribes to one. The store arrived a moment later and
    nothing was listening. No console error, correct-looking markup, a
    selection toolbar that could never appear.
    """
    markup = _strip_comments((REPO / "mailosh/web/templates/layouts/app.html").read_text())
    scripts = re.findall(r"<script src=\"\{\{ static\('([^']+)'\) \}\}\"", markup)
    assert "vendor/alpine.min.js" in scripts, scripts
    assert "js/app.js" in scripts, scripts
    assert scripts.index("vendor/alpine.min.js") > scripts.index("js/app.js"), scripts

    # ...and app.js is what registers the stores those directives read.
    source = LIST_APP_JS.read_text()
    assert 'window.Alpine.store("list", list)' in source
    assert 'document.addEventListener("alpine:init", registerStores' in source


def test_keys_js_is_imported_rather_than_also_carrying_a_script_tag():
    """A `<script>`'s versioned `?v=` URL and an `import`'s unversioned one
    are two different module records, so a module that is both tagged and
    imported is fetched and evaluated twice.

    It happened: the tagged `keys.js` instance's `registry` sat empty at 0
    entries while the imported one held 43, so the shortcuts dialog bound
    its close/click-out listeners a second time and ~30 KB (9.2 KiB
    gzipped) was downloaded for nothing on every cold load. Importing it
    is what runs it -- all three consumers already do.
    """
    markup = _strip_comments(APP_LAYOUT.read_text())
    tagged = re.findall(r"<script src=\"\{\{ static\('([^']+)'\) \}\}\"", markup)
    assert "js/keys.js" not in tagged, tagged

    importers = {
        path.name
        for path in (REPO / "mailosh/web/static/js").glob("*.js")
        if 'from "./keys.js"' in path.read_text()
    }
    # If nothing imported it, removing the tag would stop it loading at all.
    assert importers == {
        "app.js",
        "actions.js",
        "palette.js",
        # Both halves of compose: the always-loaded boot shim registers the
        # `c`/`r`/`a`/`f` runners so the keys work before the editor is
        # fetched, and `compose.js` replaces that registration when it lands.
        "compose-boot.js",
        "compose.js",
        "labels.js",
    }, importers

    # And the entry point that must run first is still tagged.
    assert "js/app.js" in tagged


def test_the_selection_toolbar_carries_the_bulk_actions_and_the_count(app):
    bar = _selection_toolbar(_login(app).get("/mail/inbox").text)
    assert re.findall(r'data-action="([^"]+)"', bar) == [
        "archive",
        "spam",
        "delete",
        "read",
        "unread",
    ]
    assert bar.count('data-role="select-all"') == 1
    assert 'x-text="$store.list.countLabel"' in bar
    # It acts on the selection, so it carries no ids of its own — see
    # tests/unit/test_actions_frontend.py for the invariant this is the one
    # documented exception to.
    assert "data-email-ids" not in bar
    # Label as and Move to went live with 1D. The "More" placeholder is gone:
    # a permanently disabled control is a tab stop that does nothing.
    assert bar.count('aria-disabled="true"') == 0
    assert bar.count('data-role="label-picker"') == 1
    assert bar.count('data-role="label-move"') == 1


def test_the_range_readout_belongs_to_the_empty_toolbar_only(app):
    """Both bars are in the document at once, and only one of them answers
    "how much of this mailbox am I looking at" — the other answers "how
    much of it have I picked".
    """
    body = _login(app).get("/mail/inbox").text
    assert body.count('id="list-range"') == 1
    assert 'id="list-range"' not in _selection_toolbar(body)


def test_the_shortcuts_overlay_ships_once_outside_every_swap_target(app):
    client = _login(app)
    page = client.get("/mail/inbox").text
    assert page.count('<dialog id="shortcuts"') == 1
    assert page.count('data-role="shortcuts-body"') == 1
    assert page.count('data-role="shortcuts-close"') == 1

    # Outside `#main`: keys.js binds its listeners and fills it once, and
    # every htmx swap in this app replaces `#main` or `#list`.
    assert page.index("</main>") < page.index('<dialog id="shortcuts"')

    # ...and it is *empty* as served. The rows come from the registry at
    # runtime, so no key is written down in two places.
    dialog = page[page.index('<dialog id="shortcuts"') : page.index("</dialog>")]
    assert "<kbd" not in dialog

    # A fragment swap must not bring a second one into the document.
    fragment = client.get("/mail/inbox", headers={"HX-Request": "true"}).text
    assert 'id="shortcuts"' not in fragment


def test_the_conversation_view_carries_the_overlay_too(app):
    """`?` is global scope, so it has to work off the list as well."""
    page = _login(app).get("/t/t1").text
    assert page.count('<dialog id="shortcuts"') == 1


# --- the hints on every control --------------------------------------------


def test_every_action_control_names_its_key_from_the_registry():
    """Spec §6.1's "Archive (e)". The tooltip is the only place most readers
    ever learn a shortcut, so a rebound key must not be able to leave a
    stale hint behind in a template.
    """
    entries = _by_id()
    seen: collections.Counter[str] = collections.Counter()
    for path in TEMPLATE_FILES:
        markup = _strip_comments(path.read_text())
        for tag in re.finditer(r"<button\b[^>]*>", markup):
            hook = re.search(r'data-action="([^"]+)"', tag.group(0))
            if hook is None:
                continue
            kind = hook.group(1)
            assert kind in ACTION_KEY_IDS, (path.name, kind)
            title = re.search(r'title="([^"]*)"', tag.group(0))
            assert title is not None, (path.name, tag.group(0))
            binding = entries[ACTION_KEY_IDS[kind]]["keys"][0]
            assert title.group(1).endswith(f"({binding})"), (path.name, title.group(1))
            seen[kind] += 1

    # Cardinality, so a template that stopped rendering its action bar
    # could not pass this by having nothing left to check.
    assert dict(seen) == {
        "select": 1,
        # The list row's star, the conversation card's own, and the
        # conversation toolbar's (spec §7: the same action bar as the list).
        "star": 3,
        "archive": 3,
        "delete": 3,
        "read": 2,
        "unread": 3,
        # The selection toolbar's, and the conversation toolbar's.
        "spam": 2,
        # Trash's pair stands where archive/delete stand, in the same three
        # templates (row, selection toolbar, conversation toolbar); the
        # templates carry both branches, so both are counted.
        "restore": 3,
        "destroy": 3,
        # Spam's "Not spam" replaces the spam button in the two bars that
        # have one; a row never had a spam button to replace.
        "unspam": 2,
    }


# --- the stylesheet the browser actually gets ------------------------------

KEY_UI_CLASSES = [
    ".shortcuts",
    ".shortcuts-head",
    ".shortcuts-body",
    ".shortcuts-group",
    ".shortcuts-row",
    ".shortcuts-keys",
    ".shortcuts-or",
    ".toolbar-count",
]


@pytest.mark.parametrize("selector", KEY_UI_CLASSES)
def test_the_overlay_styles_survived_the_build(selector: str):
    """`layouts/app.html` links the *compiled* `static/app.css`, so a rule
    added to `styles/input.css` and never built changes nothing at all.
    """
    pattern = re.escape(selector) + r"\s*[{,]"
    assert re.search(pattern, INPUT_CSS.read_text()), selector
    assert re.search(pattern, BUILT_CSS.read_text()), selector


# ---------------------------------------------------------------------------
# Task 11/12: the ⌘K palette and quick settings.
#
# The two ship together because they are one surface — both are shell
# `<dialog>`s rendered once by `layouts/app.html`, opened from the top bar
# and from the key registry, and both write through state the other reads.
# What is checked here is, again, agreement across files that cannot see
# each other:
#
#   palette.js <-> keys.js        the modes the runners ask for
#   palette.js <-> palette.py     the payload's groups, and the six ids
#   palette.js <-> mailbox_tree   a "Go to Inbox" row's glyph
#   palette.js <-> palette.html   every icon a row clones is rendered
#   quick_settings.html <-> prefs.py   the fields, and each one's values
#   app.js <-> keys.js            `data-shortcuts`, written and read
# ---------------------------------------------------------------------------

PALETTE_JS = REPO / "mailosh/web/static/js/palette.js"
PALETTE_HTML = REPO / "mailosh/web/templates/shell/palette.html"
QUICK_HTML = REPO / "mailosh/web/templates/shell/quick_settings.html"
TOPBAR_HTML = REPO / "mailosh/web/templates/shell/topbar.html"
APP_LAYOUT = REPO / "mailosh/web/templates/layouts/app.html"
ICONS_DIR = REPO / "mailosh/web/static/icons"

#: Every reserved nav key's own sidebar glyph, read off the two tables
#: `mailosh.services.mailbox_tree` builds the nav from.
NAV_ICONS = {
    key: icon
    for key, _label, icon, *_rest in (*mailbox_tree._SYSTEM_SPEC, *mailbox_tree._MORE_SPEC)
}


def _js_method(source: str, header: str) -> str:
    """The body of an object-literal method (`  setPref(name, value) {`),
    matched by counting braces from its opening one — `_js_block` above
    only finds declarations whose closing brace is in column 0, and the
    `ui` store's methods are nested inside one.
    """
    start = source.index("{", source.index(header))
    depth = 0
    for at in range(start, len(source)):
        if source[at] == "{":
            depth += 1
        elif source[at] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : at]
    raise AssertionError(header)


def _js_source(path: pathlib.Path) -> str:
    """A JavaScript file without its prose. These modules explain
    themselves at length and quote the very calls they are documenting
    (`om.act("read")` appears only in palette.js's header, saying why the
    palette does *not* call it), so a rule about the code has to read the
    code — the same reason `_js_block` strips comments out of a block.
    """
    body = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("//"))


def _dialog(markup: str, dialog_id: str) -> str:
    at = markup.index(f'<dialog id="{dialog_id}"')
    return markup[at : markup.index("</dialog>", at)]


def _palette_icons() -> dict[str, str]:
    """`palette.js`'s `ICONS` table: result id -> design-system icon name."""
    block = _js_block(PALETTE_JS.read_text(), "const ICONS =")
    return dict(re.findall(r'^  "?([\w-]+)"?: "([\w-]+)",$', block, re.M))


def _palette_modes() -> dict[str, list[str]]:
    """`palette.js`'s `MODES`: mode name -> the groups it draws, in order."""
    block = _js_block(PALETTE_JS.read_text(), "const MODES =")
    found = re.findall(r"(\w+): \{\s*groups: \[([^\]]*)\]", block)
    assert found, "palette.js no longer declares its modes as literal groups"
    return {name: re.findall(r'"([^"]+)"', groups) for name, groups in found}


def _pref_inputs(markup: str) -> list[str]:
    """Every control in the quick-settings panel, by either of the two ways
    one reaches `POST /prefs`.

    `data-pref` is the *appearance* half: `app.js`'s `ui` store paints
    `<html>` first and posts second. The reading half changes nothing on the
    current page, so it posts itself (`hx-post="/prefs"`) and carries no
    store entry. Both are pref controls, and the panel/endpoint contract
    below has to see both — matching only the first is how a whole group of
    controls could go missing from a test that still passed.
    """
    return [
        tag
        for tag in re.findall(r"<input\b[^>]*>", markup)
        if "data-pref=" in tag or 'hx-post="/prefs"' in tag
    ]


def _pref_field(tag: str) -> str:
    """Which preference one control writes: its `data-pref`, or the `name`
    htmx posts it under. Never both spellings for one control — that would
    be two clients writing one field."""
    marked = re.search(r'data-pref="(\w+)"', tag)
    named = re.search(r'name="(\w+)"', tag)
    if marked is not None:
        return marked.group(1)
    assert named is not None, tag
    assert 'hx-post="/prefs"' in tag, tag
    return named.group(1)


def _offered_prefs(panel: str) -> dict[str, list[str | None]]:
    """`{field: [value, ...]}` as the quick-settings panel offers them; a
    checkbox contributes `None`, having no value list of its own.
    """
    offered: dict[str, list[str | None]] = {}
    for tag in _pref_inputs(panel):
        value = re.search(r'value="(-?[\w-]+)"', tag)
        offered.setdefault(_pref_field(tag), []).append(None if value is None else value.group(1))
    return offered


def _checked_prefs(panel: str) -> dict[str, object]:
    """What the served panel says is currently chosen."""
    chosen: dict[str, object] = {}
    for tag in _pref_inputs(panel):
        field = _pref_field(tag)
        value = re.search(r'value="(-?[\w-]+)"', tag)
        if value is None:
            chosen[field] = "checked" in tag
        elif "checked" in tag:
            chosen[field] = value.group(1)
    return chosen


def _post_pref(client: TestClient, **values: str):
    """`POST /prefs` with the CSRF token the shell rendered."""
    page = client.get("/mail/inbox").text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page).group(1)
    return client.post("/prefs", data=values, headers={"X-CSRF-Token": token})


# --- the palette -----------------------------------------------------------


def test_the_palette_opens_in_exactly_the_modes_the_registry_asks_for():
    """`Mod+K` and `g l` are live now, and each names a mode. A mode the
    runners never ask for is dead code; a mode they ask for that does not
    exist is a key that opens an empty panel.
    """
    entries = _by_id()
    assert entries["palette"]["available"] is True
    assert entries["goto-label"]["available"] is True

    runners = _js_block(KEYS_JS.read_text(), "const RUNNERS =")
    asked = set(re.findall(r'showPalette\("(\w+)"\)', runners))
    assert asked == set(_palette_modes())


def test_every_palette_group_is_drawn_by_something_and_drawn_once():
    """A group a mode lists that nothing produces renders an empty heading;
    one produced but unlisted never appears at all.
    """
    source = PALETTE_JS.read_text()
    modes = _palette_modes()
    produced = set(re.findall(r'group: "([^"]+)"', source))
    assert set(modes["command"]) == produced
    assert len(modes["command"]) == len(produced), "a group listed twice"
    for name, groups in modes.items():
        assert groups, name
        assert set(groups) <= produced, name

    # ...and one builder per group, each reading its own key off the
    # `GET /palette/index` payload.
    assert set(palette.PaletteIndex.model_fields) == {"actions", "goto", "labels", "settings"}
    for builder, key in (("action", "actions"), ("goto", "goto"), ("setting", "settings")):
        block = _js_block(source, rf"function {builder}Candidates\(\)")
        assert f"index.{key}" in block, builder


def test_the_palette_runs_an_action_through_the_registry_rather_than_its_own_map():
    """`palette.py`'s ids are `keys.js`'s ids, and the palette resolves one
    to the registry entry it belongs to — so the row's key chip, whether
    the row appears at all, and what `Enter` does all come from the one
    table. A second mapping here would have to know that `mark-read` means
    `om.act("read")`, and would be free to drift.
    """
    source = _js_source(PALETTE_JS)
    block = _js_block(source, r"function actionCandidates\(\)")
    assert "entryFor(action.id)" in block
    assert "!entry.available" in block
    assert "run: entry.run" in block
    assert "om.act(" not in source

    # Spec §6.2's "context-aware": with nothing selected and no
    # conversation open, the six mutations are left out rather than listed
    # and inert — and what counts as a target is actions.js's answer, the
    # same one keys.js asks for.
    assert "targets().length === 0" in block
    assert "window.om?.targets" in source


def test_every_palette_row_can_draw_its_own_icon(app):
    """Four files have to agree for one row to render: the id comes from
    `palette.py` or `mailbox_tree`, the glyph name from `palette.js`, the
    rendered SVG from `palette.html`, and the file itself from the vendored
    icon set. A gap anywhere is a row with an empty square in it.
    """
    icons = _palette_icons()
    for action in palette.ACTIONS:
        assert action.id in icons, action.id
    # A "Go to Inbox" row wears the sidebar's own glyph for Inbox.
    for key, glyph in NAV_ICONS.items():
        assert icons.get(key) == glyph, key
    assert mailbox_tree._RESERVED_KEYS <= set(icons)
    assert "settings" in icons

    dialog = _dialog(_login(app).get("/mail/inbox").text, "palette")
    rendered = set(re.findall(r'<span data-icon="([\w-]+)">', dialog))
    # Exactly the glyphs the rows clone: a missing one is a null icon, a
    # spare one is markup every page carries and nothing ever uses.
    assert rendered == set(icons.values())
    for name in rendered:
        assert (ICONS_DIR / f"{name}.svg").exists(), name


def test_the_palette_ships_once_outside_every_swap_target_and_empty(app):
    client = _login(app)
    page = client.get("/mail/inbox").text
    assert page.count('<dialog id="palette"') == 1
    assert page.count('data-role="palette-input"') == 1
    assert page.count('data-role="palette-results"') == 1
    assert page.index("</main>") < page.index('<dialog id="palette"')

    # The rows come from the registry and the index at runtime, so no
    # command is written down in two places.
    assert "palette-option" not in _dialog(page, "palette")

    fragment = client.get("/mail/inbox", headers={"HX-Request": "true"}).text
    assert 'id="palette"' not in fragment

    # `Mod+K` is global scope, so it has to work off the list too.
    assert _login(app).get("/t/t1").text.count('<dialog id="palette"') == 1


def test_the_palette_owns_the_one_copy_of_the_vendored_matcher():
    """command-score is an ES module. It used to be loaded by a `<script>`
    nothing imported — inert, but a versioned `?v=` URL and an `import`'s
    unversioned one are two different module records, so keeping both
    would fetch and evaluate the matcher twice.
    """
    assert 'import commandScore from "../vendor/command-score.js";' in PALETTE_JS.read_text()
    vendored = REPO / "mailosh/web/static/vendor/command-score.js"
    assert vendored.read_text().rstrip().endswith("export default commandScore;")
    assert "command-score" not in _strip_comments(APP_LAYOUT.read_text())


def test_the_palette_scores_with_the_matcher_and_lets_recents_reorder():
    block = _js_block(PALETTE_JS.read_text(), r"function compute\(\)")
    assert "commandScore(candidate.search, query)" in block
    assert "if (base < THRESHOLD) continue;" in block
    assert "RECENT_BOOST" in block
    # An empty query is not scored at all, so the server's own order
    # stands and recency is the only thing that reorders it.
    assert 'query === "" ? 1 :' in block


def test_the_palette_imports_keys_js_and_keys_js_does_not_import_it_back():
    """A cycle would work in a browser only by accident of when each side
    first touches the other's bindings; a temporal-dead-zone throw during
    module evaluation is invisible in this app.
    """
    palette_js = _js_source(PALETTE_JS)
    assert re.search(r'^import \{[^}]*\} from "\./keys\.js";$', palette_js, re.M)
    assert "registerPalette(open);" in palette_js
    # keys.js reaches the palette through the opener it is handed, never
    # through an import of its own.
    keys_js = _js_source(KEYS_JS)
    assert "import" not in keys_js.split("export const registry")[0]
    assert "palette.js" not in keys_js


# --- quick settings --------------------------------------------------------


def test_the_gear_is_live_and_opens_the_panel():
    """It shipped `aria-disabled` and titled "arrives in a later task" —
    which would now be a false claim on the only control a mouse has for
    theme and density.
    """
    marked = [
        path.name
        for path in TEMPLATE_FILES
        if 'data-role="settings"' in _strip_comments(path.read_text())
    ]
    assert marked == ["topbar.html"]

    topbar = _strip_comments(TOPBAR_HTML.read_text())
    button = re.search(r'<button[^>]*data-role="settings"[^>]*>', topbar)
    assert button is not None
    assert "aria-disabled" not in button.group(0)
    assert "arrives" not in button.group(0)
    # `data-action` names one of the six routes; this control posts nothing.
    assert "data-action" not in button.group(0)
    # Nothing is left disabled up there: the nav-rail toggle placeholder went
    # the way of the "More" buttons -- absent until the feature exists.
    assert topbar.count('aria-disabled="true"') == 0

    handler = _js_block(LIST_APP_JS.read_text(), r"function openQuickSettings\(\)")
    assert "showModal()" in handler
    assert "syncPrefControls()" in handler


def test_quick_settings_offers_exactly_the_fields_the_endpoint_accepts(app):
    """The panel and `POST /prefs` are one contract. A control for a field
    the route rejects is a 422 on click; a legal value the panel omits is
    a preference nobody can reach.
    """
    panel = _dialog(_login(app).get("/mail/inbox").text, "quick-settings")
    offered = _offered_prefs(panel)

    accepted = {
        name
        for name in inspect.signature(prefs.update_prefs).parameters
        if name not in {"user", "db"}
    }
    assert set(offered) == accepted

    assert offered["theme"] == list(typing.get_args(prefs.Theme))
    assert offered["density"] == list(typing.get_args(prefs.Density))
    assert offered["mark_read_delay"] == list(typing.get_args(prefs.MarkReadDelay))
    assert offered["auto_advance"] == list(typing.get_args(prefs.AutoAdvance))
    assert sorted(offered["remote_images"]) == sorted(typing.get_args(prefs.RemoteImages))
    assert sorted(offered["dark_restyle"]) == sorted(typing.get_args(prefs.Flag))
    # A checkbox, not a value list — `shortcuts` is a boolean either side.
    assert offered["shortcuts"] == [None]

    assert _login(app).get("/mail/inbox").text.count('<dialog id="quick-settings"') == 1


def test_quick_settings_is_a_control_per_value_and_one_of_each_is_chosen(app):
    client = _login(app)
    page = client.get("/mail/inbox").text
    panel = _dialog(page, "quick-settings")

    # One input per legal value, plus the one switch.
    assert len(_pref_inputs(panel)) == 3 + 3 + 1 + 4 + 3 + 3 + 2
    assert _checked_prefs(panel) == {
        "theme": "system",
        "density": "comfortable",
        "shortcuts": True,
        "mark_read_delay": "0",
        "auto_advance": "older",
        "remote_images": "ask",
        "dark_restyle": "true",
    }


def test_a_reading_control_posts_itself_rather_than_going_through_the_store(app):
    """`app.js`'s `ui` store drops any `data-pref` it does not know
    (`PREF_FIELDS`), and it exists to paint `<html>` before the round trip.
    The reading preferences have nothing to paint — `mailosh/web/mail.py`
    reads them on the *next* conversation — so marking them `data-pref`
    would have been a control that posts nothing at all, silently.
    """
    panel = _dialog(_login(app).get("/mail/inbox").text, "quick-settings")
    store_fields = set(re.findall(r"const PREF_FIELDS = \[([^\]]*)\]", LIST_APP_JS.read_text()))
    known = set(re.findall(r'"(\w+)"', store_fields.pop()))

    for tag in _pref_inputs(panel):
        field = _pref_field(tag)
        if 'hx-post="/prefs"' in tag:
            assert "data-pref" not in tag, field
            assert field not in known, field
            # htmx sends a named input's own value, so one control is one
            # partial update — and it needs a `change` trigger, because the
            # default for a radio/checkbox is `change` only by accident of
            # htmx's own defaulting rules for `<input>`.
            assert 'hx-trigger="change"' in tag, field
            assert 'hx-swap="none"' in tag, field
        else:
            assert field in known, field


def test_a_preference_survives_the_reload_in_both_places_it_is_written(app):
    """The point of the whole panel: what you pick is applied to `<html>`
    (which is what the token blocks and `--row-h` select on) *and* comes
    back that way on the next page.
    """
    client = _login(app)
    assert _post_pref(client, theme="dark", density="compact").status_code == 204

    page = client.get("/mail/inbox").text
    root = re.search(r"<html[^>]*>", page).group(0)
    assert 'data-theme="dark"' in root
    assert 'data-density="compact"' in root
    assert _checked_prefs(_dialog(page, "quick-settings")) == {
        "theme": "dark",
        "density": "compact",
        "shortcuts": True,
        # Untouched by that POST and therefore still at their defaults —
        # asserted rather than filtered out, because "a one-field write
        # left the others alone" is the property this test is really about
        # and it now has five more fields to be true of.
        "mark_read_delay": "0",
        "auto_advance": "older",
        "remote_images": "ask",
        "dark_restyle": "true",
    }


def test_a_reading_preference_survives_the_reload_too(app):
    """The same round trip for the half that posts itself. `<html>` is not
    involved — nothing about these paints — so the served panel is the only
    place the stored value shows, and a control that came back on its
    default would silently lose every choice on the next navigation.
    """
    client = _login(app)
    assert _post_pref(client, mark_read_delay="3", auto_advance="list").status_code == 204
    assert _post_pref(client, remote_images="always", dark_restyle="false").status_code == 204

    panel = _dialog(client.get("/mail/inbox").text, "quick-settings")
    chosen = _checked_prefs(panel)
    assert chosen["mark_read_delay"] == "3"
    assert chosen["auto_advance"] == "list"
    assert chosen["remote_images"] == "always"
    assert chosen["dark_restyle"] == "false"
    # The appearance half was never in either body and did not move.
    assert chosen["theme"] == "system"


@pytest.mark.parametrize(
    "field,value",
    [("mark_read_delay", "7"), ("auto_advance", "sideways"), ("remote_images", "never")],
)
def test_a_value_no_control_offers_is_refused_and_changes_nothing(app, field, value):
    """The `Literal`s are the whole validation. A `mark_read_delay` typed as
    an `int` would take `7` happily, and the delay a reader never chose
    would then be what the conversation page armed its timer with.
    """
    client = _login(app)
    before = _checked_prefs(_dialog(client.get("/mail/inbox").text, "quick-settings"))

    assert _post_pref(client, **{field: value}).status_code == 422

    after = _checked_prefs(_dialog(client.get("/mail/inbox").text, "quick-settings"))
    assert after == before


def test_the_shell_renders_the_shortcuts_flag_the_dispatcher_reads(app):
    """`keys.js` has read `[data-shortcuts=off]` since the registry landed
    and no template wrote it, so the toggle was inert. Both halves, in the
    one spelling they have to share.
    """
    assert 'dataset.shortcuts === "off"' in KEYS_JS.read_text()

    client = _login(app)
    root = re.search(r"<html[^>]*>", client.get("/mail/inbox").text).group(0)
    assert 'data-shortcuts="on"' in root

    assert _post_pref(client, shortcuts="false").status_code == 204
    page = client.get("/mail/inbox").text
    assert 'data-shortcuts="off"' in re.search(r"<html[^>]*>", page).group(0)
    assert _checked_prefs(_dialog(page, "quick-settings"))["shortcuts"] is False


def test_one_write_path_serves_the_panel_and_the_palette():
    """A theme changed from the gear and a theme changed from the palette
    have to be the same write — otherwise one of them applies optimistically
    and the other does not, or they post different bodies.
    """
    app_js = LIST_APP_JS.read_text()
    palette_js = PALETTE_JS.read_text()

    assert app_js.count('fetch("/prefs"') == 1
    assert "/prefs" not in palette_js
    assert "setPref(" in palette_js

    setter = _js_method(app_js, "  setPref(name, value) {")
    # Optimistic: the page changes before the request leaves...
    assert setter.index("this.applyPref(name, value)") < setter.index("savePrefs(")
    # ...and goes back if the write did not land, down one path that both
    # a refused write and a request that never arrived reach — a revert
    # that ran on one and not the other would leave the page lying.
    assert setter.count("this.applyPref(name, before)") == 1
    assert re.search(r"savePrefs\(values\)\.then\(settle, \(\) => settle\(null\)\);", setter)
    # A second change while the first is in flight wins: neither the
    # server's echo nor the revert may touch a value the reader has
    # already moved on from.
    assert "if (this[name] !== value) return stored;" in setter

    # `<html>`'s dataset is the whole mechanism, and the three fields are
    # exactly the ones the endpoint takes.
    fields = re.search(r"const PREF_FIELDS = \[(.*?)\];", app_js, re.S)
    assert set(re.findall(r'"(\w+)"', fields.group(1))) == {"theme", "density", "shortcuts"}
    applier = _js_method(app_js, "  applyPref(name, value) {")
    assert "root.dataset[name] = value" in applier
    assert 'root.dataset.shortcuts = value ? "on" : "off"' in applier


# --- the stylesheet the browser actually gets ------------------------------

SHELL_DIALOG_CLASSES = [
    ".palette",
    ".palette-field",
    ".palette-input",
    ".palette-results",
    ".palette-group",
    ".palette-option",
    ".palette-keys",
    ".palette-empty",
    ".palette-foot",
    ".quick",
    ".quick-head",
    ".quick-field",
    ".quick-row",
    ".segmented",
    ".segment",
    ".segment-face",
    ".switch",
]


@pytest.mark.parametrize("selector", SHELL_DIALOG_CLASSES)
def test_the_shell_dialog_styles_survived_the_build(selector: str):
    pattern = re.escape(selector) + r"\s*[{,: ]"
    assert re.search(pattern, INPUT_CSS.read_text()), selector
    assert re.search(pattern, BUILT_CSS.read_text()), selector


def test_the_palette_chord_survives_turning_shortcuts_off():
    """`suppressed()` honours `data-shortcuts="off"` for single-key
    shortcuts and exempts the palette's chord.

    The toggle exists because single keys fire while you are reading mail
    -- `e` archives whatever is focused -- so someone who does not want
    that needs a way out. A chord cannot go off by accident, and the
    palette is the only route to the command surface: nothing in the top
    bar opens it. Honouring the toggle for Cmd/Ctrl+K would quietly turn
    "off" into "lose the command palette until you find the gear".
    """
    source = KEYS_JS.read_text()
    body = _js_block(source, re.escape("function suppressed(event)"))

    # The toggle still governs shortcuts -- this must not become a blanket
    # exemption that makes "off" mean nothing.
    assert 'dataset.shortcuts === "off"' in body

    guard = body[body.index('dataset.shortcuts === "off"') :]
    # The exemption is the palette chord specifically: `k` AND a modifier.
    assert re.search(r'key\.toLowerCase\(\)\s*===\s*"k"', guard)
    assert re.search(r"metaKey\s*\|\|\s*event\.ctrlKey", guard)

    # And it is an exemption, not a reordering: the suppressing `return
    # true` still exists inside the toggle's branch.
    assert (
        "return true" in guard[: guard.index("}")] or "if (!isPaletteChord) return true;" in guard
    )


# ---------------------------------------------------------------------------
# Where a conversation sits: `/t/{id}?key=&pos=` and `/mail/{key}/at/{n}`
#
# The header's `4 of 1,284` and its two arrows need a position, and a
# conversation has none of its own — it is a view *of* a mailbox. The row
# that was clicked knows where it sat, so it hands that over rather than
# leaving this route to search a mailbox for the thread it is already
# rendering.
#
# These read the template context rather than the rendered header: the
# markup that draws the readout is `thread/page.html`'s, and what is pinned
# here is the *decision* — which query was issued, what it was asked for,
# and what came back — so that a template change cannot quietly turn a
# wrong number into a passing test by moving the string.
# ---------------------------------------------------------------------------


def _place(response):
    """The `ThreadPlace` a conversation response was rendered with, or
    `None` when the caller gave it no position to work from."""
    assert response.status_code == 200, response.status_code
    return response.context["place"]


def test_the_thread_route_learns_its_place_from_one_short_window_query(app, fake):
    """One `Email/query`, for this conversation and the two beside it, from
    the position the row handed over. A page's worth would be a
    fifty-message `Email/get` to render a number; a single row would leave
    auto-advance with no id to move by.
    """
    fake.total = 1284
    client = _login(app)
    fake.queries.clear()

    place = _place(client.get("/t/t1?key=inbox&pos=3"))

    assert [(q["position"], q["limit"]) for q in fake.queries] == [(2, 3)]
    # `total` is the server's own `calculateTotal`, never a length: this
    # window holds three rows at most, so a length would read "4 of 3".
    assert place.label == "4 of 1,284"
    assert (place.prev_url, place.next_url) == ("/mail/inbox/at/2", "/mail/inbox/at/4")


def test_the_window_is_two_rows_at_the_top_of_a_mailbox(app, fake):
    """There is no newer neighbour above row 0, so nothing asks for one."""
    fake.total = 1284
    client = _login(app)
    fake.queries.clear()

    _place(client.get("/t/t1?key=inbox&pos=0"))

    assert [(q["position"], q["limit"]) for q in fake.queries] == [(0, 2)]


@pytest.mark.parametrize(
    "position,prev_url,next_url",
    [(0, None, "/mail/inbox/at/1"), (1, "/mail/inbox/at/0", None)],
)
def test_an_arrow_with_nowhere_to_go_is_absent_from_the_context_not_bent(
    app, fake, position, prev_url, next_url
):
    """The two ends of a mailbox. `None` rather than a URL that would 404 or
    silently clamp back onto the conversation already on screen — the header
    renders that arrow disabled, so the control keeps its place instead of
    disappearing and shifting the one beside it.
    """
    fake.total = 2
    place = _place(_login(app).get(f"/t/t1?key=inbox&pos={position}"))

    assert (place.prev_url, place.next_url) == (prev_url, next_url)


def test_a_conversation_opened_without_a_position_queries_nothing_at_all(app, fake):
    """The palette, a pasted link and a history restore all reach `/t/{id}`
    with no position. Searching the mailbox for one would be exactly the
    query the row carries its position to avoid — and would be wrong as
    often as not, since a thread sits in more than one mailbox.
    """
    client = _login(app)
    fake.queries.clear()

    response = client.get("/t/t1")

    assert response.status_code == 200
    assert fake.queries == []
    assert response.context["place"] is None
    # With nowhere to advance to, archiving from here goes back to the list.
    assert response.context["advance_url"] == response.context["back_url"] == "/mail/inbox"


@pytest.mark.parametrize("query", ["key=../../etc&pos=0", "key=mb-nope&pos=2", "pos=3"])
def test_a_position_is_only_taken_from_a_key_the_nav_knows(app, fake, query):
    """`key` is validated exactly as `/mail/{key}` validates it — an unknown
    one must never reach `Email/query` as an `inMailbox` filter — and `pos`
    without a `key` names a place in no particular mailbox. Either way the
    conversation still renders, with no readout and a back link that goes
    somewhere real.
    """
    client = _login(app)
    fake.queries.clear()

    response = client.get(f"/t/t1?{query}")

    assert response.status_code == 200
    assert fake.queries == []
    assert response.context["place"] is None
    assert response.context["back_url"] == "/mail/inbox"
    assert 'href="/mail/inbox"' in response.text


def test_a_negative_position_is_clamped_before_it_reaches_the_query(app, fake):
    """A hand-edited `?pos=-5` must not become a negative JMAP offset."""
    fake.total = 2
    client = _login(app)
    fake.queries.clear()

    place = _place(client.get("/t/t1?key=inbox&pos=-5"))

    assert [q["position"] for q in fake.queries] == [0]
    assert place.label == "1 of 2"
    assert place.prev_url is None


@pytest.mark.parametrize("posted,stored", [("0", 0), ("1", 1), ("3", 3), ("-1", -1)])
def test_the_mark_read_delay_reaches_the_page_as_the_number_the_reader_chose(app, posted, stored):
    """`static/js/actions.js` arms one timer from this number and treats
    anything below zero as "never". A string here would compare as text and
    silently never arm."""
    client = _login(app)
    assert _post_pref(client, mark_read_delay=posted).status_code == 204

    context = client.get("/t/t1").context

    assert context["mark_read_delay"] == stored
    assert isinstance(context["mark_read_delay"], int)
    # The POST target is named once, by the module that owns the routes.
    assert context["mark_read_url"] == "/a/read"


@pytest.mark.parametrize(
    "mode,target",
    [
        # The conversation one older than t2 and the one one newer, each
        # named by id — not by the position it happens to occupy now.
        ("older", "/t/t3?key=inbox&pos=1"),
        ("newer", "/t/t1?key=inbox&pos=0"),
        ("list", "/mail/inbox"),
    ],
)
def test_where_archiving_lands_follows_the_readers_own_setting(app, fake, mode, target):
    _positioned(fake, count=3)
    client = _login(app)
    assert _post_pref(client, auto_advance=mode).status_code == 204

    assert client.get("/t/t2?key=inbox&pos=1").context["advance_url"] == target


def test_advancing_names_the_conversation_rather_than_the_place_it_sits_in(app, fake):
    """The race the positional form cannot win.

    Archiving the conversation being read removes it from the mailbox, so
    the next-older one slides into *this* position. Whether
    `/mail/{key}/at/{pos + 1}` then names that conversation or the one
    after it depends on whether the server has reindexed by the time the
    request lands — the same click, two answers, decided by timing. The
    id was captured while the two were still side by side, so it survives
    the shift; the `pos` beside it is only the readout's hint, and names
    where that conversation will be once this one is gone.
    """
    _positioned(fake, count=4)
    client = _login(app)
    assert _post_pref(client, auto_advance="older").status_code == 204

    url = client.get("/t/t2?key=inbox&pos=1").context["advance_url"]
    assert url == "/t/t3?key=inbox&pos=1"

    # And it really is the conversation, not the place: t2 is archived out
    # from under it, everything after shifts up one, and the URL still
    # opens t3 — where the positional form would now open t4.
    fake.threads.pop("t2")
    fake.total = 3
    assert client.get(url).context["view"].thread_id == "t3"
    assert client.get("/mail/inbox/at/2").context["view"].thread_id == "t4"


def test_the_positional_form_is_kept_for_a_neighbour_whose_id_is_unknown(app, fake):
    """The end of a page: the mailbox says there is another conversation
    but this window did not reach it. A URL that skips one is still better
    than dropping the reader back to the list.
    """
    _positioned(fake, count=3)
    fake.total = 10
    client = _login(app)
    assert _post_pref(client, auto_advance="older").status_code == 204

    assert client.get("/t/t3?key=inbox&pos=2").context["advance_url"] == "/mail/inbox/at/3"


def test_the_landing_url_encodes_the_thread_id_and_the_mailbox_key(app, fake):
    """A thread id is server-chosen and opaque, and a label key is a
    mailbox id. Neither may be assumed URL-safe."""
    fake.threads = {"a b": [_header("e1", "a b")], "c&d": [_header("e2", "c&d")]}
    fake.total = 2
    fake.paged = True
    client = _login(app)
    assert _post_pref(client, auto_advance="older").status_code == 204

    advance = client.get("/t/a%20b?key=inbox&pos=0").context["advance_url"]
    assert advance == "/t/c%26d?key=inbox&pos=0"


@pytest.mark.parametrize("mode,position", [("older", 1), ("newer", 0)])
def test_archiving_the_last_conversation_in_a_direction_lands_on_the_list(
    app, fake, mode, position
):
    """There is no next conversation past the end, and inventing one would
    send the reader to a 404 immediately after a successful archive."""
    _positioned(fake, count=2)
    client = _login(app)
    assert _post_pref(client, auto_advance=mode).status_code == 204

    thread = f"t{position + 1}"
    landing = client.get(f"/t/{thread}?key=inbox&pos={position}").context["advance_url"]
    assert landing == "/mail/inbox"


# --- GET /mail/{key}/at/{position} -----------------------------------------


def _positioned(fake, count: int = 3) -> None:
    """`count` single-message threads in the inbox, in order, with
    `query_page` actually honouring `position`/`limit`."""
    fake.threads = {f"t{n}": [_header(f"e{n}", f"t{n}", minute=n)] for n in range(1, count + 1)}
    fake.total = count
    fake.paged = True


def test_the_at_route_resolves_a_position_to_that_conversation(app, fake):
    """One query does both halves — which thread is there, and the readout
    the page it renders needs — so an arrow click is a single round trip
    rather than a redirect into `/t/{id}`.
    """
    _positioned(fake)
    client = _login(app)
    fake.queries.clear()

    response = client.get("/mail/inbox/at/1")

    assert response.status_code == 200
    assert [(q["position"], q["limit"]) for q in fake.queries] == [(0, 3)]
    assert fake.thread_calls == ["t2"]
    assert 'id="msg-e2"' in response.text
    assert _place(response).label == "2 of 3"


def test_the_at_route_past_the_end_is_the_404_page(app, fake):
    _positioned(fake, count=1)
    response = _login(app).get("/mail/inbox/at/9")

    assert response.status_code == 404
    # The page, not a bare `{"detail": ...}` — the way out is one click away.
    assert 'href="/mail/inbox"' in response.text


def test_the_at_route_validates_its_key_the_way_the_list_does(app, fake):
    _positioned(fake)
    client = _login(app)
    fake.queries.clear()

    response = client.get("/mail/nope/at/0")

    assert response.status_code == 404
    assert fake.queries == []


def test_the_at_route_pushes_the_address_it_was_reached_by(app, fake):
    """The arrows swap `#main` in place, so nothing else pushes a URL —
    without this the address bar would still name the conversation before
    the one on screen, and a reload would take the reader back to it.
    """
    _positioned(fake)
    response = _login(app).get("/mail/inbox/at/2", headers={"HX-Request": "true"})

    assert response.headers["HX-Push-Url"] == "/mail/inbox/at/2"
    assert "<html" not in response.text


def test_both_ways_into_a_conversation_render_one_page(app, fake):
    """`/t/{id}` and `/mail/{key}/at/{n}` are two addresses for one view.
    Two context builders is how the same conversation starts behaving
    differently depending on which one you arrived by — a missing
    `advance_url` on one path is a silent loss of auto-advance.
    """
    _positioned(fake)
    client = _login(app)

    direct = client.get("/t/t2?key=inbox&pos=1")
    positioned = client.get("/mail/inbox/at/1")

    assert set(direct.context) == set(positioned.context)
    assert direct.template.name == positioned.template.name == "thread/page.html"
    for field in ("label", "prev_url", "next_url", "total"):
        assert getattr(_place(direct), field) == getattr(_place(positioned), field), field
    for key in ("back_url", "advance_url", "mark_read_delay", "mark_read_url"):
        assert direct.context[key] == positioned.context[key], key
    # Both are cacheable GETs, so the `preload` prefetch is reused.
    assert direct.headers["cache-control"] == positioned.headers["cache-control"]


# ---------------------------------------------------------------------------
# ...and what the page actually renders from it
#
# The section above pins the decision; this one pins that it reaches the
# document. Both halves are needed: a route that computes a perfect
# `advance_url` into a template that reads nothing is the state this whole
# feature shipped in.
# ---------------------------------------------------------------------------


def _scroll(html: str) -> dict[str, str]:
    """The conversation's `.thread-scroll` element, as its attributes."""
    (found,) = [
        attrs
        for attrs in parse_attrs(html)["div"]
        if "thread-scroll" in attrs.get("class", "").split()
    ]
    return found


def _toolbar(html: str) -> str:
    """The conversation's action bar, which holds no nested `<div>`."""
    return html.split('class="list-toolbar"', 1)[1].split("</div>", 1)[0]


def _toolbar_pager(html: str) -> list[tuple[str, str]]:
    """`(tag, href-or-"")` for the two arrows beside the readout, **in
    document order** — a `<span>` is the disabled end of the mailbox, and
    which end it is is exactly what the order says."""
    found = []
    for match in re.finditer(r"<(a|span)\b[^>]*>", _toolbar(html)):
        tag = match.group(1)
        (attrs,) = parse_attrs(f"{match.group(0)}</{tag}>")[tag]
        if attrs.get("rel") in {"prev", "next"} or attrs.get("aria-disabled") == "true":
            found.append((tag, attrs.get("href", "")))
    return found


def test_the_conversation_carries_the_delay_and_the_landing_place(app, fake):
    """`static/js/actions.js` reads both off this one element, keyed to the
    conversation `data-thread-id` names — one element, so a timer armed for
    the conversation you just left cannot fire against this one.
    """
    fake.total = 10
    client = _login(app)
    assert _post_pref(client, mark_read_delay="3", auto_advance="list").status_code == 204

    scroll = _scroll(client.get("/t/t1?key=inbox&pos=3").text)

    assert scroll["data-thread-id"] == "t1"
    assert scroll["data-mark-read-delay"] == "3"
    assert scroll["data-advance-url"] == "/mail/inbox"


def test_the_delay_reaches_the_page_as_the_reader_set_it(app, fake):
    """`-1` is "never", and it has to survive as a number the client can
    compare — the parse in actions.js treats anything below zero as never
    and everything else as seconds."""
    client = _login(app)
    assert _post_pref(client, mark_read_delay="-1").status_code == 204
    assert _scroll(client.get("/t/t1").text)["data-mark-read-delay"] == "-1"


def test_the_readout_and_both_arrows_render_where_the_mockup_puts_them(app, fake):
    fake.total = 1284
    html = _login(app).get("/t/t1?key=inbox&pos=3").text

    assert "4 of 1,284" in html
    assert _toolbar_pager(html) == [
        ("a", "/mail/inbox/at/2"),
        ("a", "/mail/inbox/at/4"),
    ]
    assert _toolbar(html).count("4 of 1,284") == 1


@pytest.mark.parametrize(
    "position,expected",
    [
        (0, [("span", ""), ("a", "/mail/inbox/at/1")]),
        (1, [("a", "/mail/inbox/at/0"), ("span", "")]),
    ],
)
def test_an_arrow_with_nowhere_to_go_keeps_its_place_disabled(app, fake, position, expected):
    """Disabled rather than absent, at both ends: a vanishing control
    shifts the one beside it, which is how a reader clicks the wrong
    thing. Newer is on the left, older on the right, and the order is the
    assertion.
    """
    fake.total = 2
    html = _login(app).get(f"/t/t1?key=inbox&pos={position}").text

    assert _toolbar_pager(html) == expected


def test_a_conversation_with_no_place_renders_no_readout_at_all(app, fake):
    """A pasted link or a palette jump knows no position. A placeholder
    "— of —" beside two dead arrows is the present-and-dead UI spec §3
    forbids, so nothing is rendered instead.
    """
    html = _login(app).get("/t/t1").text

    assert _toolbar_pager(html) == []
    assert " of " not in html.split('class="list-toolbar"')[1].split("</div>")[0]


# ---------------------------------------------------------------------------
# Compose entry points (Phase 1C wiring)
#
# The dock, the routes and the keyboard all shipped before anything the
# reader could click. `c` opened a dock while the Compose button was still
# `aria-disabled`, and `r`/`a`/`f` replied while the conversation had no
# reply control at all. These pin the controls, because a feature reachable
# only from the keyboard is a feature most people do not have.
# ---------------------------------------------------------------------------


def test_the_nav_compose_button_is_live_and_routes_through_compose_js(app):
    body = _login(app).get("/mail/inbox").text
    button = re.search(r"<button[^>]*class=\"compose-btn\".*?</button>", body, re.S)
    assert button is not None
    markup = button.group(0)
    assert "aria-disabled" not in markup
    assert "data-compose-open" in markup
    # No `hx-get` of its own: `compose.js`'s `open()` owns the three-dock
    # limit and its toast, and a button that fetched for itself would open a
    # fourth without ever seeing it.
    assert "hx-get" not in markup


def test_a_drafts_row_reopens_the_dock_instead_of_opening_a_conversation(app, fake):
    # The default fixture's headers all live in the inbox, so `/mail/drafts`
    # renders its empty state; this seeds an actual draft to have a row.
    fake.threads = {"td": [_header("ed", "td", mailbox_ids={"mb-drafts"}, keywords={"$draft"})]}
    body = _login(app).get("/mail/drafts").text
    link = re.search(r"<a class=\"row-link\".*?</a>", body, re.S)
    assert link is not None
    markup = link.group(0)
    assert 'data-compose-draft="ed"' in markup
    assert 'href="/compose/ed"' in markup
    # A draft is a message to keep writing, not a conversation to read.
    assert "/t/" not in markup
    # No htmx of its own: `compose.js`'s `open()` owns the three-dock limit.
    assert "hx-get" not in markup


def test_a_non_draft_row_still_opens_the_conversation(app):
    body = _login(app).get("/mail/inbox").text
    link = re.search(r"<a class=\"row-link\".*?</a>", body, re.S)
    assert link is not None
    markup = link.group(0)
    assert "data-compose-draft" not in markup
    assert "/t/" in markup


def test_the_x_show_style_guard_asks_about_both_sides_of_the_morph():
    """A morph swap must not un-hide what `x-show` hid — and must not hide
    anything else in the process.

    `x-show` hides by writing `display: none` straight onto the element. The
    server markup it is morphed against carries no inline style, so
    idiomorph removes the attribute as surplus, and Alpine does not put it
    back because its expression never changed. The selection toolbar
    reappeared as "0 selected" after any navigation into `#main`.

    The first fix refused every `style` update on a node that *currently*
    had `x-show`, and that was too broad in the worst way: navigating from
    the list to a conversation morphs a hidden `.list-toolbar` onto
    `.thread-scroll`, which has no `x-show` of its own, so the preserved
    `display: none` hid every message. **Reading mail broke to keep a
    toolbar honest**, and it shipped.

    So the guard has to ask about both sides. `beforeNodeMorphed` sees the
    incoming node too, and the inline style is carried across only when
    both are `x-show` elements. It also *writes* rather than *refuses* —
    nothing is blocked, so it cannot strand an unrelated element the way
    refusing did.
    """
    source = (REPO / "mailosh/web/static/js/app.js").read_text()
    assert "beforeNodeMorphed" in source
    # Both sides, not just the node being updated.
    assert 'oldNode.hasAttribute("x-show")' in source
    assert 'newNode.hasAttribute("x-show")' in source
    # Writes the style onto the incoming node rather than refusing an update.
    assert 'newNode.setAttribute("style", inline)' in source
    assert "beforeAttributeUpdated" not in source, (
        "the attribute-level guard was too broad and hid message bodies"
    )
    # The previous callback is chained, not replaced.
    assert "previous ? previous(" in source


def test_leaving_the_list_keeps_the_selection_and_the_scroll_position():
    """Opening a conversation must not throw away the reader's work.

    `#list` leaves the document entirely when a conversation opens, so every
    selected id looked "gone" to `ensureFocus` and was dropped on the way
    *in*. Pressing `u` then returned to nothing selected, scrolled to the
    top. It reproduced at 1200px as readily as at 390px — the single-pane
    layout found it, but it was never about the layout.

    The list being absent is not the list showing different rows, and that
    is the distinction the fix turns on: a mailbox switch really must drop a
    stale selection, and it still does.

    The scroll offset is remembered rather than the focused row, because
    those are different things. Selecting with the checkbox never moves
    focus, so "scroll the focused row back into view" scrolled to whichever
    row the list defaulted to — the top — which is indistinguishable from
    not restoring at all. That was the first attempt and it measured as a
    failure.
    """
    source = LIST_APP_JS.read_text()
    assert "if (document.querySelector(LIST) === null) {" in source
    assert "this.away = true;" in source
    assert "const returning = this.away === true;" in source
    # The offset, captured before the swap that removes the list.
    assert "state.scrollTop = el.scrollTop;" in source
    assert "if (returning && this.scrollTop > 0) {" in source
    # Clamped: coming back to a shorter list must not land at the bottom.
    assert "Math.min(this.scrollTop, el.scrollHeight - el.clientHeight)" in source


def test_a_refresh_that_keeps_the_list_does_not_move_the_reader():
    """`returning` is what makes the restore safe.

    A `mail:changed` refresh swaps the same view in place and never sets
    `away`, so the reader scrolled halfway down a list is not yanked back to
    where they were before the last conversation they opened.
    """
    source = LIST_APP_JS.read_text()
    body = source.split("ensureFocus(previousIds = null) {", 1)[1]
    guard = body.split("if (returning", 1)[0]
    # Nothing between entering `ensureFocus` and the guard may write scrollTop.
    assert "el.scrollTop =" not in guard


# ---------------------------------------------------------------------------
# Trash and Spam semantics
# ---------------------------------------------------------------------------


def _hover_actions(body: str, thread_id: str) -> list[str]:
    row = _row_html(body, thread_id)
    actions = row[row.index('class="row-actions"') :]
    return re.findall(r'data-action="([^"]+)"', actions)


def test_trash_rows_and_bars_offer_restore_and_delete_forever_instead(app, fake):
    fake.threads = {"t9": [_header("e9", "t9", mailbox_ids={"mb-trash"})]}
    body = _login(app).get("/mail/trash").text
    assert _hover_actions(body, "t9") == ["restore", "destroy", "read", "unread"]
    bar = _selection_toolbar(body)
    assert re.findall(r'data-action="([^"]+)"', bar) == ["restore", "destroy", "read", "unread"]
    assert "Delete forever (#)" in bar
    assert "Restore (e)" in bar
    # No spam button in Trash: there is nothing to report from there.
    assert 'data-action="spam"' not in bar


def test_spam_bars_swap_report_spam_for_not_spam(app, fake):
    fake.threads = {"t9": [_header("e9", "t9", mailbox_ids={"mb-junk"})]}
    body = _login(app).get("/mail/spam").text
    bar = _selection_toolbar(body)
    assert re.findall(r'data-action="([^"]+)"', bar) == [
        "archive",
        "unspam",
        "delete",
        "read",
        "unread",
    ]
    assert "Not spam (!)" in bar
    # Rows keep the ordinary pair — a row never had a spam button.
    assert _hover_actions(body, "t9") == ["archive", "delete", "read", "unread"]


def test_the_inbox_is_untouched_by_the_trash_and_spam_branches(app):
    body = _login(app).get("/mail/inbox").text
    first = next(iter(_default_threads()))
    assert _hover_actions(body, first) == ["archive", "delete", "read", "unread"]
    assert 'data-role="empty-mailbox"' not in body


@pytest.mark.parametrize(("key", "label"), [("trash", "Trash"), ("spam", "Spam")])
def test_trash_and_spam_carry_an_empty_now_control_naming_their_key(app, key, label):
    body = _login(app).get(f"/mail/{key}").text
    controls = re.findall(r'<button[^>]*data-role="empty-mailbox"[^>]*>.*?</button>', body, re.S)
    assert len(controls) == 1
    assert f'data-mailbox-key="{key}"' in controls[0]
    assert f"Empty {label} now" in controls[0]
    # It lives in the default toolbar, which is the one the reader sees
    # with nothing selected.
    assert 'data-role="empty-mailbox"' not in _selection_toolbar(body)


def test_every_toolbar_names_the_mailbox_the_keys_should_read(app):
    """`keys.js`'s `viewKey()` reads `data-view-key` off the toolbar, so
    `#` can mean delete forever in Trash and plain delete elsewhere. Both
    the list's bar and the conversation's own carry it — the conversation
    keeps the mailbox it was opened from."""
    client = _login(app)
    assert 'data-view-key="trash"' in client.get("/mail/trash").text
    first = next(iter(_default_threads()))
    page = client.get(f"/t/{first}", headers={"HX-Current-URL": "http://x/mail/trash"}).text
    assert 'data-view-key="trash"' in page
    bar = re.search(r'<div class="list-toolbar"[^>]*>.*?</div>', page, re.S)
    assert bar is not None
    assert re.findall(r'data-action="([^"]+)"', bar.group(0))[:2] == ["restore", "destroy"]

    source = KEYS_JS.read_text()
    assert 'querySelector("[data-view-key]")' in source
    runners = _js_block(source, "const RUNNERS =")
    assert re.search(
        r'^  archive: \(\) => inView\("trash", "restore", "archive"\),$', runners, re.M
    )
    assert re.search(r'^  delete: \(\) => inView\("trash", "destroy", "delete"\),$', runners, re.M)
    assert re.search(r'^  spam: \(\) => inView\("spam", "unspam", "spam"\),$', runners, re.M)
    # ...and the `?` overlay tells the reader about both readings.
    entries = _by_id()
    assert "Trash" in entries["delete"]["label"]
    assert "Trash" in entries["archive"]["label"]
    assert "Spam" in entries["spam"]["label"]


@pytest.mark.parametrize(
    ("key", "title"),
    [
        ("trash", "Trash is empty"),
        ("spam", "No spam"),
        ("drafts", "No drafts"),
        ("sent", "Nothing sent yet"),
    ],
)
def test_the_four_system_folders_have_their_own_empty_state(app, fake, key, title):
    fake.threads = {}
    fake.total = 0
    body = _login(app).get(f"/mail/{key}").text
    assert title in body
    assert "Nothing here" not in body
    assert "all caught up" not in body
