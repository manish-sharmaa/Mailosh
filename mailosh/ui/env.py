"""`build_env`: a standalone `jinja2.Environment` over
`mailosh/web/templates`, wired with this package's globals/filters.

Kept separate from `mailosh.web.app`'s own `Jinja2Templates` (FastAPI's
thin wrapper around this exact same kind of `Environment`, used for the
actual request/response cycle from Task 5/6 onward) specifically so the
design system — icons, versioned assets, the base layout — can be
unit-tested with no FastAPI app, no HTTP request, and no route wiring at
all, as `tests/unit/test_ui_macros.py` does.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from mailosh.ui import format, macros, static

#: Relative to the process's current working directory — every Makefile
#: target that runs pytest (`test`/`itest`) does so from the repo root, so
#: this resolves the same way in CI, `make test`, and a bare `pytest`
#: invocation from the repo root alike. Matches `mailosh.web.app`'s own
#: `_TEMPLATES_DIR` in content (not reused directly: that one is an
#: absolute `Path(__file__).parent / "templates"`, which this package has
#: no reason to import just to borrow one constant from).
_TEMPLATES_DIR = "mailosh/web/templates"


def build_env(static_dir: Path | str) -> Environment:
    """A Jinja2 `Environment` loading templates from `mailosh/web/templates`,
    with this design system's globals (`icon`, `static`, `kbd`) and filters
    (`initials`, `avatar_color`, `label_color`) registered — `static_dir` is where `icon`
    looks for vendored SVGs (`{static_dir}/icons/`) and `static` hashes
    real asset bytes from (e.g. `mailosh/web/static` in production, matching
    whatever directory `StaticFiles` in `mailosh.web.app` serves as `/static`).
    """
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["icon"] = macros.make_icon(static_dir)
    env.globals["static"] = static.make_static(static_dir)
    env.globals["kbd"] = macros.kbd
    env.filters["initials"] = format.initials
    env.filters["avatar_color"] = format.avatar_color
    env.filters["label_color"] = format.label_color
    return env
