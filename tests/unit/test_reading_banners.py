"""The two strips above a framed message body (`thread/banner.html`): the
blocked-remote-images banner and the dark-restyle "Original colours"
toggle.

Two levels, because the two strips are wired from two different places.

The template is exercised directly through the real Jinja environment: it
self-guards on its own context, and "renders nothing at all when neither
set was passed" is the property that lets one file be included from two
scopes without either drawing the other's markup. Nothing there needs a
route.

The restyle strip is *also* exercised through `GET /t/{thread_id}`,
because that is the only view that knows the sender it posts, and because
where it sits in the document is the whole design: outside
`#frame-{id}` (so a "Show images" swap cannot take it away) and inside
`#frame-area-{id}` (so its own swap can).

The remote strip has no route-level test here on purpose: it is rendered
by `GET /m/{id}/frame`, whose sanitise produces the count, and the tests
that the count reaching it is that pass's own live beside that route in
`tests/unit/test_frame_routes.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
import pytest_asyncio
from conftest import make_settings
from helpers import parse_attrs
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from mailosh.db import repo
from mailosh.db.models import AppUser
from mailosh.jmap.models import Address, EmailBody, Mailbox
from mailosh.render.html_sanitize import SanitizeContext, sanitize_email_html
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey
from mailosh.ui.env import build_env
from mailosh.web import deps
from mailosh.web.app import create_app

ME = "d@x"
STATIC = "mailosh/web/static"


@pytest.fixture(scope="module")
def banner():
    return build_env(STATIC).get_template("thread/banner.html")


# ---------------------------------------------------------------------------
# Reading the rendered strips
# ---------------------------------------------------------------------------


_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _Strips(HTMLParser):
    """Every `.frame-banner` with its ancestors' ids, its own classes, its
    text and the form fields under it.

    Ancestry is what several of these tests are actually about ("outside the
    frame wrapper, inside the frame area"), and a flat attribute map cannot
    answer it — so this keeps an open-tag stack, skipping void elements,
    which would otherwise never be popped and put every later element at
    the wrong depth.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.strips: list[dict] = []
        self._stack: list[str] = []
        self._ids: list[str] = []
        self._depth: int | None = None

    def _record(self, tag, attrs):
        mapped = {k: ("" if v is None else v) for k, v in attrs}
        classes = mapped.get("class", "").split()
        if self._depth is None and "frame-banner" in classes:
            self.strips.append(
                {"ancestor_ids": list(self._ids), "classes": classes, "text": "", "fields": {}}
            )
            self._depth = len(self._stack)
        elif self._depth is not None and tag == "input" and "name" in mapped:
            self.strips[-1]["fields"][mapped["name"]] = mapped.get("value", "")
        return mapped

    def handle_starttag(self, tag, attrs):
        mapped = self._record(tag, attrs)
        if tag not in _VOID:
            self._stack.append(tag)
            self._ids.append(mapped.get("id", ""))

    def handle_startendtag(self, tag, attrs):
        self._record(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if self._stack:
            self._stack.pop()
            self._ids.pop()
        if self._depth is not None and len(self._stack) == self._depth:
            self._depth = None

    def handle_data(self, data):
        if self._depth is not None:
            self.strips[-1]["text"] += data


def strips_in(html: str) -> list[dict]:
    parser = _Strips()
    parser.feed(html)
    parser.close()
    return parser.strips


def text_of(strip: dict) -> str:
    return " ".join(strip["text"].split())


class _Controls(HTMLParser):
    """The accessible name of every control inside the first `.msg` card.

    Approximated the way a screen reader resolves one for these elements:
    `aria-label` when it is there, otherwise the element's text content —
    including a `.sr-only` span, and excluding any subtree marked
    `aria-hidden`, which is how `icon()` renders a glyph that is not the
    label.

    Scoped to one card because that is the scope the rule is about. The
    same name on two cards is two messages offering the same action; the
    same name twice within one card is two different effects a reader
    cannot tell apart.
    """

    _CONTROLS = frozenset({"a", "button", "summary"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.names: list[str] = []
        self._stack: list[str] = []
        self._card: int | None = None
        self._control: int | None = None
        self._hidden: int | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        mapped = {k: ("" if v is None else v) for k, v in attrs}
        if self._card is None and "msg" in mapped.get("class", "").split():
            self._card = len(self._stack)
        elif self._card is not None:
            if self._hidden is None and mapped.get("aria-hidden") == "true":
                self._hidden = len(self._stack)
            elif self._control is None and tag in self._CONTROLS:
                self._control = len(self._stack)
                self._text = [mapped["aria-label"]] if "aria-label" in mapped else []
                self._label = "aria-label" in mapped
        if tag not in _VOID:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _VOID or not self._stack:
            return
        self._stack.pop()
        depth = len(self._stack)
        if self._hidden == depth:
            self._hidden = None
        elif self._control == depth:
            self._control = None
            name = " ".join("".join(self._text).split())
            if name:
                self.names.append(name)
        elif self._card == depth:
            # One card only: the rule is about a single message's controls.
            self._card = -1

    def handle_data(self, data):
        if self._control is not None and self._hidden is None and not self._label:
            self._text.append(data)


def control_names(html: str) -> list[str]:
    parser = _Controls()
    parser.feed(html)
    parser.close()
    return parser.names


# ---------------------------------------------------------------------------
# The template guards
# ---------------------------------------------------------------------------


def test_neither_context_renders_nothing_at_all(banner):
    """The property the whole two-scope arrangement rests on. An include
    that supplies only one strip's variables must not leave an empty
    bordered box where the other one would have been.
    """
    assert banner.render(email_id="E1").strip() == ""


def test_each_strip_is_drawn_only_by_its_own_context(banner):
    images = strips_in(banner.render(email_id="E1", thread_id="T1", blocked_remote=1))
    restyle = strips_in(banner.render(email_id="E1", restyle_sender="p@x.test"))
    assert len(images) == 1 and "frame-banner-dark" not in images[0]["classes"]
    assert len(restyle) == 1 and "frame-banner-dark" in restyle[0]["classes"]


# ---------------------------------------------------------------------------
# The remote-images strip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blocked,expected", [(1, "1 remote image"), (2, "2 remote images")])
def test_the_count_is_singular_or_plural_as_it_should_be(banner, blocked, expected):
    out = banner.render(email_id="E1", thread_id="T1", blocked_remote=blocked)
    assert expected in text_of(strips_in(out)[0])


def test_both_controls_appear_exactly_once(banner):
    out = banner.render(email_id="E1", thread_id="T1", blocked_remote=2, sender="news@t.test")
    assert out.count("Show images") == 1
    assert out.count("Always show from") == 1


def test_a_message_with_no_usable_sender_is_offered_only_show_images(banner):
    """There is no address to key an allow-list row on, so the second
    control could not do what it says. Spec §3: absent, not dead.
    """
    out = banner.render(email_id="E1", thread_id="T1", blocked_remote=1, sender=None)
    assert out.count("Show images") == 1
    assert "Always show from" not in out


def test_show_images_asks_for_this_message_this_thread_and_remote_on(banner):
    out = banner.render(email_id="E1", thread_id="T 1", blocked_remote=1)
    (button,) = parse_attrs(out)["button"]
    assert button["hx-get"] == "/m/E1/frame?thread=T%201&remote=1"
    assert button["hx-target"] == "#frame-E1"
    assert button["hx-swap"] == "outerHTML"


def test_the_allow_control_posts_the_address_as_a_form_field(banner):
    """Not `hx-vals`. That would carry JSON through an HTML attribute, and a
    `From` header is sender-controlled text — one quote character in it and
    the JSON silently stops parsing.
    """
    out = banner.render(email_id="E1", thread_id="T1", blocked_remote=1, sender='a"b@t.test')
    (form,) = parse_attrs(out)["form"]
    assert form["hx-post"] == "/m/E1/images/allow"
    assert form["hx-target"] == "#frame-E1"
    (strip,) = strips_in(out)
    assert strip["fields"] == {"sender": 'a"b@t.test'}


def test_the_hosts_line_names_at_most_three_and_says_how_many_more(banner):
    hosts = ("a.test", "b.test", "c.test", "d.test", "e.test")
    text = text_of(strips_in(banner.render(email_id="E1", blocked_remote=5, remote_hosts=hosts))[0])
    assert "a.test, b.test, c.test and 2 more" in text
    assert "d.test" not in text and "e.test" not in text


def test_no_hosts_means_no_hosts_line(banner):
    out = banner.render(email_id="E1", blocked_remote=1, remote_hosts=())
    assert "would load from" not in out


def test_the_banner_never_claims_more_than_the_counter_can_know():
    """The honesty rule, asserted against the sanitiser rather than against
    the template's prose.

    `srcset`, `poster`, `background` and `data-src` are struck out as
    *attributes*, before any URL policy runs — so their hosts never reach
    `remote_hosts` and were never in `blocked_remote`. A message built
    entirely out of them therefore raises no banner at all, which is
    exactly why the copy may only speak about images that would have
    loaded and never about every host that wanted to track the reader.
    """
    hostile = (
        '<img srcset="https://a.test/x.png 1x" data-src="https://b.test/y.png">'
        '<video poster="https://c.test/z.jpg"></video>'
        '<table background="https://d.test/bg.png"><tr><td>x</td></tr></table>'
    )
    result = sanitize_email_html(
        hostile, SanitizeContext(email_id="E1", origin="https://mail.test", remote=False)
    )
    assert (result.blocked_remote, result.remote_hosts) == (0, ())

    env = build_env(STATIC).get_template("thread/banner.html")
    rendered = env.render(
        email_id="E1",
        thread_id="T1",
        blocked_remote=result.blocked_remote,
        remote_hosts=result.remote_hosts,
    )
    assert rendered.strip() == ""


def test_a_real_blocked_image_does_raise_the_banner():
    """The other half of the test above: the counter is not simply always
    zero.
    """
    result = sanitize_email_html(
        '<img src="https://t.test/a.gif"><img src="https://t.test/b.gif">',
        SanitizeContext(email_id="E1", origin="https://mail.test", remote=False),
    )
    assert (result.blocked_remote, result.remote_hosts) == (2, ("t.test",))


# ---------------------------------------------------------------------------
# The dark-restyle strip
# ---------------------------------------------------------------------------


def test_the_restyle_control_posts_the_sender_and_replaces_the_whole_area(banner):
    """`POST /m/{id}/restyle` answers with the frame alone. Targeting the
    area rather than the frame is what makes the strip remove itself by
    doing its job — and there is no route that puts the restyle back, so a
    toggle offering the return trip would be offering a control that does
    not exist.
    """
    out = banner.render(email_id="E1", restyle_sender="Priya@X.test")
    (form,) = parse_attrs(out)["form"]
    assert form["hx-post"] == "/m/E1/restyle"
    assert form["hx-target"] == "#frame-area-E1"
    assert form["hx-swap"] == "outerHTML"
    (strip,) = strips_in(out)
    assert strip["fields"] == {"sender": "Priya@X.test"}
    assert out.count("Original colours") == 1


def test_the_restyle_strip_posts_no_remote_flag(banner):
    """The route defaults `remote` to false, which is the state
    `thread/message.html` first rendered the frame in. Sending a stale `1`
    from a page that may never have swapped would be worse than sending
    nothing.
    """
    (strip,) = strips_in(banner.render(email_id="E1", restyle_sender="p@x.test"))
    assert "remote" not in strip["fields"]


# ---------------------------------------------------------------------------
# The restyle strip in the conversation view
# ---------------------------------------------------------------------------


class FakeAdmin:
    async def create_api_key(self, username: str, name: str) -> ApiKey:
        return ApiKey(id="k1", secret="API_secret_1")

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.threads: dict[str, list[EmailBody]] = {}

    @property
    def account_id(self) -> str:
        return "acct-1"

    def thread(self, thread_id: str, specs: list, *, sender: str | None = "priya@x.test") -> None:
        from_ = [Address(name="Priya", email=sender)] if sender else []
        self.threads[thread_id] = [
            EmailBody(
                id=email_id,
                thread_id=thread_id,
                mailbox_ids={"mb-inbox"},
                keywords={"$seen"},
                from_=from_,
                to=[Address(name="Demo", email=ME)],
                subject="Offsite agenda",
                received_at=datetime(2026, 9, 2, 10, tzinfo=UTC) + timedelta(minutes=minute),
                preview="…",
                has_attachment=False,
                text_body=text,
                html_body=html,
            )
            for minute, (email_id, html, text) in enumerate(specs)
        ]

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

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
        return self.threads.get(thread_id, [])


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def application(monkeypatch, sqlite_url, fake):
    async def fake_verify(url, username, password):
        return VerifiedAccount(username, "acc1", username) if password == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    app = create_app(settings=make_settings(sqlite_url))
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


async def set_prefs(application, **fields) -> None:
    """Write the reader's `UiPref` row directly.

    Through the database rather than `POST /prefs`, so these tests say what
    they mean about a *preference* without also depending on which subset of
    the panel that endpoint happens to accept today.
    """
    async with application.state.sessionmaker() as db:
        user = (await db.execute(select(AppUser))).scalars().first()
        await repo.set_prefs(db, user.id, **fields)


def dark_strips(html: str) -> list[dict]:
    return [s for s in strips_in(html) if "frame-banner-dark" in s["classes"]]


async def test_an_html_message_offers_the_restyle_control_once(authed, fake):
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    html = (await authed.get("/t/T1")).text
    assert len(dark_strips(html)) == 1
    assert html.count("Original colours") == 1


async def test_no_two_controls_in_one_card_answer_to_the_same_name(authed, fake):
    """Spec §7 names two of this card's controls "Show original": the
    banner's dark-restyle toggle and the ⋮ menu's raw RFC 5322 source. They
    do entirely different things, and a screen-reader user hearing one name
    twice can guess neither — so they are "Original colours" and "View
    source", and §7 carries the note saying why.

    Asserted over every control in the card rather than over those two, so
    the next control that arrives cannot quietly collide with one of them.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    names = control_names((await authed.get("/t/T1")).text)

    assert sorted(names) == sorted(set(names)), f"duplicate control name: {names}"
    assert "Original colours" in names
    assert "View source" in names


async def test_the_strip_is_outside_the_frame_wrapper_and_inside_the_area(authed, fake):
    """Outside `#frame-E1`, because a "Show images" swap replaces that and
    must not carry the restyle control off with it. Inside
    `#frame-area-E1`, because its own swap replaces *that*.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    (strip,) = dark_strips((await authed.get("/t/T1")).text)
    assert "frame-area-E1" in strip["ancestor_ids"]
    assert "frame-E1" not in strip["ancestor_ids"]


async def test_one_strip_per_html_message_and_none_for_a_text_one(authed, fake):
    """Also the double-draw guard: `thread/banner.html` is included twice
    per card's subtree once `thread/frame.html` wires the remote strip, and
    a `restyle_sender` left in scope would draw this one in both.
    """
    fake.thread(
        "T1",
        [("E1", "<p>rich</p>", None), ("E2", None, "plain"), ("E3", "<p>rich</p>", None)],
    )
    assert len(dark_strips((await authed.get("/t/T1")).text)) == 2


async def test_a_light_reader_is_offered_nothing_to_undo(authed, fake, application):
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    await set_prefs(application, theme="light")
    assert dark_strips((await authed.get("/t/T1")).text) == []


async def test_a_reader_who_turned_the_restyle_off_is_offered_nothing_to_undo(
    authed, fake, application
):
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    await set_prefs(application, dark_restyle=False)
    assert dark_strips((await authed.get("/t/T1")).text) == []


@pytest.mark.parametrize("theme", ["dark", "system"])
async def test_both_themes_that_can_paint_dark_get_the_strip(authed, fake, application, theme):
    """`system` too: the OS decides after this HTML is written, which is why
    `.frame-banner-dark` carries the media-query half of the gate.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)])
    await set_prefs(application, theme=theme)
    assert len(dark_strips((await authed.get("/t/T1")).text)) == 1


async def test_a_message_with_no_from_address_is_offered_nothing(authed, fake):
    """The row is keyed on the sender. With no address there is nothing to
    remember the choice against, and the route would refuse the post.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)], sender=None)
    assert dark_strips((await authed.get("/t/T1")).text) == []


# ---------------------------------------------------------------------------
# ...and only when it has something to undo
#
# The strip's sentence is true whenever the page is dark, but its button
# undoes an *inversion*. `MessageView.restyled` is the same question
# `GET /m/{id}/html` answers before it emits the filter, so a card only
# carries the control when that filter is actually on the message.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '<meta name="color-scheme" content="dark light"><p>hi</p>',
        "<style>:root{color-scheme:light dark}</style><p>hi</p>",
        '<body bgcolor="#0d1015"><p>dark newsletter</p></body>',
    ],
)
async def test_a_message_nothing_was_done_to_carries_no_control(authed, fake, body):
    """A mail that declares its own colour scheme is trusted and left
    alone; one whose background already reads dark is left alone because
    inverting it would turn it light. Neither was restyled, so neither has
    an original to go back to.
    """
    fake.thread("T1", [("E1", body, None)])
    assert dark_strips((await authed.get("/t/T1")).text) == []


async def test_a_sender_already_opted_out_is_not_asked_again(authed, fake, application):
    """`POST /m/{id}/restyle` writes `dark_restyle = False` for the sender,
    and that is what `/m/{id}/html` then reads. A control that would
    rewrite the row already there is a control that does nothing.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)], sender="priya@x.test")
    assert len(dark_strips((await authed.get("/t/T1")).text)) == 1

    async with application.state.sessionmaker() as db:
        user = (await db.execute(select(AppUser))).scalars().first()
        await repo.set_sender_restyle(db, user.id, "priya@x.test", False)

    assert dark_strips((await authed.get("/t/T1")).text) == []


async def test_one_senders_opt_out_does_not_silence_another(authed, fake, application):
    """The row is per sender, and so is the strip that reads it."""
    fake.thread("T1", [("E1", "<p>rich</p>", None)], sender="priya@x.test")
    async with application.state.sessionmaker() as db:
        user = (await db.execute(select(AppUser))).scalars().first()
        await repo.set_sender_restyle(db, user.id, "someone@else.test", False)

    assert len(dark_strips((await authed.get("/t/T1")).text)) == 1


async def test_the_posted_sender_is_the_address_the_route_will_check(authed, fake):
    """`POST /m/{id}/restyle` compares the field against the message's own
    `From`, folded, and answers `403` on a mismatch. What the card posts has
    to be that address and not the display name beside it.
    """
    fake.thread("T1", [("E1", "<p>rich</p>", None)], sender="Priya@X.test")
    (strip,) = dark_strips((await authed.get("/t/T1")).text)
    assert strip["fields"] == {"sender": "Priya@X.test"}


async def test_a_hostile_sender_address_stays_inside_the_field(authed, fake):
    fake.thread("T1", [("E1", "<p>rich</p>", None)], sender='"><img src=x onerror=alert(1)>@x')
    html = (await authed.get("/t/T1")).text
    (strip,) = dark_strips(html)
    assert strip["fields"] == {"sender": '"><img src=x onerror=alert(1)>@x'}
    assert [
        a for tag in parse_attrs(html).values() for a in tag if any(k.startswith("on") for k in a)
    ] == []
