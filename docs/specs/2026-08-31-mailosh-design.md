# Mailosh — Design Specification

- **Date:** 2026-08-31
- **Status:** Approved direction (v2, open-source-first). This spec freezes the design for phases 0–3.
- **Owner:** Manish Sharma
- **Companion research:** "Mailosh Blueprint" artifact (v2), full evidence and pricing tables.

## 1. Product definition

Mailosh is an open-source, self-hosted mail platform: Stalwart Mail Server as the engine, a Gmail-class webmail and an operations layer (setup wizard, DNS generation and verification, health panel, backups, CLI) as the product.

**Definition of done for v1.0:** a developer with a small VPS (2 vCPU / 4 GB / 80 GB) and a domain runs `git clone … && docker compose up -d`, completes the setup wizard, adds the DNS records Mailosh prints (verified live in the UI), and within 30 minutes has `hello@example.com` receiving from and sending to Gmail — with webmail at `mail.example.com`, working IMAP for Thunderbird/phones, one-command backup/restore, and a health panel. They never learn what Postfix is.

**Positioning:** mailcow's job, Stalwart's engine, Gmail's UX. Competitors (mailcow, Mailu, mail-in-a-box, docker-mailserver, Poste.io) bundle 6–9 daemons around folder-based webmail; none speak JMAP, none do S3 blob storage, none have a Gmail-class UI.

**Business frame:** the self-hosted edition is the complete product, free forever (no mailbox caps). A future Mailosh Cloud sells operation of the same images. Nothing in v1 may depend on Cloud-only services.

## 2. Goals and non-goals

### Goals (v1.0 = phases 0–3)
1. One-command install; three core containers (`mailosh`, `stalwart`, `postgres`; optional `worker` profile and `caddy`).
2. Setup wizard: domain → admin account → storage profile → SMTP mode (with outbound port-25 probe) → DNS records page with live verification.
3. Gmail-class webmail on a single native account: conversation view, labels (colors, nesting), star/archive/trash/spam, bulk actions, keyboard shortcuts, full compose (rich text, attachments, drafts), Gmail-operator search, live updates, safe HTML rendering, responsive + dark mode.
4. Operations: admin panel (domains, users, aliases, quotas, queue, DKIM), `mailosh` CLI, one-tarball backup/restore with a tested drill, per-domain deliverability score, documented update path.
5. Real-time behaviour comparable to Gmail: new mail visible in the open inbox in under 2 s from SMTP acceptance; common actions feel instant (optimistic UI).

### Non-goals for v1.0 (deferred, see §13)
- External account connectors / unified inbox (phase 5), calendar/contacts apps (phase 5), offline/PWA, category tabs / importance ML, multi-tenancy, SaaS billing, POP3 UI (Stalwart serves POP3; we just don't surface settings), AI features, mobile native apps.

## 3. Architecture

```
                    ┌────────────────────────────────────────────┐
 Browser ──HTTP/2──▶│  mailosh (FastAPI, one process type)      │
  HTMX+Alpine       │  web: wizard · webmail · admin · REST API  │
  ◀──SSE────────────│  jmap: async client (httpx+Pydantic)       │
                    │  sse hub · sanitizer · search parser       │
                    └───────┬───────────────────────┬────────────┘
                            │ JMAP (batched HTTPS)  │ SQL
                            │ ◀ EventSource push    │
                    ┌───────▼────────┐      ┌───────▼────────┐
 Internet ◀─SMTP──▶ │   stalwart     │      │   postgres     │
 Thunderbird ◀IMAP▶ │ SMTP·IMAP·JMAP │      │ app state +    │
                    │ Sieve·FTS·spam │      │ Procrastinate  │
                    └───────┬────────┘      └───────▲────────┘
                            │ blobs                 │ jobs
                    ┌───────▼────────┐      ┌───────┴────────┐
                    │ local FS or S3 │      │ worker         │
                    │ (BLAKE3 dedup) │      │ (Procrastinate)│
                    └────────────────┘      └────────────────┘
```

- **Modular monolith.** One Python package `mailosh/` with modules `web/` (routes+templates), `jmap/`, `admin/`, `wizard/`, `dns/`, `health/`, `search/`, `render/`, `backup/`, `cli/`, `jobs/`, `db/`. Two runtime entrypoints: `mailosh web` (uvicorn) and `mailosh worker` (Procrastinate). The CLI is the same package (Typer).
- **The UI and CLI talk only the Mailosh API**, which fronts (a) JMAP for mail data and (b) Stalwart's HTTP management API for domains/users/DKIM/queue. No component except `db/` touches PostgreSQL, and nothing except `jmap/` + `admin/` talks to Stalwart. This is the seam the Cloud reuses.
- **Stalwart owns** mail storage (metadata: RocksDB default; blobs: filesystem or S3), full-text search, threading, push, Sieve, spam filtering, DKIM signing, IMAP/POP3 interop, ACME TLS for mail protocols.
- **PostgreSQL owns** Mailosh app state only: user prefs, label metadata (color/order/visibility), scheduled jobs, health/backup history, wizard state, audit log. Mail content never enters Postgres.

## 4. Tech stack (frozen for v1)

| Layer | Choice | Notes |
|---|---|---|
| Mail engine | Stalwart (AGPL-3.0), pin latest 0.16.x; move to 1.0 when released (~Oct 2026) and tag Mailosh v1.0 then | Rust; JMAP/IMAP/SMTP/POP3/Sieve/CalDAV/CardDAV; S3 blobs; FTS; spam |
| Backend | Python 3.12+, FastAPI, uvicorn | async throughout |
| JMAP client | In-house `mailosh.jmap` on httpx + Pydantic v2 | `jmapc` is sync + GPL-3.0; JMAP is plain JSON |
| Templates/UI | Jinja2 + HTMX 2.0.x + `htmx-ext-sse` + Alpine.js 3 | HTMX 2 "supported indefinitely"; gate in P0 |
| CSS | Tailwind 4 via standalone CLI (`pytailwindcss`) | no Node toolchain; ship built CSS |
| Editor | Squire 2.4 + DOMPurify (browser) + `nh3` (server) | Fastmail's email editor, MIT |
| DB / ORM | PostgreSQL 16+, SQLAlchemy 2 async, Alembic | app state only |
| Jobs | Procrastinate (Postgres LISTEN/NOTIFY + SKIP LOCKED) | no Redis |
| SSE | sse-starlette; HTTP/2 at the proxy | one stream per tab |
| S3 | obstore (or aioboto3) — used by backup/restore tooling only; Stalwart does its own S3 I/O | |
| DNS checks | dnspython; socket probes for ports | health panel |
| CLI | Typer | `mailosh …` |
| Proxy/TLS (web) | Caddy (optional container) or user's own proxy; mail-protocol TLS via Stalwart ACME | settle exact split in P0 (SPK-4) |
| License | AGPL-3.0 + DCO sign-offs | CLA decision before outside contributions grow |

Rejected on record: writing our own SMTP/IMAP/JMAP core (Stalwart exists, AGPL, Thundermail's engine); Next.js/React (conflicts with stack and simplicity); Redis/ClamAV in the default profile; `bleach` (EOL 2026-06-05); `aioimaplib`/`jmapc` as dependencies (GPL).

## 5. Data model

**Mail data:** JMAP objects pass through; Mailosh persists none of it. Canonical concepts map as: label = Mailbox (nesting via `parentId`, order via `sortOrder`); star = `$flagged`; unread = no `$seen`; spam-report = `$junk` + move to Junk role mailbox (trains Stalwart); archive = remove Inbox id from `mailboxIds`; snooze = custom keyword `snoozed` + due row (phase 4); thread = JMAP `Thread`.

**Postgres tables (v1):**
- `app_user(id, stalwart_principal, display_name, created_at, is_admin)`
- `session(id, app_user_id, created_at, expires_at, csrf_secret)` — server-side sessions, secure cookies
- `credential_vault(app_user_id, ciphertext, nonce, kdf_meta)` — see §9 auth
- `label_meta(app_user_id, account_id, mailbox_id, color, hidden, pinned)`
- `ui_pref(app_user_id, key, value_json)`
- `domain_health(domain, check_name, status, detail, checked_at)`
- `backup_run(id, started_at, finished_at, status, size_bytes, location)`
- `audit_log(id, actor, action, target, at, detail_json)`
- Procrastinate's own schema for jobs.

## 6. Real-time sync design (the "fast like Gmail" answer)

**Pipeline:** SMTP accept → Stalwart indexes and emits JMAP `StateChange` → Mailosh's per-user EventSource listener (one per active user, started on first session activity, stopped after idle timeout) → SSE hub fans out to that user's browser tabs → HTMX `sse-swap` patches the DOM (prepend thread row, bump counts via `hx-swap-oob`, toast).

**Budgets (v1 acceptance targets, same-box deployment):**
- New mail visible in an open inbox: **< 2 s** from SMTP acceptance (expected typical < 500 ms).
- Open a thread: **< 300 ms** server time; one batched JMAP request (`Email/get` with `Thread/get` back-reference).
- Inbox page render: **one** JMAP round trip (`Email/query` → `Email/get` via result references, `collapseThreads: true`), **< 400 ms** server time at 100k messages.
- Action feedback (archive/star/read): **0 ms perceived** — Alpine applies the change optimistically, POST returns the canonical partial; on failure revert + toast. Archive/delete get a Gmail-style inline Undo (client-side timer + reverse op; protocol-level undo in phase 4).

**Mechanics:** state strings cached per (user, account, type) in-process; `Email/changes`/`Mailbox/changes` re-sync on SSE reconnect (`Last-Event-ID`); heartbeat comment every 25 s; exponential backoff reconnect to Stalwart; HTTP/2 termination so per-tab SSE streams don't exhaust the 6-connection budget; graceful degradation to 30 s polling if the EventSource cannot connect (logged as a health warning). WebSocket (RFC 8887) is the named upgrade if SSE ever limits us — not in v1.

## 7. Search

Delegated to Stalwart FTS through `Email/query`. `mailosh.search` parses Gmail syntax — `from: to: cc: bcc: subject: label:/in: is:unread/read/starred has:attachment before:/after: older_than:/newer_than: larger:/smaller: list: "phrases" -negation OR ( )` — into JMAP `FilterOperator` trees (mapping table in the Blueprint §7). Snippets via `SearchSnippet/get`. Unsupported operators (`filename:`, `category:`) degrade to `text` with a UI hint. Parser is a hand-rolled recursive-descent tokenizer (~200 lines) with property-based tests; no third-party parser dependency.

## 8. Rendering and content security

- Server pipeline: raw HTML part → `nh3` allowlist sanitize with `attribute_filter` rewriting `cid:` → part URLs and remote `src`/`href` → blocked placeholders (v1 blocks remote images by default; per-sender allow + proxy endpoint in phase 4).
- Delivery: message body served from a dedicated path with `Content-Security-Policy: sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; img-src data: 'self'; style-src 'unsafe-inline'` inside `<iframe sandbox>` (never `allow-scripts` + `allow-same-origin`). The mox/Proton recipe.
- Dark mode: render original by default with a per-message "dark-adapt" toggle (Proton-style contrast check), never silent transformation.
- Plain-text parts rendered with linkification only. Attachments streamed via JMAP `Blob` download with `Content-Disposition: attachment` unless previewable type.

## 9. Security

- **Web auth:** username + password (verified against Stalwart) creating a server-side session; TOTP in phase 2, passkeys in phase 2; strict CSRF (per-session token, double-submit), SameSite=Lax, Secure cookies; login rate limiting (per-IP + per-account, Postgres-backed).
- **Auth to Stalwart (SPK-3, settled in P0):** design independently validated by ihasmail's production implementation (AES-256-GCM sealing with an HKDF key from per-session cookie secret + app secret — `server/src/crypto.ts`). Preferred — per-user OAuth/API token minted via Stalwart's management API at first login and stored envelope-encrypted (`credential_vault`, XChaCha20-Poly1305, master key from `MAILOSH_SECRET_KEY` env, account id as AAD). Fallback if unsupported — session-scoped encrypted credential (key half in cookie, half server-side; nothing durable). The spike verifies which Stalwart offers.
- Admin actions audited (`audit_log`); wizard writes secrets only to env/config with 0600; no telemetry (opt-in only, phase 3+); dependency pinning + `pip-audit` in CI.

## 10. Storage profiles, backup, updates

- `STORAGE_DRIVER=local` (default): Stalwart filesystem blobs + RocksDB under `/data`; `STORAGE_DRIVER=s3`: endpoint/bucket/keys envs → wizard writes Stalwart blob config (docs default to Backblaze B2; Hetzner EU; IDrive e2 Asia; OVH Mumbai / DO Bangalore for India residency; R2 hobby tier; never Zoho WorkDrive/consumer drives; SeaweedFS or Garage for on-prem S3).
- `SMTP_MODE=direct|relay`: relay = Stalwart smarthost config (host, port, credentials); wizard probes outbound 25 and recommends relay when blocked.
- **Backup:** `mailosh backup` → quiesce → Postgres dump + Stalwart data snapshot + blob sync (rclone for S3, tar for local) + config → one timestamped tarball, optional S3 target; scheduled by worker; `mailosh restore <tarball>` documented and exercised in CI against a disposable stack (the restore drill).
- **Updates:** `docker compose pull && docker compose up -d`; Alembic migrations run on boot with lock; release notes per tag; images published for amd64+arm64.

## 11. Setup wizard, DNS, health

- Wizard steps: admin account → domain → hostname (`mail.example.com`) → storage → SMTP mode (probe) → summary. Creates Stalwart domain + DKIM keys via management API.
- DNS page per domain: MX, SPF (`v=spf1 mx ~all`), DKIM (from Stalwart), DMARC (`p=none` → guided upgrade path), rDNS/PTR instruction with provider-specific notes; copy-paste values + BIND zone export; live verification (green/amber) via dnspython against public resolvers.
- Health panel checks: ports 25/465/587/143/993/443 listening; outbound 25 reachability; cert expiry; queue depth (management API); disk usage; rDNS ↔ HELO match; SPF/DKIM/DMARC/MX presence; each failure links to a fix page. Deliverability score = weighted sum, per domain.

## 12. Deployment profile and sizing

- Reference box: 2 vCPU / 4 GB RAM / 80 GB NVMe VPS runs all containers for a family/small-team install (tens of mailboxes). RAM budget: Stalwart ≲ 512 MB, Postgres tuned small (`shared_buffers=256MB`), uvicorn workers ×2 ≈ 300 MB, worker ≈ 150 MB, headroom for FTS bursts. (Provider/pricing guidance lives in docs, not code; see hosting doc.)
- Requirements on the operator: a domain, ability to set MX/TXT/PTR, ports 25/443 (or relay mode).
- dev: `docker compose --profile dev up` with mailpit-style test flows against Stalwart directly; CI: compose-based e2e (send SMTP → assert SSE event → assert rendered row).

## 13. Phases

P0 spike (1–2 wk) → P1 real mail server on a VPS (3–4 wk) → P2 Gmail-class webmail (6–8 wk) → P3 platform ops = launch gate, v1.0 on Stalwart 1.0 → P4 Gmail parity (snooze, schedule/undo send, Sieve filters UI, vacation, unsubscribe, image proxy) → P5 importers, connectors/unified inbox, calendar/contacts → Cloud track separately. Full phase content and exit criteria: Blueprint §11; the P0 plan is a separate document under `docs/plans/`.

## 14. Spike questions P0 must answer (gates)

- **SPK-1 (HTMX gate):** Squire + Alpine compose island inside the HTMX page — acceptable DX and UX? Fallback: Datastar or one Preact island; server contract unchanged.
- **SPK-2:** `Email/import` semantics on Stalwart (multi-`mailboxIds`, `receivedAt` preserved, threading applied) — needed for Takeout import later; verify now while modelling.
- **SPK-3:** Stalwart per-user API tokens / OAuth for the credential design in §9.
- **SPK-4:** ACME split — Stalwart's native ACME for mail + Caddy for web, or Stalwart terminating web TLS too.
- **SPK-5:** Management API coverage for wizard needs (create domain, create account, read DKIM public key, queue stats).
- **SPK-6:** Real-time budget measurement (§6) on the reference box.

## 15. Risks

Stalwart pre-1.0 schema changes (pin; v1.0 rides 1.0; standards are the escape hatch) · Stalwart's own webmail post-1.0 (moat = platform ops + later connectors) · deliverability/port-25 reality (probe + relay default suggestion; SES docs) · HTMX compose friction (SPK-1 fallback) · solo-maintainer bus factor (docs + tests from P0; boring dependencies) · name collision ("Mailosh" HP legacy — check before public launch).

## 16. Reference implementation: ihasmail

[Coffey-Labs/ihasmail](https://github.com/Coffey-Labs/ihasmail) (AGPL-3.0-or-later; React/Node SPA for Stalwart, reviewed 2026-08-31) is adopted as Mailosh's primary field reference — not a fork base (zero stack overlap; 33k lines of TypeScript + Node toolchain our constraints forbid; bus factor of one). Its value: `KNOWN-ISSUES.md` documents live-verified Stalwart spec deviations (synthetic-ID renumbering, per-account capability advertising, silent scheduled-send drop without FUTURERELEASE, proxy gzip/content-length trap, signature length cap); its JMAP client, sanitizer policy, SSRF-proof image proxy, credential sealing and EventSource relay are proven designs to port. License is compatible with ours: ported code carries an attribution comment and an entry in `NOTICE` (created at first port). Their shadow-DOM + CSS-hardening rendering approach is noted as the alternative to our §8 iframe sandbox; we keep the iframe (defense in depth), and may add their CSS-neutralisation rules inside it.

## 17. Open items (do not block P0)

License file text final (AGPL-3.0 assumed) · name/trademark check before launch · GitHub org vs personal repo at publish time · CLA decision when outside contributions arrive.
