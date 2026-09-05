"""Real-world mail, read for **legibility** rather than only for safety.

Every other sanitiser test on this branch — `test_html_sanitize.py`,
`test_css_sanitize.py`, `tests/fixtures/xss/` — asks "did the payload we
wrote get through?". A security review has already thrown 450 000 fuzz
iterations at that question and could not break it. Phase 1B's stated exit
criterion is a different one: *open real-world HTML mail safely and
legibly*. Nothing on the branch asked the second half of that until this
module.

So `tests/fixtures/mail/corpus/` is six message bodies in the shapes real
clients actually emit — Gmail's `gmail_quote` wrapper, Outlook on the web's
`appendonsend`/`divRplyFwdMsg` pair, Word's `MsoNormal`/`<o:p>`/`mso-*`
export, Apple Mail's `class=""`-on-everything body, one table-layout
newsletter with VML ghost tables and one modern responsive newsletter with
a `prefers-color-scheme` block — and every assertion below is of the form
"the reader can still read this", not "the payload died".

The distinction matters because the two failure modes point in opposite
directions. A safety test fails *open*: it passes on an empty string. A
legibility test fails *closed*: it passes only when something survived. A
sanitiser that returned `""` for every input would score perfectly on the
whole of `test_html_sanitize.py` and would fail almost every test here.

Two known gaps are pinned at the bottom of this file rather than papered
over. Both were found *by* this corpus, both are recorded in
`docs/spikes/p1b-findings.md`, and neither is fixed here — widening a
security allow-list or a quote-selector list is not a test module's call to
make. Each is asserted as the behaviour that ships **today**, so whoever
fixes one gets a loud, specific failure pointing at the finding rather than
a silent change of behaviour.

Kept separate from `test_html_sanitize.py` on purpose: that module owns the
adversarial corpus and the nh3 behaviours, and mixing "nothing got through"
with "everything got through" in one file makes it impossible to tell, from
a failure, which property just broke.
"""

from __future__ import annotations

import collections
import pathlib
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import pytest
import tinycss2
from helpers import parse_attrs
from tinycss2 import ast

from mailosh.render import quote_trim
from mailosh.render.css_sanitize import ALLOWED_PROPERTIES, sanitize_declarations
from mailosh.render.frame_document import FRAME_SCRIPT, render_frame
from mailosh.render.html_sanitize import (
    SanitizeContext,
    SanitizeResult,
    extract_styles,
    sanitize_email_html,
)

CORPUS_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "mail" / "corpus"

#: This app's own origin in every test below. Absolute because nh3 deletes a
#: filter result that is relative (`html_sanitize` behaviour 1), so every
#: rewritten URL the corpus produces must start with this.
ORIGIN = "https://mail.example.test"


@dataclass(frozen=True)
class Message:
    """One corpus fixture and what a reader must still be able to see.

    Every field is an assertion someone would make by *looking* at the
    rendered message: the sentences are what they read, `cids` is the
    inline image they see, `remote_hosts` is who wanted to know they
    opened it, and `quote_opens_with` is where the reply stops and the
    history starts.

    `layout_css` names declarations that carry the message's *shape* rather
    than its decoration — the widths and paddings that make a 600px
    newsletter a newsletter instead of a column of unstyled text. They are
    listed one by one, not counted, because "70% of declarations survived"
    is satisfied by keeping seventy per cent of the font sizes and dropping
    every width.
    """

    name: str
    #: `{content-id: blob id}` — what `frames._cid_parts` builds from the
    #: message's own MIME parts, and what the sanitiser tests membership in.
    cids: dict[str, str]
    #: Prose the reader must still be able to read, verbatim.
    sentences: tuple[str, ...]
    #: Hosts whose images must be blocked at `remote=0` and proxied at
    #: `remote=1`. Sorted, because `SanitizeResult.remote_hosts` is.
    remote_hosts: tuple[str, ...] = ()
    #: The exact opening of the quoted half, or `None` for a message that
    #: has no quoted history at all (a newsletter).
    quote_opens_with: str | None = None
    #: Declarations from the message's `<style>` blocks that must survive.
    layout_css: tuple[str, ...] = ()
    #: Declarations from the message's `style=""` attributes that must survive.
    layout_inline: tuple[str, ...] = ()
    #: Every property name lost from the `<style>` blocks, with repeats —
    #: an exact multiset, not a ratio. A ratio is satisfied by keeping the
    #: font sizes and dropping the widths; this is not.
    dropped_css: tuple[str, ...] = ()
    #: Every property name lost from the `style=""` attributes, with
    #: repeats. Includes properties lost because their *element* was
    #: unwrapped or removed, which is a different thing from a property
    #: policy — noted per fixture where it happens.
    dropped_inline: tuple[str, ...] = ()
    #: Markers that prove the *raw* fixture is really this client's output.
    fingerprints: tuple[str, ...] = ()
    #: Links the reader must still be able to follow.
    links: tuple[str, ...] = field(default_factory=tuple)


#: Named one by one rather than globbed. A corpus test that iterates a
#: directory passes when the directory is empty, and every assertion in this
#: module is "something survived" — the exact shape that a missing fixture
#: turns into a silent pass. `test_the_corpus_is_on_disk_and_is_real_mail`
#: asserts this list *is* the directory.
CORPUS: tuple[Message, ...] = (
    Message(
        name="gmail_reply.html",
        cids={"ii_lz7k2m9p0_19a4c1f2b3": "B-inline-1"},
        sentences=(
            "Thanks — Thursday works.",
            "the summary chart is inline below",
            "the September number moved after the reconciliation",
            "Priya Raman",
            # From the quoted half — it must survive the sanitiser even
            # though the frame hides it behind the toggle.
            "The room is booked from 14:00.",
        ),
        remote_hosts=("cdn.vendor.example", "track.vendor.example"),
        quote_opens_with='<div class="gmail_quote gmail_quote_container">',
        layout_inline=("border-left:1px solid rgb(204, 204, 204)", "padding-left:1ex"),
        # Gmail loses nothing at all: it writes plain colours, margins and
        # paddings and no `<style>` block.
        fingerprints=('class="gmail_signature"', 'data-smartmail="gmail_signature"'),
        links=("mailto:dan@example.test",),
    ),
    Message(
        name="outlook_web_reply.html",
        cids={"1a2b3c4d-5e6f-4071-8899-aabbccddeeff": "B-inline-1"},
        sentences=(
            "I'll bring the revised figures and the signed copy.",
            "Can you make Thursday?",
            "Dan Okafor",
        ),
        quote_opens_with='<div id="appendonsend"></div>',
        # OWA's whole stylesheet is this one rule, wrapped in an HTML
        # comment. If the CDO/CDC wrapper defeated the CSS parser it would
        # be the only declaration in the message and this would be empty.
        layout_css=("margin-top:0", "margin-bottom:0"),
        layout_inline=("font-family:Aptos", "font-size:12pt", "width:98%"),
        # `display` is the `style="display:none"` on the `<style>` element
        # itself, which goes with the element — not a property policy. The
        # signature image's `border-bottom-*` rule now survives.
        dropped_inline=("display",),
        fingerprints=('id="divRplyFwdMsg"', 'id="appendonsend"', "<!--"),
    ),
    Message(
        name="outlook_desktop_reply.html",
        cids={"image001.png@01DC1B7A.4F2E9C10": "B-inline-1"},
        sentences=(
            "bring the revised figures.",
            "the reconciliation cut-off",
            "who signs the vendor note",
            "whether Q4 reuses this template",
            "Field operations",
            "1,208",
            "Can you make Thursday?",
        ),
        quote_opens_with=(
            '<div>\n<div style="border:none;border-top:solid #E1E1E1 1.0pt;'
            'padding:3.0pt 0in 0in 0in">'
        ),
        layout_css=("font-family", "font-size:12.0pt", "margin:0in"),
        layout_inline=("text-indent:-0.25in", "padding:0in 5.4pt 3.0pt 5.4pt"),
        dropped_css=(
            "mso-ligatures",
            "mso-ligatures",
            "mso-style-priority",
            "mso-style-priority",
            "mso-style-type",
            "mso-style-type",
            "page",
        ),
        dropped_inline=(
            "mso-list",
            "mso-list",
            "mso-list",
            "word-wrap",
        ),
        fingerprints=("MsoNormal", "<o:p>", "mso-style-priority", "urn:schemas-microsoft-com"),
        links=("mailto:dan@example.test", "mailto:priya@example.test"),
    ),
    Message(
        name="apple_mail_reply.html",
        cids={"24A1F0C7-9B3E-4D62-8E51-0FA3C9D71B02": "B-inline-1"},
        sentences=(
            "Thursday is fine.",
            "the full deck is attached",
            "anything circulated before Friday is stale",
            "The room is booked from 14:00.",
        ),
        remote_hosts=("cdn.vendor.example",),
        quote_opens_with='<blockquote type="cite" class="">',
        layout_inline=("font-family:Helvetica", "font-size:12px"),
        # Every loss here is either vendor-private or a typographic hint the
        # frame's own base stylesheet already provides (`word-break`), and
        # `word-wrap`/`-webkit-nbsp-mode`/`line-break` are lost twice over
        # because they sit on `<body>`, which is unwrapped.
        dropped_inline=(
            "-webkit-nbsp-mode",
            "-webkit-nbsp-mode",
            "-webkit-text-stroke-width",
            "font-variant-caps",
            "line-break",
            "line-break",
            "word-wrap",
            "word-wrap",
        ),
        fingerprints=('class="Apple-converted-space"', 'apple-inline="yes"', "-webkit-nbsp-mode"),
        links=("mailto:dan@example.test",),
    ),
    Message(
        name="newsletter_table.html",
        cids={"dispatch-mark@newsletter.example": "B-inline-1"},
        sentences=(
            "September moved, and nobody told the deck",
            "shifted the September line by",
            "Anything circulated before Friday is stale",
            "The template question, again",
            "Read the argument",
            "The Thursday Dispatch, 4 Wharf Road, London N1",
        ),
        remote_hosts=("cdn.dispatch.example", "open.dispatch.example"),
        quote_opens_with=None,
        # The 600px column, the responsive breakout and the button colour:
        # lose any one and this stops being a newsletter on screen.
        layout_css=(
            "width:600px",
            "max-width:600px",
            "@media only screen and (max-width: 620px)",
            "background-color:#1a3d7c",
        ),
        layout_inline=("width:600px", "border-collapse:collapse", "background-color:#1a3d7c"),
        # Six of the thirteen are the border longhands: the 3px masthead
        # rule under the brand and the 1px divider between stories. Both
        # are gone, and the newsletter reads as one undivided column.
        dropped_css=(
            "-ms-interpolation-mode",
            "-ms-text-size-adjust",
            "-webkit-text-size-adjust",
            "background",
            "mso-table-lspace",
            "mso-table-rspace",
            "outline",
        ),
        # `margin`/`padding`/`background` here are `<body>`'s own attribute,
        # lost with the unwrapped element rather than to a property policy.
        dropped_inline=(
            "background",
            "margin",
            "overflow",
            "padding",
        ),
        fingerprints=("[if (gte mso 9)|(IE)]", 'role="presentation"', 'bgcolor="#f4f4f4"'),
        links=("https://dispatch.example/u/9f2c1b",),
    ),
    Message(
        name="newsletter_modern.html",
        cids={"ledger-mark@weekly.example": "B-inline-1"},
        sentences=(
            "Four percent, and everyone downstream",
            "lifted the September line by",
            "Who has to be told",
            "Read the full issue",
            "40 Rue Sainte-Anne, 75002 Paris",
        ),
        remote_hosts=("images.ledgerweekly.example", "px.ledgerweekly.example"),
        quote_opens_with=None,
        layout_css=(
            "max-width:560px",
            "border-radius:14px",
            "@media (prefers-color-scheme: dark)",
            "@media only screen and (max-width: 600px)",
            "background-color:#2f5bd6",
        ),
        layout_inline=("display:none", "max-height:0"),
        # `color-scheme`/`supported-color-schemes` are deliberate (see
        # `mailosh.render.dark`); `overflow:hidden` on the card is what
        # clips its 14px corners. The dark-mode `border-bottom-color`
        # override of the card header rule now survives too.
        dropped_css=(
            "-webkit-font-smoothing",
            "box-sizing",
            "color-scheme",
            "overflow",
            "supported-color-schemes",
        ),
        dropped_inline=("mso-hide", "overflow"),
        fingerprints=("[data-ogsc]", "x-apple-disable-message-reformatting", "@import"),
        links=("https://ledgerweekly.example/unsubscribe/62/9f2c1b",),
    ),
)

BY_NAME = {m.name: m for m in CORPUS}


def raw(message: Message) -> str:
    return (CORPUS_DIR / message.name).read_text(encoding="utf-8")


def context(message: Message, *, remote: bool = False) -> SanitizeContext:
    """The `SanitizeContext` `frames._sanitize_context` would build for this
    message, with a stub signer.

    The stubs return the thing they were handed rather than a real token:
    these tests assert *that* a remote image was routed through `/img?u=`
    and *that* an inline one carries a capability at all, while
    `tests/unit/test_image_policy.py` owns what a token contains and
    `tests/unit/test_frame_routes.py` owns what the routes do with one.

    `sign_cid` is unconditional, matching `frames._sanitize_context`: an
    inline part is not gated on the remote-image decision, and a frame with
    images off still renders the sender's own logo.
    """
    return SanitizeContext(
        email_id="E-corpus",
        origin=ORIGIN,
        remote=remote,
        cid_parts=message.cids,
        sign_image=(lambda url: url) if remote else None,
        sign_cid=lambda cid: f"cap-{cid}",
    )


def clean(message: Message, *, remote: bool = False) -> SanitizeResult:
    return sanitize_email_html(raw(message), context(message, remote=remote))


def text_of(html: str) -> str:
    """The rendered text of a fragment, entity-decoded and whitespace-flat.

    Assertions in this module are about what a *reader* sees, so they run
    against this rather than against the markup: `&#8212;` and `—` are the
    same character on screen, and a sentence broken across a source line is
    one sentence in the frame.
    """
    parser = _Text()
    parser.feed(html)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.chunks))


class _Text(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Text nodes only, with a space wherever a tag boundary was.

    The space matters: `<b>From:</b> Dan` is "From: Dan" to a reader, and
    concatenating the text nodes without one would make it "From:Dan" and
    quietly fail every sentence assertion for the wrong reason.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self.chunks.append(" ")


def declarations(css: str) -> collections.Counter[str]:
    """Every property name declared in a stylesheet, at any `@media` depth.

    Used to compare a message's own `<style>` blocks before and after
    sanitising, which is the only way to say "we kept 19 of its 32
    properties" rather than "the output was non-empty".
    """
    found: collections.Counter[str] = collections.Counter()

    def walk(rules: list[ast.Node], depth: int) -> None:
        if depth > 8:
            return
        for rule in rules:
            if isinstance(rule, ast.QualifiedRule):
                for node in tinycss2.parse_blocks_contents(
                    rule.content, skip_comments=True, skip_whitespace=True
                ):
                    if isinstance(node, ast.Declaration):
                        found[node.lower_name] += 1
            elif isinstance(rule, ast.AtRule) and rule.content is not None:
                walk(
                    tinycss2.parse_rule_list(
                        rule.content, skip_comments=True, skip_whitespace=True
                    ),
                    depth + 1,
                )

    walk(tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True), 0)
    return found


_STYLE_ATTR_RE = re.compile(r'style\s*=\s*"([^"]*)"', re.IGNORECASE)


def style_attr_declarations(html: str) -> collections.Counter[str]:
    """Property names across every `style=""` attribute in a fragment."""
    found: collections.Counter[str] = collections.Counter()
    for match in _STYLE_ATTR_RE.finditer(html):
        for node in tinycss2.parse_blocks_contents(
            match.group(1), skip_comments=True, skip_whitespace=True
        ):
            if isinstance(node, ast.Declaration):
                found[node.lower_name] += 1
    return found


IDS = [m.name for m in CORPUS]


# --- the corpus itself ------------------------------------------------


def test_the_corpus_is_on_disk_and_is_real_mail():
    """Guards the guard, the same way `test_html_sanitize.py` guards the
    adversarial corpus — but in the opposite direction.

    There, an empty fixture would make every "the payload died" assertion
    pass. Here, a missing fixture would make every "the message survived"
    assertion fail — which is the safe direction, but it would fail with
    `FileNotFoundError` from six different tests instead of one sentence
    saying the corpus moved. The fingerprints are the second half: a
    fixture that no longer carries `MsoNormal` is no longer Word's output,
    whatever the filename says, and would quietly turn this module into a
    test of some simplified stand-in.
    """
    on_disk = sorted(p.name for p in CORPUS_DIR.glob("*.html"))
    assert on_disk == sorted(IDS), f"corpus changed on disk: {on_disk}"
    for message in CORPUS:
        body = raw(message)
        assert len(body) > 1500, f"{message.name} is too small to be a real message body"
        for marker in message.fingerprints:
            assert marker in body, f"{message.name} no longer carries {marker!r}"
        assert "<img" in body, f"{message.name} has no image and so tests no image policy"


@pytest.mark.parametrize("message", CORPUS, ids=IDS)
def test_the_reader_can_still_read_it(message: Message):
    """Every sentence the sender wrote is still in the rendered text.

    Asserted against decoded *text*, not markup: the point is what reaches
    the reader's eye, and half these fixtures spell their punctuation as
    entities because their client did.
    """
    rendered = text_of(clean(message).html)
    for sentence in message.sentences:
        assert sentence in rendered, f"{message.name} lost {sentence!r}"


@pytest.mark.parametrize("message", CORPUS, ids=IDS)
def test_the_document_structure_survives(message: Message):
    """A message keeps the elements that make it a document.

    Specifically: no fixture collapses to a run of bare text. Tables stay
    tables (a 600px newsletter is a table), block containers stay block
    containers, and the anchors the reader is meant to click are still
    anchors with an `href`.
    """
    out = clean(message).html
    tags = parse_attrs(out)
    assert tags, f"{message.name} sanitised to no elements at all"
    assert {"div", "table", "p", "td"} & set(tags), (
        f"{message.name} kept no block-level structure: {sorted(tags)}"
    )
    hrefs = {a.get("href", "") for a in tags.get("a", [])}
    for link in message.links:
        assert link in hrefs, f"{message.name} lost the link {link!r} (kept: {sorted(hrefs)})"


# --- CSS legibility ----------------------------------------------------


@pytest.mark.parametrize(
    "message", [m for m in CORPUS if m.layout_css], ids=[m.name for m in CORPUS if m.layout_css]
)
def test_layout_bearing_css_is_not_stripped_to_nothing(message: Message):
    """The declarations that carry the message's *shape* survive.

    Named individually rather than counted. A ratio test passes on a
    sanitiser that keeps every `color` and drops every `width`, which is
    precisely the failure this phase's exit criterion is about — the
    message would be safe, present, and unreadable as a newsletter.
    """
    css = clean(message).css
    assert css, f"{message.name}'s whole stylesheet was dropped"
    for fragment in message.layout_css:
        assert fragment in css, f"{message.name} lost {fragment!r} from its stylesheet"


@pytest.mark.parametrize("message", CORPUS, ids=IDS)
def test_layout_bearing_inline_styles_survive(message: Message):
    """`style=""` is where real mail keeps its layout, and it survives.

    Table-layout mail puts almost everything in attributes because the
    clients it was written for never supported a `<style>` block. Named
    declarations again rather than a count, for the same reason.
    """
    out = clean(message).html
    for fragment in message.layout_inline:
        assert fragment in out, f"{message.name} lost the inline declaration {fragment!r}"


@pytest.mark.parametrize("message", CORPUS, ids=IDS)
def test_exactly_these_declarations_are_lost_and_no_others(message: Message):
    """The full inventory of what each message loses, as a multiset.

    A ratio ("we kept 82%") is the wrong instrument twice over: it passes a
    sanitiser that keeps every `color` and drops every `width`, and it says
    nothing about *which* properties a new allow-list entry would rescue.
    An exact multiset says both, and turns any future change to
    `ALLOWED_PROPERTIES` into a failure that names the property.

    The numbers behind these lists are tabulated in
    `docs/spikes/p1b-findings.md` under "Sanitiser".
    """
    before_css: collections.Counter[str] = collections.Counter()
    for block in extract_styles(raw(message)):
        before_css.update(declarations(block))
    lost_css = before_css - declarations(clean(message).css)
    assert sorted(lost_css.elements()) == sorted(message.dropped_css), (
        f"{message.name} stylesheet losses changed: {sorted(lost_css.elements())}"
    )

    lost_inline = style_attr_declarations(raw(message)) - style_attr_declarations(
        clean(message).html
    )
    assert sorted(lost_inline.elements()) == sorted(message.dropped_inline), (
        f"{message.name} inline losses changed: {sorted(lost_inline.elements())}"
    )


#: Prefixes no engine outside their own vendor reads. Losing one costs a
#: sender a hint, never a reader a layout.
VENDOR_PREFIXES = ("-webkit-", "-ms-", "-moz-", "-o-", "mso-")

#: Standard, widely-used properties this build drops from real mail. Every
#: entry is a legibility cost rather than a safety win, and every one is
#: argued out in `docs/spikes/p1b-findings.md`. Kept as one list, across
#: the whole corpus, because that is the shape of the decision someone will
#: eventually make about `ALLOWED_PROPERTIES` — not six per-client ones.
STANDARD_LOSSES = frozenset(
    {
        # Deliberate, with reasons on record.
        "background",  # can carry url(); `background-color` is allowed
        "color-scheme",  # `mailosh.render.dark` owns the frame's scheme
        "supported-color-schemes",
        "page",  # `@page` is print geometry for a document we do not own
        "outline",
        "overflow",
        "box-sizing",
        # Typographic hints the frame's own base stylesheet supplies.
        "word-wrap",
        "line-break",
        "font-variant-caps",
        # Lost with an unwrapped or removed *element*, not to a property
        # policy: `<body style=…>` and `<style style="display:none">`.
        "margin",
        "padding",
        "display",
    }
)


def test_nothing_standard_and_unaccounted_for_is_lost():
    """Across the whole corpus, every lost property is either vendor-private
    or on a list somebody has argued about.

    This is the test that would catch a *new* legibility regression. The
    per-message multisets above pin today's behaviour exactly, but they are
    also the thing a careless change would simply be updated to match. This
    one cannot be satisfied that way: a newly-dropped standard property
    fails it by name until it is either allow-listed or added to
    `STANDARD_LOSSES` with a reason.
    """
    unaccounted: set[str] = set()
    for message in CORPUS:
        for prop in set(message.dropped_css) | set(message.dropped_inline):
            if prop.startswith(VENDOR_PREFIXES) or prop in STANDARD_LOSSES:
                continue
            unaccounted.add(f"{message.name}:{prop}")
    assert not unaccounted, f"unexplained losses: {sorted(unaccounted)}"


def test_html_comment_wrapped_stylesheets_survive():
    """Outlook and Word wrap `<style>` contents in `<!-- -->`; the CSS
    inside still reaches the reader.

    Worth its own test because the failure would be total and silent: CDO
    and CDC tokens (`<!--`, `-->`) are legal at the top level of a
    stylesheet, but a parser that treated them as an error — or a
    sanitiser whose `<`-check looked at the *input* rather than the output
    — would return `""` for every Word and Outlook message ever sent, and
    `test_the_reader_can_still_read_it` would not notice, because the text
    is in the markup, not the sheet.
    """
    owa = BY_NAME["outlook_web_reply.html"]
    word = BY_NAME["outlook_desktop_reply.html"]
    assert "<!--" in raw(owa) and "<!--" in raw(word)
    assert clean(owa).css, "OWA's comment-wrapped stylesheet was dropped whole"
    assert "MsoNormal" in clean(word).css, "Word's comment-wrapped stylesheet was dropped whole"


def test_the_font_import_is_dropped_but_the_rest_of_the_sheet_is_not():
    """A `@font-face`/`@import` block costs the sender their web font, not
    the reader their layout.

    `newsletter_modern.html` carries two `<style>` elements, the first of
    which is nothing but an `@import` of a Google font. `ALLOWED_AT_RULES`
    is `{"media"}`, so that block sanitises to `""` — and the *second*
    block, which holds the entire design, must be unaffected. The two are
    joined by `sanitize_email_html`, and a bug that let one empty block
    swallow the other would be invisible in any test that only asked
    whether the output was non-empty.
    """
    modern = BY_NAME["newsletter_modern.html"]
    blocks = extract_styles(raw(modern))
    assert len(blocks) == 2 and "@import" in blocks[0]
    css = clean(modern).css
    assert "@import" not in css and "fonts.googleapis" not in css
    assert "max-width:560px" in css and "prefers-color-scheme" in css


def test_dark_mode_rules_a_newsletter_ships_itself_are_preserved():
    """A sender's own `prefers-color-scheme` block survives.

    This is what `mailosh.render.dark` defers to: a message that dresses
    itself for dark mode is not restyled. If the sanitiser dropped the
    media query the message would look light-on-light in a dark client
    *and* be denied the automatic restyle, which is the worst of both.
    """
    css = clean(BY_NAME["newsletter_modern.html"]).css
    dark = css[css.index("@media (prefers-color-scheme: dark)") :]
    assert "background-color:#11141b" in dark
    assert "color:#f2f4f8" in dark


# --- images ------------------------------------------------------------


@pytest.mark.parametrize("message", CORPUS, ids=IDS)
def test_inline_images_resolve_to_the_cid_route(message: Message):
    """Every `cid:` image the message declares comes out as an absolute
    same-origin `/m/{id}/cid/{cid}?u=…` URL.

    Absolute for two independent reasons (`html_sanitize` behaviour 1 and
    the frame's `img-src 'self'`), so the assertion is on the whole URL
    rather than on the path — a relative rewrite would be deleted by nh3
    and the reader would see a broken-image box with no explanation.

    The `?u=` is not decoration on real mail: the frame that fetches these
    has an opaque origin and sends no session cookie, so a rewrite without
    a capability is the broken-image box this corpus exists to rule out. It
    is asserted per fixture, against the id the message really carries,
    because a signer handed the wrong spelling of a Content-ID 404s every
    inline image in the app while every string in the document still looks
    right.
    """
    images = parse_attrs(clean(message).html).get("img", [])
    srcs = [img.get("src", "") for img in images]
    for cid in message.cids:
        quoted = quote(cid, safe="")
        expected = f"{ORIGIN}/m/E-corpus/cid/{quoted}?u={quote(f'cap-{cid}', safe='')}"
        assert expected in srcs, f"{message.name}: no img resolved to {expected!r} (got {srcs})"


@pytest.mark.parametrize(
    "message",
    [m for m in CORPUS if m.remote_hosts],
    ids=[m.name for m in CORPUS if m.remote_hosts],
)
def test_remote_images_are_blocked_then_proxied(message: Message):
    """`remote=0` blocks every remote image and names its host; `remote=1`
    routes every one through `/img?u=`, and none directly.

    The `remote=1` half is the one that matters for privacy: a single image
    that kept its original `src` would leak the read to the sender's server
    while the banner claimed everything was proxied.
    """
    blocked = clean(message)
    assert blocked.remote_hosts == message.remote_hosts
    assert blocked.blocked_remote >= len(message.remote_hosts)
    for img in parse_attrs(blocked.html).get("img", []):
        assert "//" not in img.get("src", "").removeprefix(f"{ORIGIN}/"), (
            f"{message.name} kept a remote src at remote=0: {img}"
        )

    shown = clean(message, remote=True)
    assert shown.blocked_remote == 0 and shown.remote_hosts == ()
    proxied = [
        img.get("src", "")
        for img in parse_attrs(shown.html).get("img", [])
        if img.get("src", "").startswith(f"{ORIGIN}/img?u=")
    ]
    assert len(proxied) == blocked.blocked_remote, (
        f"{message.name}: {blocked.blocked_remote} blocked but {len(proxied)} proxied"
    )
    for host in message.remote_hosts:
        assert host not in shown.html.replace("%3A%2F%2F", "://").replace(
            f"{ORIGIN}/img?u=", ""
        ) or any(host in url for url in proxied), f"{message.name} leaked {host} outside the proxy"


def test_a_tracking_pixel_is_counted_as_its_own_blocked_image():
    """The banner's number is images, not hosts.

    Both newsletters carry a 1x1 open-tracking pixel on a host of its own,
    beside their real artwork. A reader told "1 image blocked" when two
    servers wanted to hear from them has been told the wrong thing, and the
    count is the only number the banner shows.
    """
    for name in ("newsletter_table.html", "newsletter_modern.html"):
        result = clean(BY_NAME[name])
        assert result.blocked_remote == 2, f"{name}: {result.blocked_remote} blocked, expected 2"
        assert len(result.remote_hosts) == 2


# --- quote trimming ----------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [m for m in CORPUS if m.quote_opens_with],
    ids=[m.name for m in CORPUS if m.quote_opens_with],
)
def test_the_quote_split_lands_where_the_client_put_it(message: Message):
    """The cut is at the client's own marker, not merely somewhere.

    `split_html` returns slices, so `visible + quoted` is the input byte for
    byte; asserting the quoted half *starts with* the client's wrapper is
    what proves the reply survived above the fold rather than being hidden
    with the history.
    """
    result = clean(message)
    visible, quoted = quote_trim.split_html(result.html)
    assert visible + quoted == result.html
    assert quoted.startswith(message.quote_opens_with), (
        f"{message.name} cut at {quoted[:80]!r}, expected {message.quote_opens_with!r}"
    )
    assert message.sentences[0] in text_of(visible), (
        f"{message.name} hid the reply itself behind the quote toggle"
    )


@pytest.mark.parametrize(
    "message",
    [m for m in CORPUS if m.quote_opens_with is None and m.name.startswith("newsletter")],
    ids=[m.name for m in CORPUS if m.quote_opens_with is None and m.name.startswith("newsletter")],
)
def test_a_newsletter_is_never_cut_in_half(message: Message):
    """No quote marker means no cut.

    A false positive here is worse than a missed split: the reader opens a
    newsletter and sees the masthead and nothing else, with the rest behind
    a toggle that looks like quoted mail. Both newsletters contain dates,
    `From`-ish footer lines and `<blockquote>`-adjacent structure, which is
    exactly the material the textual heuristics look at.
    """
    visible, quoted = quote_trim.split_html(clean(message).html)
    assert quoted == "", f"{message.name} was split at {quoted[:120]!r}"
    for sentence in message.sentences:
        assert sentence in text_of(visible)


# --- the assembled frame ----------------------------------------------


@pytest.mark.parametrize("message", CORPUS, ids=IDS)
def test_the_assembled_frame_is_legible_and_carries_one_script(message: Message):
    """The end of the pipeline: sanitise, split, render.

    Both halves at once, because this is the only place they meet. The
    message's own CSS must be inlined (legibility) and the only script in
    the finished document must be the hash-pinned resize block (safety) —
    a corpus fixture that smuggled a second `<script>` past the sanitiser
    would break the CSP that pins it, and nothing else in this module
    looks at the assembled document.
    """
    result = clean(message)
    visible, quoted = quote_trim.split_html(result.html)
    document = render_frame(
        visible_html=visible,
        quoted_html=quoted,
        mail_css=result.css,
        theme="light",
        restyle="none",
    )
    assert document.lower().count("<script") == 1
    assert FRAME_SCRIPT in document
    if result.css:
        assert result.css in document, f"{message.name}'s stylesheet did not reach the frame"
    for sentence in message.sentences:
        assert sentence in text_of(document)
    for element in parse_attrs(document).values():
        for attrs in element:
            assert not any(key.startswith("on") for key in attrs), attrs


# --- the two gaps this corpus found, now closed -----------------------
#
# These began as assertions of the behaviour that shipped, written so that
# a fix would fail by name rather than pass silently. Both were fixed in
# `fix(render): keep border longhands, trim modern Outlook replies`, so
# they now assert the corrected behaviour and guard against regressing to
# what the corpus originally caught. The evidence for both is in
# docs/spikes/p1b-findings.md.


@pytest.mark.parametrize(
    "longhand",
    [
        f"border-{side}-{prop}"
        for side in ("top", "right", "bottom", "left")
        for prop in ("color", "style", "width")
    ],
)
def test_two_level_border_longhands_survive(longhand: str):
    """A rule written as longhands survives, exactly as its shorthand does.

    Four of the six corpus messages write their rules this way — it is what
    Word emits for a table underline, what OWA emits for a signature image,
    and what the table newsletter uses for its masthead rule and story
    dividers. Every one of those rules used to be silently lost, which was
    visible on screen: one table row kept its underline (shorthand) and the
    next lost it (longhands).

    Allowing them grants no new capability. Each is a component of a
    shorthand that was already allow-listed, and the value filter is
    *property-agnostic* — `_unsafe_values` takes a component-value list and
    is never told which property it is filtering, so a longhand cannot be
    treated more permissively than its shorthand. (An earlier note claimed
    the value check runs *before* the property name is consulted; that is
    not the code order. Property-agnosticism is the half that holds, and it
    is the half this relies on.) `test_css_sanitize.py` proves the
    consequence directly, over every allow-listed property.
    """
    assert longhand in ALLOWED_PROPERTIES
    assert sanitize_declarations(f"{longhand}:1px") == f"{longhand}:1px"
    shorthand = longhand.rsplit("-", 1)[0]
    assert shorthand in ALLOWED_PROPERTIES, (
        f"{longhand} is allowed, so its shorthand {shorthand} must be too"
    )


def test_word_desktop_reply_is_quote_trimmed():
    """A modern Outlook-for-Windows reply collapses its history.

    `QUOTE_MATCHERS` covers OWA (`#divRplyFwdMsg`, `div[id^=appendonsend]`)
    and the textual `-----Original Message-----` divider. Current Word
    builds emit neither: they open the history with an unmarked
    `<div style="border:none;border-top:solid #E1E1E1 1.0pt;…">` followed
    by a `From:/Sent:/To:/Subject:` block, and the HTML heuristics only
    look for an `On … wrote:` attribution or that divider.

    `tests/fixtures/mail/quotes/outlook_desktop.html` passes because it
    uses the `-----Original Message-----` shape — the one Word variant that
    was always covered. This corpus fixture uses the other one, and the
    whole quoted history used to render inline.

    The HTML path now has the header-block heuristic the plain-text path
    already had. It keys on the *header text*, never on the border, so a
    message that legitimately opens with a bordered div is left alone.
    """
    word = BY_NAME["outlook_desktop_reply.html"]
    body = raw(word)
    assert "-----Original Message-----" not in body
    assert "border-top:solid #E1E1E1 1.0pt" in body

    visible, quoted = quote_trim.split_html(clean(word).html)
    assert quoted.startswith(
        '<div>\n<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">'
    ), f"cut landed at {quoted[:80]!r}"
    # The history is behind the toggle, and the reply itself is not.
    assert "Can you make Thursday?" in text_of(quoted)
    assert "Sent:" in text_of(quoted)
    assert "bring the revised figures." in text_of(visible)
    assert visible + quoted == clean(word).html
