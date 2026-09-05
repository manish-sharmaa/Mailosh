"""HTTP-level tests for search (Task 1D, design spec §10): the results
page, the chips row that edits the query, the advanced panel, and the two
promises this surface makes — that no query 500s, and that no hint the
parser produced is swallowed.

Same shape as `tests/unit/test_mail_routes.py`: a real `create_app` (real
routers, real Jinja environment, real session/CSRF plumbing over a
file-backed aiosqlite db) with exactly two things faked,
`mailosh.web.auth.verify_password` and `deps.client_for`. No network call
is ever made, and `FakeClient` records every `query_search` call so the
filter that reached the server can be asserted on.

The *grammar* is not tested here — `tests/unit/test_search_query.py` owns
it, and the whole point of this module's design is that
`mailosh.web.search` never decides what a query means. What is tested here
is the seam: which filter the route sends, what it does with the parser's
hints and exclusions, and what the chips do to the query text.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient

from mailosh.jmap.client import QueryPage, Snippet
from mailosh.jmap.errors import MethodError
from mailosh.jmap.models import Address, EmailHeader, Mailbox
from mailosh.security.exchange import VerifiedAccount
from mailosh.services.mailbox_tree import build_nav
from mailosh.services.search_query import Hint, ParseResult
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps, search
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


def _mailbox(
    mailbox_id: str, name: str, role: str | None, sort_order: int, *, parent: str | None = None
) -> Mailbox:
    return Mailbox(
        id=mailbox_id,
        name=name,
        role=role,
        parent_id=parent,
        sort_order=sort_order,
        total_emails=0,
        unread_emails=0,
    )


def _mailboxes() -> list[Mailbox]:
    """Six role mailboxes plus a nested pair of user labels, so the
    resolver's "by path as well as by name" behaviour has something to
    resolve."""
    return [
        _mailbox("mb-inbox", "Inbox", "inbox", 10),
        _mailbox("mb-sent", "Sent", "sent", 20),
        _mailbox("mb-drafts", "Drafts", "drafts", 30),
        _mailbox("mb-archive", "Archive", "archive", 50),
        _mailbox("mb-junk", "Spam", "junk", 60),
        _mailbox("mb-trash", "Trash", "trash", 70),
        _mailbox("m-work", "Work", None, 80),
        _mailbox("m-clients", "Clients", None, 81, parent="m-work"),
    ]


def _header(email_id: str, thread_id: str, *, subject: str = "Q3 roadmap review") -> EmailHeader:
    return EmailHeader(
        id=email_id,
        thread_id=thread_id,
        mailbox_ids={"mb-inbox"},
        keywords=set(),
        from_=[Address(name="Priya Natarajan", email="priya@example.com")],
        subject=subject,
        received_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        preview="Attaching the deck we walked through",
        has_attachment=False,
    )


class FakeClient:
    """Stands in for the pooled `JmapClient`, recording every
    `query_search` call — the filter the route sent is the thing most of
    these tests are actually about."""

    def __init__(self, *, total: int = 1, snippet_error: bool = False) -> None:
        self.mailboxes = _mailboxes()
        self.threads = {"t1": [_header("e1", "t1")]}
        self.total = total
        self.searches: list[dict[str, object]] = []
        self.snippets: dict[str, Snippet] = {}
        #: Emulates a JMAP server with no `SearchSnippet/get`: the whole
        #: batch comes back as one `unknownMethod` error, exactly as
        #: `_call` would raise it.
        self.snippet_error = snippet_error

    @property
    def account_id(self) -> str:
        return ACCOUNT

    async def get_mailboxes(self) -> list[Mailbox]:
        return self.mailboxes

    async def query_search(self, **kwargs: object) -> QueryPage:
        self.searches.append(kwargs)
        if self.snippet_error and kwargs.get("snippets"):
            raise MethodError("unknownMethod", "n0")
        return QueryPage(
            thread_order=list(self.threads),
            total=self.total,
            emails_by_thread=self.threads,
            position=int(kwargs["position"]),  # type: ignore[arg-type]
            snippets=self.snippets if kwargs.get("snippets") else {},
        )


#: `None` is a meaningful `ParseResult.filter`, so it cannot double as
#: "argument not given".
_UNSET = object()


def _result(
    *,
    filter: object = _UNSET,
    hints: tuple[Hint, ...] = (),
    exclude: tuple[str, ...] = (),
    explicit: bool = False,
) -> ParseResult:
    return ParseResult(
        filter={"text": "x"} if filter is _UNSET else filter,  # type: ignore[arg-type]
        hints=hints,
        exclude_mailbox_ids=exclude,
        scope_was_explicit=explicit,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


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


def _stub_parser(monkeypatch, result: ParseResult) -> list[str]:
    """Replace the parser with one that returns `result` for anything, and
    hand back the list of query strings it was asked about.

    The seam is deliberately stubbed rather than driven through the real
    grammar: these tests are about what the *route* does with a
    `ParseResult`, and a route that only behaved correctly for queries the
    parser happens to compile today would be tested by accident.
    """
    seen: list[str] = []

    def fake_parse(text, *, resolver, now=None):
        seen.append(text)
        return result

    monkeypatch.setattr(search, "parse_query", fake_parse)
    return seen


# ---------------------------------------------------------------------------
# GET /search: the page, the fragment, and the empty box
# ---------------------------------------------------------------------------


def test_full_page_renders_the_shell_and_the_list(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    r = _login(app).get("/search?q=roadmap")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()
    assert 'id="main"' in r.text
    assert 'id="list"' in r.text
    # The list component, not a parallel one: the row contract every other
    # surface in this app binds to.
    assert 'data-id="t1"' in r.text
    assert 'class="chips-row"' in r.text


def test_hx_request_returns_the_fragment_and_pushes_the_url(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    r = _login(app).get("/search?q=roadmap", headers={"HX-Request": "true"})
    assert "<html" not in r.text
    assert 'id="list"' in r.text
    assert r.headers["HX-Push-Url"] == "/search?q=roadmap"


def test_history_restore_gets_a_full_page(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    r = _login(app).get(
        "/search?q=roadmap",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<html" in r.text


def test_html_answers_are_no_cache_and_vary_on_hx_request(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    r = _login(app).get("/search?q=roadmap")
    assert r.headers["Vary"] == "HX-Request"
    assert "no-cache" in r.headers["Cache-Control"]


def test_an_empty_query_queries_nothing_and_shows_the_operator_card(app, monkeypatch, fake):
    seen = _stub_parser(monkeypatch, _result())
    r = _login(app).get("/search")
    assert r.status_code == 200
    assert "Search your mail" in r.text
    assert "has:attachment" in r.text
    # Neither the parser nor the mail server is asked about nothing.
    assert seen == []
    assert fake.searches == []


def test_no_results_names_the_query_that_found_nothing(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result())
    fake.threads = {}
    fake.total = 0
    r = _login(app).get("/search?q=nothingmatches")
    assert "No results for" in r.text
    assert "nothingmatches" in r.text


# ---------------------------------------------------------------------------
# The filter that reaches the server
# ---------------------------------------------------------------------------


def test_the_parsers_filter_is_sent_verbatim(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result(filter={"subject": "roadmap"}))
    _login(app).get("/search?q=subject:roadmap")
    assert fake.searches[0]["filter"] == {"subject": "roadmap"}


def test_the_default_scope_exclusion_is_applied_by_the_caller(app, monkeypatch, fake):
    """`search_query` refuses to emit `inMailboxOtherThan` (it is not on its
    allow-list) and hands the exclusion back for the caller to apply. This
    is that caller doing it — without this the default scope would silently
    include Spam and Trash."""
    _stub_parser(monkeypatch, _result(filter={"text": "x"}, exclude=("mb-junk", "mb-trash")))
    _login(app).get("/search?q=x")
    assert fake.searches[0]["filter"] == {
        "operator": "AND",
        "conditions": [{"text": "x"}, {"inMailboxOtherThan": ["mb-junk", "mb-trash"]}],
    }


def test_an_explicit_scope_adds_no_exclusion(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result(filter={"inMailbox": "mb-junk"}, explicit=True))
    _login(app).get("/search?q=in:spam")
    assert fake.searches[0]["filter"] == {"inMailbox": "mb-junk"}


def test_a_filterless_query_still_searches_within_the_default_scope(app, monkeypatch, fake):
    """`filter=None` means "constrains nothing", not "match nothing" — with
    an exclusion still to apply, that is a real search for everything
    except Spam and Trash."""
    _stub_parser(monkeypatch, _result(filter=None, exclude=("mb-junk",)))
    _login(app).get("/search?q=-in:spam")
    assert fake.searches[0]["filter"] == {"inMailboxOtherThan": ["mb-junk"]}


# ---------------------------------------------------------------------------
# Hints
# ---------------------------------------------------------------------------


def test_every_hint_is_rendered(app, monkeypatch):
    _stub_parser(
        monkeypatch,
        _result(
            hints=(
                Hint("approximated", "Attachment names are not searched separately.", "filename:x"),
                Hint("unknown-label", "There is no label called that.", "label:Nope"),
            )
        ),
    )
    body = _login(app).get("/search?q=filename:x label:Nope").text
    assert "Attachment names are not searched separately." in body
    assert "There is no label called that." in body
    # The token is echoed beside the message, so a hint points at the term
    # that caused it rather than at the query as a whole.
    assert "filename:x" in body
    assert "label:Nope" in body


def test_a_hint_carrying_markup_is_escaped(app, monkeypatch):
    _stub_parser(
        monkeypatch, _result(hints=(Hint("unknown-operator", "Nope", "<img src=x onerror=1>"),))
    )
    body = _login(app).get("/search?q=x").text
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body


def test_a_nonsense_query_is_a_page_with_a_hint_not_a_500(app):
    """The real parser, not the stub: this is the promise spec §10 makes
    about every malformed thing a person can type."""
    r = _login(app).get('/search?q=((( OR label: before:notadate larger:12x "unclosed')
    assert r.status_code == 200
    assert 'class="search-hints"' in r.text


# ---------------------------------------------------------------------------
# The chips row edits the query
# ---------------------------------------------------------------------------


def _chip_hrefs(body: str) -> list[str]:
    row = re.search(r'<div class="chips-row".*?</div>\s*$', body, re.S)
    return re.findall(r'href="(/search[^"]*)"', row.group(0) if row else body)


def test_a_toggle_chip_adds_its_operator_to_the_query(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    body = _login(app).get("/search?q=roadmap").text
    assert "/search?q=roadmap%20is%3Aunread" in body
    assert "/search?q=roadmap%20has%3Aattachment" in body


def test_a_lit_toggle_chip_removes_its_operator(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    body = _login(app).get("/search?q=roadmap is:unread").text
    assert 'aria-pressed="true"' in body
    # The same chip, now offering the query without the operator.
    assert "/search?q=roadmap</a>" in body or 'href="/search?q=roadmap"' in body


def test_the_time_menu_replaces_every_other_date_operator(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    body = _login(app).get("/search?q=x before:2026-01-01").text
    # Picking "Past week" clears `before:` rather than ANDing a second,
    # contradictory date term onto it.
    assert "/search?q=x%20newer_than%3A7d" in body


def test_the_time_chip_shows_a_value_it_did_not_offer_as_the_whole_token(app, monkeypatch):
    """`before:notadate` in a chip labelled with a time would read as a
    date this app understood. It is not one — the parser dropped it and
    said so — so the chip shows what the reader actually typed."""
    _stub_parser(monkeypatch, _result())
    body = _login(app).get("/search?q=before:notadate").text
    assert ">before:notadate" in body.replace("\n", "").replace("  ", "")


def test_the_time_chip_names_a_value_it_did_offer(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    assert "Past week" in _login(app).get("/search?q=newer_than:7d").text


def test_the_label_menu_offers_every_label_by_path(app, monkeypatch):
    _stub_parser(monkeypatch, _result())
    body = _login(app).get("/search?q=x").text
    assert "/search?q=x%20label%3AWork" in body
    assert "label%3AWork%2FClients" in body


def test_the_default_scope_is_stated_and_escapable(app, monkeypatch):
    _stub_parser(monkeypatch, _result(explicit=False))
    body = _login(app).get("/search?q=x").text
    assert "Excluding Spam" in body
    assert "/search?q=x%20in%3Aanywhere" in body


def test_a_stated_scope_drops_the_note(app, monkeypatch):
    _stub_parser(monkeypatch, _result(explicit=True))
    assert "Excluding Spam" not in _login(app).get("/search?q=in:inbox x").text


# ---------------------------------------------------------------------------
# GET /search/refine and GET /search/build
# ---------------------------------------------------------------------------


def test_refine_replaces_one_operator_and_pushes_the_new_url(app, monkeypatch):
    seen = _stub_parser(monkeypatch, _result())
    r = _login(app).get(
        "/search/refine?q=roadmap+from%3Aold&field=from&value=priya",
        headers={"HX-Request": "true"},
    )
    assert seen[-1] == "roadmap from:priya"
    assert r.headers["HX-Push-Url"] == "/search?q=roadmap%20from%3Apriya"


def test_refine_with_a_blank_value_removes_the_operator(app, monkeypatch):
    seen = _stub_parser(monkeypatch, _result())
    _login(app).get("/search/refine?q=roadmap+from%3Apriya&field=from&value=")
    assert seen[-1] == "roadmap"


def test_refine_ignores_a_field_it_does_not_own(app, monkeypatch):
    """A chip is what reaches this URL; the worst a stale one may do is
    re-run the search the reader already had."""
    seen = _stub_parser(monkeypatch, _result())
    _login(app).get("/search/refine?q=roadmap&field=inMailbox&value=mb-junk")
    assert seen[-1] == "roadmap"


def test_the_advanced_panel_composes_a_query_a_reader_could_have_typed(app, monkeypatch):
    seen = _stub_parser(monkeypatch, _result())
    r = _login(app).get(
        "/search/build?from=priya&subject=Q3+plan&words=deck&without=draft"
        "&size_op=larger&size=5&size_unit=m&within=7d&scope=inbox&attachment=1",
        headers={"HX-Request": "true"},
    )
    assert seen[-1] == (
        'from:priya subject:"Q3 plan" deck -draft larger:5m newer_than:7d in:inbox has:attachment'
    )
    # ...and the reader lands on that query's own shareable URL, not on
    # `/search/build`'s.
    assert urlsplit(r.headers["HX-Push-Url"]).path == "/search"


def test_the_advanced_panel_reads_a_label_scope_as_a_label(app, monkeypatch):
    seen = _stub_parser(monkeypatch, _result())
    _login(app).get("/search/build?scope=Work%2FClients")
    assert seen[-1] == "label:Work/Clients"


def test_the_advanced_panel_renders_prefilled_from_the_current_url(app, monkeypatch):
    r = _login(app).get(
        "/search/advanced",
        headers={"HX-Request": "true", "HX-Current-URL": "http://x/search?q=from%3Apriya"},
    )
    assert r.status_code == 200
    assert 'name="from" value="priya"' in r.text
    # The "Search in" select mixes the scope words with this account's own
    # labels, by path.
    assert "Work/Clients" in r.text


def test_the_advanced_panel_never_reaches_the_mail_server_with_a_query(app, monkeypatch, fake):
    _login(app).get("/search/advanced?q=anything")
    assert fake.searches == []


# ---------------------------------------------------------------------------
# GET /search/rows
# ---------------------------------------------------------------------------


def test_rows_carry_the_query_into_the_sentinel(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result())
    fake.total = 300
    body = _login(app).get("/search/rows?q=roadmap&position=0&limit=50").text
    sentinel = re.search(r'<div class="row-sentinel"\s+hx-get="([^"]+)"', body)
    assert sentinel is not None
    params = parse_qs(urlsplit(sentinel.group(1)).query)
    assert params["q"] == ["roadmap"]
    assert params["position"] == ["50"]


def test_rows_re_render_the_range_and_the_nav_out_of_band(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result())
    fake.total = 300
    body = _login(app).get("/search/rows?q=roadmap").text
    assert 'id="list-range"' in body
    assert 'hx-swap-oob="true"' in body
    assert "<title>" in body


def test_the_page_size_is_clamped(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result())
    _login(app).get("/search?q=x&limit=100000")
    assert fake.searches[0]["limit"] == search.MAX_PAGE_SIZE


# ---------------------------------------------------------------------------
# SearchSnippet highlights
# ---------------------------------------------------------------------------


def test_highlights_replace_the_rows_own_subject_and_preview(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result())
    fake.snippets = {"t1": Snippet(subject="Q3 <mark>roadmap</mark>", preview=None)}
    body = _login(app).get("/search?q=roadmap").text
    assert "Q3 <mark>roadmap</mark>" in body
    # `preview` came back null, so that half falls back to the message's
    # own text rather than rendering empty.
    assert "Attaching the deck we walked through" in body


def test_a_snippet_is_re_escaped_by_us(app, monkeypatch, fake):
    """The server's escaping is not something this client can check, and
    this is a mail client. Everything but `<mark>` is neutralised here."""
    _stub_parser(monkeypatch, _result())
    fake.snippets = {
        "t1": Snippet(subject="<mark>x</mark><img src=x onerror=alert(1)>", preview=None)
    }
    body = _login(app).get("/search?q=x").text
    assert "<img src=x" not in body
    assert "<mark>x</mark>" in body


def test_unbalanced_marks_are_balanced(app, monkeypatch, fake):
    _stub_parser(monkeypatch, _result())
    fake.snippets = {"t1": Snippet(subject="a</mark>b<mark>c", preview=None)}
    body = _login(app).get("/search?q=x").text
    assert body.count("<mark>") == body.count("</mark>")


def test_a_server_without_search_snippets_still_returns_results(app, monkeypatch, sqlite_url):
    """RFC 8621 §5 is not separately advertised — a server that has not
    implemented it says so by erroring the batch. Losing the highlights is
    the right price; losing the page is not."""

    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    _stub_parser(monkeypatch, _result())
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    client = FakeClient(snippet_error=True)
    application.dependency_overrides[deps.client_for] = lambda: client
    with TestClient(application):
        r = _login(application).get("/search?q=roadmap")
    assert r.status_code == 200
    assert 'data-id="t1"' in r.text
    # Asked once with snippets, once without — never a third time.
    assert [call.get("snippets", False) for call in client.searches] == [True, False]


# ---------------------------------------------------------------------------
# The resolver handed to the parser
# ---------------------------------------------------------------------------


def _nav():
    async def go():
        return await build_nav(FakeClient(), active_key="", label_meta={})

    return asyncio.run(go())


def test_the_resolver_answers_both_spellings_of_the_spam_role():
    """Spec §10's operator is `in:spam`; RFC 8621's role is `junk`. A
    resolver that answered only one of them would make `in:spam` return
    nothing, which is indistinguishable from an account with no Spam
    folder."""
    resolver = search.NavResolver(_nav())
    assert resolver.by_role("spam") == "mb-junk"
    assert resolver.by_role("junk") == "mb-junk"
    assert resolver.by_role("inbox") == "mb-inbox"
    assert resolver.by_role("nope") is None


def test_the_resolver_matches_a_label_by_name_and_by_path():
    resolver = search.NavResolver(_nav())
    assert resolver.by_label_name("Clients") == "m-clients"
    assert resolver.by_label_name("work/clients") == "m-clients"
    assert resolver.by_label_name("  WORK  ") == "m-work"
    assert resolver.by_label_name("Nope") is None


# ---------------------------------------------------------------------------
# The key registry
# ---------------------------------------------------------------------------


def test_the_search_key_is_live_in_the_registry():
    """`/` was reserved-but-unavailable while search had no page. Flipping
    that word is what puts it in the `?` overlay and in dispatch, so this
    pins the two together the way `test_mail_routes.py` pins the rest of
    the table."""
    source = pathlib.Path("mailosh/web/static/js/keys.js").read_text(encoding="utf-8")
    table = source[source.index("const DEFAULTS = [") + len("const DEFAULTS = ") :]
    entries = json.loads(table[: table.index("\n];") + 2])
    entry = next(e for e in entries if e["id"] == "search")
    assert entry["keys"] == ["/"]
    assert entry["available"] is True
    assert 'data-role="search-input"' in source
