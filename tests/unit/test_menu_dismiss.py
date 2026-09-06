"""Floating menus close when you click away from them.

A native `<details>` is a disclosure, not a menu: the platform opens and
closes it from its own summary and does nothing about a click elsewhere. So
the account menu stayed open over the settings page while the reader clicked
around behind it, and so did advanced search, a search chip's dropdown, and a
message's ⋮.

`static/js/app.js` closes any open `details[data-menu]` on a click outside
it. These pin the two halves that have to agree: which elements are marked,
and that the handler is the shape that can actually reach them.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "mailosh/web/templates"
APP_JS = (REPO / "mailosh/web/static/js/app.js").read_text(encoding="utf-8")
COMPOSE_JS = (REPO / "mailosh/web/static/js/compose.js").read_text(encoding="utf-8")

#: Every `<details>` that floats over the page. The value is the class it
#: carries, so a renamed element fails here rather than silently losing its
#: dismissal.
FLOATING = {
    "shell/topbar.html": ["search-adv", "account-menu"],
    "search/chips.html": ["schip-menu", "schip-menu"],
    "thread/menu.html": ["msg-menu"],
    "thread/details.html": ["msg-details"],
}

#: `<details>` elements that are sections of the page rather than menus. A
#: fold that collapsed itself when you clicked elsewhere would be broken,
#: not tidy -- so these must never be marked.
ANCHORED = {
    "shell/nav.html": ["nav-more"],
    "thread/message.html": ["msg-fold", "quote"],
}


def _details(path: str) -> list[str]:
    """Every `<details ...>` start tag in a template."""
    return re.findall(r"<details\b[^>]*>", (TEMPLATES / path).read_text(encoding="utf-8"))


def test_every_floating_menu_is_marked():
    for path, classes in FLOATING.items():
        marked = [tag for tag in _details(path) if "data-menu" in tag]
        assert len(marked) == len(classes), (path, marked)
        for tag, name in zip(marked, classes, strict=True):
            assert f'class="{name}"' in tag, (path, tag)


def test_page_sections_are_not_marked():
    for path, classes in ANCHORED.items():
        for tag in _details(path):
            assert "data-menu" not in tag, (path, tag)
        text = (TEMPLATES / path).read_text(encoding="utf-8")
        for name in classes:
            assert f'class="{name}"' in text, (path, name)


def test_the_handler_closes_on_an_outside_click_in_the_capture_phase():
    assert 'const MENU = "details[data-menu]"' in APP_JS
    body = APP_JS[APP_JS.index("function closeMenus") :]
    # Capture phase: a menu must close even when something deeper stops
    # propagation.
    assert re.search(r'"click",\n\s*\(event\) => \{\n\s*closeMenus\(', body)
    assert re.search(r"\n\s*true,\n\s*\);", body)
    # The clicked menu itself is spared -- the platform is already toggling
    # it, and closing it here would make that toggle re-open it.
    assert "closeMenus(event.target?.closest?.(MENU) ?? null)" in APP_JS


def test_escape_closes_the_innermost_menu_and_restores_focus():
    assert 'if (event.key !== "Escape" || event.defaultPrevented) return;' in APP_JS
    assert "menus[menus.length - 1]" in APP_JS
    assert 'innermost.querySelector("summary")?.focus();' in APP_JS
    # It has to win over keys.js's global Escape, which clears the selection.
    assert "event.stopPropagation();" in APP_JS


def test_a_swap_closes_whatever_was_open():
    assert 'document.body.addEventListener("htmx:beforeSwap", () => closeMenus());' in APP_JS


def test_the_attribute_does_not_collide_with_composes_own_popover():
    """`compose.js` hides its formatting popover by setting `hidden` on
    `[data-popover]`. Had these menus been marked with the same attribute,
    opening one and clicking away would have hidden it outright instead of
    closing it -- and a `<details hidden>` cannot be reopened by its summary.
    """
    assert '"[data-popover]:not([hidden])"' in COMPOSE_JS
    for path in FLOATING:
        for tag in _details(path):
            assert "data-popover" not in tag, (path, tag)
