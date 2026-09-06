"""HTTP-level tests for `GET /t/{thread_id}` (design spec §7): the card
contract later tasks bind to (`id="msg-{id}"`, `data-first-unread`,
`data-action="star"`), the HTML-vs-text body split, the per-message menu's
deliberately short list, escaping of hostile mail, and the fragment/full-page
split the shell depends on.

Everything runs against a real `create_app` — real routers, real Jinja
environment, real session/CSRF plumbing over a file-backed aiosqlite db —
with exactly two things faked: `mailosh.web.auth.verify_password` (no
Stalwart) and `deps.client_for` (a `FakeClient` in place of the pooled
`JmapClient`). No network call is ever made.

The client is `httpx.AsyncClient` over `ASGITransport` rather than
`TestClient`, and the app's lifespan is entered by hand: that keeps the
engine, the sessionmaker and every request on pytest-asyncio's own event
loop, which is the loop these `async def` tests await on.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from conftest import make_settings
from helpers import parse_attrs
from httpx import ASGITransport, AsyncClient

from mailosh.jmap.errors import TransportError
from mailosh.jmap.models import Address, EmailBody, Mailbox
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps
from mailosh.web.app import create_app

ACCOUNT = "acct-1"
ME = "d@x"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdmin:
    """`StalwartAdmin` stand-in — see `tests/unit/test_auth_routes.py`."""

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        return ApiKey(id="k1", secret="API_secret_1")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None


def _mailbox(mailbox_id: str, name: str, role: str | None, sort_order: int) -> Mailbox:
    return Mailbox(
        id=mailbox_id,
        name=name,
        role=role,
        sort_order=sort_order,
        total_emails=0,
        unread_emails=0,
    )


class FakeClient:
    """Stands in for the pooled `JmapClient`.

    `thread(...)` registers a conversation from a compact spec: a bare id
    for a plain-text message, or `(id, html, text)` when a test cares which
    body type it gets. `raise_on_thread` makes the fetch fail the way an
    unreachable Stalwart does.
    """

    def __init__(self) -> None:
        self.threads: dict[str, list[EmailBody]] = {}
        self.raise_on_thread: Exception | None = None
        self.thread_calls: list[str] = []

    @property
    def account_id(self) -> str:
        return ACCOUNT

    def thread(
        self,
        thread_id: str,
        specs: list,
        *,
        subject: str = "Offsite agenda",
        unread: tuple[str, ...] | list[str] = (),
        labelled: tuple[str, ...] | list[str] = (),
        starred: tuple[str, ...] | list[str] = (),
        return_path: str | None = None,
        auth_results: str | None = None,
    ) -> None:
        messages = []
        for minute, spec in enumerate(specs):
            email_id, html, text = spec if isinstance(spec, tuple) else (spec, None, "plain body")
            keywords = set()
            if email_id not in unread:
                keywords.add("$seen")
            if email_id in starred:
                keywords.add("$flagged")
            mailbox_ids = {"mb-inbox"}
            if email_id in labelled:
                mailbox_ids.add("m-work")
            messages.append(
                EmailBody(
                    id=email_id,
                    thread_id=thread_id,
                    mailbox_ids=mailbox_ids,
                    keywords=keywords,
                    from_=[Address(name="Priya Natarajan", email="priya@example.com")],
                    to=[Address(name="Demo", email=ME)],
                    subject=subject,
                    received_at=datetime(2026, 9, 2, 10, tzinfo=UTC) + timedelta(minutes=minute),
                    preview="Attaching the deck we walked through",
                    has_attachment=False,
                    text_body=text,
                    html_body=html,
                    return_path=return_path,
                    auth_results=auth_results,
                )
            )
        self.threads[thread_id] = messages

    async def get_mailboxes(self) -> list[Mailbox]:
        return [
            _mailbox("mb-inbox", "Inbox", "inbox", 10),
            _mailbox("mb-archive", "Archive", "archive", 50),
            _mailbox("mb-trash", "Trash", "trash", 70),
            _mailbox("m-work", "Work", None, 80),
        ]

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
        self.thread_calls.append(thread_id)
        if self.raise_on_thread is not None:
            raise self.raise_on_thread
        return self.threads.get(thread_id, [])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def application(monkeypatch, sqlite_url, fake):
    async def fake_verify(url, username, password):
        return VerifiedAccount(username, "acc1", username) if password == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    app = create_app(settings=make_settings(sqlite_url))
    # Set before the lifespan runs: it only builds its own admin when the
    # attribute is absent.
    app.state.admin = FakeAdmin()
    app.dependency_overrides[deps.client_for] = lambda: fake
    return app


@pytest_asyncio.fixture
async def authed(application):
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            login = await client.post("/login", data={"username": ME, "password": "right"})
            assert login.status_code == 303, login.text
            yield client


# ---------------------------------------------------------------------------
# The card contract
# ---------------------------------------------------------------------------


async def test_thread_page_renders_one_card_per_message_with_stable_ids(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"])
    html = (await authed.get("/t/T1")).text
    assert html.count('id="msg-E') == 3
    assert 'id="msg-E1"' in html


async def test_cards_are_ordered_oldest_first(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"])
    html = (await authed.get("/t/T1")).text
    assert re.findall(r'id="msg-(E\d)"', html) == ["E1", "E2", "E3"]


async def test_only_the_newest_card_is_open_when_everything_has_been_read(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"])
    html = (await authed.get("/t/T1")).text
    opened = re.findall(r'<details class="msg-fold"( open)?>', html)
    assert opened == ["", "", " open"]


async def test_every_unread_card_is_open_alongside_the_newest(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"], unread=["E1"])
    html = (await authed.get("/t/T1")).text
    opened = re.findall(r'<details class="msg-fold"( open)?>', html)
    assert opened == [" open", "", " open"]


async def test_html_messages_get_a_frame_placeholder_and_text_messages_do_not(authed, fake):
    """One placeholder per HTML message, each aimed at that message's own
    frame fragment — and none at all for a text body, which this view
    renders inline.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None), ("E2", None, "plain")])
    html = (await authed.get("/t/T1")).text

    placeholders = [attrs for attrs in parse_attrs(html)["div"] if "hx-get" in attrs]
    assert [attrs["hx-get"] for attrs in placeholders] == ["/m/E1/frame?thread=T1"]
    assert [attrs["id"] for attrs in placeholders] == ["frame-E1"]
    assert "plain" in html


async def test_the_conversation_page_frames_nothing_itself(authed, fake):
    """The `<iframe>` and its `sandbox` list live in `thread/frame.html`
    alone, behind `GET /m/{id}/frame`. Two hand-written copies of that
    attribute is how one of them comes to differ from the other, and the
    one that matters is the omission of `allow-same-origin`.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    html = (await authed.get("/t/T1")).text
    assert parse_attrs(html).get("iframe", []) == []
    assert "/m/E1/html" not in html


async def test_the_placeholder_asserts_nothing_about_remote_images(authed, fake):
    """`remote` is absent, not `0`. The reader's `remote_images` policy and
    their per-sender allow list are what decide, and a hardcoded `0` here
    would overrule a reader who chose "always show" before the route this
    fragment comes from ever saw the request.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    (placeholder,) = [
        attrs for attrs in parse_attrs((await authed.get("/t/T1")).text)["div"] if "hx-get" in attrs
    ]
    assert "remote" not in placeholder["hx-get"]
    assert placeholder["hx-swap"] == "outerHTML"
    assert placeholder["hx-trigger"] == "intersect once"


async def test_subject_and_body_from_a_hostile_message_are_escaped_in_the_shell(authed, fake):
    fake.thread(
        "T1",
        [("E1", None, "<script>alert(1)</script>")],
        subject="<img src=x onerror=alert(1)>",
    )
    html = (await authed.get("/t/T1")).text
    # Nothing the sender wrote became markup: their script tag is inert
    # text, and no element anywhere in the document carries an inline event
    # handler (which the CSP would refuse to run in any case, but the point
    # is that the attribute never gets written).
    assert "<script>alert(1)</script>" not in html
    assert re.search(r"<[^>]*\son\w+=", html) is None
    # ...and both are still shown, escaped, where they belong: the body
    # once, the subject in the tab title and again in the heading.
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 1
    assert html.count("&lt;img src=x onerror=alert(1)&gt;") == 2


async def test_a_quoted_run_is_folded_behind_the_pill(authed, fake):
    fake.thread("T1", [("E1", None, "Sounds good.\n\nOn Mon, Dan wrote:\n> earlier")])
    html = (await authed.get("/t/T1")).text
    assert html.count('class="quote-toggle"') == 1
    assert html.count('class="q1"') == 1


# ---------------------------------------------------------------------------
# Per-message controls
# ---------------------------------------------------------------------------


async def test_menu_offers_only_the_actions_this_task_implements(authed, fake):
    """Scoped to the per-message menu, not the whole page.

    It used to assert "Reply all"/"Forward" appeared nowhere in the
    response, which held only while nothing anywhere could reply. Phase 1C
    put both in the conversation's action bar, and a whole-page assertion
    then failed for a change it was never about — the menu is still exactly
    as it was. Narrowed to `details.msg-menu` so it keeps testing the menu's
    contents and stops testing the rest of the page.
    """
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    menus = re.findall(r'<details class="msg-menu".*?</details>', html, re.S)
    assert len(menus) == 1
    menu = menus[0]
    for present in ("Mark unread from here", "View source", "Delete message"):
        assert menu.count(present) == 1
    for absent in ("Reply all", "Forward", "Print"):
        assert absent not in menu


async def test_the_conversation_bar_offers_reply_reply_all_and_forward(authed, fake):
    """The buttons for `r` / `a` / `f`, which existed as keys before they
    existed as controls.

    They carry `data-compose-reply` and no `hx-*` of their own: opening goes
    through `compose.js`, which owns where the card lands and the limit on
    how many are open. A button with its own `hx-get` would bypass both.
    """
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    for mode in ("reply", "reply_all", "forward"):
        assert f'data-compose-reply="{mode}"' in html
    bar = re.search(r'<div class="list-toolbar".*?</div>', html, re.S)
    assert bar is not None
    assert 'hx-get="/compose' not in bar.group(0)


async def test_delete_message_posts_only_that_message(authed, fake):
    fake.thread("T1", ["E1", "E2"])
    html = (await authed.get("/t/T1")).text
    forms = re.findall(r'<form class="menu-form" hx-post="/a/delete".*?</form>', html, re.S)
    assert len(forms) == 2
    assert re.findall(r'name="ids" value="(E\d)"', forms[0]) == ["E1"]
    assert re.findall(r'name="ids" value="(E\d)"', forms[1]) == ["E2"]


async def test_mark_unread_from_here_covers_this_message_and_every_later_one(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"])
    html = (await authed.get("/t/T1")).text
    forms = re.findall(r'<form class="menu-form" hx-post="/a/read".*?</form>', html, re.S)
    assert [re.findall(r'name="ids" value="(E\d)"', form) for form in forms] == [
        ["E1", "E2", "E3"],
        ["E2", "E3"],
        ["E3"],
    ]
    # ...and unread, never read: `on=0` is what makes it "mark unread".
    assert all('name="on" value="0"' in form for form in forms)


async def test_every_message_has_a_star_control_pointing_at_the_action_route(authed, fake):
    fake.thread("T1", ["E1", "E2"])
    html = (await authed.get("/t/T1")).text
    # One per message card, plus the conversation toolbar's own (spec §7:
    # the same action bar as the list), which acts on the whole thread.
    assert len(re.findall(r'class="[^"]*\bmsg-star\b[^"]*"', html)) == 2
    assert html.count('data-action="star"') == 3


async def test_an_already_starred_message_says_so_so_a_click_unstars_it(authed, fake):
    fake.thread("T1", ["E1", "E2"], starred=["E2"])
    html = (await authed.get("/t/T1")).text
    pressed = re.findall(r'data-action="star"\s+aria-pressed="(\w+)"', html)
    assert pressed == ["false", "true"]


async def test_the_first_unread_card_is_the_scroll_target(authed, fake):
    fake.thread("T1", [("E1", None, "a"), ("E2", None, "b")], unread=["E2"])
    html = (await authed.get("/t/T1")).text
    assert html.count("data-first-unread") == 1
    marked = [
        m.group(1)
        for m in re.finditer(r'<article[^>]*\bid="msg-(E\d)"[^>]*\bdata-first-unread', html)
    ]
    assert marked == ["E2"]


async def test_an_all_read_conversation_still_has_somewhere_to_scroll_to(authed, fake):
    fake.thread("T1", ["E1", "E2"])
    html = (await authed.get("/t/T1")).text
    marked = [
        m.group(1)
        for m in re.finditer(r'<article[^>]*\bid="msg-(E\d)"[^>]*\bdata-first-unread', html)
    ]
    assert marked == ["E2"]


# ---------------------------------------------------------------------------
# The details popover
# ---------------------------------------------------------------------------


async def test_details_popover_omits_rows_whose_header_is_missing(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    assert "mailed-by" not in html and "signed-by" not in html


async def test_details_popover_shows_provenance_when_the_headers_are_there(authed, fake):
    fake.thread(
        "T1",
        ["E1"],
        return_path="<bounce@news.test>",
        auth_results="mx.test; dkim=pass header.d=list.test",
    )
    html = (await authed.get("/t/T1")).text
    assert html.count("mailed-by") == 1 and html.count("signed-by") == 1
    assert ">news.test<" in html and ">list.test<" in html


async def test_the_viewer_is_named_me_in_the_recipient_line(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    assert ME not in html.split('class="msg-details-toggle"', 1)[1].split("</summary>", 1)[0]
    assert html.count('class="msg-details-toggle"') == 1


# ---------------------------------------------------------------------------
# The conversation header
# ---------------------------------------------------------------------------


async def test_the_conversation_header_chips_the_labels_its_messages_carry(authed, fake):
    fake.thread("T1", ["E1", "E2"], labelled=["E2"])
    html = (await authed.get("/t/T1")).text
    chips = re.findall(r'<span class="chip"[^>]*>([^<]+)</span>', html)
    assert [chip.strip() for chip in chips] == ["Work"]


async def test_an_unlabelled_conversation_renders_no_chip_container(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    assert "thread-chips" not in html


# ---------------------------------------------------------------------------
# Missing conversations, failures, fragments
# ---------------------------------------------------------------------------


async def test_an_empty_thread_is_a_404_page_not_an_empty_conversation(authed, fake):
    page = await authed.get("/t/T404")
    assert page.status_code == 404
    assert "msg-" not in page.text


async def test_a_jmap_failure_reaches_the_global_error_surface_not_a_traceback(authed, fake):
    fake.raise_on_thread = TransportError("stalwart unreachable")
    page = await authed.get("/t/T1")
    assert page.status_code == 502 and "<html" in page.text.lower()
    frag = await authed.get("/t/T1", headers={"HX-Request": "true"})
    assert frag.status_code == 200 and "om:error" in frag.headers["HX-Trigger"]


async def test_fragment_vs_full_page_split_is_preserved(authed, fake):
    fake.thread("T1", ["E1"])
    full = await authed.get("/t/T1")
    frag = await authed.get("/t/T1", headers={"HX-Request": "true"})
    assert "<!doctype html>" in full.text.lower()
    assert "<!doctype html>" not in frag.text.lower()
    assert frag.headers["HX-Push-Url"] == "/t/T1"


async def test_the_conversation_stays_a_cacheable_prefetchable_get(authed, fake):
    fake.thread("T1", ["E1"])
    page = await authed.get("/t/T1")
    assert page.headers["cache-control"] == "private, max-age=60"
    assert page.headers["vary"].lower().find("hx-request") != -1
    # A prefetch must not mark anything read: this route only ever reads.
    assert fake.thread_calls == ["T1"]


async def test_the_action_bar_carries_every_message_id_in_the_conversation(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"])
    html = (await authed.get("/t/T1")).text
    ids = re.search(r'class="thread-scroll"[^>]*data-email-ids="([^"]*)"', html)
    assert ids is not None and ids.group(1) == "E1,E2,E3"


async def test_the_mailbox_the_reader_came_from_stays_selected(authed, fake):
    fake.thread("T1", ["E1"])
    html = (
        await authed.get(
            "/t/T1",
            headers={"HX-Request": "true", "HX-Current-URL": "http://test/mail/archive"},
        )
    ).text
    active = re.findall(r'<a href="(/mail/\w+)"[^>]*aria-current="page"', html)
    assert active == ["/mail/archive"]


# ---------------------------------------------------------------------------
# The conversation's own keyboard (`thread` scope, spec §6.1)
#
# `n` `p` `;` `:` and `Shift+U` act on the cards this route renders, so what
# holds them together is the card contract the tests above already pin —
# `id="msg-{id}"`, `data-email-ids`, `data-first-unread`. There is no JS
# runtime here (Global Constraints), so `keys.js`'s half is read as control
# flow, and the half that matters is *which markup it reaches for*: a
# selector naming a class the stylesheet owns would keep passing here and
# stop working the first time that class was renamed.
# ---------------------------------------------------------------------------

KEYS_JS = pathlib.Path("mailosh/web/static/js/keys.js")


def _keys_block(header: str) -> str:
    """One top-level `function`/`const` body from `keys.js`, minus its
    whole-line comments — this file explains itself at length, and a rule
    about what the code does must not be satisfiable by prose."""
    source = KEYS_JS.read_text()
    found = re.search(rf"^{header}\s*\{{$(.*?)^\}};?$", source, re.M | re.S)
    assert found is not None, header
    body = re.sub(r"/\*.*?\*/", "", found.group(1), flags=re.S)
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("//"))


def _registry_entry(entry_id: str) -> dict:
    """One `DEFAULTS` entry, parsed as the JSON that table is deliberately
    written as — never sliced out of the file as text, which would let one
    entry's assertion read a neighbour's `available` flag."""
    block = re.search(r"^const DEFAULTS = (\[.*?^\]);$", KEYS_JS.read_text(), re.M | re.S)
    assert block is not None, "keys.js no longer declares a DEFAULTS table"
    found = [entry for entry in json.loads(block.group(1)) if entry["id"] == entry_id]
    assert len(found) == 1, entry_id
    return found[0]


@pytest.mark.parametrize(
    "entry_id,binding",
    [
        ("next-message", "n"),
        ("prev-message", "p"),
        ("expand-all", ";"),
        ("collapse-all", ":"),
        ("mark-unread-from-here", "Shift+U"),
    ],
)
def test_the_conversation_keys_are_live_and_scoped_to_a_conversation(entry_id, binding):
    entry = _registry_entry(entry_id)
    assert entry["available"] is True
    assert entry["scope"] == "thread"
    assert entry["keys"] == [binding]
    # Live means it has somewhere to go: an available entry whose runner is
    # `() => undefined` is the unavailable case wearing a costume — it would
    # show up in the `?` overlay promising a key that does nothing.
    runners = _keys_block("const RUNNERS =")
    runner = re.search(rf'^  "{re.escape(entry_id)}": (.*),$', runners, re.M)
    assert runner is not None, entry_id
    assert runner.group(1) != "() => undefined"


@pytest.mark.parametrize(
    ("entry_id", "mode"),
    [("reply", "reply"), ("reply-all", "reply_all"), ("forward", "forward")],
)
def test_reply_reply_all_and_forward_shipped_with_the_compose_card(entry_id, mode):
    """`r`/`a`/`f` were reserved-and-silent until 1C; they now open the
    inline composer card at the end of the conversation.

    Spec §3's rule has not changed — the UI never advertises a key that
    does nothing — so this asserts the two halves that make the key real:
    it is `available`, and its runner reaches `compose.js` in the mode the
    key means. An `available` entry whose runner is `() => undefined` is
    the unavailable case wearing a costume.

    The mode strings are `mailosh.services.compose.build_reply`'s own
    (`"reply"`/`"reply_all"`/`"forward"`) and travel to it untouched
    through `GET /compose/reply/{id}?mode=`, so a rename there has to
    change this line.
    """
    entry = _registry_entry(entry_id)
    assert entry["available"] is True
    assert entry["scope"] == "thread"
    runners = _keys_block("const RUNNERS =")
    runner = re.search(rf'^  "?{re.escape(entry_id)}"?: (.*),$', runners, re.M)
    assert runner is not None, entry_id
    assert runner.group(1) == f'() => composeCall("reply", "{mode}")'


def test_the_conversation_keys_find_their_cards_by_contract_not_by_class():
    """`.msg` is styling; `data-email-ids` is the card's contract with
    `actions.js` (it is what an action on that one message posts) and
    `data-thread-id` is the wrapper's. Keying off the class would take every
    key here down with a rename nothing else in the suite would notice.
    """
    source = KEYS_JS.read_text()
    selector = re.search(r'const CARD_SELECTOR = "([^"]+)";', source)
    assert selector is not None, "keys.js no longer names the card selector"
    assert selector.group(1) == "[data-thread-id] article[data-email-ids]"
    # One spelling: the selector text exists only in that declaration, and
    # every lookup goes through the name instead.
    assert source.count(selector.group(1)) == 1
    assert source.count("CARD_SELECTOR") > 1
    # ...and no card lookup falls back to a stylesheet class.
    assert '".msg' not in source and "'.msg" not in source


async def test_the_selector_the_keys_use_matches_every_card_and_only_cards(authed, fake):
    """The other half of that contract, against real rendered markup: the
    conversation wrapper carries `data-email-ids` too — the whole thread —
    and `n`/`p` must not be able to land on it.
    """
    fake.thread("T1", ["E1", "E2", "E3"], unread=("E2", "E3"))
    html = (await authed.get("/t/T1")).text

    cards = re.findall(r"<article\b[^>]*>", html)
    assert len(cards) == 3
    assert all("data-email-ids=" in card for card in cards)
    # Exactly one message id each — the card is the message, not the thread.
    assert [re.search(r'data-email-ids="([^"]*)"', card).group(1) for card in cards] == [
        "E1",
        "E2",
        "E3",
    ]

    # The wrapper is not an `<article>`, so it is out of reach of the cursor.
    wrapper = re.search(r'<div class="thread-scroll"[^>]*>', html)
    assert wrapper is not None
    assert "data-thread-id=" in wrapper.group(0)

    # And the card the conversation opens at — where `n` starts counting
    # from — is marked once, on a card.
    assert html.count("data-first-unread") == 1
    assert "data-first-unread" in cards[1]


def test_the_cursor_starts_where_the_conversation_opened():
    """`n` as the very first key after `o` has to mean something. Focus is
    the truth once the reader has moved; before that it is the server's own
    choice of opening card, and only then the newest message.
    """
    body = _keys_block(r"function currentCard\(\)")
    held = body.index("activeElement")
    opened = body.index("data-first-unread")
    assert held < opened
    # The fallback is the newest message (the cards run oldest -> newest),
    # not the oldest — a fully-read conversation opens at its last card.
    assert body.rindex("all.length - 1") > opened


def test_expand_all_reaches_the_fold_and_not_the_menu_or_the_quote_pill():
    """A card holds three `<details>`: the fold, the ⋮ menu beside it and
    the quoted-text pill nested in its body. `;` opens the fold — document
    order is what tells them apart without naming a class.
    """
    body = _keys_block(r"function foldAll\(open\)")
    assert 'querySelector("details")' in body
    assert "querySelectorAll" not in body
    assert "fold.open = open" in body

    runners = _keys_block("const RUNNERS =")
    assert re.search(r'^  "expand-all": \(\) => foldAll\(true\),$', runners, re.M)
    assert re.search(r'^  "collapse-all": \(\) => foldAll\(false\),$', runners, re.M)


def test_mark_unread_from_here_takes_this_card_and_every_one_after_it():
    """ "From here" means the tail of the conversation — the reader is
    putting the rest of it back on their pile, not just this one card. Same
    id set as the ⋮ menu's own item, through the same action layer.
    """
    body = _keys_block(r"function markUnreadFromHere\(\)")
    assert "all.slice(at)" in body
    assert "dataset.emailIds" in body
    assert 'window.om?.act?.("unread", ids)' in body
    # Nothing is posted when the cursor is on no card at all.
    assert body.index("if (at === -1) return undefined;") < body.index("slice(at)")
