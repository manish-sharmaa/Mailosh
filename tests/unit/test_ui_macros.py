"""Unit tests for `mailosh.ui.env.build_env` (Task 2): the `icon`/`static`
globals and the `layouts/app.html` base layout's required elements.

Uses the *real* `mailosh/web/static` tree, not a fixture/temp directory —
`icon()` reads real vendored Lucide SVGs (`make icons`) and `static()`
hashes real file bytes (`make vendor icons fonts css`), so these tests
exercise the actual build pipeline output, not a mock of it. This mirrors
`tests/conftest.py`'s "no fixture stands in for what real files must
actually contain" philosophy for the JMAP/db layers.
"""

import logging
import re
from pathlib import Path

from mailosh.ui.env import build_env
from mailosh.ui.format import avatar_color, initials

STATIC = Path("mailosh/web/static")


def test_icon_inlines_svg_with_class_and_aria():
    env = build_env(STATIC)
    html = env.globals["icon"]("archive", class_="size-4")
    assert html.startswith("<svg") and 'class="size-4' in html and 'aria-hidden="true"' in html
    assert 'stroke="currentColor"' in html


def test_static_is_content_versioned():
    env = build_env(STATIC)
    url = env.globals["static"]("js/app.js")
    assert url.startswith("/static/js/app.js?v=") and len(url.split("v=")[1]) == 8


def test_app_layout_renders_theme_density_and_csrf():
    env = build_env(STATIC)
    t = env.from_string('{% extends "layouts/app.html" %}{% block main %}<p>hi</p>{% endblock %}')
    html = t.render(
        prefs={"theme": "dark", "density": "compact", "shortcuts": True},
        csrf_token="tok123",
        user={"email": "d@x"},
        nav=None,
    )
    assert 'data-theme="dark"' in html and 'data-density="compact"' in html
    assert '<meta name="csrf-token" content="tok123">' in html and "X-CSRF-Token" in html
    assert 'role="status"' in html and 'id="compose-dock"' in html
    # An explicit (non-"system") theme resolves to exactly one theme-color
    # meta tag, no media query -- see the "system" case below for the
    # other branch.
    assert html.count('name="theme-color"') == 1
    assert '<meta name="theme-color" content="#0D1015">' in html


# ---------------------------------------------------------------------------
# Review finding: theme-color has the same "system" gap color-scheme did
# (below) -- a server that can't resolve "system" to a concrete colour must
# emit both, gated by their own prefers-color-scheme media query, rather
# than guessing one.
# ---------------------------------------------------------------------------


def test_theme_color_is_a_media_conditional_pair_for_system_theme():
    env = build_env(STATIC)
    t = env.from_string('{% extends "layouts/app.html" %}{% block main %}{% endblock %}')
    html = t.render(
        prefs={"theme": "system", "density": "comfortable", "shortcuts": True},
        csrf_token="tok",
        user={"email": "d@x"},
        nav=None,
    )
    assert (
        '<meta name="theme-color" content="#EBEEF3" media="(prefers-color-scheme: light)">' in html
    )
    assert (
        '<meta name="theme-color" content="#0D1015" media="(prefers-color-scheme: dark)">' in html
    )
    assert html.count('name="theme-color"') == 2


def test_bare_layout_theme_color_also_handles_system():
    env = build_env(STATIC)
    t = env.from_string('{% extends "layouts/bare.html" %}{% block content %}{% endblock %}')
    html = t.render(
        prefs={"theme": "system", "density": "comfortable"},
        csrf_token="tok",
    )
    assert html.count('name="theme-color"') == 2
    assert 'media="(prefers-color-scheme: dark)"' in html


# ---------------------------------------------------------------------------
# Controller decision (Task 2 brief): a missing icon must never crash a
# production page render, but must be caught loudly during development —
# two different guarantees, covered by two different tests rather than one
# function trying to behave both ways for the same input.
# ---------------------------------------------------------------------------


def test_icon_falls_back_for_unknown_name(caplog):
    """Runtime half: `icon()` itself never raises for a bad name — it logs
    a warning and returns an empty, decorative placeholder so the rest of
    the page still renders.
    """
    env = build_env(STATIC)
    with caplog.at_level(logging.WARNING, logger="mailosh.ui.macros"):
        html = env.globals["icon"]("not-a-real-icon")
    assert html == '<span aria-hidden="true"></span>'
    assert "not-a-real-icon" in caplog.text


def test_every_listed_icon_is_vendored():
    """Build/test-time half: every name `mailosh/ui/icons.txt` promises is
    actually present under `mailosh/web/static/icons/` — fails loudly
    (rather than each one silently degrading to `icon()`'s empty-span
    fallback above) the moment `make icons` hasn't been run for a name a
    template/macro call is about to rely on.
    """
    names = Path("mailosh/ui/icons.txt").read_text().split()
    assert names, "icons.txt should list at least the icons Task 2 itself needs"
    missing = [n for n in names if not (STATIC / "icons" / f"{n}.svg").exists()]
    assert not missing, f"not vendored — run `make icons`: {missing}"


def test_icon_with_label_uses_role_img_not_aria_hidden():
    env = build_env(STATIC)
    html = env.globals["icon"]("star", label="Starred")
    assert 'role="img"' in html and 'aria-label="Starred"' in html
    assert "aria-hidden" not in html


def test_icon_uses_spec_stroke_width():
    """Spec §4.2: "1.75 stroke" — Lucide's own default is 2."""
    env = build_env(STATIC)
    html = env.globals["icon"]("archive")
    assert 'stroke-width="1.75"' in html


def test_kbd_renders_one_element_per_space_separated_key():
    env = build_env(STATIC)
    html = env.globals["kbd"]("g i")
    assert html == '<kbd class="kbd">g</kbd><kbd class="kbd">i</kbd>'


def test_initials_filter_prefers_name_then_email_then_placeholder():
    assert initials("Daniel Okafor", "daniel@okafor.dev") == "D"
    assert initials(None, "github@example.com") == "G"
    assert initials("", "@example.com") == "?"


def test_avatar_color_filter_is_stable_and_in_palette_range():
    first = avatar_color("Alice@Example.com")
    # Same address, different casing/whitespace: same colour every time —
    # not a fresh hash() salt per process, not a coin flip per call.
    assert avatar_color(" alice@example.com ") == first
    assert 0 <= first < 12


# ---------------------------------------------------------------------------
# Review finding: `color-scheme` needs the same three-way treatment as every
# custom-property token (light / explicit dark / OS-preference dark) — every
# token got it, `color-scheme` itself originally didn't, so a
# `prefs.theme="system"` page under a dark OS kept every color token dark
# but rendered native form controls/scrollbars with light UA chrome. These
# parse the *built* `app.css` (not `styles/input.css`) since that's what a
# browser actually receives — `make css` must already have run, same
# precondition as `test_every_listed_icon_is_vendored` above.
# ---------------------------------------------------------------------------

#: Leg 1: unconditional light default on the root element. The negative
#: lookbehind keeps this from matching some other selector that merely
#: *ends* in "html" (none does today, but the built file is minified to a
#: single line, so being precise here costs nothing).
_COLOR_SCHEME_LIGHT_RE = re.compile(r"(?<![\w-])html\s*\{[^}]*color-scheme\s*:\s*light[^}]*\}")

#: Leg 2: explicit user override to dark, regardless of OS preference.
_COLOR_SCHEME_EXPLICIT_DARK_RE = re.compile(
    r"\[data-theme=dark\]\s*\{[^}]*color-scheme\s*:\s*dark[^}]*\}"
)

#: Leg 3 — the one the review finding caught missing: OS prefers dark AND
#: the user hasn't explicitly forced light. Requires `color-scheme:dark`
#: nested *inside* the matching `:root:not([data-theme=light])` selector
#: inside the matching media query, not just present anywhere in the file
#: (the token blocks above use this exact same media query for `--bg` etc.,
#: so a looser "does this substring appear somewhere" check could pass
#: without this leg ever existing).
_COLOR_SCHEME_SYSTEM_DARK_RE = re.compile(
    r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*"
    r"\{\s*:root:not\(\[data-theme=light\]\)\s*"
    r"\{[^}]*color-scheme\s*:\s*dark[^}]*\}"
)


def test_color_scheme_tracks_all_three_theme_legs_in_built_css():
    css = Path("mailosh/web/static/app.css").read_text()
    assert _COLOR_SCHEME_LIGHT_RE.search(css), "missing `html { color-scheme: light }`"
    assert _COLOR_SCHEME_EXPLICIT_DARK_RE.search(css), (
        "missing `[data-theme=dark] { color-scheme: dark }`"
    )
    assert _COLOR_SCHEME_SYSTEM_DARK_RE.search(css), (
        "missing `@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) "
        "{ color-scheme: dark } }` — the OS-preference leg"
    )
