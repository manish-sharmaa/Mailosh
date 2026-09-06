# Two stages. The first builds the static assets that git deliberately does
# not track -- the vendored JS, the Lucide icons, the subsetted Inter font
# and the compiled Tailwind stylesheet -- exactly as `make vendor icons
# fonts css` does on a developer's machine. The second is the image that
# runs, and copies only those results in.
#
# Why this exists: the first production deployment was built from a bare
# `git clone`, where none of those files exist, and every page answered 500
# from `static()` trying to hash a font that was never there. A developer
# never saw it because `make test` and `make up` build the assets first;
# the Dockerfile assumed they were on disk. It no longer assumes anything
# about the checkout it is handed.

FROM python:3.12-slim AS assets

RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends curl make ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Only the two tools the asset recipes call, not the whole `dev` extra: the
# font subsetter (`fontTools`) and the Tailwind standalone CLI wrapper. The
# Makefile's venv rule would otherwise run `pip install -e '.[dev]'`, which
# pulls the test suite into an image layer for nothing -- so the stamp it
# checks is written here, after installing exactly what is needed, and the
# rule is satisfied without running. The Tailwind binary itself is fetched
# on first use, at build time, which is the same thing the dev flow does.
RUN python3 -m venv .venv \
 && .venv/bin/pip install --no-cache-dir 'fonttools[woff]>=4.50' 'pytailwindcss>=0.2' \
 && touch .venv/.install-stamp \
 && make vendor icons fonts css


FROM python:3.12-slim

WORKDIR /app
COPY . /app
COPY --from=assets /app/mailosh/web/static/vendor  /app/mailosh/web/static/vendor
COPY --from=assets /app/mailosh/web/static/icons   /app/mailosh/web/static/icons
COPY --from=assets /app/mailosh/web/static/fonts   /app/mailosh/web/static/fonts
COPY --from=assets /app/mailosh/web/static/app.css /app/mailosh/web/static/app.css

# `.[build]` rather than `.`: it adds rjsmin, which the next line needs and
# nothing else does. Minifying happens here, against the copy inside the
# image, so the source tree on the host and in git keeps its comments --
# they are a large part of how this codebase explains itself, and they have
# no business on the wire. Measured at the time of writing: 92,013 -> 30,588
# bytes gzipped across this app's own modules.
#
# One layer, so a failed minify fails the build rather than leaving an image
# with a half-processed static directory. `vendor/` is untouched: those files
# are SHA-pinned against the URLs they came from.
RUN pip install --no-cache-dir -e ".[build]" \
 && python scripts/minify-js.py

# The assets the first stage built are the ones a request will hash; a build
# that lost any of them fails here, not on the first page view in production.
RUN test -s mailosh/web/static/app.css \
 && test -s mailosh/web/static/fonts/inter-latin.woff2 \
 && test -s mailosh/web/static/vendor/htmx.min.js \
 && test -n "$(ls mailosh/web/static/icons/*.svg 2>/dev/null | head -1)"

CMD ["uvicorn", "mailosh.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
