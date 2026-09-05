"""Rendering a `text/plain` body (spec §7, Task 8).

A plain-text body is the one kind of mail content this app renders *inside
its own origin* — no sandboxed frame stands between it and the session — so
the whole module rests on one ordering rule: **escape first, scan second.**
`linkify` hands `markupsafe.escape` the raw text and only then looks for
URLs, in the escaped string. By the time the regex runs, a `"` the sender
wrote is already `&#34;` and cannot close the `href` it lands in.

`test_the_reverse_order_would_have_been_exploitable` writes the wrong order
out in full and asserts the injection it allows, so the rule is pinned by a
demonstration rather than by a comment. The tests either side of it parse
`linkify`'s real output with `HTMLParser` and assert on the *attributes the
anchor carries*, not on substrings: an injected `onmouseover=` is an extra
attribute, and counting attributes catches it however it was spelled.
"""

from __future__ import annotations

import pathlib
import re
from html.parser import HTMLParser

import pytest
from markupsafe import Markup, escape

from mailosh.render.plain_text import TextLine, linkify, render_plain, split_quoted

REPO = pathlib.Path(__file__).resolve().parents[2]
INPUT_CSS = REPO / "styles/input.css"
BUILT_CSS = REPO / "mailosh/web/static/app.css"

#: markupsafe writes `"` as `&#34;`, not `&quot;` — asserted against the
#: escaper's own spelling rather than a hardcoded guess at which of the two
#: forms it picked.
QUOTE_ENTITY = str(escape('"'))


def _tags(html: str, name: str) -> list[dict[str, str]]:
    """Every `<name>` element's attributes, as the browser would see them."""
    found: list[dict[str, str]] = []

    class _Collect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == name:
                found.append({attr: value or "" for attr, value in attrs})

    parser = _Collect(convert_charrefs=True)
    parser.feed(html)
    parser.close()
    return found


def _anchors(html: str) -> list[dict[str, str]]:
    return _tags(html, "a")


# ---------------------------------------------------------------------------
# Escaping, and the order it happens in.
# ---------------------------------------------------------------------------


def test_text_is_escaped_before_anything_else_happens():
    visible, _ = render_plain("<script>alert(1)</script> & <b>x</b>")
    joined = "".join(str(line.html) for line in visible)
    assert "<script" not in joined and "&lt;script&gt;" in joined
    assert "&amp;" in joined and "<b>" not in joined


def test_a_url_cannot_break_out_of_the_href_it_lands_in():
    # linkify scans the ALREADY-ESCAPED string, so a quote in the source text
    # is an entity by the time the regex sees it and can never close href="".
    out = str(linkify('go to https://ok.test/x"onmouseover=alert(1) now'))
    assert out.count("<a ") == 1
    assert 'onmouseover="' not in out
    assert QUOTE_ENTITY + "onmouseover" in out


def test_a_hostile_url_cannot_add_an_attribute_to_the_anchor():
    anchors = _anchors(str(linkify('go to https://ok.test/x" onmouseover="alert(1) now')))
    assert len(anchors) == 1
    assert set(anchors[0]) == {"href", "target", "rel"}


def test_the_reverse_order_would_have_been_exploitable():
    """The wrong order, written out so the bug is visible rather than
    merely asserted.

    Scanning the *raw* text and marking the result safe is the shape a
    linkifier takes when escaping is bolted on afterwards. The URL character
    class stops at the sender's `"`, so everything they wrote after it — an
    `<img onerror=...>` — reaches the page as live markup. Escaping first
    turns that whole region into text *before* the regex ever runs, which is
    why the two steps cannot be reordered.
    """
    hostile = 'go to https://ok.test/x" now <img src=x onerror="alert(1)">'
    scan_first = re.sub(
        r"\b(?:https?://|mailto:)[^\s<>\"]+",
        lambda m: (
            f'<a href="{m.group(0)}" target="_blank" '
            f'rel="noopener noreferrer nofollow">{m.group(0)}</a>'
        ),
        hostile,
    )
    assert _tags(scan_first, "img") == [{"src": "x", "onerror": "alert(1)"}]

    safe = str(linkify(hostile))
    assert _tags(safe, "img") == []
    assert "&lt;img" in safe
    assert len(_anchors(safe)) == 1


def test_linkify_returns_markup_so_a_template_does_not_escape_it_twice():
    assert isinstance(linkify("plain"), Markup)
    assert all(isinstance(line.html, Markup) for line in render_plain("a\nb")[0])


# ---------------------------------------------------------------------------
# What becomes a link.
# ---------------------------------------------------------------------------


def test_links_become_anchors_with_the_same_rel_as_sanitised_mail():
    out = str(linkify("see https://ok.test/a?b=1&c=2 now"))
    assert 'href="https://ok.test/a?b=1&amp;c=2"' in out
    assert 'target="_blank"' in out
    assert set(out.split('rel="')[1].split('"')[0].split()) == {
        "noopener",
        "noreferrer",
        "nofollow",
    }
    assert out.count("<a ") == 1


def test_mailto_and_bare_www_behaviour_is_explicit():
    assert 'href="mailto:a@b.test"' in str(linkify("write to mailto:a@b.test"))
    assert "<a " not in str(linkify("visit www.example.test"))  # http/https/mailto only, spec §7


@pytest.mark.parametrize(
    "text",
    [
        "ftp://files.test/x",
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
    ],
)
def test_no_other_scheme_is_ever_linked(text: str):
    assert "<a " not in str(linkify(f"see {text} please"))


def test_every_url_in_a_line_is_linked_not_only_the_first():
    out = str(linkify("https://a.test/1 and https://b.test/2 and mailto:c@d.test"))
    hrefs = [a["href"] for a in _anchors(out)]
    assert hrefs == ["https://a.test/1", "https://b.test/2", "mailto:c@d.test"]


def test_the_text_around_a_link_survives_verbatim():
    out = str(linkify("see https://ok.test/a now"))
    assert out.startswith("see ") and out.endswith(" now")


# ---------------------------------------------------------------------------
# Lines, depths, and the cut.
# ---------------------------------------------------------------------------


def test_depth_classes_ride_on_the_lines_not_on_a_wrapper():
    visible, quoted = render_plain("reply\n\n> a\n>> b")
    assert [line.depth for line in visible] == [0, 0]
    assert [line.depth for line in quoted] == [1, 2]


def test_a_body_with_no_quote_has_no_quoted_lines():
    visible, quoted = render_plain("one\ntwo\nthree")
    assert [str(line.html) for line in visible] == ["one", "two", "three"]
    assert quoted == []


def test_an_empty_or_missing_body_renders_nothing_rather_than_raising():
    assert render_plain(None) == ([], [])
    assert render_plain("") == ([], [])


def test_a_line_keeps_its_own_leading_marker_characters():
    """The `>` characters stay in the rendered text — the depth class is a
    colour, not a replacement for the marker the sender actually sent.
    """
    _visible, quoted = render_plain("hi\n\n> quoted\n>> deeper")
    assert [str(line.html) for line in quoted] == ["&gt; quoted", "&gt;&gt; deeper"]


def test_lines_are_textline_records_not_tuples():
    (line,) = render_plain("just one")[0]
    assert isinstance(line, TextLine)
    assert (str(line.html), line.depth) == ("just one", 0)


def test_a_url_inside_a_quoted_line_is_linked_too():
    _visible, quoted = render_plain("hi\n\n> see https://ok.test/a")
    assert _anchors(str(quoted[0].html))[0]["href"] == "https://ok.test/a"


# ---------------------------------------------------------------------------
# `split_quoted` — the `>`-run grouper, moved here from `mailosh.web.app`.
# Its own grouping cases stay in `tests/unit/test_web_thread.py`; what is
# checked here is only that it lives in this module now.
# ---------------------------------------------------------------------------


def test_the_run_grouper_moved_into_this_module():
    assert split_quoted("hi\n> quoted") == [("hi", False), ("> quoted", True)]


# ---------------------------------------------------------------------------
# The depth colours have to reach the browser, not just the source sheet.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selector", [".q1", ".q2", ".q3", ".q4"])
def test_the_depth_colours_survived_the_css_build(selector: str):
    """`layouts/app.html` links the *compiled* `static/app.css`, so a rule
    added to `styles/input.css` and never built changes nothing at all.
    """
    pattern = re.escape(selector) + r"\s*[{,]"
    assert re.search(pattern, INPUT_CSS.read_text()), selector
    assert re.search(pattern, BUILT_CSS.read_text()), selector
