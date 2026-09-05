# Contributing to Mailosh

Thanks for looking. Mailosh is early — Phase 1A, the foundation — so the
most useful contributions right now are bug reports against what exists and
patches for things already on the roadmap. Before starting anything large,
read the design spec and the current plan under `docs/` and open
an issue to check the direction; the phases are ordered deliberately and a
feature landing out of order tends to fight the one it was supposed to build
on.

Contributions are accepted under the project's license,
[AGPL-3.0-or-later](LICENSE).

## Setting up

```bash
make venv                       # .venv with the project + dev extras
cp .env.example .env            # fill in MAILOSH_STALWART_ADMIN_SECRET,
                                #   MAILOSH_SECRET_KEY, MAILOSH_DEMO_PASSWORD
make dev                        # the live-reload stack (see README)
bash scripts/stalwart-init.sh   # bootstraps the domain + a demo mailbox
```

`make dev` bind-mounts your working tree over `/app` and runs uvicorn with
`--reload`, so Python, template, CSS and JS edits take effect on save. Use
`make up` only when you want to test the baked image. Full detail is in the
README's "Development loop".

## Tests, lint, format

```bash
make test                              # unit tests — no running stack needed
make itest                             # integration tests — needs `make up`
.venv/bin/python -m ruff check .       # lint
.venv/bin/python -m ruff format .      # format (add --check in CI)
```

**Always call tools through `.venv/bin/`.** Plain `python` is not on PATH on
a typical dev machine here; `.venv/bin/python -m pytest` and
`.venv/bin/python -m ruff` are the forms that reliably work. `make test` and
`make itest` already do this for you.

Ruff is configured in `pyproject.toml`: line length 100, rules `E`, `F`, `I`,
`RUF`, with `*.md` excluded (the docs embed illustrative snippets that are
not shipped source). Both `ruff check` and `ruff format --check` must be
clean.

Test output must be **warning-free**. A new deprecation warning is a real
finding, not noise to scroll past.

The Makefile's `test`, `itest`, `up` and `dev` targets all depend on the
vendored assets and the compiled CSS, so a fresh clone fetches and builds
them on the first run. They are real file targets — later runs skip them.

## How this codebase is written

- **Tests first.** The implementation plans are TDD-structured: a failing
  test, then the code that satisfies it. New behaviour arrives with the test
  that pins it.
- **Comments record decisions, not mechanics.** This codebase leans heavily
  on long explanatory comments and docstrings that say *why* something is
  the way it is — a review finding, a measured trade-off, an upstream quirk
  that cost debugging time. Match that. A comment restating the line below
  it is worth less than nothing; a comment recording the bug that made a
  line necessary is what stops someone deleting it in six months.
- **Layers.** Routers in `mailosh/web/` stay thin and call view-model
  builders in `mailosh/services/`, which talk to `mailosh/jmap/`. Pure
  library code lives in `mailosh/security/` and `mailosh/ui/`.
- **Postgres holds application state only.** Mail content never touches the
  database.
- **No UI control for a feature that does not work.** Render it
  `aria-disabled` with a `title` that says when it arrives, or do not render
  it at all. A control that silently does nothing is worse than an absent
  one. See `shell/topbar.html` and `shell/nav.html` for the pattern.

### Commit messages

Conventional-commit subject, then a body that explains itself. Looking at
recent history:

```
fix(keys): keep the palette reachable when shortcuts are off
feat(shell): the ⌘K palette and the quick-settings panel
build(dev): reload on template, style and script edits too
docs(vendor): state alpine's real csp grammar, not a stricter one
```

- `type(scope): summary` — types in use are `feat`, `fix`, `docs`, `build`,
  `test`, `design`, `chore`. The scope is the area touched (`keys`, `shell`,
  `list`, `actions`, `auth`, `vendor`, `rt`, …) and may be omitted.
- Summary in lowercase, imperative, no trailing period.
- The body is prose wrapped at ~72–78 columns. Say what was wrong, why the
  fix is the right one, and how you verified it — including the check that
  failed before the change. Multi-paragraph bodies are normal here.
- **No AI-attribution trailers of any kind** — no `Co-Authored-By:`, no
  session links, no "generated with" lines. Commits are authored by the
  person who wrote them.

## Constraints you will otherwise trip over

These are deliberate. Each one exists for a reason, and a patch that
violates one will be sent back.

### No Node toolchain

There is no `package.json`, no `npm`, no bundler, and none is welcome.
Tailwind runs through the **standalone CLI** (installed into `.venv` by
`pytailwindcss`), and the Inter subset is produced by `pyftsubset` from
`fonttools`. A contribution that needs a Node build step needs a design
discussion first.

### All frontend assets are vendored and pinned

Every third-party script, icon and font is fetched by a Makefile rule into
`mailosh/web/static/vendor/`, `.../icons/` and `.../fonts/` — all
gitignored build output, all baked into the Docker image. **Nothing is
loaded from a CDN at runtime.**

Adding or bumping one means all three of:

1. a Makefile rule pinned to an **exact** version, tag or commit SHA — never
   a floating major or a branch name;
2. a matching entry in [`NOTICE`](NOTICE) with the real version and license,
   checked against the package's own metadata rather than assumed; and
3. a note there of any modification you made to the upstream source
   (`command-score` is vendored with one export line rewritten, and says so).

### CSP: `script-src 'self'`, no `unsafe-eval`

The app serves a strict Content-Security-Policy. Two consequences bite
constantly:

- **htmx's `hx-on:`, `hx-vals='js:…'` and `hx-trigger="…[expr]"` do not
  work.** htmx compiles those attribute values with the `Function`
  constructor, which the CSP forbids. Behaviour that would use them is
  driven from one delegated `click` listener keyed off `data-action` /
  `data-role` in `static/js/app.js` and `static/js/actions.js`.
- **Alpine is the CSP build** (`@alpinejs/csp`), which ships its own
  expression parser instead of `new AsyncFunction`. Property paths, computed
  access, method calls with arguments, ternaries, arithmetic, comparison,
  `&&`/`||`, assignment and object/array literals all work. These do not:
  template literals; anything outside the Alpine scope (`window`,
  `document`, `Math`, `JSON` included); more than one statement in an
  expression; spread and destructuring; shorthand object keys (write
  `open: open`); optional chaining; and every form of inline function. An
  `x-data` literal therefore carries data only — its methods come from
  `Alpine.data()`.

If a directive silently does nothing, this is almost always why: the CSP
build reports an evaluation failure by rethrowing asynchronously, and a
failed effect never subscribes to the reactive property it was reading.

Also note script order in `layouts/app.html`: htmx before its extensions,
and **Alpine last**, after our own modules. That is load-bearing and the
comment there explains what breaks otherwise.

### `styles/input.css` needs `make css`

The app serves the compiled `mailosh/web/static/app.css`, and compiling it
is Tailwind's job, not uvicorn's. `make dev`'s reloader will not do it for
you — edit the source, run `make css`, reload.

Design tokens live at the top of `styles/input.css` and are the single
source of truth for both themes; both must stay at WCAG AA contrast on
interactive controls.

### Every mutation is POST + CSRF

Mutating routes are `POST`, carry the session's CSRF token (via htmx's
inherited `hx-headers` reading `<meta name="csrf-token">`, or a hidden form
field), reject `Sec-Fetch-Site: cross-site`, and answer `204` with an
`HX-Trigger` header or a rendered fragment. `HX-Request` is never trusted as
proof of anything. See `mailosh/security/csrf.py` and
`mailosh/web/deps.py`.

### Other standing rules

- Python ≥ 3.12.
- No GPL/AGPL Python dependencies (the app is AGPL; its dependencies must be
  permissively licensed so downstream packaging stays simple).
- No Redis, and no second datastore. Postgres is it.
- Static assets are served through `mailosh/ui/static.py`'s versioned URLs,
  never a bare path.

## Security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).
