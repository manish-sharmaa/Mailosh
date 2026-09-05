"""Accessibility regressions the design spec §11 asks us to hold (WCAG 2.2 AA).

Most of an accessibility pass is verified in a browser, and the 1E findings
record those runs. What lives here is the half that a browser check cannot
protect: the *arithmetic*. Contrast is a property of two colour tokens and a
compositing rule, so it can be recomputed from the stylesheet on every run,
and a future retune of the palette will fail this module the moment it drops
a pair below its threshold instead of shipping and being found by hand a
release later.

The numbers are computed from `styles/input.css` itself rather than
hard-coded here, so there is exactly one definition of every colour (the
same rule the stylesheet's own header states). The thresholds are spec §11's:
4.5:1 for text, 3:1 for UI and focus.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STYLES = ROOT / "styles"
TEMPLATES = ROOT / "mailosh" / "web" / "templates"

INPUT_CSS = (STYLES / "input.css").read_text(encoding="utf-8")
A11Y_CSS = (STYLES / "a11y.css").read_text(encoding="utf-8")

TEXT_AA = 4.5
UI_AA = 3.0

#: The twelve label colours, in `mailosh.ui.format.avatar_color`'s order.
LABELS = (
    "indigo",
    "emerald",
    "rose",
    "amber",
    "sky",
    "violet",
    "teal",
    "orange",
    "pink",
    "lime",
    "slate",
    "red",
)

#: Chip wash alpha per theme — spec §4.1's "colour at 14 % alpha", raised to
#: 18 % in dark by the same rule in `styles/input.css`.
CHIP_ALPHA = {"light": 0.14, "dark": 0.18}


# ---------------------------------------------------------------------
# Colour maths (WCAG 2.x relative luminance and contrast ratio)
# ---------------------------------------------------------------------

Rgb = tuple[float, float, float]


def _hex_to_rgb(value: str) -> Rgb:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _parse_color(value: str) -> tuple[Rgb, float]:
    """Return `(rgb, alpha)` for the two notations the token file uses."""
    value = value.strip()
    if value.startswith("#"):
        return _hex_to_rgb(value), 1.0
    match = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    if match is None:  # pragma: no cover - guards a token typo, not a branch
        raise ValueError(f"unsupported colour notation: {value!r}")
    parts = [p.strip() for p in re.split(r"[,\s/]+", match.group(1)) if p.strip()]
    nums = [float(p) for p in parts]
    alpha = nums[3] if len(nums) > 3 else 1.0
    return (nums[0], nums[1], nums[2]), alpha


def _over(fg: Rgb, alpha: float, bg: Rgb) -> Rgb:
    """Composite `fg` at `alpha` over an opaque `bg` (simple source-over)."""
    return tuple(f * alpha + b * (1 - alpha) for f, b in zip(fg, bg, strict=True))  # type: ignore[return-value]


def _luminance(rgb: Rgb) -> float:
    def channel(raw: float) -> float:
        c = raw / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: Rgb, b: Rgb) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------


def _block(selector: str) -> str:
    """The body of the first top-level rule whose selector matches."""
    start = INPUT_CSS.index(selector)
    open_brace = INPUT_CSS.index("{", start)
    depth = 0
    for index in range(open_brace, len(INPUT_CSS)):
        if INPUT_CSS[index] == "{":
            depth += 1
        elif INPUT_CSS[index] == "}":
            depth -= 1
            if depth == 0:
                return INPUT_CSS[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces after {selector!r}")  # pragma: no cover


def _tokens(body: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


LIGHT = _tokens(_block("\n:root {"))
DARK = _tokens(_block(":root[data-theme=dark] {"))
DARK_MEDIA = _tokens(_block("@media (prefers-color-scheme: dark) {"))

THEMES = {"light": LIGHT, "dark": DARK}

#: The four row grounds a chip or a focused row can be painted on: an unread
#: row, a read row, a hovered row and a selected row.
ROW_GROUNDS = ("--surface", "--read", "--hover", "--accent-soft")


def _solid(tokens: dict[str, str], name: str, under: str = "--surface") -> Rgb:
    """A token as an opaque colour, compositing it over `under` if it is not."""
    rgb, alpha = _parse_color(tokens[name])
    if alpha >= 1.0:
        return rgb
    return _over(rgb, alpha, _solid(tokens, under))


# ---------------------------------------------------------------------
# The two dark blocks must stay identical
# ---------------------------------------------------------------------


def test_the_two_dark_token_blocks_agree() -> None:
    """`input.css`'s header makes this promise; a11y tokens rely on it.

    Dark is declared twice — once for `data-theme=dark` and once for "the OS
    prefers dark and the reader has not forced light" — because they are two
    genuinely different activation conditions. Every contrast figure below is
    computed from the first block only, so a token that lands in one and not
    the other would be a theme that silently fails AA for exactly the readers
    who never touched the setting.
    """
    assert DARK == DARK_MEDIA


# ---------------------------------------------------------------------
# Label chips (spec §4.1, §11) — the recorded AA gap this pass closed
# ---------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("ground", ROW_GROUNDS)
def test_label_chip_text_clears_aa_on_every_row_ground(theme: str, label: str, ground: str) -> None:
    """A chip's wash is transparent, so its background is the row's.

    That is the whole reason this needs all four grounds: the same chip is
    lighter on a hovered row than on a read one, and the ratio that matters
    is the worst of them. Deriving the ink from the label colour with a
    `color-mix` (what shipped before this pass) failed nine of twelve labels
    in light and six in dark.
    """
    tokens = THEMES[theme]
    wash, _ = _parse_color(tokens[f"--label-{label}"])
    ink = _solid(tokens, f"--label-{label}-ink")
    background = _over(wash, CHIP_ALPHA[theme], _solid(tokens, ground))
    ratio = contrast(ink, background)
    assert ratio >= TEXT_AA, f"{theme} {label} chip on {ground}: {ratio:.2f}:1 (needs {TEXT_AA})"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_label_colour_has_an_ink(theme: str) -> None:
    tokens = THEMES[theme]
    missing = [n for n in LABELS if f"--label-{n}-ink" not in tokens]
    assert not missing, f"{theme} has no ink for: {', '.join(missing)}"


def test_chip_text_comes_from_the_ink_token_not_a_mix() -> None:
    """The `.chip` rule must read `--chip-ink`, and both templates must set it.

    A chip whose ink is derived at paint time cannot be tuned for contrast
    without changing the label's identity colour, which is what the failing
    `color-mix` did.
    """
    # Comments are stripped first: the rule this replaced is quoted verbatim
    # in the token block's explanation, and a test that reads prose as code
    # would fail on its own documentation.
    css = re.sub(r"/\*.*?\*/", "", INPUT_CSS, flags=re.S)
    assert "color: var(--chip-ink" in css
    assert "color-mix(in srgb, var(--chip) 58%" not in css
    for name in ("list/row.html", "thread/header.html"):
        markup = (TEMPLATES / name).read_text(encoding="utf-8")
        for chip in re.findall(r'<span class="chip"[^>]*>', markup):
            assert "--chip-ink:" in chip, f"{name}: chip without an ink: {chip}"


# ---------------------------------------------------------------------
# Focus ring (spec §4.2 geometry, §11's 3:1)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    "ground", ["--bg", "--surface", "--read", "--hover", "--accent-soft", "--field"]
)
def test_focus_ring_clears_three_to_one_on_every_ground(theme: str, ground: str) -> None:
    """Why `--focus` is not simply `--accent`.

    In light the two are the same colour and this passes either way. In dark
    the brand accent is deliberately dimmed (#1E6FD6), and dimmed measures
    2.83 on a hovered row and 2.98 on a selected one — both under 3:1.
    """
    tokens = THEMES[theme]
    ring = _solid(tokens, "--focus")
    ratio = contrast(ring, _solid(tokens, ground))
    assert ratio >= UI_AA, f"{theme} focus ring on {ground}: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("row", ["--surface", "--read", "--hover", "--accent-soft"])
def test_row_focus_ring_clears_three_to_one_on_the_focused_fill(theme: str, row: str) -> None:
    """`.row:focus-visible` pulls its outline inside the row.

    It has to: the row is the full width of `.list`, whose `overflow-y: auto`
    clipped the sides of an outside ring. Inside means the ring sits on the
    focused row's own fill — the row colour plus `--accent-wash` — not on the
    list ground, so that is what it owes 3:1 against.
    """
    tokens = THEMES[theme]
    ring = _solid(tokens, "--focus")
    wash, alpha = _parse_color(tokens["--accent-wash"])
    fill = _over(wash, alpha, _solid(tokens, row))
    ratio = contrast(ring, fill)
    assert ratio >= UI_AA, f"{theme} row ring on focused {row}: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_toast_re_points_the_ring_because_it_is_inverted(theme: str) -> None:
    """The toast is the one surface where `--focus` is the wrong colour.

    Dark theme's toast is a *light* slab, and the lifted ring that clears the
    Carbon greys measures 2.99 on it. `.toast { --focus: var(--accent) }` in
    `input.css` swaps it back; this checks the colour it swaps to actually
    pays, in both themes.
    """
    tokens = THEMES[theme]
    ratio = contrast(_solid(tokens, "--accent"), _solid(tokens, "--toast"))
    assert ratio >= UI_AA, f"{theme} toast ring: {ratio:.2f}:1"
    assert "--focus: var(--accent);" in INPUT_CSS


def test_no_focus_outline_is_painted_in_the_brand_accent() -> None:
    """One token governs every ring, so retuning it cannot miss a component."""
    stray = []
    for name in ("input.css", "labels.css", "search.css", "a11y.css"):
        text = (STYLES / name).read_text(encoding="utf-8")
        for line in re.findall(r"outline:[^;]*;", text):
            if "var(--accent)" in line and "dashed" not in line:
                stray.append(f"{name}: {line}")
    assert not stray, "focus rings must use --focus:\n" + "\n".join(stray)


# ---------------------------------------------------------------------
# Motion, forced colors, grid semantics
# ---------------------------------------------------------------------


def test_reduced_motion_zeroes_delays_as_well_as_durations() -> None:
    """Spec §4.2 says the preference "sets all durations to 0".

    A zeroed transition that still waits out a `transition-delay` is motion
    the reader asked not to have — `.list-skeleton`'s 300 ms was exactly that.
    """
    block = _block("@media (prefers-reduced-motion: reduce) {")
    for prop in (
        "transition-duration",
        "transition-delay",
        "animation-duration",
        "animation-delay",
    ):
        assert f"{prop}: 0s !important;" in block, f"reduced motion misses {prop}"


def test_forced_colors_support_exists_for_every_fill_only_affordance() -> None:
    """Windows High Contrast drops `box-shadow` and overrides every colour.

    Each selector below names a state this app draws with a background wash
    or a `box-shadow` bar and nothing else, which is to say a state that
    ceases to exist in forced-colors mode unless it is redrawn.
    """
    assert "@media (forced-colors: active)" in A11Y_CSS
    for selector in (
        ".row.is-focused",
        ".row.is-selected",
        '.row[aria-current="true"]',
        ".btn-icon:hover",
        ".compose-btn",
        ".row-star.is-on svg",
        '.nav-item[aria-current="page"]',
        ".chip",
        ".toast",
        ".palette-option.is-active",
        ".switch",
        ".msg.is-unread",
    ):
        assert selector in A11Y_CSS, f"forced-colors has no rule for {selector}"


@pytest.mark.parametrize("name", ["list/rows.html", "search/rows.html"])
def test_grid_children_are_rows(name: str) -> None:
    """`role="grid"` owns rows, not loose `<div>`s (spec §5.3).

    `#list` is the grid and this fragment *is* its children, so the empty
    state and the endless-scroll sentinel both have to present as rows or the
    grid stops describing its own shape partway down.
    """
    markup = (TEMPLATES / name).read_text(encoding="utf-8")
    sentinel = re.search(r'<div class="row-sentinel"(.*?)>', markup, re.S)
    assert sentinel is not None
    assert 'role="row"' in sentinel.group(1)
    assert 'class="sentinel-cell" role="gridcell"' in markup
    empty = re.search(r'<div role="row">\s*<div role="gridcell">\{% include', markup)
    assert empty is not None, "the empty state is not wrapped in a row/gridcell"


def test_the_row_is_the_grids_only_tab_stop() -> None:
    """The roving tabindex (spec §5.3): controls inside a row are reached by
    the arrow keys, so every one of them stays out of the Tab sequence."""
    markup = (TEMPLATES / "list" / "row.html").read_text(encoding="utf-8")
    controls = re.findall(r"<button[^>]*>", markup, re.S)
    assert controls, "row.html renders no controls"
    for control in controls:
        assert 'tabindex="-1"' in control, f"row control in the tab order: {control}"


def test_every_row_control_has_an_accessible_name() -> None:
    """Spec §11: "every icon button labelled". These carry only an icon."""
    markup = (TEMPLATES / "list" / "row.html").read_text(encoding="utf-8")
    for block in re.findall(r"<button[^>]*>(?:(?!</button>).)*</button>", markup, re.S):
        named = "aria-label=" in block or "label=" in block
        assert named, f"unnamed icon button: {block[:120]}"
