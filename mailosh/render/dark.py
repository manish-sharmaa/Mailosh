"""The dark restyle decision (design spec §7) — whether, and how, a
message's own colours get overridden when the reader is in dark mode.

Three pure functions, deliberately free of database and request objects so
`tests/unit/test_dark_restyle.py` can call them directly with plain strings
and booleans, no `db`/`Request` fixture required:

    declares_color_scheme(html, css) -> bool
    background_is_light(html, css)   -> bool
    restyle_mode(*, theme, enabled, declares, light) -> str

`restyle_mode` is the actual decision table; the other two just answer the
factual questions it needs answered (`declares`, `light`) before it is
called. The persistence half — the per-sender "Show original" override that
outranks all three of these — is `mailosh.db.models.SenderPref` /
`mailosh.db.repo.sender_pref`/`set_sender_restyle`, not here: this module
never opens a session, so it cannot remember anything on its own.

Raw HTML/CSS, not sanitised
---------------------------
`declares_color_scheme` must run on the message's **raw** source, before
`mailosh.render.html_sanitize`/`css_sanitize` touch it: `<meta>` is one of
nh3's `CLEAN_CONTENT_TAGS` (dropped whole, contents included) and
`color-scheme` is not in `css_sanitize.ALLOWED_PROPERTIES`, so by the time
either sanitiser has run, both forms of evidence this function looks for
are already gone. The frame route (not this module) is what has to keep the
raw body around long enough to ask.

`background_is_light`, by contrast, is meant to be handed the *sanitised*
CSS in production: `background-color` **does** survive `css_sanitize`
(it's on the allow-list), so asking the sanitised value is both safe — no
un-scrubbed sender bytes are being interpreted here as a fetch or an
expression, just a colour — and exactly what the reader will actually see.
Nothing below enforces which one arrives, though; both functions just parse
whatever string they are given, which is what keeps them pure and testable
in isolation.

Colour parsing is deliberately narrow
--------------------------------------
`#rgb`, `#rrggbb`, and the sixteen HTML 4.01 colour keywords (`black`
through `aqua`, `_HTML_COLOR_NAMES` below) — not `rgb()`/`hsl()` functions,
not the full CSS3 colour-name table. Real mail overwhelmingly uses hex or
one of these sixteen names for a `bgcolor` attribute or a plain
`background-color` declaration; a value this cannot parse is treated as "no
usable signal from this source" and the caller falls through to the next
one, rather than this module guessing.

Luminance, and why "no background" reads as light
---------------------------------------------------
`_relative_luminance` is the standard WCAG definition (sRGB channels
linearised, then weighted 0.2126/0.7152/0.0722), not the cheaper gamma-naive
"luma" shortcut some codebases substitute for it — the two agree at the
extremes real mail actually uses (pure white, near-black) but "relative
luminance" is a specific, defined term and this earns the name honestly.
`background_is_light` returns `True` (light) at exactly the boundary
`0.5`... no — strictly *above* it; `0.5` itself reads as dark, matching
"returns True above 0.5" verbatim.

A mail with **no** background signal anywhere — no declared colour scheme,
no `bgcolor`, no `background-color` on `body`/`table` — returns `True`.
This is not "assume light" as a coin flip: mail defaults to white paper the
same way a blank page does, and the alternative (treating "unknown" as
dark) would leave a plain-text-with-no-styling message's ordinary black
text sitting on the frame's own dark chrome background with no inversion
ever applied to fix it, since `restyle_mode` only inverts when
`background_is_light` says light.

The two CSS fragments a caller actually inlines
-------------------------------------------------
`restyle_mode` returns one of three strings; the frame document (built
elsewhere — this module never touches Jinja or a request) turns
`"color-scheme"`/`"invert"` into the CSS spec §7 asks for. Both fragments
are pinned here, verbatim, as the one place their exact text lives:

    COLOR_SCHEME_CSS  ->  html{color-scheme:light dark}
    INVERT_CSS        ->  html{filter:...} + img,[style*="background-image"]{filter:...}

`INVERT_CSS` carries *two* rules, not one — the interesting failure mode
this task calls out by name: inverting `html` alone turns every photograph
and every already-dark inline image into its own negative, since the
`filter` on an ancestor applies to raster image content too. The second
rule counter-inverts exactly the elements affected (`img`, and anything
whose inline `style` sets a `background-image`), cancelling the ancestor
filter back to the original colours for pixels that were never text.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import tinycss2
from tinycss2 import ast

from mailosh.render.css_sanitize import serialize_bounded

#: spec §7 verbatim -- the CSS a frame emits when it declares its own
#: colour-scheme support instead of second-guessing the mail's colours.
COLOR_SCHEME_CSS: str = "html{color-scheme:light dark}"

#: spec §7 verbatim -- the CSS a frame emits when it inverts a light mail
#: for a dark reader. Two rules: `html` does the actual inversion, and the
#: second rule counter-inverts raster image content (`img`, plus anything
#: painting a `background-image` from an inline `style=""`) so a photograph
#: comes back out the other side unchanged instead of as a negative.
INVERT_CSS: str = (
    "html{filter:invert(1) hue-rotate(180deg)}"
    'img,[style*="background-image"]{filter:invert(1) hue-rotate(180deg)}'
)

#: The sixteen HTML 4.01 / CSS Level 1 colour keywords -- the complete
#: named-colour vocabulary this module understands, deliberately not the
#: much larger CSS3 list. See the module docstring's "Colour parsing is
#: deliberately narrow" section for why.
_HTML_COLOR_NAMES: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "silver": (192, 192, 192),
    "gray": (128, 128, 128),
    "white": (255, 255, 255),
    "maroon": (128, 0, 0),
    "red": (255, 0, 0),
    "purple": (128, 0, 128),
    "fuchsia": (255, 0, 255),
    "green": (0, 128, 0),
    "lime": (0, 255, 0),
    "olive": (128, 128, 0),
    "yellow": (255, 255, 0),
    "navy": (0, 0, 128),
    "blue": (0, 0, 255),
    "teal": (0, 128, 128),
    "aqua": (0, 255, 255),
}

_HEX6 = re.compile(r"^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$")
_HEX3 = re.compile(r"^#([0-9a-f])([0-9a-f])([0-9a-f])$")

#: How many levels of at-rule (`@media` inside `@media`, ...) the CSS scan
#: below will descend into looking for a `color-scheme`/`background-color`
#: declaration. This module is never handed attacker input it re-emits (it
#: only ever answers `True`/`False`/a three-way string), so this is a
#: sanity bound against pathological nesting, not a security control the
#: way `css_sanitize`'s own depth guards are.
_MAX_RULE_DEPTH = 4


def restyle_mode(*, theme: str, enabled: bool, declares: bool, light: bool) -> str:
    """`"none"` / `"color-scheme"` / `"invert"` -- the entire decision.

    - `"none"` when the effective theme is light (nothing to restyle
      against), or when `enabled` is false (the per-user "Appearance" dark
      restyle setting, or a per-sender "Show original" override -- the
      caller has already folded both into this one flag before calling).
    - `"color-scheme"` when the mail declares its own colour scheme: trust
      it, touch nothing but the frame's own `color-scheme` CSS.
    - `"invert"` when it does not declare one and its background reads
      light: apply the inversion filter.
    - `"none"` otherwise -- a mail that does not declare a colour scheme
      but whose background already reads dark is left alone; inverting an
      already-dark mail would turn it light, exactly backwards.

    `theme` is treated as "light" only for the literal string `"light"`;
    every other value (`"dark"`, and `"system"` per the Task 12 brief's own
    note that system resolves to "the same mode" as dark, with the
    OS-conditional wrapping happening in the caller's CSS, not here) takes
    the `enabled`/`declares`/`light` path below. That keeps this function's
    contract exactly "light theme never restyles", independent of whatever
    set of theme names a caller happens to use.
    """
    if theme == "light" or not enabled:
        return "none"
    if declares:
        return "color-scheme"
    if light:
        return "invert"
    return "none"


def declares_color_scheme(html: str, css: str) -> bool:
    """True when the message itself declares a colour scheme -- a
    `<meta name="color-scheme">` tag anywhere in `html`, or a
    `color-scheme` CSS declaration anywhere in `css` (any selector: real
    mail overwhelmingly puts this on `:root` or `html`, but this checks
    every rule rather than assuming one).

    Presence only -- the declared *value* (`"dark"`, `"light dark"`,
    whatever) never matters here. Spec §7 is unconditional: any
    declaration at all means "trust it, do nothing else", precisely
    because the value is the mail author's call to make, not this
    function's to second-guess.
    """
    return (
        _scan_html(html).has_color_scheme_meta
        or _first_declaration(css, "color-scheme")[1] is not None
    )


def background_is_light(html: str, css: str) -> bool:
    """True when this message's background reads light.

    Reads, in order, stopping at the first usable answer:

    1. A colour-scheme hint (`<meta name="color-scheme">`'s `content`, or
       a CSS `color-scheme` declaration's value) that names `light` or
       `dark` but not both -- `"light dark"`/`"dark light"` (supports
       either, no default asserted) and an empty/missing value give no
       hint here and fall through, same as no declaration at all.
    2. `<body bgcolor="...">`, else the first `<table bgcolor="...">`.
    3. A `background-color` declaration on a `body` selector in `css`,
       else the first one on a `table` selector.

    A value that does not parse (an unparseable colour, an unknown name)
    is treated as "no signal from this source", not as an answer -- the
    scan continues to the next source rather than this function guessing.

    Returns `True` -- "light" -- when nothing above yields an answer at
    all: mail defaults to white paper, and treating "unknown" as dark
    would leave ordinary text on the frame's own dark background with no
    inversion ever applied to fix it (`restyle_mode` only inverts a
    background that reads light).
    """
    hint = _color_scheme_lightness_hint(html, css)
    if hint is not None:
        return hint

    scan = _scan_html(html)
    for bgcolor in (scan.body_bgcolor, scan.table_bgcolor):
        if bgcolor is None:
            continue
        rgb = _parse_color(bgcolor)
        if rgb is not None:
            return _relative_luminance(rgb) > 0.5

    for tag in ("body", "table"):
        value = _css_background_color(css, tag)
        if value is None:
            continue
        rgb = _parse_color(value)
        if rgb is not None:
            return _relative_luminance(rgb) > 0.5

    return True


def _color_scheme_lightness_hint(html: str, css: str) -> bool | None:
    """`True`/`False` when a declared colour-scheme value unambiguously
    names one side, `None` when there is none or it names both/neither.
    """
    scan = _scan_html(html)
    value = scan.meta_content if scan.has_color_scheme_meta else None
    if value is None:
        _, value = _first_declaration(css, "color-scheme")
    if not value:
        return None
    tokens = value.strip().lower().split()
    has_light, has_dark = "light" in tokens, "dark" in tokens
    if has_light and not has_dark:
        return True
    if has_dark and not has_light:
        return False
    return None


def _parse_color(value: str) -> tuple[int, int, int] | None:
    """A `#rgb`/`#rrggbb` hex colour or one of the sixteen HTML colour
    names, or `None` for anything else (a CSS function, an unrecognised
    name, garbage).
    """
    token = value.strip().lower()
    if match := _HEX6.match(token):
        r, g, b = (int(part, 16) for part in match.groups())
        return (r, g, b)
    if match := _HEX3.match(token):
        r, g, b = (int(part * 2, 16) for part in match.groups())
        return (r, g, b)
    return _HTML_COLOR_NAMES.get(token)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance: each sRGB channel linearised, then combined
    0.2126/0.7152/0.0722 (Rec. 709) -- not the cheaper gamma-naive "luma"
    weighting of the raw 0-255 values, which is a different, if similarly
    named, quantity.
    """

    def channel(value: int) -> float:
        s = value / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


class _MailScan(HTMLParser):
    """One lenient pass over raw mail HTML, pulling exactly the signals
    this module needs: whether a `<meta name="color-scheme">` tag exists
    anywhere (and its `content`, first tag wins), the first `<body>`'s
    `bgcolor`, and the first `<table>`'s `bgcolor`.

    Built on `html.parser.HTMLParser`, the same choice
    `mailosh.render.html_sanitize`/`quote_trim` make for "a stranger's
    markup, read leniently, no full DOM needed" -- not a security
    boundary, just attribute extraction; the actual sanitisation is
    `html_sanitize`'s job, done separately, on a separate (later) pass.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_color_scheme_meta = False
        self.meta_content: str | None = None
        self.body_bgcolor: str | None = None
        self.table_bgcolor: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta" and values.get("name", "").strip().lower() == "color-scheme":
            self.has_color_scheme_meta = True
            if self.meta_content is None:
                self.meta_content = values.get("content", "")
        elif tag == "body" and self.body_bgcolor is None and "bgcolor" in values:
            self.body_bgcolor = values["bgcolor"]
        elif tag == "table" and self.table_bgcolor is None and "bgcolor" in values:
            self.table_bgcolor = values["bgcolor"]


def _scan_html(html: str) -> _MailScan:
    scan = _MailScan()
    scan.feed(html)
    scan.close()
    return scan


def _iter_rule_declarations(css: str) -> list[tuple[str, ast.Declaration]]:
    """`(selector, declaration)` for every declaration in every qualified
    rule in `css`, at-rules (`@media`, ...) included up to `_MAX_RULE_DEPTH`
    levels deep -- the same "`@media` can nest, recurse via
    `parse_rule_list`" idiom `mailosh.render.css_sanitize._sanitize_rules`
    uses, just collecting instead of rebuilding.

    `selector` is the rule's prelude, serialised and lower-cased, exactly
    as written (a comma-separated list stays one string -- callers that
    care about one side of a list split it themselves).
    """
    return _walk(tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True), depth=1)


def _walk(rules: list[ast.Node], depth: int) -> list[tuple[str, ast.Declaration]]:
    if depth > _MAX_RULE_DEPTH:
        return []
    found: list[tuple[str, ast.Declaration]] = []
    for rule in rules:
        if isinstance(rule, ast.QualifiedRule):
            # serialize_bounded, never tinycss2.serialize: this walk reads the
            # RAW <style> blocks (frames.py needs the unsanitised text to see a
            # colour-scheme declaration the sanitiser would have dropped), so
            # the depth guard css_sanitize applies before its own serialise is
            # the only thing standing between a nested prelude and a
            # RecursionError on the reading route. A rule too deep to read is
            # skipped rather than crashing the message.
            prelude = serialize_bounded(rule.prelude)
            if prelude is None:
                continue
            selector = prelude.strip().lower()
            for decl in tinycss2.parse_blocks_contents(
                rule.content, skip_comments=True, skip_whitespace=True
            ):
                if isinstance(decl, ast.Declaration):
                    found.append((selector, decl))
        elif isinstance(rule, ast.AtRule) and rule.content is not None:
            found.extend(
                _walk(
                    tinycss2.parse_rule_list(
                        rule.content, skip_comments=True, skip_whitespace=True
                    ),
                    depth=depth + 1,
                )
            )
    return found


def _first_declaration(css: str, property_name: str) -> tuple[str | None, str | None]:
    """`(selector, serialised value)` of the first declaration named
    `property_name` anywhere in `css`, or `(None, None)` if there is none.
    """
    for selector, decl in _iter_rule_declarations(css):
        if decl.lower_name == property_name:
            value = serialize_bounded(decl.value)
            if value is None:
                continue
            return selector, value.strip()
    return None, None


def _css_background_color(css: str, tag: str) -> str | None:
    """The first `background-color` value declared on a bare `tag`
    selector (`"body"` or `"table"`, alone or alongside others in a
    comma-separated list), or `None`.
    """
    for selector, decl in _iter_rule_declarations(css):
        if decl.lower_name != "background-color":
            continue
        if tag in {part.strip() for part in selector.split(",")}:
            value = serialize_bounded(decl.value)
            if value is None:
                continue
            return value.strip()
    return None
