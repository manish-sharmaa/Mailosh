"""Template-rendering helpers registered as Jinja globals by
`mailosh.ui.env.build_env`: `icon()` (inlined, restyled Lucide SVGs) and
`kbd()` (a run of `<kbd>` elements for a shortcut hint, e.g. `"g i"` for a
two-keystroke chord).
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable
from pathlib import Path

from markupsafe import Markup, escape

logger = logging.getLogger(__name__)

#: Matches one `name="value"` pair from a Lucide static SVG's opening
#: `<svg ...>` tag. Safe as a non-general-purpose, single-tag matcher only
#: because Lucide's own published output (verified directly against
#: `lucide-static`'s `.svg` files) is a controlled format: every attribute
#: double-quoted, no attribute value ever contains `"` or `>` — this is not
#: an HTML parser and must never be pointed at arbitrary/untrusted markup.
_ATTR_RE = re.compile(r'([A-Za-z0-9:-]+)="([^"]*)"')

#: Root-tag attributes `_style_svg` always replaces itself, so whatever
#: `_ATTR_RE` lifted from the source file under these particular names is
#: discarded rather than kept: `class`/`width`/`height` are Lucide's own
#: (sizing instead comes from the caller's `class_`, via Tailwind — an
#: inline `width`/`height` attribute would otherwise still set the SVG's
#: intrinsic box); `stroke-width` is normalized to the design system's
#: 1.75 (spec §4.2), overriding Lucide's own default of 2.
_OVERRIDDEN_ROOT_ATTRS = frozenset({"class", "width", "height", "stroke-width"})

#: Spec §4.2: "Icons: Lucide (ISC) via a Jinja macro ... 1.75 stroke."
_STROKE_WIDTH = "1.75"

#: What an unknown icon name renders as (controller decision, Task 2
#: brief): never a broken/crashed page in production — an empty,
#: decorative placeholder instead. `aria-hidden="true"` since a `<span>`
#: with nothing in it has nothing meaningful to announce either way.
_UNKNOWN_ICON_MARKUP = Markup('<span aria-hidden="true"></span>')


@functools.lru_cache(maxsize=None)
def _read_icon(icons_dir: Path, name: str) -> str | None:
    """The raw text of `{icons_dir}/{name}.svg`, or `None` if it doesn't
    exist — never raises. Cached per `(icons_dir, name)` (the brief's
    "functools.lru_cache"): `icons_dir` is part of the key, rather than
    baked into a closure over just `name`, so two `build_env` calls against
    different static roots (e.g. two tests using different fixtures) never
    share a stale cache entry.
    """
    path = icons_dir / f"{name}.svg"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _style_svg(raw: str, class_: str, label: str | None) -> str:
    """Rewrite a vendored Lucide SVG's opening `<svg ...>` tag in place:
    drop `class`/`width`/`height`, force `stroke-width` to 1.75, and add
    either `aria-hidden="true"` (decorative — the default) or
    `role="img" aria-label="..."` (the icon itself carries the meaning) —
    spec §4.2's "`aria-hidden` unless the icon is the label." Every other
    root attribute (`viewBox`, `fill`, `stroke`, `stroke-linecap`,
    `stroke-linejoin`, `xmlns`) passes through unchanged, and the entire
    inner `<path>`/`<rect>`/... content, plus the `<!-- @license ... -->`
    banner Lucide prefixes every file with, is dropped from the *start* by
    slicing from the first `<svg` — SVG's presentation attributes inherit
    from the root down to its children, so restyling only the root tag is
    enough to restyle the whole icon.

    Plain string slicing on the opening tag rather than an XML parser:
    `xml.etree.ElementTree` treats a root `xmlns="..."` as a namespace
    declaration rather than a literal attribute and by default
    re-serializes tags back out with a generated `ns0:` prefix (needing
    `register_namespace` ceremony to avoid) — slicing sidesteps that
    entirely, and is safe specifically because Lucide's output is the
    controlled, always-double-quoted format `_ATTR_RE` documents.
    """
    start = raw.index("<svg")
    end = raw.index(">", start) + 1
    head, tail = raw[start:end], raw[end:]
    kept = [(k, v) for k, v in _ATTR_RE.findall(head) if k not in _OVERRIDDEN_ROOT_ATTRS]
    kept.append(("stroke-width", _STROKE_WIDTH))
    kept.append(("class", class_))
    if label:
        kept.append(("role", "img"))
        kept.append(("aria-label", label))
    else:
        kept.append(("aria-hidden", "true"))
    attrs = " ".join(f'{k}="{escape(v)}"' for k, v in kept)
    return f"<svg {attrs}>{tail}"


def make_icon(static_dir: Path | str) -> Callable[..., Markup]:
    """Build the `icon(name, class_="size-4", label=None)` Jinja global
    bound to `static_dir` (Lucide SVGs live at `{static_dir}/icons/`,
    populated by `make icons` from `mailosh/ui/icons.txt`).

    An unknown `name` never raises here — a broken/misspelled icon must
    not be able to 500 a whole page in production (controller decision,
    Task 2 brief) — it logs a warning and returns an empty, decorative
    `<span aria-hidden="true">` instead. Catching a genuinely missing icon
    *loudly* is instead a build/test-time concern:
    `tests/unit/test_ui_macros.py::test_every_listed_icon_is_vendored`
    fails the moment `mailosh/ui/icons.txt` names something `make icons`
    didn't fetch, well before this graceful fallback would ever be reached
    by real traffic; `test_icon_falls_back_for_unknown_name` in the same
    module covers this function's own runtime behavior.
    """
    icons_dir = Path(static_dir) / "icons"

    def icon(name: str, class_: str = "size-4", label: str | None = None) -> Markup:
        raw = _read_icon(icons_dir, name)
        if raw is None:
            logger.warning("unknown icon %r requested (looked in %s)", name, icons_dir)
            return _UNKNOWN_ICON_MARKUP
        return Markup(_style_svg(raw, class_, label))

    return icon


def kbd(keys: str) -> Markup:
    """Render a shortcut hint as one `<kbd class="kbd">` per space-separated
    key — e.g. `kbd("g i")` for the two-keystroke "go to inbox" chord —
    with no separator between them (the `.kbd` component rule's own
    margin/gap, applied by whatever wraps a call to this, spaces them out).
    `class="kbd"` on each element rather than a bare `<kbd>` tag: `.kbd` in
    `styles/input.css`'s `@layer components` is a class selector, matching
    that layer's other component rules (`.chip`, `.nav-item`, ...), not a
    bare-element one.
    """
    return Markup("".join(f'<kbd class="kbd">{escape(key)}</kbd>' for key in keys.split()))
