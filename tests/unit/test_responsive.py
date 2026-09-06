"""The responsive and touch pass (Phase 1E; design spec §4.3/§11), pinned
from Python.

There is no Node toolchain and no headless browser in this project (Global
Constraints), so what a browser *paints* was checked by hand — measured
rectangles at 1440, 1100, 1099, 900, 768, 767, 600 and 390 CSS px, recorded
in this plan's report. What this file exists for is the set of breakages
those measurements cannot catch again tomorrow:

1. **The breakpoints themselves.** Spec §4.3 is three numbers. A rule that
   drifts to `max-width: 768px` overlaps the rail range by a pixel; one
   that drifts to `max-width: 1100px` puts the rail on a 1100px window the
   spec says is the desktop layout. Both look fine on any screen you happen
   to be sitting at.
2. **Width vs pointer.** Layout keys on width; touch affordances key on
   `(pointer: coarse)`. Getting it backwards gives desktop users swipe
   gestures they cannot perform and phone users hover states they cannot
   trigger, and neither shows up in a screenshot of the machine that wrote
   the CSS. So the two axes are asserted against each other here: no
   breakpoint may claim a touch-only rule, and no pointer query may carry a
   width.
3. **The compiled stylesheet.** The app serves `static/app.css`; a rule
   added to `styles/responsive.css` and never built changes nothing at all.
4. **The drawer's mechanism.** It must not be a `<dialog>` — `keys.js`
   stops dispatching entirely while `dialog[open]` matches, so a `<dialog>`
   drawer would silently kill every shortcut in the app — and it must not
   tag `keys.js` a second time (a `<script>`'s versioned URL and an
   `import`'s are two module records).
5. **The skip link.** `#main` has to be focusable or the link scrolls
   without moving focus, which is a skip link that skips nothing.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESPONSIVE_CSS = ROOT / "styles/responsive.css"
APP_CSS = ROOT / "mailosh/web/static/app.css"
NAV_JS = ROOT / "mailosh/web/static/js/nav.js"
APP_LAYOUT = ROOT / "mailosh/web/templates/layouts/app.html"
NAV_HTML = ROOT / "mailosh/web/templates/shell/nav.html"

#: Spec §4.3's three shapes, as the media queries that draw them. `.98`
#: rather than `px - 1`: a window can be dragged to a fractional width on a
#: HiDPI screen, and 767.5px must be the drawer, not a one-pixel limbo.
RAIL_QUERY = "@media (min-width: 768px) and (max-width: 1099.98px)"
DRAWER_QUERY = "@media (max-width: 767.98px)"
COARSE_QUERY = "@media (pointer: coarse)"


def _css() -> str:
    return RESPONSIVE_CSS.read_text()


def _without_comments(text: str) -> str:
    """`text` minus its `/* … */` blocks. This stylesheet explains itself at
    length, and a rule about what it *does* must not be satisfied by prose
    describing it."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _blocks(css: str, query: str) -> str:
    """The body of every top-level `@media` block matching `query`."""
    out = []
    for start in (m.end() for m in re.finditer(re.escape(query) + r"\s*\{", css)):
        depth, i = 1, start
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[start : i - 1])
    return "\n".join(out)


def _jinja_free(markup: str) -> str:
    return re.sub(r"\{#.*?#\}", "", markup, flags=re.S)


# ---------------------------------------------------------------------------
# The three shapes
# ---------------------------------------------------------------------------


def test_the_three_breakpoints_are_the_ones_the_spec_names():
    """1100 and 768, and nothing else. Every media query in this file is one
    of the three the spec describes; a fourth number would be a layout
    nobody designed and nobody looked at."""
    css = _without_comments(_css())
    queries = set(re.findall(r"@media[^{]+", css))
    assert queries == {
        RAIL_QUERY + " ",
        DRAWER_QUERY + " ",
        COARSE_QUERY + " ",
    }, queries


def test_the_rail_and_the_drawer_do_not_overlap_and_leave_no_gap():
    """768px exactly is the narrowest rail, 767.98px the widest drawer.
    A rail written `max-width: 1100px` would claim the 1100px window spec
    §4.3 gives to the full sidebar."""
    css = _css()
    assert RAIL_QUERY in css
    assert DRAWER_QUERY in css
    assert "max-width: 1100px" not in css
    assert "max-width: 768px" not in css


def test_the_rail_is_64px_and_the_drawer_leaves_the_grid():
    css = _without_comments(_css())
    rail = _blocks(css, RAIL_QUERY)
    drawer = _blocks(css, DRAWER_QUERY)
    # Spec §4.3: "collapses to a 64px icon rail".
    assert "grid-template-columns: 64px minmax(0, 1fr);" in rail
    # ...and under 768 the nav is not a column at all.
    assert "grid-template-columns: minmax(0, 1fr);" in drawer
    assert "position: fixed;" in drawer


def test_every_grid_track_is_floored_at_zero():
    """A bare `1fr` is `minmax(auto, 1fr)`, and that `auto` floor is the
    track's min-content — so one item that refuses to shrink drags the whole
    grid past the viewport. The top bar is exactly such an item: a nowrap
    flex row whose search `<input>` carries an intrinsic width, measured at
    519px of min-content against a 500px window. It pushed the list, its
    rows and the avatar off the right edge, and because the viewport clips
    rather than scrolls (body is `overflow: hidden`) nothing looked wrong —
    the date column was simply gone."""
    css = _without_comments(_css())
    tracks = re.findall(r"grid-template-columns:([^;]+);", css)
    assert tracks, "no grid tracks declared"
    for track in tracks:
        assert "minmax(0, 1fr)" in track, track


def test_the_rail_hides_names_without_taking_them_away():
    """The icons are `aria-hidden` (spec §4.2), so a nav item's label *is*
    its accessible name. `display: none` on it would leave a rail of
    unnamed links; clipping leaves the name and takes the pixels."""
    rail = _without_comments(_blocks(_css(), RAIL_QUERY))
    label_rule = re.search(r"\.nav-label,[^{]*\{([^}]*)\}", rail)
    assert label_rule is not None
    body = label_rule.group(1)
    assert "clip-path: inset(50%)" in body
    assert "display: none" not in body


def test_the_nav_indent_is_a_variable_rather_than_an_inline_style():
    """An inline `style="padding-left: …"` outranks every stylesheet, so the
    icon rail could not have un-indented a nested label and its colour dot
    would sit off-centre by 14px per level."""
    markup = _jinja_free(NAV_HTML.read_text())
    assert "--nav-depth:" in markup
    assert "padding-left" not in markup
    css = _without_comments(_css())
    assert "calc(var(--nav-depth, 0) * 14px)" in css
    assert "padding-left: 0;" in _blocks(css, RAIL_QUERY)


# ---------------------------------------------------------------------------
# Width is not touch
# ---------------------------------------------------------------------------

#: Affordances that exist because a finger cannot do what a pointer can.
#: Spec §4.3: "keyed on `(pointer: coarse)`, not width". `.compose-btn .kbd`
#: is deliberately absent: the icon rail hides it too, but for a reason
#: about *width* (there is no room for a chip on a 48px square button), and
#: a rule may legitimately have both kinds of reason.
TOUCH_ONLY = (".row:hover .row-actions", ".search-kbd", ".list-toolbar .kbd")


@pytest.mark.parametrize("selector", TOUCH_ONLY)
def test_touch_affordances_are_keyed_on_the_pointer_not_the_width(selector: str):
    """A narrow window on a desktop is not a phone. Hover actions must not
    be removed because a window is small — they work fine with a mouse at
    600px — and a tablet at 1024px must lose them even though it is wide."""
    css = _without_comments(_css())
    assert selector in _blocks(css, COARSE_QUERY), selector
    assert selector not in _blocks(css, DRAWER_QUERY), selector
    assert selector not in _blocks(css, RAIL_QUERY), selector


def test_the_pointer_query_carries_no_width_of_its_own():
    """`(pointer: coarse) and (max-width: …)` would be the same mistake
    written the other way round."""
    css = _css()
    for match in re.findall(r"@media[^{]*pointer[^{]*", css):
        assert "width" not in match, match


def test_the_row_date_comes_back_wherever_the_hover_actions_go_away():
    """`.row:hover .row-date { display: none }` is only correct while
    something replaces it. On a touch screen nothing does — there is no
    hover to reveal the actions with, and nothing inside a `display: none`
    subtree can be tapped to focus it — so hiding the date there would blink
    it out under a fingertip for nothing."""
    coarse = _without_comments(_blocks(_css(), COARSE_QUERY))
    assert ".row-actions" in coarse and "display: none" in coarse
    back = r"\.row:hover \.row-date,\s*\.row:focus-within \.row-date \{\s*display: inline"
    assert re.search(back, coarse)


def test_the_shortcut_chips_that_stay_are_the_ones_that_are_all_content():
    """`.kbd` wholesale would empty the `?` overlay and the palette's
    shortcut column, whose entire content is key chips. Only the three
    chrome placements go."""
    coarse = _without_comments(_blocks(_css(), COARSE_QUERY))
    assert re.search(r"^\s*\.kbd \{", coarse, re.M) is None


# ---------------------------------------------------------------------------
# 44px targets and the safe area
# ---------------------------------------------------------------------------


def test_touch_targets_reach_44px_under_768():
    drawer = _without_comments(_blocks(_css(), DRAWER_QUERY))
    wanted = (".btn-icon", ".check-box", ".nav-item", ".compose-btn", ".nav-menu", ".search-pill")
    for selector in wanted:
        assert selector in drawer, selector
    assert drawer.count("44px") >= 10


def test_the_row_checkbox_keeps_its_box_and_borrows_a_44px_hit_area():
    """A 44px checkbox reads as a button. The pseudo-element hit-tests as
    the button itself, and the 14px gap is what makes the checkbox's 44px
    area and the star's tile exactly rather than overlap — measured with
    `elementFromPoint` at ±21px around each centre."""
    drawer = _without_comments(_blocks(_css(), DRAWER_QUERY))
    assert re.search(r"\.row-check::after \{[^}]*width: 44px;[^}]*height: 44px;", drawer, re.S)
    assert re.search(r"\.row-controls \{\s*gap: 14px;\s*\}", drawer)


def test_every_safe_area_inset_has_a_fallback():
    """`env()` with no fallback makes the whole declaration invalid where the
    function is unknown, which drops the padding *and* whatever it was
    written beside."""
    for call in re.findall(r"env\([^)]*\)", _without_comments(_css())):
        assert "," in call, call
        assert call.endswith("0px)"), call


def test_the_page_declares_viewport_fit_so_the_insets_are_ever_non_zero():
    """Without `viewport-fit=cover` a notched phone letterboxes the page
    inside the safe area and every `env()` above resolves to 0 — the padding
    would be dead code rather than a fix."""
    markup = _jinja_free(APP_LAYOUT.read_text())
    assert 'name="viewport"' in markup
    assert "viewport-fit=cover" in markup


def test_the_bottom_action_bar_is_the_selections_and_pays_its_own_inset():
    """Spec §11's "bottom action bar with safe-area padding". Its own
    padding rather than the grid's, so the bar's surface runs under the home
    indicator instead of stopping above it."""
    drawer = _without_comments(_blocks(_css(), DRAWER_QUERY))
    bar = re.search(r"#main > \.list-toolbar \+ \.list-toolbar \{([^}]*)\}", drawer)
    assert bar is not None
    assert "order: 2;" in bar.group(1)
    assert "padding-bottom: env(safe-area-inset-bottom, 0px);" in bar.group(1)
    assert "#main > .list-body" in drawer


# ---------------------------------------------------------------------------
# The drawer
# ---------------------------------------------------------------------------


def test_the_drawer_is_not_a_dialog():
    """`static/js/keys.js` returns from `dispatch()` before it looks at
    anything while `dialog[open]` matches anywhere in the document — that is
    how the palette and the `?` overlay take the keyboard. A nav drawer
    built on `<dialog>` would silently kill every shortcut in the app for as
    long as it was open."""
    # Comments stripped: this module's header explains at length why
    # `showModal()` is the wrong answer here, and a test that matched its
    # own explanation would fail for saying so.
    source = _without_comments(re.sub(r"^\s*//.*$", "", NAV_JS.read_text(), flags=re.M))
    assert "showModal" not in source
    assert "<dialog" not in _jinja_free(APP_LAYOUT.read_text())
    keys = (ROOT / "mailosh/web/static/js/keys.js").read_text()
    assert 'document.querySelector("dialog[open]")' in keys


def test_the_drawer_does_not_change_the_navs_role_at_a_breakpoint():
    """One element cannot be a `navigation` landmark at 768px and a dialog
    at 767px. Modality is `inert` on everything else instead, which is what
    `aria-modal` only claims."""
    markup = _jinja_free(APP_LAYOUT.read_text())
    nav = re.search(r"<nav[^>]*>", markup)
    assert nav is not None
    assert 'aria-label="Primary"' in nav.group(0)
    assert "role=" not in nav.group(0)
    assert "aria-modal" not in markup
    source = NAV_JS.read_text()
    assert 'setAttribute("inert", "")' in source
    assert 'removeAttribute("inert")' in source


def test_the_drawer_marks_the_whole_frame_inert_but_not_the_scrim():
    """An inert scrim is a scrim you cannot click, which is why it is a
    sibling of the grid rather than inside it. `#toasts` stays live too, so
    an undo counting down is still there to be taken."""
    source = NAV_JS.read_text()
    behind = re.search(r"const BEHIND = \[([^\]]*)\]", source)
    assert behind is not None
    assert set(re.findall(r'"([^"]+)"', behind.group(1))) == {
        ".app-header",
        ".app-banner",
        "#main",
        "#compose-dock",
    }
    assert "nav-scrim" not in behind.group(1)
    assert "#toasts" not in behind.group(1)


def test_escape_closes_the_drawer_and_gives_focus_back():
    source = NAV_JS.read_text()
    assert 'event.key === "Escape"' in source
    # Inert comes off before focus is restored: focus cannot be moved into
    # an inert subtree, and the toggle lives in the header.
    close = source[source.index("function close(") :]
    close = close[: close.index("\n}")]
    assert close.index("removeAttribute") < close.index("back?.focus()")
    assert "opener" in close


def test_the_drawer_swallows_keystrokes_rather_than_letting_them_reach_the_list():
    """`j` and `k` would walk the list cursor behind a panel covering it,
    `e` would archive a conversation the reader cannot see. A capture-phase
    listener on `document` is what beats `keys.js`'s bubble-phase listener
    on `window`."""
    source = NAV_JS.read_text()
    assert re.search(r'document\.addEventListener\(\s*"keydown",.*?true,\s*\)', source, re.S)
    assert "event.stopPropagation();" in source
    # ...but never over an open `<dialog>`: `Esc` closing one is a *default
    # action* on that very keydown, and `preventDefault()` would cancel it.
    assert 'document.querySelector("dialog[open]") !== null) return;' in source


def test_the_toggle_is_the_layouts_and_the_dead_placeholder_is_hidden():
    """The phone frame has exactly one hamburger: `nav.js`'s drawer toggle.
    The old `aria-disabled` placeholder for spec §5.1's desktop rail toggle
    is gone until that feature exists."""
    markup = _jinja_free(APP_LAYOUT.read_text())
    assert 'data-role="nav-toggle"' in markup
    assert 'aria-expanded="false"' in markup
    assert 'aria-controls="app-nav"' in markup
    drawer = _without_comments(_blocks(_css(), DRAWER_QUERY))
    # The desktop rail-toggle placeholder is gone from the markup, so there is
    # nothing for the drawer block to hide any more.
    assert "button[aria-disabled=true]" not in drawer
    assert ".nav-drawer-toggle" in drawer
    # ...and it holds no space at all above 768px.
    top = _without_comments(re.split(r"@media", _css())[0])
    assert re.search(r"\.nav-drawer-toggle,\s*\.nav-drawer-head \{\s*display: none;", top)


def test_nav_js_is_tagged_once_and_never_tags_the_key_registry():
    """A `<script>`'s versioned `?v=` URL and an `import`'s unversioned one
    are two module records for the same file, so a module that is both
    tagged and imported is fetched and evaluated twice."""
    markup = _jinja_free(APP_LAYOUT.read_text())
    tagged = re.findall(r"<script src=\"\{\{ static\('([^']+)'\) \}\}\"", markup)
    assert tagged.count("js/nav.js") == 1
    assert "js/keys.js" not in tagged
    assert tagged.index("js/nav.js") < tagged.index("vendor/alpine.min.js")
    assert 'from "./keys.js"' not in NAV_JS.read_text()


def test_the_breakpoint_in_javascript_matches_the_one_in_css():
    """One breakpoint written twice. If they drift, the drawer opens at a
    width where the CSS still draws a sidebar — or refuses to open at one
    where it does not."""
    assert 'matchMedia("(max-width: 767.98px)")' in NAV_JS.read_text()
    assert DRAWER_QUERY in _css()


# ---------------------------------------------------------------------------
# Single pane, and the skip link
# ---------------------------------------------------------------------------


def test_the_conversation_slides_over_without_a_second_history_mechanism():
    """`#main` is never replaced — only morphed — so there is no new element
    to animate; the animation starts because the selector begins to match.
    Nothing here records or restores history: `mailosh/web/mail.py` pushes
    the URL and htmx owns the rest."""
    css = _without_comments(_css())
    assert "#main:has(> .thread-scroll)" in _blocks(css, DRAWER_QUERY)
    assert "@keyframes screen-over" in css
    source = NAV_JS.read_text()
    for banned in ("pushState", "replaceState", "popstate", "scrollTop"):
        assert banned not in source, banned


def test_the_skip_link_is_first_and_actually_moves_focus():
    """Fragment navigation focuses its target only when the target is
    focusable, and a bare `<main>` is not: without `tabindex="-1"` the
    browser scrolls (a no-op in a frame that does not scroll) and leaves
    focus on the link, so the next Tab lands in the top bar — a skip link
    that skips nothing."""
    markup = _jinja_free(APP_LAYOUT.read_text())
    body = markup[markup.index("<body") :]
    first_tag = re.search(r"<(\w[\w-]*)[^>]*>", body[body.index(">") + 1 :])
    assert first_tag is not None and first_tag.group(1) == "a", first_tag.group(0)
    link = re.search(r'<a href="#main"[^>]*>', markup)
    assert link is not None
    # Parked with a transform and returned on focus: never `display: none`
    # (which would take it out of the accessibility tree) and never
    # `position: static` on focus (which would shove the 100dvh frame down).
    assert "absolute" in link.group(0)
    assert "-translate-y-[200%]" in link.group(0)
    assert "focus:translate-y-0" in link.group(0)
    main = re.search(r"<main\b[^>]*>", markup, re.S)
    assert main is not None
    assert 'tabindex="-1"' in main.group(0)


# ---------------------------------------------------------------------------
# ...and it has to have been compiled
# ---------------------------------------------------------------------------

#: One selector per shape, so a build that dropped the partial fails rather
#: than passing on a stylesheet nobody rebuilt.
BUILT = (".nav-drawer-toggle", ".nav-scrim", "[data-drawer=open]", "screen-over", "--nav-depth")


@pytest.mark.skipif(not APP_CSS.exists(), reason="`make css` has not run in this tree")
@pytest.mark.parametrize("needle", BUILT)
def test_the_responsive_rules_survived_the_build(needle: str):
    """The app serves the compiled `static/app.css`; a rule added to
    `styles/responsive.css` and never built changes nothing in a browser."""
    assert needle in APP_CSS.read_text()


@pytest.mark.skipif(not APP_CSS.exists(), reason="`make css` has not run in this tree")
def test_the_built_stylesheet_kept_both_breakpoints_apart():
    built = APP_CSS.read_text()
    assert "max-width:1099.98px" in built
    assert "max-width:767.98px" in built
    assert "pointer:coarse" in built
