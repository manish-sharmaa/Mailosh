"""Where a reply ends and the quoted history begins (spec §7, Task 8).

Two independent questions live in `mailosh.render.quote_trim`, and this
module keeps them apart:

* **HTML** — `split_html` slices the *already-sanitised* fragment at the
  first start tag matching `QUOTE_MATCHERS` (ihasmail's selector list),
  falling back to the two textual heuristics only when no selector hit.
  The slice is by offset, never by re-serialisation, so the split cannot
  introduce markup the sanitiser did not already approve — pinned below by
  `test_splitting_never_introduces_markup_the_sanitiser_did_not_produce`,
  which asserts the two halves concatenate back to the exact input.
* **Plain text** — `find_quote_start` scores every family of marker and
  returns the *earliest* one, so a body carrying two of them (an
  attribution line and an Outlook divider) cuts at the one the reader sees
  first, not at whichever pattern happens to be listed first.

The eight `tests/fixtures/mail/quotes/*.html` files are one realistic body
per client family. Each writes its reply half as `<p>REPLY-{name}</p>` (or
the family's own equivalent block) and its quoted half as
`QUOTED-{name}`, so a fixture proves the two halves actually landed on
opposite sides of the cut rather than merely that some string survived.
The `quote_fixtures` fixture lives here rather than in `tests/conftest.py`:
nothing outside this module reads those files.

Outlook for Windows has **two** files because it emits two shapes and the
older one was giving this module false confidence: `outlook_desktop.html`
opens its history with `-----Original Message-----`, which the divider
heuristic has always caught, while current builds emit
`outlook_desktop_modern.html` — an unmarked `<div>`, a bordered `<div>`
inside it and a `From:`/`Sent:`/`To:`/`Subject:` paragraph, with no class,
no id and no divider anywhere. That second body went untrimmed and the
whole thread rendered inline, which nothing here noticed because the
fixture directory only held the first.
"""

from __future__ import annotations

import pathlib

import pytest

from mailosh.render.quote_trim import (
    QUOTE_MATCHERS,
    find_quote_start,
    quote_depth,
    split_html,
    split_plain,
)

QUOTES_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "mail" / "quotes"

#: One file per client family, and the exact opening tag (or, for the two
#: Outlook desktop bodies, the exact opening line) the quoted half must
#: begin with — i.e. *where* the cut is, not just that a cut happened. The
#: two Outlook desktop entries are the only fixtures carrying no selector at
#: all: one is the `-----Original Message-----` divider and the other is a
#: bare `From:`/`Sent:` header block, so between them they exercise both
#: textual fallbacks through a whole realistic body rather than a synthetic
#: snippet.
CLIENT_FAMILIES: tuple[tuple[str, str], ...] = (
    ("gmail", '<div class="gmail_quote">'),
    ("outlook_web", '<div id="appendonsend">'),
    ("outlook_desktop", '<p class="MsoNormal">-----Original Message-----</p>'),
    (
        "outlook_desktop_modern",
        '<div>\n<div style="border:none;border-top:solid #E1E1E1 1.0pt',
    ),
    ("apple", '<blockquote type="cite">'),
    ("thunderbird", '<div class="moz-cite-prefix">'),
    ("yahoo", '<div id="yahoo_quoted_9182736450" class="yahoo_quoted">'),
    ("protonmail", '<div class="protonmail_quote">'),
)


@pytest.fixture
def quote_fixtures() -> tuple[tuple[str, str, str], ...]:
    """`(name, html, expected_visible_snippet)` for every client family."""
    return tuple(
        (name, (QUOTES_DIR / f"{name}.html").read_text(encoding="utf-8"), f"REPLY-{name}")
        for name, _open_tag in CLIENT_FAMILIES
    )


# ---------------------------------------------------------------------------
# HTML: the selector list.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        '<div class="gmail_quote">',
        '<blockquote type="cite">',
        '<div class="moz-cite-prefix">',
        '<div id="divRplyFwdMsg">',
        '<div class="yahoo_quoted">',
        '<div id="appendonsend-1">',
        '<div class="ms-outlook-mobile-reference-message">',
        '<div id="OLK_SRC_BODY_SECTION">',
        '<div class="protonmail_quote">',
        '<div class="mailosh_quote">',
    ],
)
def test_every_spec_selector_splits_the_body(marker: str):
    visible, quoted = split_html(f"<p>my reply</p>{marker}<p>old text</p></div>")
    assert visible == "<p>my reply</p>"
    assert quoted.startswith(marker[:8])
    assert "old text" in quoted


def test_the_matcher_list_is_the_ten_the_spec_names_in_the_order_it_names_them():
    assert len(QUOTE_MATCHERS) == 10
    assert [(m.tag, m.klass, m.id_exact, m.id_prefix, m.attr) for m in QUOTE_MATCHERS] == [
        (None, "gmail_quote", None, None, None),
        ("blockquote", None, None, None, ("type", "cite")),
        (None, "moz-cite-prefix", None, None, None),
        (None, None, "divRplyFwdMsg", None, None),
        (None, "yahoo_quoted", None, None, None),
        ("div", None, None, "appendonsend", None),
        (None, "ms-outlook-mobile-reference-message", None, None, None),
        (None, None, "OLK_SRC_BODY_SECTION", None, None),
        (None, "protonmail_quote", None, None, None),
        (None, "mailosh_quote", None, None, None),
    ]


def test_a_body_with_no_quote_returns_an_empty_second_half():
    visible, quoted = split_html("<p>just a reply</p>")
    assert visible == "<p>just a reply</p>" and quoted == ""


def test_the_first_marker_wins_when_several_are_present():
    visible, quoted = split_html(
        '<p>a</p><div class="gmail_quote">g</div><div class="yahoo_quoted">y</div>'
    )
    assert visible == "<p>a</p>"
    assert quoted.count("gmail_quote") == 1 and "yahoo_quoted" in quoted


def test_document_order_beats_matcher_order():
    """The cut is the *first marker in the body*, not the first entry in
    `QUOTE_MATCHERS` that happens to match somewhere: `.mailosh_quote` is
    last in the list and first in this body, and it is where the cut lands.
    """
    visible, quoted = split_html(
        '<p>a</p><div class="mailosh_quote">mine</div><div class="gmail_quote">g</div>'
    )
    assert visible == "<p>a</p>"
    assert quoted.startswith('<div class="mailosh_quote">')


def test_a_class_list_containing_the_marker_still_matches():
    visible, _ = split_html('<p>a</p><div class="x gmail_quote y">q</div>')
    assert visible == "<p>a</p>"


def test_a_class_that_merely_contains_the_marker_as_a_substring_does_not_match():
    """Token match, not substring match — `gmail_quoteish` is somebody
    else's class name, and cutting a reply in half on it would lose text
    the reader wrote.
    """
    visible, quoted = split_html('<p>a</p><div class="gmail_quoteish">still mine</div>')
    assert quoted == ""
    assert "still mine" in visible


def test_an_id_prefix_matcher_does_not_fire_on_a_bare_prefix_elsewhere():
    """`div[id^=appendonsend]` is anchored at the start of the id and to
    `<div>`: a `<section>` or an id that merely ends with the word is not a
    quote marker.
    """
    assert split_html('<p>a</p><section id="appendonsend-1">x</section>')[1] == ""
    assert split_html('<p>a</p><div id="x-appendonsend">y</div>')[1] == ""


def test_the_attribute_matcher_needs_the_value_not_just_the_attribute():
    assert split_html('<p>a</p><blockquote type="quote">x</blockquote>')[1] == ""
    assert split_html("<p>a</p><blockquote>x</blockquote>")[1] == ""


def test_a_nested_marker_splits_at_its_own_offset():
    visible, quoted = split_html('<div><p>a</p><div class="gmail_quote">q</div></div>')
    assert "a" in visible and "gmail_quote" in quoted


def test_splitting_never_introduces_markup_the_sanitiser_did_not_produce():
    src = '<p>a</p><div class="gmail_quote"><p>b</p></div>'
    visible, quoted = split_html(src)
    assert visible + quoted == src


def test_the_split_is_by_offset_even_across_lines_and_entities():
    """`getpos()` is (line, column), so the offset table has to be right for
    a body that is not one line — and entity references must not shift it,
    since they are counted in the source, not the decoded, string.
    """
    src = '<p>&amp;&lt;&gt; café</p>\n<p>second</p>\r\n<div class="gmail_quote">q</div>'
    visible, quoted = split_html(src)
    assert visible + quoted == src
    assert quoted == '<div class="gmail_quote">q</div>'


# ---------------------------------------------------------------------------
# HTML: the two textual heuristics, used only when no selector hit.
# ---------------------------------------------------------------------------


def test_an_attribution_paragraph_splits_a_body_with_no_selector():
    src = "<p>sure</p><div>On Sep 1, 2026 at 8:41 PM, Dan &lt;d@x&gt; wrote:</div><p>old</p>"
    visible, quoted = split_html(src)
    assert visible == "<p>sure</p>"
    assert quoted.startswith("<div>On Sep 1")


def test_an_original_message_divider_splits_a_body_with_no_selector():
    src = "<p>sure</p><p>-- Original Message --</p><p>old</p>"
    visible, quoted = split_html(src)
    assert visible == "<p>sure</p>"
    assert "old" in quoted


def test_the_divider_heuristic_is_case_insensitive():
    assert split_html("<p>sure</p><p>-----ORIGINAL MESSAGE-----</p><p>old</p>")[1].startswith(
        "<p>-----ORIGINAL"
    )


def test_an_attribution_directly_above_a_selector_folds_in_with_it():
    """An Apple Mail reply puts its attribution line *above* the
    `blockquote[type=cite]`; Gmail puts its own inside `.gmail_quote`. The
    line means the same thing in both, so the cut lands in the same place:
    above it.

    It is metadata about the quote — a name and a date the card header
    already shows — rather than something the sender wrote, and in a long
    thread every reply carries one. The plain-text side has always folded
    it away; this is the HTML side agreeing.
    """
    src = (
        "<p>sure</p><div>On Sep 1, 2026 at 8:41 PM, Dan wrote:</div>"
        '<blockquote type="cite"><p>old</p></blockquote>'
    )
    visible, quoted = split_html(src)
    assert visible == "<p>sure</p>"
    assert quoted.startswith("<div>On Sep 1")
    assert visible + quoted == src


def test_a_selector_still_beats_a_heuristic_below_it():
    """Pulling the cut back is only ever onto the line *immediately* above
    the marker. An attribution inside the quoted history is part of the
    history, and moving the cut to it would leave the quote's own opening
    element in the visible half.
    """
    src = (
        "<p>sure</p>"
        '<blockquote type="cite"><div>On Sep 1, 2026 at 8:41 PM, Dan wrote:</div>'
        "<p>old</p></blockquote>"
    )
    visible, quoted = split_html(src)
    assert visible == "<p>sure</p>"
    assert quoted.startswith('<blockquote type="cite">')


def test_only_whitespace_may_stand_between_the_attribution_and_the_quote():
    """The condition that keeps this from swallowing a reply. A paragraph
    the reader wrote between the two means the line above is not this
    quote's attribution, whatever it reads like.
    """
    src = (
        "<p>sure</p><div>On Sep 1, 2026 at 8:41 PM, Dan wrote:</div>"
        "<p>...and I disagree</p>"
        '<blockquote type="cite"><p>old</p></blockquote>'
    )
    visible, quoted = split_html(src)
    assert "and I disagree" in visible
    assert quoted == '<blockquote type="cite"><p>old</p></blockquote>'


def test_the_container_holding_the_attribution_is_what_folds_in():
    """`<div><p>On … wrote:</p></div>` matches at both depths — the inner
    element's text content is the line, and so is the outer's. Cutting at
    the inner one would leave the container's opening tag stranded in the
    visible half.
    """
    src = (
        "<p>sure</p><div><p>On Sep 1, 2026 at 8:41 PM, Dan wrote:</p></div>"
        '<blockquote type="cite"><p>old</p></blockquote>'
    )
    visible, quoted = split_html(src)
    assert visible == "<p>sure</p>"
    assert quoted.startswith("<div><p>On Sep 1")


def test_a_paragraph_that_only_mentions_wrote_is_not_an_attribution():
    """The attribution regex anchors both ends: a sentence that merely
    contains the word is a sentence, and cutting there would hide the rest
    of the reply.
    """
    src = "<p>I wrote: nothing came of it</p><p>still my reply</p>"
    assert split_html(src)[1] == ""


def test_an_attribution_longer_than_the_regex_allows_is_not_a_marker():
    body = "On " + ("x" * 400) + " wrote:"
    assert split_html(f"<p>a</p><div>{body}</div>")[1] == ""


# ---------------------------------------------------------------------------
# HTML: the forwarded-header block, which is all modern Outlook for Windows
# marks its history with.
# ---------------------------------------------------------------------------
#
# Word emits no class, no id and no `-----Original Message-----`: an
# unmarked `<div>`, a bordered `<div>` inside it, and a paragraph reading
# `From: … Sent: … To: … Subject: …`. `QUOTE_MATCHERS` and both other
# heuristics walk straight past all three, so the entire conversation
# history rendered inline with no toggle.
#
# The tests below are weighted deliberately towards the *false positive*.
# Failing to trim costs the reader some scrolling; trimming in the wrong
# place hides what somebody wrote, which is the worse mistake and the one
# a text-shaped heuristic is prone to.

#: The block Word actually sends, without the `<div>` wrappers.
WORD_HEADER_BLOCK = (
    "<p><b>From:</b> Dan Okafor &lt;dan@example.test&gt;<br>"
    "<b>Sent:</b> Tuesday, September 1, 2026 8:41 PM<br>"
    "<b>To:</b> Priya Raman &lt;priya@example.test&gt;<br>"
    "<b>Subject:</b> Thursday review</p>"
)


def test_a_word_header_block_splits_a_body_carrying_no_marker_of_any_kind():
    src = f"<p>Confirmed.</p>{WORD_HEADER_BLOCK}<p>Can you make Thursday?</p>"
    visible, quoted = split_html(src)
    assert visible == "<p>Confirmed.</p>"
    assert quoted.startswith("<p><b>From:</b>")
    assert "Can you make Thursday?" in quoted
    assert visible + quoted == src


def test_the_line_breaks_may_be_br_tags_with_no_newlines_in_the_source():
    """`WORD_HEADER_BLOCK` above is one source line: the four fields are
    separated by `<br>` and nothing else.

    Whether a client also puts a newline after each `<br>` is a fact about
    how it wrote the file, not about the message, so the heuristic reads
    `<br>` as the line break it is. Word happens to emit both; other builds
    emit neither.
    """
    assert "\n" not in WORD_HEADER_BLOCK
    assert split_html(f"<p>a</p>{WORD_HEADER_BLOCK}<p>old</p>")[1].startswith("<p><b>From:")


def test_the_container_holding_the_header_block_is_what_folds_in():
    """Word's real shape: `<div><div style="border-top:…"><p>From: …</p></div></div>`.

    All three elements' text content is the header block, so all three
    match; the cut lands on the outermost, because cutting at the `<p>`
    would strand two opening `<div>`s — one of them carrying the rule Word
    draws above the quote — in the visible half.
    """
    src = (
        "<p>Confirmed.</p>"
        '<div>\n<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">'
        f"\n{WORD_HEADER_BLOCK}\n</div>\n</div>"
        "<p>Can you make Thursday?</p>"
    )
    visible, quoted = split_html(src)
    assert visible == "<p>Confirmed.</p>"
    assert quoted.startswith("<div>\n<div style=")
    assert visible + quoted == src


@pytest.mark.parametrize(
    "block",
    [
        # Word and Outlook on the web.
        "<p>From: Dan<br>Sent: Tuesday<br>To: Priya<br>Subject: Thursday</p>",
        # Apple Mail and Gmail forwards say Date: rather than Sent:.
        "<p>From: Dan<br>Date: 1 September 2026<br>Subject: Thursday<br>To: Priya</p>",
        # Case and spacing are the sender's.
        "<p>FROM : Dan<br>SENT : Tuesday<br>SUBJECT : Thursday</p>",
        # Block elements instead of <br>, and a Cc.
        "<div><div>From: Dan</div><div>Sent: Tuesday</div><div>Cc: Sam</div></div>",
    ],
)
def test_the_header_names_and_shapes_clients_actually_send(block: str):
    visible, quoted = split_html(f"<p>a</p>{block}<p>old</p>")
    assert visible == "<p>a</p>"
    assert quoted, block


# --- false positives: everything below must NOT be trimmed ---------------


def test_a_sentence_that_merely_mentions_from_and_sent_is_not_a_header_block():
    """The stated risk, in the shape it actually arrives in.

    Every header name appears, in order, with a colon after it — and it is
    one line of prose with the sender's own point at the end of it. A
    heuristic that scanned for the words would cut here and hide the point.
    """
    src = (
        "<p>From: the desk of Dan, Sent: whenever he feels like it, To: whoever is "
        "listening — anyway, Thursday works and I will bring the figures.</p>"
        "<p>See you then.</p>"
    )
    assert split_html(src)[1] == ""


def test_a_header_block_must_start_at_from():
    """`Subject:` first is Thunderbird's *forward* header, and it is also
    what a checklist someone typed looks like. `From:` first is the shape
    every client this heuristic is aimed at emits, and requiring it costs
    nothing here and rules a great deal out.
    """
    src = "<p>a</p><p>Subject: Thursday<br>From: Dan<br>Sent: Tuesday</p><p>my own notes</p>"
    assert split_html(src)[1] == ""


def test_a_from_line_with_no_sent_or_date_is_not_a_header_block():
    """The same rule the plain-text side has always applied
    (`test_a_from_line_with_no_sent_or_date_after_it_is_not_a_header_block`):
    `From:` and `To:` alone are how people write a memo header.
    """
    src = "<p>a</p><p>From: Priya<br>To: the team<br>Subject: Thursday</p><p>Please read.</p>"
    assert split_html(src)[1] == ""


def test_two_header_lines_are_not_enough():
    src = "<p>a</p><p>From: Dan<br>Sent: Tuesday</p><p>and here is my answer</p>"
    assert split_html(src)[1] == ""


def test_a_line_long_enough_to_be_a_paragraph_is_a_paragraph():
    """The length bound. A header line is a name, a date or a subject; what
    opens with `To:` and then runs for a paragraph is a paragraph, and the
    sender's point is at the end of it.
    """
    paragraph = (
        "To: the room — and I want to flag that this is exactly the kind of thing we said "
        "we would stop doing, because every time we do it somebody ends up rewriting the "
        "deck at midnight and then nobody trusts the numbers in the morning, which is the "
        "actual problem here."
    )
    assert len(paragraph) > 200
    assert split_html(f"<p>a</p><p>From: Dan<br>Sent: Tuesday<br>{paragraph}</p>")[1] == ""


def test_an_element_holding_a_header_block_and_a_sentence_is_not_a_marker():
    """Every line must be a header line. A `<p>` that quotes a header block
    and then answers it is the reader's own writing, and the answer is
    below the header — exactly where an over-eager cut would hide it.
    """
    src = (
        "<p>a</p>"
        "<p>From: Dan<br>Sent: Tuesday<br>Subject: Thursday<br>"
        "Note the date — that is before the cut-off.</p>"
    )
    assert split_html(src)[1] == ""


def test_a_bordered_div_on_its_own_is_not_a_marker():
    """The heuristic keys on the header text, never on the rule Word draws
    above it.

    A `<div>` with a top border is ordinary layout — a card, a callout, a
    signature separator — and a message may legitimately open with one.
    Keying on the border would have made every one of those a quote.
    """
    bordered = '<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">'
    assert split_html(f"{bordered}<p>The whole message is in this box.</p></div>")[1] == ""
    assert split_html(f"<p>a</p>{bordered}<p>and a callout under it</p></div>")[1] == ""


def test_a_body_that_is_nothing_but_a_forward_is_left_whole():
    """The guard on the cut itself, rather than on the marker.

    Forwarding without a covering note is normal, and the result is a body
    whose very first element is the header block. Trimming there is
    technically correct and practically useless: the reader opens the
    message and finds an empty pane with a toggle under it. Under-trimming
    is the cheaper mistake, so this one is not made.
    """
    src = f"{WORD_HEADER_BLOCK}<p>Can you make Thursday?</p>"
    visible, quoted = split_html(src)
    assert quoted == ""
    assert visible == src


def test_the_same_guard_applies_to_the_other_textual_heuristics():
    """It is a rule about heuristic cuts, not about header blocks: an
    attribution line or a divider at the very top of a body would empty the
    pane just as completely.
    """
    assert split_html("<div>On Sep 1, 2026 at 8:41 PM, Dan wrote:</div><p>old</p>")[1] == ""
    assert split_html("<p>-----Original Message-----</p><p>old</p>")[1] == ""


def test_a_marked_quote_at_the_top_of_a_body_is_still_trimmed():
    """The guard is exempt for selectors, and deliberately so: a heuristic
    is this module reading a stranger's prose, but `.gmail_quote` is the
    sending client stating in markup that everything below is history.
    """
    visible, quoted = split_html('<div class="gmail_quote"><p>old</p></div>')
    assert visible == ""
    assert quoted == '<div class="gmail_quote"><p>old</p></div>'


def test_a_forward_with_a_covering_note_keeps_the_note_visible():
    """The case the guard is *not* for. One line of the sender's own is
    enough to make the cut worth taking, and it is the line that stays.
    """
    src = f"<p>Priya — see below, and note the date.</p>{WORD_HEADER_BLOCK}<p>old</p>"
    visible, quoted = split_html(src)
    assert visible == "<p>Priya — see below, and note the date.</p>"
    assert "note the date" not in quoted


def test_an_image_only_reply_above_a_forward_counts_as_a_reply():
    """ "See the chart" written as the chart. There is no text above the
    header block, but there is something to look at, so the cut is taken.
    """
    src = f'<p><img src="cid:chart" alt="chart"></p>{WORD_HEADER_BLOCK}<p>old</p>'
    visible, quoted = split_html(src)
    assert visible == '<p><img src="cid:chart" alt="chart"></p>'
    assert quoted.startswith("<p><b>From:")


def test_word_filler_paragraphs_do_not_count_as_a_reply():
    """`<p>&nbsp;</p>` is what Word puts between everything. It is not
    content, and a body that is only filler above a forward is still a body
    with nothing above the forward.
    """
    src = f"<p>&nbsp;</p>\n<p>&nbsp;</p>\n{WORD_HEADER_BLOCK}<p>old</p>"
    assert split_html(src)[1] == ""


def test_a_selector_below_a_header_block_still_wins():
    """A selector anywhere in the body beats every heuristic, including one
    that matched earlier — the same rule the attribution heuristic follows.
    """
    src = f'<p>a</p>{WORD_HEADER_BLOCK}<div class="gmail_quote"><p>old</p></div>'
    visible, quoted = split_html(src)
    assert quoted.startswith("<p><b>From:")
    assert visible + quoted == src


def test_the_two_word_variants_cut_at_their_own_markers():
    """The regression this fix is about, stated as the two files.

    `outlook_desktop.html` was the only Word body on disk and it carries a
    `-----Original Message-----` divider, so it passed while the shape
    current Word builds actually emit went untrimmed. Both are covered now,
    and each cuts in a different place.
    """
    old = (QUOTES_DIR / "outlook_desktop.html").read_text(encoding="utf-8")
    modern = (QUOTES_DIR / "outlook_desktop_modern.html").read_text(encoding="utf-8")

    assert "-----Original Message-----" in old
    assert "-----Original Message-----" not in modern
    for body in (old, modern):
        assert "gmail_quote" not in body and "appendonsend" not in body
        assert 'class="moz-cite-prefix"' not in body and "blockquote" not in body

    assert split_html(old)[1].startswith('<p class="MsoNormal">-----Original Message-----')
    assert split_html(modern)[1].startswith("<div>\n<div style=")


# ---------------------------------------------------------------------------
# HTML: the seven client families, end to end.
# ---------------------------------------------------------------------------


def test_the_fixture_directory_holds_exactly_one_body_per_client_family():
    assert sorted(p.name for p in QUOTES_DIR.glob("*.html")) == sorted(
        f"{name}.html" for name, _open_tag in CLIENT_FAMILIES
    )


def test_client_fixtures_split_where_expected(quote_fixtures):
    assert len(quote_fixtures) == len(CLIENT_FAMILIES)
    for name, html, expected_visible_snippet in quote_fixtures:
        visible, quoted = split_html(html)
        assert expected_visible_snippet in visible, name
        assert quoted, name
        assert expected_visible_snippet not in quoted, name


@pytest.mark.parametrize(("name", "open_tag"), CLIENT_FAMILIES)
def test_each_client_family_cuts_at_its_own_marker(name: str, open_tag: str):
    html = (QUOTES_DIR / f"{name}.html").read_text(encoding="utf-8")
    visible, quoted = split_html(html)

    assert visible + quoted == html
    assert quoted.startswith(open_tag)
    # The reply half survives whole and the history half is gone from it.
    assert f"REPLY-{name}" in visible and f"REPLY-{name}" not in quoted
    assert f"QUOTED-{name}" in quoted and f"QUOTED-{name}" not in visible


# ---------------------------------------------------------------------------
# Plain text.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "head"),
    [
        ("hi\n\nOn Sep 1, 2026 at 8:41 PM, Dan <d@x> wrote:\n> old", "hi"),
        ("hi\n\n-----Original Message-----\nFrom: Dan", "hi"),
        ("hi\n\n________________________________\nFrom: Dan\nSent: Monday", "hi"),
        ("hi\n\nFrom: Dan <d@x>\nSent: Monday, 1 Sep\nTo: me", "hi"),
        ("hi\n\n> old line\n> older line", "hi"),
    ],
)
def test_find_quote_start_locates_each_family(body: str, head: str):
    idx = find_quote_start(body)
    assert idx is not None
    assert body[:idx].strip() == head


def test_no_quote_returns_none_and_split_keeps_everything_visible():
    assert find_quote_start("just a note\nwith two lines") is None
    visible, quoted = split_plain("just a note")
    assert visible == "just a note" and quoted == ""


def test_the_earliest_marker_wins():
    body = "hi\n\nOn Sep 1 Dan wrote:\n\n-----Original Message-----\n"
    assert find_quote_start(body) == body.index("On Sep 1")


def test_split_plain_cuts_at_the_index_find_quote_start_reports():
    body = "hi\n\n> old line\n> older line"
    visible, quoted = split_plain(body)
    assert visible + quoted == body
    assert quoted == "> old line\n> older line"


def test_an_attribution_wrapped_over_two_lines_is_still_one_marker():
    body = "hi\n\nOn Tue, Sep 1, 2026 at 8:41 PM Dan Okafor\n<dan@example.test> wrote:\n> old"
    assert find_quote_start(body) == body.index("On Tue")


def test_a_from_line_with_no_sent_or_date_after_it_is_not_a_header_block():
    """`From:` on its own is prose (or a signature) — it only reads as a
    forwarded header block when `Sent:`/`Date:` follows within three lines.
    """
    assert find_quote_start("hi\n\nFrom: the desk of Dan\nsee you Thursday") is None


def test_an_underscore_rule_with_no_from_after_it_is_not_a_divider():
    assert find_quote_start("hi\n\n________________________________\njust a rule") is None


def test_only_a_trailing_run_of_quoted_lines_counts():
    """A `>` run with the reader's own text after it is an inline quote, not
    the start of the history — cutting there would hide live reply text.
    """
    body = "hi\n> they said this\nand here is my answer"
    assert find_quote_start(body) is None


def test_the_trailing_quoted_run_starts_at_its_own_first_line():
    body = "hi\n\n> one\n>> two\n> three\n"
    assert find_quote_start(body) == body.index("> one")


def test_a_wholly_quoted_body_is_quoted_from_its_first_character():
    assert find_quote_start("> all of it\n> every line") == 0


def test_an_empty_body_has_no_quote():
    assert find_quote_start("") is None
    assert split_plain("") == ("", "")


@pytest.mark.parametrize(
    ("line", "depth"),
    [("text", 0), ("> a", 1), (">> a", 2), ("  >  >  > a", 3), (">>>>>>> a", 4)],
)
def test_quote_depth_counts_and_caps_at_four(line: str, depth: int):
    assert quote_depth(line) == depth


@pytest.mark.parametrize("line", ["", "   ", "a > b", "  hello"])
def test_quote_depth_is_zero_for_anything_that_does_not_open_with_a_marker(line: str):
    assert quote_depth(line) == 0
