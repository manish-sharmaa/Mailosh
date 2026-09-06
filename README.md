<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/mailosh-wordmark-dark.png">
    <img src="docs/images/mailosh-wordmark.png" alt="Mailosh" width="300">
  </picture>
</h1>

[![Release](https://img.shields.io/badge/release-v0.1.2-1a73e8)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-passing-2ea043)](#tests)
[![Status](https://img.shields.io/badge/status-early%20release-orange)](#status)

**A self-hosted webmail client you run yourself.** Mailosh is a
keyboard-first mail UI on top of the [Stalwart](https://stalw.art) mail
server, aiming for Gmail's frame and affordances with Superhuman's manners.
Python and HTML on the server, no Node toolchain anywhere, AGPL-3.0.

![The Mailosh inbox, dark theme](docs/images/inbox-dark.png)

**[Quick start](#quick-start)** · **[What works](#what-works-today)** ·
**[What is unproven](#what-is-not-proven-yet)** · **[Deployment](#deployment)** ·
**[Changelog](CHANGELOG.md)**

## What this is

If you already run — or want to run — your own mail server, you still need
a client for it, and the ones that feel genuinely modern are all hosted
services you do not control. Mailosh aims to be the missing piece: a
webmail UI that a Gmail user recognises in five seconds, that answers to
the keyboard, and that runs on a small VPS next to the mail server it talks
to.

It speaks **JMAP** to Stalwart. Mail never touches Mailosh's own database —
Postgres holds application state only (users, sessions, label metadata, UI
preferences, login attempts, an audit log).

**Who it's for:** people comfortable running Docker Compose on a small
server, who want to own their mail. It is not a hosted product and there is
no signup.

<a id="status"></a>

## Status — v0.1.2

**Reading, composing, searching and organising all work**, covered by 2,877
unit tests plus integration tests that run against a live stack.

This is an **early release**: Mailosh works as webmail against your own
Stalwart, but it has not yet been run as a public mail host. Read
[what is not proven yet](#what-is-not-proven-yet) before pointing a domain
you care about at it.

### What works today

- **Reading, with HTML mail rendered safely.** Conversation view with
  quoted runs folded away. Hostile mail passes three independent layers:
  `nh3` strips the HTML server-side, `tinycss2` re-serialises CSS through
  an allow-list, and the result is served into a sandboxed iframe under its
  own CSP. `allow-same-origin` appears in no sandbox anywhere. Remote
  images are blocked until you ask for them, and the frame's `img-src` is
  byte-identical either way, so a sanitiser miss still cannot leak your IP.
- **Compose and send.** Rich text, drafts with autosave, attachments, and
  reply / reply all / forward with correct `In-Reply-To` and `References`
  threading. Quoted mail is sanitised on the way *out* as well as in.
- **Search** with Gmail operators — `from: to: subject: body: has: is:
  in: label: before: after: older_than: newer_than: larger: smaller:`,
  quoted phrases, `-` negation, `OR`, parentheses. Unknown operators and
  malformed input produce an inline hint, never an error page.
- **Labels.** Create, rename, nest and delete over JMAP mailboxes, with
  colour, visibility and ordering, and a picker for applying several at
  once. Deleting a label never deletes mail.
- **Triage with undo.** Archive, delete, spam, star, read, unread — applied
  optimistically, each with a signed undo token and a 10-second toast.
  Bulk selection with range extend, select-by-state, and a confirmation
  above 100 messages.
- **Per-user login** against Stalwart. Your password is verified and
  discarded; what is stored is a Stalwart API key held encrypted at rest.
  CSRF on every mutating route, Postgres-backed login rate limiting, sign
  out and sign out everywhere.
- **Live updates** over SSE, with a connection-lost banner and recovery.
- **Keyboard-first** throughout — `j`/`k`/`o`/`u`, `g`-prefixed jumps, `x`
  to select, `e`/`#`/`!`/`s` to act, `z` to undo, `c`/`r`/`a`/`f` to
  write, `/` to search, `l`/`v` to label and move, `?` for the overlay —
  plus a `⌘K` command palette.
- **Light and dark themes**, three densities, a nav drawer and single-pane
  layout below 768px, 44px touch targets, and a WCAG 2.2 AA pass with
  contrast measured in both themes rather than assumed.
- **Operations.** A production compose file publishing only the four mail
  ports and terminating TLS; backup and restore scripts exercised by a
  real restore drill; a liveness endpoint; and scripted first-boot
  configuration for a new Stalwart.

<a id="what-is-not-proven-yet"></a>

### What is *not* proven yet

- **Outbound delivery to other providers is untested.** Mailosh has never
  delivered a message to an outside recipient: the first public host has
  outbound port 25 blocked pending the provider's unblock. Inbound *is*
  proven — one public instance receives mail from Gmail over its MX.
- **No queue visibility.** If outbound delivery fails the interface says
  "Sent", then nothing until your server bounces the message.
- **Deployed exactly once**, on 2026-09-06, from a bare clone — which found
  and fixed one release-blocking bug (0.1.2). Webmail and mail ports both
  carry Let's Encrypt production certificates; DNS-01 is verified end to
  end. One instance, one operator, one day: not yet a track record.
- **Chromium and Safari only, and no external security review.**
  [`SECURITY.md`](SECURITY.md) names the parts most worth attacking.

## Requirements

- **Docker** with the Compose plugin (`docker compose`) — runs Stalwart,
  Postgres and Mailosh.
- **Python 3.12+** on the host, with `venv`. Used for the local
  virtualenv that runs the tests, Alembic, Ruff, the Tailwind standalone
  CLI and the font subsetter.
- **`make`** and **`curl`**. `curl` fetches the vendored frontend assets;
  the first `make test` / `make up` needs network for that, and nothing
  after it does.
- Enough RAM for three containers — Stalwart itself idles around 100 MB, and
  the reference deployment box is 2 vCPU / 4 GB. See
  [`docs/hosting.md`](docs/hosting.md) for sizing, VPS options and the
  port-25 realities of self-hosted mail.

There is **no Node toolchain** — no `npm`, no `package.json`. That is
deliberate; see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Quick start

```bash
git clone https://github.com/manish-sharmaa/Mailosh.git mailosh && cd mailosh

make venv                       # .venv + the project and its dev deps
cp .env.example .env            # then edit .env — see "Configuration"
                                #   MAILOSH_STALWART_ADMIN_SECRET
                                #   MAILOSH_SECRET_KEY  (openssl rand -hex 32)
                                #   MAILOSH_DEMO_PASSWORD

make up                         # docker compose up -d: stalwart, postgres, mailosh
                                #   (fetches vendored assets + builds CSS first)
bash scripts/stalwart-init.sh   # idempotent: bootstraps the domain and a demo mailbox
```

Then open <http://localhost:8000> and sign in as the account the init
script created — `demo@mailosh.test`, with the password you put in
`MAILOSH_DEMO_PASSWORD`.

The app refuses to boot while `MAILOSH_SECRET_KEY` is still the
placeholder, so generate a real one before `make up`.

Database migrations run automatically every time the `mailosh` container
starts (`docker/entrypoint.sh`). To re-apply them by hand — after pulling a
new migration, say — without restarting anything:

```bash
make db-upgrade        # alembic upgrade head, inside the running container
make db-upgrade-host   # ...or from .venv, against the published port
```

**Ports published by the development stack**, all bound to `127.0.0.1`:
`8000` Mailosh, `8080` Stalwart HTTP/JMAP, `55432` Postgres (not 5432,
which a host-native Postgres commonly already owns), `2525` SMTP, `1587`
submission (STARTTLS), `1993` IMAPS. Every one of them reaches a listener
that exists — `1143 -> 143` used to be published against nothing and is
gone, because plain IMAP is not configured and 993 covers every modern
client. Production publishes almost none of these — see below.

### Creating real accounts

`scripts/stalwart-init.sh` exists to make the dev stack usable in one
command; it creates one demo mailbox on `mailosh.test`. Mailosh has no
account-management UI of its own yet, so for anything real use the CLI —
it works on the dev stack and on a production one alike, because it runs
inside the app container and reaches Stalwart over the internal network:

```bash
docker compose exec -T mailosh mailosh setup \
    --domain mailosh.com --email you@mailosh.com
```

That creates the domain if it is missing, creates the mailbox, and prints
the DNS records to publish (A/AAAA, MX, SPF, the live DKIM key read from
the server, DMARC). It prints the generated password too, so pass
`--password` if your scrollback is shared. A setup wizard is Phase 2 work.

## Deployment

The quick start above is a development stack: it publishes Stalwart's
admin/JMAP port, publishes Postgres behind a hardcoded credential, and
terminates no TLS. **`docker-compose.prod.yml` is the one to deploy.** It
adds Caddy in front, moves the app, the database and Stalwart's admin API
off the network entirely, and leaves open only the ports that have to be.

The decisions behind it — and an explicit list of what it does *not* make
production-safe — are in
[`docs/specs/2026-09-05-production-deployment.md`](docs/specs/2026-09-05-production-deployment.md).
Read §12 before you rely on this for real mail.

### Before you start

1. **DNS.** Three records, and they are for three different names:

   ```
   app.mailosh.com    A     -> this box   the webmail — MAILOSH_SITE_ADDRESS
   mail.mailosh.com   A     -> this box   SMTP/IMAP, and the PTR target
   mailosh.com        MX 10 -> mail.mailosh.com
   ```

   Caddy's first act is an ACME challenge on port 80 for
   `MAILOSH_SITE_ADDRESS`, so that record has to exist before the first
   `up`. The `A` for the MX target matters just as much and is the one
   people forget: an MX pointing at a name that does not resolve means mail
   silently never arrives.
2. **Reverse DNS (PTR)** for the box's IP, matching the **mail** hostname
   (`mail.mailosh.com`), not the webmail one. A missing or mismatched PTR is
   the most common reason self-hosted mail lands in spam.
3. **Outbound port 25**, which most budget VPS providers block by default.
   [`docs/hosting.md`](docs/hosting.md) has the per-provider table and the
   relay alternatives.
4. **Firewall**: allow 80, 443 (tcp+udp), 25, 465, 587, 993, and nothing
   else. Note that Docker publishes ports with DNAT rules that on many
   distributions bypass `ufw`/`firewalld` — check what is reachable from
   outside, not what your rules say.

### Bring it up

```bash
cp .env.example .env && chmod 600 .env
```

Fill in the "Production deployment" block plus the two required secrets at
the top of the file. Every one of them has a generator command in its
comment; none of the placeholders work, and the stack refuses to start with
any of them missing.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```

Run this first, every time. It exits non-zero with a readable message if
anything is unset, and it prints the exact port list you are about to open.
Then:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Caddy obtains a certificate for `MAILOSH_SITE_ADDRESS` on its own and
renews it on its own — there is no cron entry and nothing to remember.

### Then set up Stalwart

**The stack does not receive mail until this is done.** A fresh Stalwart
container boots into *bootstrap mode* with no mail listeners running at all
until it has been given a domain and a server hostname — and it reports
itself healthy the whole time, so nothing tells you.

Two commands. Both run inside containers that are already on the internal
Docker network, so Stalwart's admin port stays published nowhere:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml

scripts/stalwart-bootstrap.sh --domain mailosh.com

docker compose exec -T mailosh mailosh setup \
    --domain mailosh.com --email you@mailosh.com
```

The first takes the server out of bootstrap mode, restarts it, and then
*verifies*: that it left bootstrap mode, that the hostname and domain are
the ones you passed, that every configured listener is accepting
connections, and that port 25 answers with an SMTP greeting naming your
hostname. It refuses (exit 3) rather than reconfiguring a server that is
already set up, and `--verify-only` reports the state without touching
anything.

The second creates your first mailbox and prints the DNS records to
publish — A/AAAA, MX, SPF, the live DKIM key, DMARC — ready to paste.

**The three names are not the same thing**, and conflating them is the easy
mistake:

```
mailosh.com        the mail domain — the part after the @ in your addresses
app.mailosh.com    the webmail hostname — MAILOSH_SITE_ADDRESS, Caddy's real certificate
mail.mailosh.com   the mail hostname — SMTP banner, MX target, and the PTR target
```

Pass the **mail domain** to both commands. `--hostname` defaults to
`mail.<domain>`, which is what the MX record and the PTR have to agree on.

**Mail-port TLS is still yours to configure.** Caddy owns 80 and 443, so
Stalwart can answer neither HTTP-01 nor TLS-ALPN-01 — use **DNS-01** with a
provider token, in Stalwart's own admin UI. Until you do, 465, 587 and 993
serve a self-signed certificate and mail clients will warn (inbound mail on
25 is unaffected; the webmail is on Caddy's real certificate throughout).
[`docs/operations.md`](docs/operations.md) §1 has the whole walkthrough, the
failure modes, and what was and was not verified.

**Client settings**, once that is done: submission on **587** with STARTTLS
or **465** with implicit TLS, IMAP on **993**. 587 is configured by
`scripts/stalwart-bootstrap.sh` rather than by Stalwart — v0.16.20 ships
listeners on 25, 465, 993, 995, 4190, 8080 and 443 and none on 587, so the
port used to be published against nothing. It requires STARTTLS before it
will accept a credential and authentication before it will accept a message;
the bootstrap script verifies both on every run.

Do **not** point `scripts/stalwart-init.sh` at a production stack. It is the
development one-liner and hardcodes `mailosh.test`.

If you need Stalwart's admin UI itself — for DNS-01, or anything the CLI
does not cover — open a temporary forwarder onto the internal network and
close it afterwards:

```bash
docker run --rm -d --name stalwart-admin-tunnel \
  --network "$(basename "$PWD")_mail" -p 127.0.0.1:8080:8080 \
  alpine/socat TCP-LISTEN:8080,fork,reuseaddr TCP:stalwart:8080
# ... work at http://127.0.0.1:8080, over `ssh -L 8080:localhost:8080 you@box`
docker rm -f stalwart-admin-tunnel
```

*(Still untested. First-boot setup no longer needs it — that was its main
use — so it is here only for the admin UI. `docker network ls` shows the
real network name if `$(basename "$PWD")_mail` guesses wrong.)*

### What is published, and what is not

| Open | Closed |
| --- | --- |
| `80`, `443` (tcp+udp) — Caddy | `8000` — the app. Reachable only from Caddy |
| `25` — inbound MX | `5432`/`55432` — Postgres, on an `internal: true` network with no route out |
| `465` (implicit TLS), `587` (STARTTLS) — submission | `8080` — Stalwart's JMAP **and admin** API. Internal network only |
| `993` — IMAPS | `143` — no listener, and none added: 993 covers every modern client |
| | `995` (pop3s), `4190` (ManageSieve) — Stalwart listens on both by default; neither is published, and nothing here needs them from outside |
| | `443` on Stalwart — Caddy owns 443 on the host |

### Upgrades, migrations and backups

Migrations run automatically at container start (`docker/entrypoint.sh`),
so an upgrade is one command — and takes the site down for a few seconds
plus however long the migrations run:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Take a database backup first; a failed migration aborts the container
rather than serving against a half-migrated schema.

Of the named volumes, **`stalwart-data` holds all mail** and is the one a
backup exists for. `stalwart-etc` holds Stalwart's config and its DKIM
private keys. `pg-data` holds application state only — users, sessions,
label colours, preferences, audit log — and never mail content; losing it
signs everyone out and loses no messages. `caddy-data` holds the ACME
account key and issued certificates; losing it means re-issuing against
Let's Encrypt's rate limits. `caddy-config` is disposable.

`scripts/backup.sh` and `scripts/restore.sh` cover both stores. Point them
at the production overlay through Compose's own environment:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml \
  scripts/backup.sh /var/backups/mailosh --keep 7
```

Nothing runs that on a timer for you, and nothing checks that the last one
restores. Add the cron entry, then actually do a restore drill.

### Known limits of this deployment

**One uvicorn worker, pinned with `WEB_CONCURRENCY=1`, and that is a
correctness constraint rather than a performance default.** Logout revokes
the session in Postgres but drops its pooled JMAP client only in its own
process, so a second worker's open `GET /events` would keep streaming a
signed-out session's mail — with no time bound, because a client carrying a
live stream is exempt from the idle sweep. `MAILOSH_SSE_FANOUT=postgres`
moves live-update *events* between workers; it does not move that drop, and
it is not clearance to scale out. Spec §12.1 has the whole thing.

Also: no rolling upgrade. Secrets are environment variables. No metrics and
no alerting beyond `docker compose ps`. Stalwart's mail-port TLS is manual
(DNS-01, by hand, in its admin UI), so 465/587/993 serve a self-signed
certificate until you configure it. The spec's §12 is the full list with
reasons.

## Development loop

Use **`make dev`**, not `make up`:

```bash
make dev    # docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

`make up` serves the image built by `docker/mailosh.Dockerfile`, which
bakes the source in with `COPY . /app` — correct for a deployment, painful
to develop against, because every edit needs a rebuild.

`make dev` adds two things (see `docker-compose.dev.yml`): a bind mount of
the working tree over `/app`, and `MAILOSH_RELOAD=1`, which turns on
uvicorn's reloader with `--reload-include` for `*.html`, `*.css` and
`*.js`. Edit a route, a template or a static script and the running
container picks it up on save.

Two things the reloader does not cover:

- **`styles/input.css` needs `make css`.** The app serves the compiled
  `app.css`; compiling it is Tailwind's job, not uvicorn's.
- **`docker/entrypoint.sh` is read at container start.** Changing it needs
  `docker compose restart mailosh` — a plain `up -d` sees no config change
  and leaves the old command running.

Useful targets:

| Target | What it does |
| --- | --- |
| `make venv` | Create `.venv` and install the project with its dev extras |
| `make vendor` | Fetch the pinned frontend libraries into `static/vendor/` |
| `make icons` | Fetch one Lucide SVG per name in `mailosh/ui/icons.txt` |
| `make fonts` | Download Inter and subset it with `pyftsubset` |
| `make css` | Compile `styles/input.css` to `static/app.css` (Tailwind CLI) |
| `make test` | Unit tests — no running stack needed |
| `make itest` | Integration tests — needs the stack from `make up` |
| `make up` / `make dev` | Bring the stack up (baked image / live reload) |
| `make db-upgrade` | Apply migrations inside the running container |

Every asset target is a real file target, so once fetched they are not
re-downloaded on later runs.

## Configuration

All configuration is environment variables prefixed `MAILOSH_`, read from
`.env` (gitignored) into `mailosh.config.Settings`.
[`.env.example`](.env.example) documents every one with a safe placeholder;
this is the summary.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAILOSH_STALWART_ADMIN_SECRET` | *(required)* | Stalwart recovery-admin password; used to mint and destroy each user's Stalwart API key |
| `MAILOSH_SECRET_KEY` | *(required)* | ≥ 32 chars. HKDF root for the session-credential encryption key and the undo-token signing key. Rejected while it begins with `change-me` |
| `MAILOSH_STALWART_URL` | `http://localhost:8080` | Stalwart's HTTP/JMAP endpoint (compose overrides to `http://stalwart:8080`) |
| `MAILOSH_STALWART_ADMIN_USER` | `admin` | Admin account name |
| `MAILOSH_DATABASE_URL` | `postgresql+asyncpg://…@localhost:5432/mailosh` | App state only, never mail (compose overrides the host to `postgres`) |
| `MAILOSH_COOKIE_SECURE` | `true` | Session cookie `Secure` flag, and which cookie name is used (`__Host-sid` vs `sid`). Set `false` for `http://localhost` |
| `MAILOSH_TRUST_PROXY` | `false` | Trust `X-Forwarded-*` for the client IP. Only behind a proxy you control |
| `MAILOSH_SESSION_IDLE_DAYS` | `14` | Sliding idle expiry |
| `MAILOSH_SESSION_REMEMBER_DAYS` | `30` | Idle expiry with "Keep me signed in" |
| `MAILOSH_SESSION_ABSOLUTE_DAYS` | `90` | Hard expiry, never extended |
| `MAILOSH_SSE_FANOUT` | `memory` | `postgres` relays live-update events between workers over `LISTEN/NOTIFY`. Transport only — it does **not** make multi-worker safe |
| `MAILOSH_DEMO_USER` / `MAILOSH_DEMO_PASSWORD` | unset | Dev-stack mailbox for `scripts/stalwart-init.sh`, `mailosh import-mbox`, `scripts/measure.py` and the integration tests. **Not** used by the web app |
| `MAILOSH_SMTP_PORT` | `2525` | Used only by `scripts/measure.py` |

Three more are read by `docker-compose.prod.yml` and the Caddyfile rather
than by `Settings`, and are needed only for a production deployment:

| Variable | Purpose |
| --- | --- |
| `MAILOSH_SITE_ADDRESS` | The hostname the webmail is served on — Caddy's site address, and what it asks a CA to certify |
| `MAILOSH_ACME_EMAIL` | Where the CA sends certificate-expiry warnings. Use an inbox that is *not* on this server |
| `MAILOSH_PG_PASSWORD` | The Postgres password. One variable feeds both `POSTGRES_PASSWORD` and the app's DSN, so the two cannot drift |

`MAILOSH_COOKIE_SECURE`, `MAILOSH_TRUST_PROXY` and uvicorn's
`FORWARDED_ALLOW_IPS` are set inline by the production overlay, where
`environment:` beats `env_file:` — a production run cannot be talked into a
non-`Secure` session cookie by a `.env` left over from local development.

## Tests

```bash
make test    # the unit suite; no Docker needed
make itest   # integration tests; needs `make up` and a bootstrapped Stalwart
```

Integration tests skip themselves when `MAILOSH_DEMO_USER` /
`MAILOSH_DEMO_PASSWORD` are unset, so they never fail merely for lack of a
live stack.

Lint and format checks, which CI-equivalent runs must be clean:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

## Project layout

```
mailosh/
  config.py            pydantic-settings; every MAILOSH_* variable
  cli.py               `mailosh` CLI (import-mbox)
  sse.py               per-user SSE hubs + the upstream Stalwart listener
  stalwart_admin.py    admin client: domains, accounts, API keys, DKIM
  jmap/                JMAP client, models, errors, per-session client pool
  db/                  SQLAlchemy 2 async models, session factory, repo helpers
  security/            crypto (HKDF/Fernet), CSRF, rate limiting, sessions,
                       Stalwart credential exchange
  services/            view-model builders: mailbox tree, thread list,
                       actions, undo tokens
  ui/                  Jinja environment, icon macro, formatters, versioned
                       static URLs
  web/
    app.py             create_app: middleware, error handlers, lifespan
    auth.py            /login, /logout, /logout/all, session reaper
    mail.py            /, /mail/{key}, /mail/{key}/rows, /t/{thread_id}
    actions.py         POST /a/archive|delete|spam|star|read|undo
    palette.py         GET /palette/index
    prefs.py           POST /prefs
    events.py          GET /events (SSE)
    templates/         Jinja2 partials, swapped by htmx
    static/            our JS modules; vendored libs, icons and fonts
                       (gitignored build output — `make vendor icons fonts`)
styles/input.css       Tailwind 4 source: design tokens + components
migrations/            Alembic
docker/                Dockerfile + entrypoint (runs migrations, then uvicorn)
                       + Caddyfile (production reverse proxy and TLS)
scripts/               stalwart-init.sh, send-test.py, measure.py
docs/                  hosting guide, design specs, plans, findings
tests/                 unit/ (no stack) and integration/ (needs the stack)
```

The frontend is server-rendered Jinja2 swapped by **htmx** (with idiomorph
for morph swaps and a preload extension), plus **Alpine** in its **CSP
build** for small pieces of local state, and ~3k lines of our own vanilla
JS across five modules for the keyboard registry, the palette, the action
layer and the SSE bridge. There is no build step beyond Tailwind, and the
app serves `script-src 'self'` with no `unsafe-eval`.

## Roadmap

Phase 1 is the webmail, split into five plans. **All five are in this
release.** The binding design document is
[`docs/specs/2026-09-02-phase1-webmail-design.md`](docs/specs/2026-09-02-phase1-webmail-design.md),
and the plan behind the current code is
[`docs/plans/2026-09-02-phase1a-foundation.md`](docs/plans/2026-09-02-phase1a-foundation.md).

| Plan | Scope | State |
| --- | --- | --- |
| **1A Foundation** | Sessions and login, design system, app shell and list, triage with undo, keyboard, ⌘K, live updates, error surface | **shipped** |
| **1B Reading** | Conversation view proper, HTML sanitiser pipeline, sandboxed frame, remote-image gate, attachments, per-message actions, auto-advance | **shipped** |
| **1C Compose** | Compose dock, rich text via Squire, recipients, attachments, drafts, reply/forward, identities, undo send | **shipped** |
| **1D Organise & find** | Label CRUD, colours and nesting, label/move pickers, search with Gmail-style operators | **shipped** — full `/settings/*` pages pending |
| **1E Polish & release** | Responsive and mobile layout, accessibility audit, performance budgets, first-run coaching | **shipped** — browser QA covers Chromium and Safari |

Beyond Phase 1: platform operations is Phase 2 — the setup wizard, DNS and
health panels, and second factors. (TLS and the hardened compose file were
scheduled there too; they landed early, as
[`docs/specs/2026-09-05-production-deployment.md`](docs/specs/2026-09-05-production-deployment.md).)
A job runner for snooze and schedule-send is Phase 3; calendar, contacts
and multi-account are Phase 4.

## Contributing

Bug reports, and patches for anything on the roadmap, are welcome. Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first — it covers the setup, the test
and lint commands, and a handful of deliberate constraints (no Node, pinned
vendored assets, a CSP with no `unsafe-eval`) that will otherwise trip you
up.

## Security

Please **do not** open a public issue for a security problem. See
[`SECURITY.md`](SECURITY.md) for how to report one and for the security
posture of the current release — including the limitations above, which
matter: Mailosh does not yet render HTML mail, and nothing here has had an
external audit.

## Third-party notices

The UI vendors a small set of pinned frontend assets — htmx, idiomorph, the
htmx preload extension, command-score, Alpine (CSP build), Squire,
DOMPurify, Lucide icons and the Inter font. [`NOTICE`](NOTICE) records each
one's version and license.

## License

Mailosh is licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE)
(AGPL-3.0-or-later). If you run a modified version as a network service,
the AGPL requires you to offer its source to your users.
