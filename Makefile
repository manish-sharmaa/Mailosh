VENDOR := mailosh/web/static/vendor
VENDOR_FILES := $(VENDOR)/htmx.min.js $(VENDOR)/idiomorph-ext.min.js $(VENDOR)/preload.js \
                $(VENDOR)/command-score.js $(VENDOR)/alpine.min.js \
                $(VENDOR)/squire.js $(VENDOR)/purify.min.js
ICONS := mailosh/web/static/icons
ICON_NAMES := $(shell cat mailosh/ui/icons.txt)
ICON_FILES := $(addprefix $(ICONS)/,$(addsuffix .svg,$(ICON_NAMES)))
LUCIDE_VERSION := 1.39.0
# command-score has no published npm dist/ and no git tags/releases
# (verified directly against the GitHub API, 2026-09-02: both endpoints
# return an empty list) -- this is the tip-of-master commit at the time it
# was vendored, pinned by full SHA rather than the floating `master` ref
# a plain branch URL would resolve every time `make vendor` re-runs on a
# fresh clone. See NOTICE for the version this corresponds to (0.5.0, per
# the same commit's own package.json).
COMMAND_SCORE_REF := a192b81315c6c4836918e6af87b2d80bf0afa9c1
FONTS := mailosh/web/static/fonts
FONT_FILE := $(FONTS)/inter-latin.woff2
# rsms.me/inter's own font-files URL is unversioned (always "whatever's
# currently published"); Inter *does* tag releases, and the exact same
# bytes rsms.me serves are checked into the tagged repo tree at this path
# (verified directly, 2026-09-02: sha256 of both is identical) -- fetched
# from there instead so this pin is immutable, not just "hasn't changed
# yet".
INTER_REF := v4.1
CSS := mailosh/web/static/app.css
VENV := .venv
VENV_STAMP := $(VENV)/.install-stamp

.PHONY: venv vendor icons fonts css test itest qa up db-upgrade db-upgrade-host \
        js-budget \
        backup backup-check restore health

# `venv` stays a phony alias so `make venv` keeps working, but the real
# work hangs off a FILE target. Without that, `make test` on a fresh clone
# curls every vendored asset, then dies on
# `.venv/bin/pyftsubset: No such file or directory` -- a message that says
# nothing about the missing step. The README's quick start does say
# `make venv` first, and anyone who reads it is fine; anyone who types
# `make test` out of habit was not. Verified against a genuine clone.
#
# Every target below whose recipe runs a `.venv/bin/...` tool takes this as
# an ORDER-ONLY prerequisite (`| $(VENV_STAMP)`). Order-only is the
# load-bearing part: a plain prerequisite would make a reinstalled venv
# newer than `app.css` and the subsetted font, and silently re-run Tailwind
# and pyftsubset on every dependency change. Order-only still *creates* the
# stamp when it is missing or stale -- it only stops its timestamp from
# ageing anything else.
#
# The stamp, rather than `$(VENV)/bin/python` itself, because `pip install`
# does not touch the interpreter it installs into: keying off it would
# leave the target permanently older than `pyproject.toml` and reinstall on
# every single run. Depending on `pyproject.toml` is what keeps `make venv`
# meaningful after a dependency is added -- otherwise it silently becomes
# "create it if missing" and does nothing when someone edits the deps.
$(VENV_STAMP): pyproject.toml
	python3 -m venv $(VENV) && $(VENV)/bin/pip install -e '.[dev]'
	@touch $@

venv: $(VENV_STAMP)

# Real file targets (phase0 final review, FIX 1b) rather than one phony
# `vendor` recipe that always re-curls all files: `test`/`itest`/`up` below
# depend on these files directly, so once they exist on disk, Make treats
# them as up to date and skips re-downloading on every run. `| $(VENDOR)`
# is an order-only prerequisite -- the directory just needs to exist first,
# its own mtime changing (e.g. from a later download) must never make an
# already-fetched sibling file look stale and get re-fetched.
$(VENDOR):
	mkdir -p $(VENDOR)

$(VENDOR)/htmx.min.js: | $(VENDOR)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js

# idiomorph (Task 2): htmx's `hx-ext="morph"` swap strategy. Registers
# itself onto the global `htmx`, so it must load *after* htmx.min.js above
# -- layouts/app.html's script order guarantees that (classic, non-module,
# non-deferred scripts execute synchronously in document order).
$(VENDOR)/idiomorph-ext.min.js: | $(VENDOR)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/idiomorph@0.7.4/dist/idiomorph-ext.min.js

# htmx-ext-preload (Task 2): htmx's `hx-ext="preload"` -- also registers
# onto the global `htmx`, same ordering requirement as idiomorph above.
$(VENDOR)/preload.js: | $(VENDOR)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/htmx-ext-preload@2.1.2/dist/preload.min.js

# command-score (Task 2): Superhuman's fuzzy-match scorer for the ⌘K
# palette (design spec §6.2), MIT-licensed. Upstream ships CommonJS
# (`module.exports = commandScore`, no built dist/ -- verified directly
# against https://github.com/superhuman/command-score's `index.js`) with
# no bundled ESM build, so this fetches the raw source (pinned to
# $(COMMAND_SCORE_REF), not the floating `master` branch -- review
# finding) and rewrites its one export line into a real ES module export
# (controller decision, Task 2 brief) -- `sed` runs as part of the
# recipe, not a separate target, specifically so this stays a single real
# file-target Make can skip once already fetched, matching every other
# vendor recipe's idiom. See NOTICE for this modification and the
# upstream license record.
$(VENDOR)/command-score.js: | $(VENDOR)
	curl -fsSL https://raw.githubusercontent.com/superhuman/command-score/$(COMMAND_SCORE_REF)/index.js \
	  | sed 's/^module\.exports = commandScore;$$/export default commandScore;/' > $@

# Alpine's **CSP build** (`@alpinejs/csp`), not the standard one. This app
# serves `script-src 'self'` with no `unsafe-eval`, and the standard build
# evaluates every template expression through `new AsyncFunction` -- it
# would not fail at build time, only the first time a directive actually
# runs. Pinned to an exact version rather than the floating `@3` this used
# to carry, matching the pinning discipline of every other vendor rule
# above (a floating major can change what ships without this diff, the
# Makefile, or NOTICE ever being touched).
#
# What the CSP build costs, measured against the vendored 3.17.1 bytes
# rather than assumed: it swaps the evaluator for its own mini-JS parser
# (tokenizer + recursive-descent parser + tree-walking evaluator, no
# `Function` constructor anywhere in the bundle), and that parser is
# narrower than JavaScript but not by much. Method calls WITH arguments
# work -- `list.select(id)`, `move(delta)` and `ui.toast(msg, token)` are
# all fine -- as do property paths, computed access, ternaries,
# arithmetic, comparison, `&&`/`||`, assignment and object/array literals
# in `x-data`. What it rejects: template literals; anything outside the
# Alpine scope, `window`/`document`/`Math`/`JSON` included; more than one
# statement per expression (`a(); b()`); spread and destructuring;
# shorthand object keys (`{ open }` -- write `open: open`); optional
# chaining; and every form of inline function (arrow, `function`, method
# shorthand), so an `x-data` literal carries data and its methods come
# from `Alpine.data()`.
#
# htmx's `hx-on:`, `hx-vals='js:...'` and `hx-trigger="...[expr]"` are
# unavailable too, and that part really is a `new Function` problem --
# htmx compiles those attribute values that way, which is exactly what
# `script-src 'self'` without `'unsafe-eval'` forbids. Behaviour that
# would have used them is driven from one delegated listener keyed off
# `data-action`/`data-role` instead (static/js/app.js).
ALPINE_VERSION := 3.17.1

$(VENDOR)/alpine.min.js: | $(VENDOR)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/@alpinejs/csp@$(ALPINE_VERSION)/dist/cdn.min.js

$(VENDOR)/squire.js: | $(VENDOR)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/squire-rte@2.4.8/dist/squire.js

$(VENDOR)/purify.min.js: | $(VENDOR)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js

# `vendor` stays a phony alias -- `make vendor` keeps working exactly as
# before, it just now costs nothing on a repeat run once the files exist.
vendor: $(VENDOR_FILES)

# Icons (Task 2): one Lucide static SVG per name in mailosh/ui/icons.txt,
# fetched from jsDelivr's lucide-static package -- same real-file-target,
# skip-if-present idiom as $(VENDOR_FILES) above. The pattern rule's stem
# (`%`) is the icon name, so `$(ICONS)/archive.svg`'s recipe fetches
# `.../icons/archive.svg`.
$(ICONS):
	mkdir -p $(ICONS)

$(ICONS)/%.svg: | $(ICONS)
	curl -fsSL -o $@ https://cdn.jsdelivr.net/npm/lucide-static@$(LUCIDE_VERSION)/icons/$*.svg

icons: $(ICON_FILES)

# Fonts (Task 2): download Inter's upstream variable font straight into
# the (gitignored) fonts/ dir alongside the subset it produces -- rather
# than a separate top-level build/ directory -- since that whole directory
# is already build-output-only. `pyftsubset`'s positional arg is the
# source font; `--unicodes` is latin + the punctuation/currency/arrow
# glyphs this UI actually sets (exact ranges per the Task 2 brief).
#
# Two steps, because the design's 48 KB budget is for a **wght-only** subset
# (docs/research/2026-09-02-webmail-ux-research.md: "Inter 4.1 variable latin
# `wght` subset (48 KB woff2)") and InterVariable ships *two* axes. Building
# it in one pyftsubset pass kept both and produced 99,740 B -- 2.08x the
# budget -- so the miss was the build deviating from the spec, not the
# budget being wrong. Measured, all four combinations, same unicode range:
#
#   both axes, --layout-features='*'   99,740 B   (what this used to build)
#   both axes, tight feature set       63,524 B
#   wght only, --layout-features='*'   64,068 B
#   wght only, tight feature set       40,904 B   <- this recipe, PASS
#
# Note the middle two: keeping `opsz` cannot reach 48 KB by any feature
# pruning, so the two levers are not independent and both are needed.
FONT_UNICODES := U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD

# The OpenType features a browser turns on by itself for horizontal Latin,
# plus `tnum` -- the one feature this stylesheet asks for explicitly, via the
# four `font-variant-numeric: tabular-nums` rules in styles/input.css (drop it
# and the list-range counter stops aligning). `--layout-features='*'` used to
# retain all 39, of which 35 are opt-in sets nothing here ever requests --
# small caps, fractions, superiors/inferiors, ordinals, oldstyle and
# proportional figures, discretionary ligatures, and the cv01-cv13/ss01-ss08
# character variants -- and the 462 alternate glyphs that exist only to be
# reached through them. Verified against the old subset before changing it:
# identical cmap coverage (283 codepoints, none lost), identical advance
# widths for all 283, and identical kerning across all 80,089 ordered pairs
# of those characters.
FONT_FEATURES := calt,ccmp,clig,kern,liga,locl,mark,mkmk,rlig,tnum

$(FONTS):
	mkdir -p $(FONTS)

$(FONTS)/InterVariable.woff2: | $(FONTS)
	curl -fsSL -o $@ https://raw.githubusercontent.com/rsms/inter/$(INTER_REF)/docs/font-files/InterVariable.woff2

# Step 1: pin `opsz` at its default (14) so only `wght` -- the axis the
# `@font-face` declares as its supported range, `font-weight: 100 900`
# (a descriptor, NOT a weight anything asks for: the values actually used
# are 500, 600 and 700 plus the implicit 400) -- survives, taking
# that axis's gvar/HVAR deltas with it. Nothing in styles/input.css sets
# `font-optical-sizing` or `font-variation-settings`, and `opsz`'s range
# starts at 14, so every size in this UI's 10.5-14px band already renders at
# opsz=14 and is byte-identical either way. Only .thread-subject (19px),
# .auth-title (17px) and the 15px band change at all: the string "Inbox
# Archive Settings Compose ..." measures +1.9% wider at 19px, +0.8% at 17px
# and +0.1% at 15px, the difference between Inter's 14pt and 19pt optical
# designs. That is the one real cost of getting under budget, and it is
# sub-pixel per glyph.
$(FONTS)/inter-wght.ttf: $(FONTS)/InterVariable.woff2 | $(VENV_STAMP)
	.venv/bin/python -m fontTools.varLib.instancer $(FONTS)/InterVariable.woff2 opsz=14 -o $@

# Step 2: subset to the glyphs and features this UI can actually reach.
$(FONT_FILE): $(FONTS)/inter-wght.ttf | $(VENV_STAMP)
	.venv/bin/pyftsubset $(FONTS)/inter-wght.ttf \
	  --unicodes="$(FONT_UNICODES)" \
	  --flavor=woff2 \
	  --layout-features='$(FONT_FEATURES)' \
	  --output-file=$(FONT_FILE)

fonts: $(FONT_FILE)

# app.css depends on the icons/fonts targets too, not just styles/input.css
# -- `styles/input.css`'s own `@font-face src: url(...)` and this design
# system's `icon()` macro are only actually *usable* once those real
# asset files exist, so `make css` alone still produces a fully working
# static/ tree rather than a stylesheet referencing files nobody fetched.
# Every stylesheet, not just the entry point. `input.css` `@import`s
# `search.css` and `labels.css`, and naming only the entry point here meant
# editing a partial rebuilt nothing at all -- `make css` said "Nothing to be
# done" and served stale CSS, which looks exactly like a broken selector.
# A wildcard rather than three names so the next partial cannot repeat it.
STYLE_FILES := $(wildcard styles/*.css)

# The trailing `touch` is not decoration: Tailwind skips the write when its
# output is byte-identical, which leaves app.css older than the stylesheet
# that triggered the build and makes this rule run again on every `make`.
# The touch is what records that the build actually happened. (A comment
# inside the recipe would be echoed by make on every run.)
$(CSS): $(STYLE_FILES) $(ICON_FILES) $(FONT_FILE) | $(VENV_STAMP)
	.venv/bin/tailwindcss -i styles/input.css -o $(CSS) --minify
	@touch $(CSS)

css: $(CSS)

# test/itest/up all need the vendored JS + icons + fonts + built CSS to
# actually exist, not just mailosh/web/static/ itself (tracked via
# static/.gitkeep so the StaticFiles mount doesn't raise on a fresh clone
# -- see app.py) -- a working app/test run needs the real files. Depending
# on the file targets above means a fresh clone's first `make
# test`/`make itest`/`make up` fetches/builds them automatically, and
# every run after that is a no-op dependency check, not a re-download/
# re-build.
test: $(VENDOR_FILES) $(ICON_FILES) $(FONT_FILE) $(CSS) | $(VENV_STAMP)
	.venv/bin/python -m pytest

itest: $(VENDOR_FILES) $(ICON_FILES) $(FONT_FILE) $(CSS) | $(VENV_STAMP)
	.venv/bin/python -m pytest -m integration

# The manual browser checklist `make test`/`make itest` cannot cover:
# anything whose answer is "what did the browser actually paint", plus the
# three failure drills that need a container stopped. Printed, not run --
# every line here needs a human looking at a screen.
#
# Deliberately a plain `@echo` block rather than a script or a doc link:
# a checklist you have to go and open is a checklist that gets skipped, and
# this one exists precisely because Phase 1A's two worst bugs both lived in
# a seam between tasks that every unit test passed straight through. The
# recorded results of the last full pass are in
# docs/spikes/p1a-findings.md ("Browser QA"), including the
# harness quirks that make a couple of these read wrong if you drive them
# from an automation tool rather than by hand.
qa:
	@echo ""
	@echo "Mailosh manual QA checklist (Phase 1A)"
	@echo "Stack: make dev   App: http://localhost:8000   Findings: docs/spikes/p1a-findings.md"
	@echo ""
	@echo "APPEARANCE"
	@echo "  [ ] Sign in; inbox renders in light AND dark (Quick settings, top right)"
	@echo "  [ ] Density Compact / Standard / Comfortable each change row height"
	@echo "  [ ] Compare against docs/design/mockups/"
	@echo ""
	@echo "KEYBOARD (no mouse)"
	@echo "  [ ] j / k move the cursor; Enter or o opens; u goes back"
	@echo "  [ ] x selects; Shift+J extends; the selection toolbar appears with a count"
	@echo "  [ ] e archives, then z brings it back (within 10 s)"
	@echo "  [ ] # deletes, then z brings it back"
	@echo "  [ ] ! reports spam, then z brings it back"
	@echo "  [ ] s stars / unstars; Shift+I marks read, Shift+U unread"
	@echo "  [ ] g s goes to Starred, g i back to Inbox"
	@echo "  [ ] ? opens the shortcuts overlay; Esc closes it"
	@echo "  [ ] Cmd+K, type 'arch', Enter runs Archive conversation"
	@echo ""
	@echo "POINTER"
	@echo "  [ ] Hovering a row replaces its date with the archive/delete/read actions"
	@echo "  [ ] Those buttons work, and clicking one does NOT also open the conversation"
	@echo ""
	@echo "LIVE UPDATES"
	@echo "  [ ] With the inbox open: .venv/bin/python scripts/send-test.py"
	@echo "      -> NOTE: unauthenticated SMTP lands in Junk, not Inbox (SPK-6)."
	@echo "         Watch /mail/spam, or import into the Inbox over JMAP, to see a new row."
	@echo "  [ ] A message that does land in the Inbox appears with no reload, and the"
	@echo "      tab title shows (N)"
	@echo "  [ ] Archive a row, let a live update land, THEN press z -- undo still works"
	@echo ""
	@echo "FAILURE DRILLS (restore every container afterwards)"
	@echo "  [ ] Two tabs open, sign out in one -> the other shows 'Reconnecting to your"
	@echo "      mailbox…' within ~6 s and later lands on /login. It must not sit silent."
	@echo "  [ ] docker compose stop mailosh   -> banner appears; start it -> banner clears."
	@echo "      (Stopping *stalwart* does NOT do this: /events is served by our app.)"
	@echo "  [ ] docker compose stop stalwart   -> archive a row: it comes back and an error"
	@echo "      toast says so. The row must not stay gone. Then start stalwart again."
	@echo "  [ ] Archive an Inbox-only message on an account with no Archive folder ->"
	@echo "      exactly one Archive mailbox is created (covered by make itest too)"
	@echo ""
	@echo "AFTERWARDS"
	@echo "  [ ] docker compose ps -- every container up and healthy"
	@echo "  [ ] No leftover QA mail in the account"
	@echo ""

up: $(VENDOR_FILES) $(ICON_FILES) $(FONT_FILE) $(CSS)
	docker compose up -d

# Development stack: same services as `up`, plus docker-compose.dev.yml's
# bind mount of the working tree over /app and uvicorn --reload. Edit a
# route, template or static file and the running container picks it up on
# save -- no rebuild, no `docker cp`. `styles/input.css` is the exception:
# the app serves the compiled app.css, so run `make css` after editing it
# (this target builds it once on the way up, like `up` does).
dev: $(VENDOR_FILES) $(ICON_FILES) $(FONT_FILE) $(CSS)
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Applies pending Alembic migrations by running alembic *inside* the
# already-running `mailosh` container. Correct regardless of what ports
# are published (works whether or not the host can reach Postgres at
# all): `docker/entrypoint.sh` already runs this same `alembic upgrade
# head` on every `mailosh` boot; this target is for re-running it by hand
# (e.g. right after `make up`, or after pulling a new migration file)
# without restarting that container.
db-upgrade:
	docker compose exec -T mailosh alembic upgrade head

# Host-side variant, for when you specifically want alembic running from
# .venv rather than inside the container (e.g. debugging env.py itself).
# `postgres` publishes 55432 on the default stack (docker-compose.yml) --
# deliberately not 5432, which a host-native Postgres commonly already
# owns -- so this needs nothing extra beyond `make up`.
db-upgrade-host:
	MAILOSH_DATABASE_URL=postgresql+asyncpg://mailosh:mailosh@localhost:55432/mailosh .venv/bin/alembic upgrade head

# ---------------------------------------------------------------------------
# Operations: backup, restore, health. docs/operations.md is the manual;
# these are the three commands you type. The scripts, not these targets, are
# the interface -- a cron job or a second host calls scripts/backup.sh
# directly, and everything below is a one-word alias for the common path.
# ---------------------------------------------------------------------------

# Both stores. Postgres is dumped online with pg_dump; Stalwart's container is
# stopped for the few seconds its volumes are archived, because a RocksDB
# store copied from under a live writer may not open again (scripts/backup.sh
# documents the exact error that proves it). Measured against this stack:
# ~12 s wall, ~9 s of which the mail server is down.
#
# Writes to ./backups, which the script makes self-ignoring to git the first
# time -- a directory holding a copy of everyone's mail must never end up in
# a commit.
#
#   make backup                    -> ./backups/mailosh-<UTC stamp>/
#   make backup DEST=/mnt/nas/mail
#   make backup KEEP=14            -> prune all but the 14 newest afterwards
backup:
	scripts/backup.sh $(DEST) $(if $(KEEP),--keep $(KEEP))

# Verify the newest backup (or BACKUP=<dir>) without changing anything:
# checksums, gzip integrity, the pg_dump completion marker, and that the mail
# archive really is a RocksDB store. Cheap enough to run from cron.
#
# It proves the bytes are readable. It does NOT prove the data comes back --
# only a real restore into a throwaway project does that. See
# docs/operations.md, "Restore drill".
backup-check:
	scripts/restore.sh --check $(or $(BACKUP),$(shell ls -d backups/mailosh-* 2>/dev/null | tail -1))

# DESTRUCTIVE. Wipes the target project's Stalwart volumes and drops its
# database before loading the backup, and asks you to type the target project
# name first. To rehearse safely, point it at a throwaway project instead of
# the one you are standing in:
#
#   make restore BACKUP=backups/mailosh-20260905T041916Z
#   COMPOSE_PROJECT_NAME=mailosh-drill COMPOSE_FILE=/tmp/drill/docker-compose.yml \
#     make restore BACKUP=backups/mailosh-20260905T041916Z
restore:
	@test -n "$(BACKUP)" || { echo "usage: make restore BACKUP=backups/mailosh-<stamp>   (see: ls backups/)"; exit 1; }
	scripts/restore.sh $(BACKUP)

# One command, one answer: containers, Stalwart's /healthz/live and
# /healthz/ready, pg_isready, the alembic revision, the app's /login, and how
# old the newest backup is. Exit 0 healthy, 1 something is down, 2 warnings.
# Every probe runs inside the compose network, so it works on a deployment
# that publishes no ports.
health:
	scripts/healthcheck.sh

# What a production image would actually serve, measured without minifying
# anything in your checkout. Needs rjsmin, which `make venv` does not install
# (it lives in the `build` extra, not `dev`), so this fetches it on demand
# rather than putting a build-only tool in every developer's environment.
js-budget: | $(VENV_STAMP)
	@$(VENV)/bin/python -c "import rjsmin" 2>/dev/null || $(VENV)/bin/pip install -q rjsmin
	@$(VENV)/bin/python scripts/js-budget.py
