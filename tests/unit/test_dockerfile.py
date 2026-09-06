"""The production image must build every static asset git does not track.

`mailosh/web/static/{vendor,icons,fonts}` and `app.css` are gitignored and
produced by `make vendor icons fonts css`. The first public deployment was
built from a bare `git clone`, where none of them exist, and every page
answered 500 from `static()` hashing a font that was never there -- a
developer never saw it because `make test` and `make up` build the assets
first. These pin the Dockerfile's answer: a build stage that runs those
same Makefile targets, and a final stage that copies the results in and
refuses to finish without them.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO / "docker/mailosh.Dockerfile").read_text()
GITIGNORE = (REPO / ".gitignore").read_text()


def test_the_untracked_assets_are_the_ones_the_image_builds():
    ignored = {
        line.strip()
        for line in GITIGNORE.splitlines()
        if "static/" in line and not line.startswith("!")
    }
    assert ignored == {
        "mailosh/web/static/vendor/",
        "mailosh/web/static/app.css",
        "mailosh/web/static/icons/",
        "mailosh/web/static/fonts/",
    }, "a new untracked static path needs building in the Dockerfile too"
    assert re.search(r"\bmake vendor icons fonts css\b", DOCKERFILE)


def test_the_image_copies_every_built_asset_from_the_build_stage():
    assert re.search(r"^FROM .+ AS assets$", DOCKERFILE, re.M)
    for path in ("static/vendor", "static/icons", "static/fonts", "static/app.css"):
        pattern = rf"^COPY --from=assets /app/mailosh/web/{re.escape(path)}\b"
        assert re.search(pattern, DOCKERFILE, re.M), path


def test_a_build_that_lost_an_asset_fails_instead_of_shipping():
    for probe in ("static/app.css", "static/fonts/inter-latin.woff2", "static/vendor/htmx.min.js"):
        assert f"test -s mailosh/web/{probe}" in DOCKERFILE, probe
    assert "static/icons/*.svg" in DOCKERFILE


def test_the_build_stage_installs_only_the_asset_tools_not_the_test_suite():
    """`fonttools` and `pytailwindcss` are what the recipes call; the Makefile's
    venv rule would install the whole `dev` extra, so the stamp it checks is
    written by hand after installing just those two."""
    stage = DOCKERFILE.split("FROM python:3.12-slim\n", 1)[0]
    # Instructions only: the comments explain *why* `.[dev]` is avoided, and
    # naming it there is not installing it.
    instructions = "\n".join(
        line for line in stage.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    assert "fonttools[woff]" in instructions and "pytailwindcss" in instructions
    assert "touch .venv/.install-stamp" in instructions
    assert ".[dev]" not in instructions
