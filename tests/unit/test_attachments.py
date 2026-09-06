"""The attachment chips, end to end through `GET /t/{thread_id}`.

What this file is about is the *chip*: which parts get one, what it says,
which of its two controls exist, and whether both are reachable without a
mouse. The route the controls point at — `GET /m/{id}/att/{blob}`, its
headers and its `inline=1` fallback — is asserted in
`tests/unit/test_frame_routes.py` alongside the rest of that router.

The seam between them is asserted here and only here:
`conversation.PREVIEW_KIND` (which types a chip offers "Open" for) has to
stay exactly `frames.PREVIEW_TYPES` (which types that route will serve
inline). They are two constants in two layers, because `mailosh.services`
must not import `mailosh.web` — so drift between them is a real
possibility, and drift is not a cosmetic bug: an "Open" for a type the
route refuses hands the reader a download while claiming to show them a
file.

Runs against a real `create_app` — real router, real Jinja environment,
real session plumbing over a file-backed aiosqlite db — with the same two
fakes `tests/unit/test_thread_routes.py` uses: no Stalwart, no network.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
import pytest_asyncio
from conftest import make_settings
from helpers import parse_attrs
from httpx import ASGITransport, AsyncClient

from mailosh.jmap.models import Address, BodyPart, EmailBody, Mailbox
from mailosh.security.exchange import VerifiedAccount
from mailosh.services.conversation import PREVIEW_KIND, _attachment_icon
from mailosh.stalwart_admin import ApiKey
from mailosh.web import deps
from mailosh.web.app import create_app
from mailosh.web.frames import PREVIEW_TYPES

ACCOUNT = "acct-1"
ME = "d@x"
ICONS = Path("mailosh/web/static/icons")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdmin:
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

    `thread(...)` takes the attachment list as dicts in RFC 8621's own
    spelling (`blobId`, `disposition`, ...) rather than as `BodyPart`
    objects, so a test reads like the wire shape the real client parses.
    """

    def __init__(self) -> None:
        self.threads: dict[str, list[EmailBody]] = {}

    @property
    def account_id(self) -> str:
        return ACCOUNT

    def thread(
        self,
        thread_id: str,
        specs: list,
        *,
        attachments: list[dict] | None = None,
        subject: str = "Offsite agenda",
    ) -> None:
        parts = [BodyPart(**a) for a in (attachments or [])]
        messages = []
        for minute, spec in enumerate(specs):
            email_id, html, text = spec if isinstance(spec, tuple) else (spec, None, "plain body")
            messages.append(
                EmailBody(
                    id=email_id,
                    thread_id=thread_id,
                    mailbox_ids={"mb-inbox"},
                    keywords={"$seen"},
                    from_=[Address(name="Priya Natarajan", email="priya@example.com")],
                    to=[Address(name="Demo", email=ME)],
                    subject=subject,
                    received_at=datetime(2026, 9, 2, 10, tzinfo=UTC) + timedelta(minutes=minute),
                    preview="Attaching the deck we walked through",
                    has_attachment=bool(parts),
                    text_body=text,
                    html_body=html,
                    attachments=parts,
                )
            )
        self.threads[thread_id] = messages

    async def get_mailboxes(self) -> list[Mailbox]:
        return [
            _mailbox("mb-inbox", "Inbox", "inbox", 10),
            _mailbox("mb-archive", "Archive", "archive", 50),
            _mailbox("mb-trash", "Trash", "trash", 70),
        ]

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
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
# Reading the rendered chips
# ---------------------------------------------------------------------------


#: Elements that never have an end tag. Tracked explicitly because this
#: parser keeps an open-tag stack, and a `<meta>` or `<input>` pushed and
#: never popped would put every later element at the wrong depth — which is
#: how an ancestry assertion silently stops asserting anything.
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


class _Chips(HTMLParser):
    """Every `.attachment-chip` in a page, with its ancestors, its links and
    its text.

    `helpers.parse_attrs` returns a flat `{tag: [attrs]}` map, which cannot
    answer "is this chip inside the fold" or "how many links does *this*
    chip carry" — and both are what these tests are actually about. So this
    keeps the open-tag stack and slices each chip's own subtree out of it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chips: list[dict] = []
        self._stack: list[str] = []
        self._depth: int | None = None

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapped = {k: ("" if v is None else v) for k, v in attrs}
        if self._depth is None and "attachment-chip" in mapped.get("class", "").split():
            self.chips.append({"ancestors": list(self._stack), "links": [], "text": ""})
            self._depth = len(self._stack)
        elif self._depth is not None and tag == "a":
            self.chips[-1]["links"].append(mapped)

    def handle_starttag(self, tag, attrs):
        self._record(tag, attrs)
        if tag not in _VOID:
            self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # `<path/>` inside an inline SVG opens and closes at once: recorded,
        # never stacked.
        self._record(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if self._stack:
            self._stack.pop()
        if self._depth is not None and len(self._stack) == self._depth:
            self._depth = None

    def handle_data(self, data):
        if self._depth is not None:
            self.chips[-1]["text"] += data


def chips_in(html: str) -> list[dict]:
    parser = _Chips()
    parser.feed(html)
    parser.close()
    return parser.chips


def _att(blob_id: str, mime: str, name: str, size: int, **extra) -> dict:
    part = {
        "blobId": blob_id,
        "type": mime,
        "name": name,
        "size": size,
        "disposition": "attachment",
    }
    part.update(extra)
    return part


# ---------------------------------------------------------------------------
# Which parts get a chip, and what it says
# ---------------------------------------------------------------------------


async def test_a_cid_part_gets_no_chip_and_a_real_attachment_does(authed, fake):
    """An inline logo is the sender's own markup referring to itself, not a
    file they attached — a chip for it would sit under every newsletter in
    the mailbox.
    """
    fake.thread(
        "T1",
        [("E1", None, "x")],
        attachments=[
            _att("B1", "image/png", "logo.png", 10, cid="logo@m", disposition="inline"),
            _att("B2", "application/pdf", "spec.pdf", 2048),
        ],
    )
    chips = chips_in((await authed.get("/t/T1")).text)
    assert len(chips) == 1
    assert "spec.pdf" in chips[0]["text"]
    assert "logo.png" not in chips[0]["text"]


async def test_a_chip_shows_the_humanised_size_not_the_byte_count(authed, fake):
    fake.thread(
        "T1", [("E1", None, "x")], attachments=[_att("B2", "application/pdf", "s.pdf", 2048)]
    )
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    assert "2 KB" in chip["text"]
    assert "2048" not in chip["text"]


async def test_every_attachment_gets_exactly_one_chip(authed, fake):
    fake.thread(
        "T1",
        [("E1", None, "x")],
        attachments=[_att(f"B{n}", "application/zip", f"f{n}.zip", 4096) for n in range(5)],
    )
    assert len(chips_in((await authed.get("/t/T1")).text)) == 5


async def test_a_message_with_nothing_attached_renders_no_chip_row(authed, fake):
    fake.thread("T1", [("E1", None, "x")])
    html = (await authed.get("/t/T1")).text
    assert chips_in(html) == []
    assert html.count('<ul class="attachments">') == 0


async def test_chips_sit_inside_the_fold_so_a_collapsed_card_stays_one_line(authed, fake):
    """A collapsed message is one line and one control. Chips hanging below
    a closed `<details>` would make it three.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "application/pdf", "s.pdf", 9)])
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    assert "details" in chip["ancestors"]


async def test_a_hostile_filename_becomes_text_and_never_markup(authed, fake):
    fake.thread(
        "T1",
        [("E1", None, "x")],
        attachments=[_att("B2", "application/pdf", "<img src=x onerror=alert(1)>.pdf", 9)],
    )
    html = (await authed.get("/t/T1")).text
    (chip,) = chips_in(html)
    # The name is shown, in full, as text...
    assert "<img src=x onerror=alert(1)>.pdf" in chip["text"]
    # ...and nothing in the document carries an inline handler.
    assert [
        a for tag in parse_attrs(html).values() for a in tag if any(k.startswith("on") for k in a)
    ] == []


# ---------------------------------------------------------------------------
# The two controls
# ---------------------------------------------------------------------------


async def test_a_previewable_chip_carries_download_and_open_once_each(authed, fake):
    fake.thread(
        "T1", [("E1", None, "x")], attachments=[_att("B2", "application/pdf", "spec.pdf", 2048)]
    )
    html = (await authed.get("/t/T1")).text
    (chip,) = chips_in(html)
    assert len(chip["links"]) == 2
    assert html.count("Download spec.pdf") == 1
    # "Preview", not "Open": the control is the stretched `.chip-link` that
    # opens the dialog now, and the label follows what the click does. The
    # anchor is still the same real `?inline=1` URL underneath, which is
    # what a modifier click and a JS-less browser still get.
    assert html.count("Preview spec.pdf") == 1


async def test_a_chip_with_no_preview_kind_offers_download_only(authed, fake):
    fake.thread(
        "T1", [("E1", None, "x")], attachments=[_att("B3", "application/zip", "bundle.zip", 4096)]
    )
    html = (await authed.get("/t/T1")).text
    (chip,) = chips_in(html)
    assert len(chip["links"]) == 1
    assert html.count("Download bundle.zip") == 1
    # Spec §7's "unsupported types fall back to download" is this absence:
    # no preview control at all, rather than a dialog that opens on a `.zip`
    # to say it cannot show one.
    assert "Preview bundle.zip" not in html


async def test_the_controls_point_at_this_messages_own_blob(authed, fake):
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "image/png", "shot.png", 9)])
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    download, open_ = chip["links"]
    assert download["href"] == "/m/E1/att/B2"
    assert download["download"] == "shot.png"
    assert open_["href"] == "/m/E1/att/B2?inline=1"


async def test_the_open_link_cannot_reach_back_through_the_opener(authed, fake):
    """It opens a document a stranger attached, on this app's own origin's
    tab strip. `noopener` is what stops that tab holding a handle to this
    one; `noreferrer` keeps the URL of the message being read out of it.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "image/png", "shot.png", 9)])
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    _, open_ = chip["links"]
    assert open_["target"] == "_blank"
    assert set(open_["rel"].split()) == {"noopener", "noreferrer"}


async def test_both_controls_stay_in_the_tab_order(authed, fake):
    """The reveal is `opacity`, not `display`/`visibility`/`hidden`: a
    control removed from the box tree cannot be focused, so it could never
    bring itself into view. Spec §11 requires every hover action to be
    keyboard-operable, and this is the attribute-level half of that — the
    CSS half is `.chip-actions` in styles/input.css.
    """
    fake.thread(
        "T1", [("E1", None, "x")], attachments=[_att("B2", "application/pdf", "spec.pdf", 9)]
    )
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    for link in chip["links"]:
        assert "hidden" not in link
        assert "tabindex" not in link
        assert link["href"]


def test_the_reveal_is_opacity_and_covers_focus_as_well_as_hover():
    """The CSS half of the rule above, asserted on the source sheet rather
    than left to a browser nobody runs in CI.
    """
    css = Path("styles/input.css").read_text()
    block = css[css.index("  .chip-actions {") : css.index("  .chip-btn {")]
    assert "opacity: 0;" in block
    assert "display: none" not in block
    assert ".attachment-chip:hover .chip-actions," in block
    assert ".attachment-chip:focus-within .chip-actions {" in block


# ---------------------------------------------------------------------------
# The seam: what a chip offers vs what the route will serve
# ---------------------------------------------------------------------------


def test_the_chip_table_is_exactly_what_the_route_serves_inline():
    """Two constants in two layers, because `mailosh.services` may not
    import `mailosh.web`. A type in one and not the other is either an
    "Open" that downloads or a preview nobody is offered.
    """
    assert set(PREVIEW_KIND) == set(PREVIEW_TYPES)


@pytest.mark.parametrize("mime", sorted(PREVIEW_TYPES))
async def test_open_is_offered_for_every_type_the_route_serves_inline(authed, fake, mime):
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", mime, "f", 9)])
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    assert len(chip["links"]) == 2, mime


@pytest.mark.parametrize(
    "mime",
    ["text/html", "image/svg+xml", "application/xhtml+xml", "application/x-msdownload", "text/csv"],
)
async def test_open_is_never_offered_for_a_type_the_route_would_downgrade(authed, fake, mime):
    """`text/html` and `image/svg+xml` are the two that make the point: both
    look previewable by prefix, and neither may ever render on this origin.
    The route downgrades them to a download silently, so an "Open" here
    would be a label that lies about what the click does.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", mime, "f", 9)])
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    assert len(chip["links"]) == 1, mime


@pytest.mark.parametrize(
    "declared", ["IMAGE/PNG", "image/png; name=a.png", "  Image/PNG ; charset=x"]
)
async def test_a_type_with_parameters_or_odd_case_is_still_recognised(authed, fake, declared):
    """`Content-Type` is a header a sender writes. A chip that only matched
    the canonical spelling would refuse to preview a perfectly ordinary PNG
    because the mailer appended `; name=`.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", declared, "a.png", 9)])
    (chip,) = chips_in((await authed.get("/t/T1")).text)
    assert len(chip["links"]) == 2, declared


# ---------------------------------------------------------------------------
# Glyphs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("image/png", "image"),
        ("image/svg+xml", "image"),
        ("application/pdf", "file-text"),
        ("text/plain", "file-text"),
        ("text/html", "file-text"),
        ("application/zip", "file"),
        ("application/octet-stream", "file"),
    ],
)
def test_the_glyph_for_a_type_is_one_that_is_actually_vendored(mime, expected):
    """`make_icon` renders an unknown name as an empty decorative span — a
    mapping to an unfetched icon goes blank in the browser without failing
    anything else.
    """
    name = _attachment_icon(mime)
    assert name == expected
    assert (ICONS / f"{name}.svg").exists(), f"{name}: run `make icons`"


# ---------------------------------------------------------------------------
# The preview dialog (spec §7)
#
# One `<dialog>` per conversation, filled by `static/js/attachments.js` from
# the chip that was clicked. There is no JS runtime here (Global
# Constraints), so the markup is asserted through the route and the module
# is read as source — and what is read is the part a browser would not tell
# us about anyway: which element it reaches for, and what it clears.
# ---------------------------------------------------------------------------

ATTACHMENTS_JS = Path("mailosh/web/static/js/attachments.js")


def _attachments_js() -> str:
    """The module minus its whole-line comments: this file explains itself
    at length, and a rule about what the code does must not be satisfiable
    by prose describing it."""
    source = ATTACHMENTS_JS.read_text()
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


async def test_the_page_carries_exactly_one_preview_dialog(authed, fake):
    """A singleton, however many chips there are: twenty copies of this
    markup is nineteen dialogs nobody opens, each with an `<iframe>` in it.
    """
    fake.thread(
        "T1",
        [("E1", None, "x"), ("E2", None, "y")],
        attachments=[_att(f"B{n}", "image/png", f"s{n}.png", 9) for n in range(3)],
    )
    html = (await authed.get("/t/T1")).text
    assert len(chips_in(html)) == 6
    assert html.count('id="att-preview"') == 1
    # Counted by its own body rather than by `<dialog`: the app layout has
    # dialogs of its own (the shortcuts overlay), and this is about there
    # being one *preview*.
    assert html.count('class="att-dialog-body"') == 1


async def test_the_dialog_starts_empty_and_names_itself_by_its_filename(authed, fake):
    """It carries no attachment until a chip is clicked — no `src` on either
    surface, no `href` on either link. A dialog that shipped one would be
    fetching a file the reader never asked for.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "image/png", "s.png", 9)])
    html = (await authed.get("/t/T1")).text
    dialog = html[html.index("<dialog") : html.index("</dialog>")]
    tags = parse_attrs(dialog)
    assert "src" not in tags["img"][0] and "hidden" in tags["img"][0]
    assert "src" not in tags["iframe"][0] and "hidden" in tags["iframe"][0]
    assert [a["href"] for a in tags["a"]] == ["", ""]
    # Named by the element the module writes the filename into.
    assert 'aria-labelledby="att-preview-name"' in dialog
    assert 'id="att-preview-name"' in dialog


async def test_the_dialogs_frame_is_sandboxed_exactly_as_a_message_frame_is(authed, fake):
    """An attachment is a file a stranger sent. The response's own
    `default-src 'none'; sandbox` is the braces; this attribute is the belt,
    and `allow-same-origin` is absent from both.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "application/pdf", "s.pdf", 9)])
    html = (await authed.get("/t/T1")).text
    (frame,) = parse_attrs(html)["iframe"]
    assert "allow-same-origin" not in frame["sandbox"].split()
    assert "allow-scripts" in frame["sandbox"].split()
    assert frame["referrerpolicy"] == "no-referrer"


async def test_a_previewable_chip_is_a_stretched_link_and_says_what_it_opens(authed, fake):
    """The whole chip is the control (`.chip-link`, stretched by
    styles/thread.css) — and it is a real link to the URL that already
    works, so a modifier click and a browser with no modules both still open
    the file. `data-preview-kind` is the Python table's answer, not a guess
    made in JS from the filename.
    """
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "image/png", "shot.png", 9)])
    html = (await authed.get("/t/T1")).text
    (link,) = [a for a in parse_attrs(html)["a"] if a.get("class") == "chip-link"]
    assert link["href"] == "/m/E1/att/B2?inline=1"
    assert link["data-role"] == "attachment-preview"
    assert link["data-preview-kind"] == "image"
    assert link["data-preview-name"] == "shot.png"
    assert link["data-download-url"] == "/m/E1/att/B2"
    assert link["target"] == "_blank"
    assert set(link["rel"].split()) == {"noopener", "noreferrer"}


@pytest.mark.parametrize(
    "mime,kind",
    [("image/png", "image"), ("application/pdf", "pdf"), ("text/plain", "text")],
)
async def test_the_chip_carries_the_kind_the_service_layer_decided(authed, fake, mime, kind):
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", mime, "f", 9)])
    html = (await authed.get("/t/T1")).text
    (link,) = [a for a in parse_attrs(html)["a"] if a.get("class") == "chip-link"]
    assert link["data-preview-kind"] == kind
    assert PREVIEW_KIND[mime] == kind


async def test_a_type_with_no_preview_gets_no_stretched_link_at_all(authed, fake):
    fake.thread("T1", [("E1", None, "x")], attachments=[_att("B2", "text/html", "page.html", 9)])
    html = (await authed.get("/t/T1")).text
    assert "chip-link" not in html
    assert 'data-role="attachment-preview"' not in html


def test_the_module_shows_a_picture_as_a_picture_and_a_document_in_the_frame():
    """`image` is the only kind that is a picture. `pdf` and `text` are
    documents a stranger sent, and a document in this app goes in a
    sandboxed frame — the same rule a message body follows.
    """
    code = _attachments_js()
    assert 'if (previewKind === "image")' in code
    assert 'previewKind === "pdf" || previewKind === "text"' in code
    # An unrecognised kind opens nothing and the click is handed back to the
    # browser, which is what makes the anchor's `href` the fallback.
    assert "return false;" in code
    assert "if (open(trigger)) event.preventDefault();" in code


def test_the_module_hands_a_modifier_click_back_to_the_browser():
    """The chip is a real link. "Open in a new tab" has to keep meaning
    that, exactly as it does on a list row and a Drafts row.
    """
    code = _attachments_js()
    assert "event.metaKey || event.ctrlKey || event.shiftKey" in code
    assert "event.button !== 0" in code


def test_the_module_clears_every_surface_on_close_and_returns_the_focus():
    """Two things a `<dialog>` does not do by itself. A closed dialog must
    hold no bytes of the attachment it showed, and the reader must land back
    on the chip rather than at the top of the conversation.
    """
    code = _attachments_js()
    assert 'frame.removeAttribute("src")' in code
    assert 'image.removeAttribute("src")' in code
    # `src = ""` would resolve against the page's own URL and point the
    # frame at the conversation.
    assert 'src = ""' not in code
    assert "opener?.focus?.({ preventScroll: true })" in code
    # `close` does not bubble, so the listener has to be a capture one on an
    # ancestor — `#main` is swapped by htmx, so it cannot be bound to the
    # dialog itself and left there.
    close_listener = re.search(
        r"document\.body\.addEventListener\(\s*\"close\",(.*?)^\);$", code, re.M | re.S
    )
    assert close_listener is not None, "the close handler is no longer a body listener"
    # The trailing `true` is what makes it capture.
    assert re.search(r"\btrue,?\s*$", close_listener.group(1).strip())


def test_escape_and_the_backdrop_are_the_platforms_and_the_dialog_is_modal():
    """`showModal()`, not `show()`: Escape, the inert page behind it and the
    focus trap all come with the modal form and with nothing else. The
    backdrop click is the one addition — a click whose target is the
    `<dialog>` itself, since every visible part of it is a child.
    """
    code = _attachments_js()
    assert "root.showModal()" in code
    assert "root.show()" not in code
    assert "event.target === root" in code
    # Escape is never bound: binding it would be a second, divergent idea of
    # what closes this dialog.
    assert "Escape" not in code
