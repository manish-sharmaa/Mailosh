"""Unit tests for `split_quoted` (Phase 0), the plain-text-body grouping
helper that hides quoted reply text behind a collapse toggle.

Task 5 (design spec §9, controller ruling #3) deletes the Phase 0 `GET
/thread/{id}`/`POST /email/{id}/keyword`/`POST /email/{id}/archive` routes
this file used to test at HTTP level (along with `mailosh.web.deps.
get_client`, the fake-JMAP-client plumbing those tests depended on) — the
real thread view is Task 7's job, built on the new design-system layout, not
Phase 0's `base.html`/`thread.html`. `split_quoted` itself survives that cut
(controller ruling #3 explicitly keeps it): it's a pure function with no
route/client dependency of its own, and its grouping logic isn't Phase 0-
specific. Task 8 moved it out of `mailosh.web.app` into
`mailosh.render.plain_text`, next to the rest of the plain-text rendering
— only the import below changed, nothing this module asserts.
"""

from __future__ import annotations

from mailosh.render.plain_text import split_quoted

# ---------------------------------------------------------------------------
# split_quoted: pure-function tests, no app/client involved.
# ---------------------------------------------------------------------------


def test_split_quoted_collapses_quoted_block():
    assert split_quoted("> line one\n> line two") == [("> line one\n> line two", True)]


def test_split_quoted_unquoted_passthrough():
    assert split_quoted("just a normal reply\nsecond line") == [
        ("just a normal reply\nsecond line", False)
    ]


def test_split_quoted_interleaved():
    assert split_quoted("hi\n> quoted 1\n> quoted 2\nthanks") == [
        ("hi", False),
        ("> quoted 1\n> quoted 2", True),
        ("thanks", False),
    ]


def test_split_quoted_handles_nested_and_indented_markers():
    # ">>"  and a leading-space "> " both count as quoted.
    assert split_quoted(">> deeply quoted\n  > indented quote") == [
        (">> deeply quoted\n  > indented quote", True)
    ]


def test_split_quoted_empty_and_none():
    assert split_quoted("") == [("", False)]
    assert split_quoted(None) == [("", False)]


def test_split_quoted_no_trailing_empty_segment_after_quoted_block():
    # Regression test: a trailing "\n" terminates the body's last real
    # line — it must not register as a further, empty, always-visible
    # (unquoted) segment tacked on right after the collapsed quote block.
    # Before the fix this produced a spurious third segment, ("", False).
    assert split_quoted("hi\n> quoted\n> more\n") == [
        ("hi", False),
        ("> quoted\n> more", True),
    ]


def test_split_quoted_no_trailing_empty_segment_after_unquoted_text():
    assert split_quoted("just text\n") == [("just text", False)]


def test_split_quoted_interior_blank_line_preserved_not_over_trimmed():
    # Only the single trailing "\n" is stripped — an interior blank line
    # (here, between "hi" and "bye", with the whole body *also* ending in
    # "\n") must still come through inside its segment exactly as before
    # the fix, proving the fix doesn't over-trim beyond the one trailing
    # newline.
    assert split_quoted("hi\n\nbye\n") == [("hi\n\nbye", False)]
