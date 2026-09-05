# Phase 1A — Foundation Implementation Plan

> Implement this plan task by task, in order. Steps use checkbox (`- [ ]`) syntax so progress can be tracked in place.

**Goal:** Turn the Phase 0 prototype into the foundation of the real webmail: real users with login and sessions, the design system (Graphite & Blue, Inter, three densities, light/dark), the list-first Gmail-frame shell with fully-featured rows, keyboard-first triage with undo, a ⌘K palette, per-user live updates, and a UI that never shows a raw error.

**Architecture:** FastAPI routers stay thin and call `services/` view-model builders over the Phase 0 `JmapClient` (one batched JMAP request per view). Postgres (SQLAlchemy 2 async + Alembic) holds app state only: users, sessions (with the user's Fernet-encrypted per-session Stalwart API key), label metadata, UI prefs, audit. Templates are Jinja2 partials swapped by HTMX with idiomorph; Alpine stores hold selection/focus/toast state; a ~120-line vanilla keyboard registry drives dispatch, tooltips and the `?` overlay; a per-user SSE hub relays Stalwart push through an in-house EventSource bridge.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (asyncpg / aiosqlite for unit tests), Alembic, cryptography (Fernet/HKDF), httpx, Jinja2, Tailwind 4 standalone CLI, HTMX 2.0.10 + idiomorph 0.7.4 + htmx-ext-preload 2.1.2, Alpine 3.17, command-score (MIT), Lucide icons, Inter 4.x variable (subset with fonttools), Stalwart v0.16.x, PostgreSQL 16.

**Spec:** `docs/specs/2026-09-02-phase1-webmail-design.md` (binding; read §2, §4–6, §9, §12–14 first). Phase 0 spec `2026-08-31-mailosh-design.md` still governs the JMAP client. Research facts: `docs/research/2026-09-02-webmail-ux-research.md`. Visual reference: `docs/design/mockups/*.html`.

## Global Constraints

- Python ≥ 3.12; AGPL-3.0-or-later; no GPL/AGPL Python dependencies; no Redis; no Node toolchain; all frontend assets vendored (`make vendor icons fonts css`), no runtime CDN.
- Mail content never touches Postgres — Postgres holds app state only (spec §12).
- Tokens exactly as spec §4.1 (light `--accent #1D6AE5`, dark `--accent #8AB4F8`, …); densities Compact 36 / Standard 44 / Comfortable 52 px with **Comfortable** as the shipped default; `data-theme` and `data-density` rendered server-side on `<html>`.
- Every mutation is `POST` + CSRF (`X-CSRF-Token` header from `<meta name="csrf-token">` via inherited `hx-headers`, or hidden field) and returns `204` with `HX-Trigger` JSON or a fragment; `HX-Request` alone is never trusted (spec §9).
- Undo = immediate commit + reverse op with a 10 s window (spec §6.3). Prefetched GETs (`HX-Preloaded: true`) never mark read (spec §6.4).
- No UI control for a deferred feature (spec §3): no Snoozed nav item, no snooze action, no schedule-send.
- Keyboard shortcuts on by default; the same registry drives dispatch, hover hints and the `?` overlay (spec §6.1).
- Zero-warning test output; `ruff check .` and `ruff format --check .` clean; conventional commits.
- **Commits carry no AI attribution of any kind** — no `Co-Authored-By` trailer, no session link, no "generated with" line. Every commit is authored solely by the person who wrote it.
- Browser QA in the last task compares against `docs/design/mockups/` and records screenshots + web-vitals in `docs/spikes/p1a-findings.md`.

---

## Reference: Phase 0 code you build on

`mailosh/jmap/client.py` — `JmapClient.connect(base_url, username, password)`, `_call`, `_call_raw`, `get_mailboxes`, `query_inbox`, `get_thread`, `set_keyword`, `move`, `upload`, `import_email`, `create_mailbox`, `get_identity`, `send`, `event_stream`, module-level `find_inbox(mailboxes)`; models in `mailosh/jmap/models.py` (`Mailbox`, `EmailHeader` with `from_`, `keywords: set[str]`, `mailbox_ids: set[str]`, `preview`, `has_attachment`, `received_at`, `thread_id`; `EmailBody`; `StateChange`); errors `JmapError`, `MethodError`, `TransportError`. `mailosh/stalwart_admin.py` — `StalwartAdmin(base_url, admin_user, admin_secret)` with `create_domain`, `create_account -> bool`, `get_dkim_record`, `try_mint_user_token` (uses `x:ApiKey/set` — see `docs/spikes/p0-findings.md` SPK-3 for the exact request shape). `mailosh/sse.py` — `SseHub`, `stalwart_listener`, `is_mail_change`. `mailosh/web/app.py` — `create_app(start_listener=True)`, routes `/inbox`, `/inbox/rows`, `/thread/{id}`, `/compose`, `/events`, `/email/{id}/keyword|archive`; `mailosh/web/deps.py::get_client`. Tests: 115 unit + 1 integration (`make test`, `make itest`), fixtures in `tests/conftest.py` (`FAKE_INBOX`, `FAKE_ROW`, `FAKE_THREAD`, respx `client` fixture).

## File structure (locked for 1A)

```
pyproject.toml                         + sqlalchemy[asyncio], asyncpg, alembic, cryptography, aiosqlite(dev), fonttools[woff](dev)
Makefile                               + icons, fonts, db-upgrade targets; test/up depend on them
alembic.ini, migrations/env.py, migrations/versions/0001_foundation.py
docker-compose.yml                     + MAILOSH_DATABASE_URL, MAILOSH_SECRET_KEY, postgres healthcheck, entrypoint runs migrations
docker/entrypoint.sh
NOTICE                                 − htmx-ext-sse  + idiomorph, htmx-ext-preload, command-score, Lucide, Inter
mailosh/config.py                     + database_url, secret_key, cookie_secure, session_* days; demo_* become Optional
mailosh/db/__init__.py, base.py, models.py, session.py, repo.py
mailosh/security/__init__.py, crypto.py, csrf.py, ratelimit.py, sessions.py, exchange.py
mailosh/stalwart_admin.py             + create_api_key(username, name) -> ApiKey, destroy_api_key(id)
mailosh/jmap/client.py                + connect_bearer(base_url, token), query_page(...) batched 4-call chain, set_mailboxes(...)
mailosh/jmap/pool.py                  ClientPool (per-session clients, idle eviction)
mailosh/sse.py                        HubRegistry (per-user hubs + listeners), keep SseHub/stalwart_listener
mailosh/ui/__init__.py, env.py (Jinja env, globals), macros.py (icon), format.py (dates, senders, initials, avatar_color), static.py (versioned URLs)
mailosh/services/__init__.py, mailbox_tree.py, thread_list.py, actions.py, undo.py
mailosh/web/app.py                    create_app: middleware, exception handlers, routers, lifespan (pool + hub registry)
mailosh/web/deps.py                   current_session, current_user, client_for(session), csrf_protect, db_session
mailosh/web/auth.py                   /login GET+POST, /logout, /logout/all
mailosh/web/mail.py                   /, /mail/{key}, /mail/{key}/rows, /t/{thread_id} (adapted P0 view)
mailosh/web/actions.py                /a/archive|delete|spam|star|read|undo (single + bulk)
mailosh/web/palette.py                /palette/index (JSON)
mailosh/web/prefs.py                  /prefs (POST theme/density/shortcuts)
mailosh/web/events.py                 /events (per-user SSE)
mailosh/web/templates/layouts/app.html, layouts/bare.html
mailosh/web/templates/auth/login.html
mailosh/web/templates/shell/topbar.html, nav.html, quick_settings.html, shortcuts_dialog.html, palette.html
mailosh/web/templates/list/page.html, rows.html, row.html, toolbar.html, empty.html, skeleton.html
mailosh/web/templates/thread/page.html  (P0 thread view re-skinned; 1B rebuilds)
mailosh/web/templates/fragments/toast.html, status.html, offline.html, error_page.html
mailosh/web/static/js/app.js, keys.js, palette.js, sse.js, actions.js
mailosh/web/static/vendor/            htmx.min.js, idiomorph-ext.min.js, preload.js, alpine.min.js, squire.js, purify.min.js, command-score.js
mailosh/web/static/icons/*.svg        (make icons; gitignored)
mailosh/web/static/fonts/inter-latin.woff2 (make fonts; gitignored)
styles/input.css                       tokens + components
tests/unit/test_db_models.py, test_crypto.py, test_csrf.py, test_ratelimit.py, test_sessions.py, test_exchange.py, test_format.py,
tests/unit/test_mailbox_tree.py, test_thread_list.py, test_auth_routes.py, test_mail_routes.py, test_actions.py, test_undo.py, test_palette.py, test_prefs.py, test_errors.py, test_ui_macros.py
tests/integration/test_live_auth_flow.py
docs/spikes/p1a-findings.md
```

Interfaces every later task relies on (exact):

```python
# mailosh/security/crypto.py
def derive_key(secret_key: str, purpose: bytes) -> bytes            # 32-byte urlsafe-b64 Fernet key via HKDF-SHA256
def encrypt(secret_key: str, plaintext: str) -> bytes                 # Fernet token
def decrypt(secret_key: str, token: bytes) -> str

# mailosh/security/csrf.py
def new_token() -> str
def validate(request: Request, session_token: str) -> None           # raises HTTPException(403)

# mailosh/security/sessions.py
COOKIE_SECURE_NAME = "__Host-sid"; COOKIE_PLAIN_NAME = "sid"
def cookie_name(settings) -> str
def cookie_params(settings) -> dict
async def create_session(db, *, user: AppUser, remember: bool, user_agent: str|None, ip: str|None,
                         api_key_id: str, api_key_secret: str, settings) -> SessionRow
async def load_session(db, sid: str, settings, now: datetime|None=None) -> SessionRow|None   # touches last_seen; enforces idle+absolute
async def revoke_session(db, sid: str) -> SessionRow|None
async def revoke_all(db, user_id: int) -> list[SessionRow]
def api_key_secret(session: SessionRow, settings) -> str            # decrypt

# mailosh/security/ratelimit.py
class LoginLimiter:
    async def retry_after(self, db, *, ip: str, account: str, now=None) -> int   # seconds, 0 = allowed
    async def record_failure(self, db, *, ip: str, account: str, now=None) -> None
    async def reset(self, db, *, ip: str, account: str) -> None

# mailosh/security/exchange.py
@dataclass class VerifiedAccount: username: str; account_id: str; email: str
async def verify_password(stalwart_url: str, username: str, password: str) -> VerifiedAccount | None

# mailosh/stalwart_admin.py
@dataclass class ApiKey: id: str; secret: str
async def create_api_key(self, username: str, name: str) -> ApiKey
async def destroy_api_key(self, key_id: str) -> None

# mailosh/jmap/client.py additions
@classmethod async def connect_bearer(cls, base_url: str, token: str) -> "JmapClient"
async def query_page(self, *, mailbox_id: str|None, position: int, limit: int, exclude_mailbox_ids: set[str]=frozenset(), has_keyword: str|None=None) -> QueryPage
    # ONE request: Email/query(collapseThreads, calculateTotal) -> Email/get(threadId) -> Thread/get -> Email/get(list props of ALL emails in those threads)
    # QueryPage(thread_order: list[str], total: int, emails_by_thread: dict[str, list[EmailHeader]], position: int)
    # AS-BUILT: `has_keyword` was added in Task 6 for the `starred` nav key and is keyword-only
    # and additive. Ruling recorded at the time: preferred over a pass-through filter dict, which
    # would have lost type safety at every call site.
async def set_mailboxes(self, email_ids: list[str], *, add: set[str]=frozenset(), remove: set[str]=frozenset()) -> None   # bulk patch, raises on any notUpdated
async def set_keywords(self, email_ids: list[str], keyword: str, on: bool) -> None

# mailosh/jmap/pool.py
class ClientPool:
    async def get(self, session: SessionRow, settings) -> JmapClient
    async def drop(self, session_id: str) -> None
    async def close_all(self) -> None

# mailosh/sse.py
class HubRegistry:
    def hub_for(self, user_id: int) -> SseHub
    async def ensure_listener(self, user_id: int, client: JmapClient) -> None
    async def stop_idle(self, idle_seconds: int = 1800) -> None
    async def close(self) -> None

# mailosh/ui/format.py
def format_date(dt: datetime, now: datetime) -> str                 # "10:42 AM" | "Sep 1" | "9/1/25"
def format_senders(emails: list[EmailHeader], me: str) -> str        # "Aisha, Tom, me (3)"
def initials(name: str|None, email: str) -> str
def avatar_color(email: str) -> int                                  # 0..11 into the label palette

# mailosh/services/mailbox_tree.py
@dataclass class NavItem: key: str; label: str; icon: str; mailbox_id: str|None; count: int|None; active: bool
@dataclass class LabelNode: mailbox_id: str; name: str; color: str|None; count: int; visibility: str; children: list["LabelNode"]
@dataclass class NavModel: system: list[NavItem]; more: list[NavItem]; labels: list[LabelNode]; inbox_id: str
async def build_nav(client, *, active_key: str, label_meta: dict[str, LabelMetaRow]) -> NavModel
def resolve_mailbox(nav: NavModel, key: str) -> str | None          # "inbox"|"sent"|"drafts"|"starred"|"all"|"archive"|"spam"|"trash"|<mailbox id>

# mailosh/services/thread_list.py
@dataclass class LabelChip: mailbox_id: str; name: str; color: str
@dataclass class ThreadRow: thread_id: str; email_ids: list[str]; latest_email_id: str; senders: str; count: int; subject: str; preview: str;
                             date_display: str; received_at: datetime; unread: bool; starred: bool; has_attachment: bool; chips: list[LabelChip]
@dataclass class ThreadPage: rows: list[ThreadRow]; position: int; limit: int; total: int; next_position: int|None
async def build_page(client, *, mailbox_key: str, nav: NavModel, position: int, limit: int, me: str, now: datetime) -> ThreadPage

# mailosh/services/actions.py
async def archive(client, nav, email_ids) -> UndoSpec; delete(...); spam(...); star(client, email_ids, on) -> UndoSpec; mark_read(client, email_ids, on) -> UndoSpec
# mailosh/services/undo.py
@dataclass class UndoSpec: kind: str; email_ids: list[str]; add: list[str]; remove: list[str]; keyword: str|None; on: bool|None; toast: str
def sign(spec: UndoSpec, secret_key: str, now=None) -> str          # HMAC token, 60 s TTL
def verify(token: str, secret_key: str, now=None) -> UndoSpec        # raises ValueError
async def apply(client, spec: UndoSpec) -> None                      # performs the reverse op
```

---

### Task 1: Postgres models, migrations, config, compose wiring

**Files:**
- Modify: `pyproject.toml` (deps + dev deps), `mailosh/config.py`, `docker-compose.yml`, `Makefile`, `.env.example`, `.gitignore` (nothing new), `README.md` (quick start adds `make db-upgrade`)
- Create: `mailosh/db/__init__.py`, `mailosh/db/base.py`, `mailosh/db/models.py`, `mailosh/db/session.py`, `mailosh/db/repo.py`, `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_foundation.py`, `docker/entrypoint.sh`
- Test: `tests/unit/test_db_models.py`, `tests/conftest.py` (add `db` fixture)

**Interfaces:**
- Produces: `Settings.database_url` (default `postgresql+asyncpg://mailosh:mailosh@localhost:5432/mailosh`), `Settings.secret_key: str` (required, ≥ 32 chars), `Settings.cookie_secure: bool = True`, `Settings.session_idle_days=14`, `session_remember_days=30`, `session_absolute_days=90`, `Settings.demo_user: str|None`, `Settings.demo_password: str|None`; models `AppUser, SessionRow (table "session"), LabelMeta, UiPref, Contact, ImageSenderAllow, LoginAttempt, AuditLog` (spec §12 list); `mailosh.db.session.make_engine(url)`, `make_sessionmaker(engine)`, `get_db()` dependency; `mailosh.db.repo` helpers `get_or_create_user(db, username, email) -> AppUser`, `get_prefs(db, user_id) -> UiPref` (creates defaults), `set_prefs(db, user_id, **fields)`, `label_meta_map(db, user_id, account_id) -> dict[str, LabelMeta]`, `audit(db, user_id, action, detail, ip)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_db_models.py
import pytest
from sqlalchemy import select
from mailosh.db import models, repo

async def test_schema_creates_and_user_roundtrip(db):
    user = await repo.get_or_create_user(db, "demo@mailosh.test", "demo@mailosh.test")
    again = await repo.get_or_create_user(db, "demo@mailosh.test", "demo@mailosh.test")
    assert user.id == again.id
    prefs = await repo.get_prefs(db, user.id)
    assert (prefs.theme, prefs.density, prefs.shortcuts) == ("system", "comfortable", True)
    await repo.set_prefs(db, user.id, theme="dark", density="compact")
    prefs = await repo.get_prefs(db, user.id)
    assert (prefs.theme, prefs.density) == ("dark", "compact")

async def test_label_meta_map_keyed_by_mailbox(db):
    user = await repo.get_or_create_user(db, "u", "u@x")
    db.add(models.LabelMeta(user_id=user.id, account_id="a", mailbox_id="m1", color="indigo", visibility="show"))
    await db.commit()
    m = await repo.label_meta_map(db, user.id, "a")
    assert m["m1"].color == "indigo"
```

conftest addition (aiosqlite so unit tests need no Postgres):

```python
# tests/conftest.py (append)
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from mailosh.db.base import Base

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails** — `make test` → ImportError on `mailosh.db`.

- [ ] **Step 3: Implement.** Deps: `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.30`, `alembic>=1.13`, `cryptography>=43`; dev: `aiosqlite>=0.20`, `fonttools[woff]>=4.50`. Models use dialect-neutral types only (`String`, `Text`, `Integer`, `Boolean`, `DateTime(timezone=True)`, `LargeBinary`, `JSON`). Tables and columns exactly:
  - `app_user(id PK autoincrement, stalwart_username String(255) unique not null, email String(255) not null, display_name String(255) null, created_at, last_login_at null, is_admin Boolean default false)`
  - `session(id String(64) PK, user_id FK app_user.id ondelete cascade, created_at, last_seen_at, expires_at, remember Boolean, user_agent Text null, ip String(64) null, api_key_id String(64) null, api_key_secret_enc LargeBinary not null, csrf_token String(64) not null)` — index on `user_id`
  - `label_meta(user_id FK, account_id String(64), mailbox_id String(64), color String(32) null, visibility String(16) default 'show', sort_order Integer null)` PK (user_id, account_id, mailbox_id)
  - `ui_pref(user_id PK FK, theme String(16) default 'system', density String(16) default 'comfortable', reading_pane String(16) default 'none', conversation_view Boolean default true, mark_read_delay Integer default 0, auto_advance String(16) default 'older', undo_send_seconds Integer default 10, remote_images String(16) default 'ask', dark_restyle Boolean default true, shortcuts Boolean default true, font_size String(8) default 'md')`
  - `contact(user_id FK, email String(255), name String(255) null, last_seen_at, count Integer default 1)` PK (user_id, email)
  - `image_sender_allow(user_id FK, sender_email String(255))` PK composite
  - `login_attempt(key String(255) PK, failures Integer default 0, window_start DateTime, locked_until DateTime null)`
  - `audit_log(id PK, user_id FK null, action String(64), detail JSON null, ip String(64) null, at DateTime)`
  `db/session.py`: `make_engine(url, echo=False)`, `make_sessionmaker(engine)`; `get_db` yields an `AsyncSession` from `request.app.state.sessionmaker`. Alembic: async `env.py` (uses `Settings().database_url`), `0001_foundation` creating all of the above (write it by hand mirroring the models — do not rely on autogenerate output blindly; run `alembic upgrade head` against the compose Postgres and paste the output into the report). `Settings`: fields per Interfaces; `demo_user`/`demo_password` become `str | None = None` (update `tests/unit/test_config.py` and the P0 integration test/CLI to skip when unset). Compose: `postgres` gets `healthcheck: pg_isready -U mailosh`; `mailosh` gets `MAILOSH_DATABASE_URL=postgresql+asyncpg://mailosh:mailosh@postgres:5432/mailosh`, depends on both healthy, and `command: ["/app/docker/entrypoint.sh"]` which runs `alembic upgrade head` then uvicorn. `.env.example` adds `MAILOSH_SECRET_KEY=change-me-to-32-plus-random-characters` and `MAILOSH_COOKIE_SECURE=false` (dev); generate a real one into the local `.env` (`openssl rand -hex 32`). Makefile: `db-upgrade: .venv/bin/alembic upgrade head`.

- [ ] **Step 4: Run tests** — `make test` → all green incl. the two new tests; `make db-upgrade` against compose Postgres succeeds; `docker compose up -d --build mailosh` boots and logs the migration.
- [ ] **Step 5: Commit** — `feat(db): postgres models, alembic baseline, secret/session settings`

---

### Task 2: Design system — tokens, fonts, icons, macros, base layout, versioned static

**Files:**
- Create: `mailosh/ui/__init__.py`, `mailosh/ui/env.py`, `mailosh/ui/macros.py`, `mailosh/ui/static.py`, `mailosh/ui/format.py` (skeleton for `initials`/`avatar_color` only; dates/senders in Task 6), `mailosh/web/templates/layouts/app.html`, `layouts/bare.html`, `fragments/status.html`
- Modify: `styles/input.css`, `Makefile` (`icons`, `fonts` targets; `css` depends on them; `test`/`up` depend on `vendor icons fonts css`), `.gitignore` (`mailosh/web/static/icons/`, `mailosh/web/static/fonts/`), `NOTICE`
- Test: `tests/unit/test_ui_macros.py`

**Interfaces:**
- Produces: `mailosh.ui.env.build_env(static_dir) -> jinja2.Environment` with globals `icon(name, class_="size-4", label=None)`, `static(path) -> "/static/{path}?v={sha256[:8]}"`, `kbd(keys: str)`; filters `initials`, `avatar_color`; `layouts/app.html` blocks `title`, `topbar`, `nav`, `main`, `dock`, `scripts`, taking context `prefs` (theme/density/shortcuts), `csrf_token`, `user`; `<html data-theme="{{ prefs.theme }}" data-density="{{ prefs.density }}">`; `<body hx-ext="morph, preload" hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'>`; permanent `<div id="status" role="status" aria-live="polite" aria-atomic="true"></div>` and `<div id="toasts">`; `<div id="compose-dock" hx-history="false">`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_ui_macros.py
from pathlib import Path
from mailosh.ui.env import build_env

STATIC = Path("mailosh/web/static")

def test_icon_inlines_svg_with_class_and_aria():
    env = build_env(STATIC)
    html = env.globals["icon"]("archive", class_="size-4")
    assert html.startswith("<svg") and 'class="size-4' in html and 'aria-hidden="true"' in html
    assert "stroke=\"currentColor\"" in html

def test_static_is_content_versioned():
    env = build_env(STATIC)
    url = env.globals["static"]("js/app.js")
    assert url.startswith("/static/js/app.js?v=") and len(url.split("v=")[1]) == 8

def test_app_layout_renders_theme_density_and_csrf():
    env = build_env(STATIC)
    t = env.from_string('{% extends "layouts/app.html" %}{% block main %}<p>hi</p>{% endblock %}')
    html = t.render(prefs={"theme": "dark", "density": "compact", "shortcuts": True}, csrf_token="tok123", user={"email": "d@x"}, nav=None)
    assert 'data-theme="dark"' in html and 'data-density="compact"' in html
    assert '<meta name="csrf-token" content="tok123">' in html and 'X-CSRF-Token' in html
    assert 'role="status"' in html and 'id="compose-dock"' in html
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
  - **Makefile `icons`:** download the Lucide SVGs listed in `mailosh/ui/icons.txt` (archive, archive-restore, arrow-left, arrow-right, at-sign, bold, chevron-down, chevron-left, chevron-right, circle-help, clock, corner-up-left, corner-up-right, ellipsis-vertical, file, file-text, forward, inbox, italic, link, list, list-ordered, mail, mail-open, menu, minus, paperclip, pen-line, plus, quote, refresh-cw, reply, reply-all, search, send, settings-2, shield-alert, sliders-horizontal, star, tag, trash-2, triangle-alert, underline, x, zap) from `https://cdn.jsdelivr.net/npm/lucide-static@1.39.0/icons/<name>.svg` into `mailosh/web/static/icons/` (skip if present). **`fonts`:** download `https://rsms.me/inter/font-files/InterVariable.woff2` to a build dir and subset with `.venv/bin/pyftsubset … --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --flavor=woff2 --layout-features='*' --output-file=mailosh/web/static/fonts/inter-latin.woff2`; record the resulting size in the report (target ≈ 50–120 KB; if larger, drop `--layout-features='*'` to `kern,liga,calt,tnum`).
  - **`styles/input.css`:** `@import "tailwindcss";` then the token blocks exactly per spec §4.1 (`:root` light; `:root[data-theme=dark]`; `@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) {…} }`), `@theme inline { --color-bg: var(--bg); --color-surface: var(--surface); --color-field: var(--field); --color-read: var(--read); --color-hover: var(--hover); --color-line: var(--line); --color-line-2: var(--line-2); --color-fg: var(--fg); --color-fg-2: var(--fg-2); --color-fg-3: var(--fg-3); --color-accent: var(--accent); --color-accent-ink: var(--accent-ink); --color-accent-soft: var(--accent-soft); --color-on-accent: var(--on-accent); --color-star: var(--star); --color-danger: var(--danger); --color-warn: var(--warn); --color-success: var(--success); --font-sans: "Inter", "Inter Fallback", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; --default-transition-duration: 120ms; --default-transition-timing-function: cubic-bezier(.2,0,0,1); --radius-kbd: 4px; --radius-ctl: 6px; --radius-field: 8px; --radius-card: 10px; --radius-palette: 12px; }`, density variables `:root, [data-density=comfortable] { --row-h: 52px; --row-lines: 2 } [data-density=standard] { --row-h: 44px; --row-lines: 1 } [data-density=compact] { --row-h: 36px; --row-lines: 1 }`, `@font-face` for Inter variable (`font-weight: 100 900; font-display: swap; src: url("/static/fonts/inter-latin.woff2") format("woff2")`) and the metric fallback face `"Inter Fallback"` (`src: local("Arial"); size-adjust: 107.47%; ascent-override: 90.14%; descent-override: 22.44%; line-gap-override: 0%`), base layer (`html { color-scheme: light } [data-theme=dark] { color-scheme: dark }`, body 13px/1.4 `--font-sans` antialiased, `:focus-visible` ring, `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition-duration: 0s !important; animation-duration: 0s !important } }`), and a `@layer components` with `.kbd`, `.btn-icon` (32 px), `.chip`, `.nav-item`, `.nav-item[aria-current=page]`, `.row` (uses `--row-h`), `.row.is-unread`, `.row.is-focused` (inset 3 px accent), `.row.is-selected`, `.toast`.
  - **`ui/env.py`:** `build_env(static_dir)` → `jinja2.Environment(loader=FileSystemLoader("mailosh/web/templates"), autoescape=select_autoescape(["html"]))` + globals/filters; `icon()` reads `static/icons/{name}.svg` (`functools.lru_cache`), injects `class`, `aria-hidden="true"` (or `role="img" aria-label=label`), returns `Markup`; `static()` hashes file bytes at first call (cache); `kbd("g i")` → `<kbd>g</kbd><kbd>i</kbd>`.
  - **`layouts/app.html`:** per Interfaces; head: `<meta charset>`, viewport, `<title>{% block title %}Mailosh{% endblock %}</title>`, `<meta name="theme-color">` per theme, `<link rel="preload" as="font" type="font/woff2" crossorigin href="{{ static('fonts/inter-latin.woff2') }}">`, `<link rel="stylesheet" href="{{ static('app.css') }}">`, `<meta name="csrf-token">`; scripts at end: vendor htmx, idiomorph-ext, preload, command-score, alpine (defer), then `js/keys.js`, `js/actions.js`, `js/palette.js`, `js/sse.js`, `js/app.js` (all `type="module"` except vendor). Skip link `<a class="sr-only focus:not-sr-only" href="#main">Skip to inbox</a>`.
  - **Makefile `vendor`:** add `idiomorph-ext.min.js` (`https://cdn.jsdelivr.net/npm/idiomorph@0.7.4/dist/idiomorph-ext.min.js`), `preload.js` (`https://cdn.jsdelivr.net/npm/htmx-ext-preload@2.1.2/dist/preload.min.js`), `command-score.js` (`https://raw.githubusercontent.com/superhuman/command-score/master/index.js` — wrap as `window.commandScore = …` if it is CommonJS: append `;window.commandScore = module.exports` after defining `var module = {}` in a tiny shim, or vendor as ESM by replacing `module.exports =` with `export default`); remove `sse.js` (htmx-ext-sse). **NOTICE:** remove htmx-ext-sse; add idiomorph 0.7.4 (0BSD), htmx-ext-preload 2.1.2 (0BSD), command-score (MIT, Superhuman Labs), Lucide 1.39.0 (ISC), Inter 4.x (SIL OFL 1.1).
- [ ] **Step 4: Run tests → PASS**; `make css` builds; `ls -la mailosh/web/static/fonts mailosh/web/static/icons | head` shows assets.
- [ ] **Step 5: Commit** — `feat(ui): design tokens, Inter + Lucide pipeline, jinja env, app layout`

---

### Task 3: Security primitives — crypto, CSRF, rate limiter, sessions (TDD)

**Files:**
- Create: `mailosh/security/__init__.py`, `crypto.py`, `csrf.py`, `ratelimit.py`, `sessions.py`
- Test: `tests/unit/test_crypto.py`, `test_csrf.py`, `test_ratelimit.py`, `test_sessions.py`

**Interfaces:** exactly the signatures in the Interfaces block.

- [ ] **Step 1: Failing tests (write all four files)**

```python
# tests/unit/test_crypto.py
from mailosh.security import crypto
def test_roundtrip_and_key_isolation():
    tok = crypto.encrypt("k" * 40, "api-secret")
    assert crypto.decrypt("k" * 40, tok) == "api-secret"
    assert crypto.derive_key("k" * 40, b"sessions") != crypto.derive_key("k" * 40, b"undo")
def test_wrong_key_fails():
    import pytest
    tok = crypto.encrypt("k" * 40, "x")
    with pytest.raises(ValueError):
        crypto.decrypt("j" * 40, tok)
```

```python
# tests/unit/test_csrf.py
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from mailosh.security import csrf

def _req(method="POST", headers=None):
    scope = {"type": "http", "method": method, "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()], "path": "/", "query_string": b""}
    return Request(scope)

def test_get_is_exempt():
    csrf.validate(_req("GET"), "t")

def test_header_token_matches():
    csrf.validate(_req(headers={"X-CSRF-Token": "t", "Sec-Fetch-Site": "same-origin"}), "t")

def test_missing_or_wrong_token_403():
    with pytest.raises(HTTPException) as e:
        csrf.validate(_req(headers={"X-CSRF-Token": "nope"}), "t")
    assert e.value.status_code == 403

def test_cross_site_rejected_even_with_token():
    with pytest.raises(HTTPException):
        csrf.validate(_req(headers={"X-CSRF-Token": "t", "Sec-Fetch-Site": "cross-site"}), "t")
```

```python
# tests/unit/test_ratelimit.py
from datetime import datetime, timedelta, timezone
from mailosh.security.ratelimit import LoginLimiter

async def test_account_lockout_after_five_failures(db):
    lim = LoginLimiter(); now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for _ in range(5):
        assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=now) == 0
        await lim.record_failure(db, ip="1.1.1.1", account="a@x", now=now)
    assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=now) >= 60
    assert await lim.retry_after(db, ip="2.2.2.2", account="a@x", now=now) >= 60      # account-keyed
    assert await lim.retry_after(db, ip="1.1.1.1", account="b@x", now=now) == 0       # other account ok
    later = now + timedelta(minutes=16)
    assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=later) == 0     # window expired

async def test_ip_lockout_after_twenty(db):
    lim = LoginLimiter(); now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for i in range(20):
        await lim.record_failure(db, ip="9.9.9.9", account=f"u{i}@x", now=now)
    assert await lim.retry_after(db, ip="9.9.9.9", account="fresh@x", now=now) >= 60

async def test_reset_clears(db):
    lim = LoginLimiter(); now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for _ in range(5): await lim.record_failure(db, ip="1.1.1.1", account="a@x", now=now)
    await lim.reset(db, ip="1.1.1.1", account="a@x")
    assert await lim.retry_after(db, ip="1.1.1.1", account="a@x", now=now) == 0
```

```python
# tests/unit/test_sessions.py
from datetime import datetime, timedelta, timezone
from mailosh.config import Settings
from mailosh.db import repo
from mailosh.security import sessions

def _settings(**kw):
    return Settings(stalwart_admin_secret="s", secret_key="k" * 40, cookie_secure=False, **kw)

async def test_create_load_touch_and_secret(db):
    s = _settings(); now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    row = await sessions.create_session(db, user=user, remember=False, user_agent="ua", ip="1.1.1.1", api_key_id="k1", api_key_secret="sekrit", settings=s)
    assert len(row.id) >= 32 and row.csrf_token and row.api_key_secret_enc != b"sekrit"
    loaded = await sessions.load_session(db, row.id, s, now=now)
    assert loaded.user_id == user.id and sessions.api_key_secret(loaded, s) == "sekrit"
    assert loaded.last_seen_at >= now - timedelta(seconds=1)

async def test_idle_and_absolute_expiry(db):
    s = _settings(session_idle_days=14, session_absolute_days=90)
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    row = await sessions.create_session(db, user=user, remember=False, user_agent=None, ip=None, api_key_id="k", api_key_secret="x", settings=s)
    created = row.created_at
    assert await sessions.load_session(db, row.id, s, now=created + timedelta(days=13)) is not None
    assert await sessions.load_session(db, row.id, s, now=created + timedelta(days=13 + 15)) is None   # idle > 14d since last touch
    row2 = await sessions.create_session(db, user=user, remember=True, user_agent=None, ip=None, api_key_id="k", api_key_secret="x", settings=s)
    assert await sessions.load_session(db, row2.id, s, now=row2.created_at + timedelta(days=91)) is None  # absolute cap

async def test_revoke_all_returns_api_key_ids(db):
    s = _settings(); user = await repo.get_or_create_user(db, "d@x", "d@x")
    a = await sessions.create_session(db, user=user, remember=False, user_agent=None, ip=None, api_key_id="ka", api_key_secret="x", settings=s)
    b = await sessions.create_session(db, user=user, remember=False, user_agent=None, ip=None, api_key_id="kb", api_key_secret="x", settings=s)
    revoked = await sessions.revoke_all(db, user.id)
    assert sorted(r.api_key_id for r in revoked) == ["ka", "kb"]
    assert await sessions.load_session(db, a.id, s) is None

def test_cookie_name_depends_on_secure():
    assert sessions.cookie_name(_settings(cookie_secure=True)) == "__Host-sid"
    assert sessions.cookie_name(_settings(cookie_secure=False)) == "sid"
    p = sessions.cookie_params(_settings(cookie_secure=True))
    assert p["httponly"] and p["samesite"] == "lax" and p["secure"] and p["path"] == "/"
```

- [ ] **Step 2: Run → FAIL (ImportError).**
- [ ] **Step 3: Implement.** `crypto`: HKDF-SHA256 (`cryptography.hazmat.primitives.kdf.hkdf.HKDF`, length 32, info=purpose) → `base64.urlsafe_b64encode` → `Fernet`; `decrypt` converts `InvalidToken` to `ValueError`. `csrf`: exempt `GET/HEAD/OPTIONS`; token from header `X-CSRF-Token` else form? (form parsing is async — accept an optional `form_token: str|None` parameter with default None so routes that parsed the form can pass it; the dependency in Task 5 handles both); `secrets.compare_digest`; reject when `Sec-Fetch-Site == "cross-site"`. `ratelimit`: rows keyed `acct:<lower>` and `ip:<ip>`; 15-minute window; thresholds 5 (account) / 20 (ip); on threshold, `locked_until = now + 60s * 2**(failures-threshold)` capped at 1 h; `retry_after` = max over both keys of `ceil((locked_until-now).seconds)`; windows older than 15 min reset. `sessions`: id `secrets.token_urlsafe(32)`, `csrf_token = secrets.token_urlsafe(32)`, `expires_at = created + absolute_days`; `load_session` returns None when `now > expires_at` or `now - last_seen_at > idle window` (idle = `session_remember_days` if `remember` else `session_idle_days`), otherwise updates `last_seen_at` (write at most once per 5 minutes to avoid write amplification) and commits; `api_key_secret_enc = crypto.encrypt(settings.secret_key, secret)`.
- [ ] **Step 4: Run → PASS, zero warnings.**
- [ ] **Step 5: Commit** — `feat(security): fernet crypto, csrf, login rate limiter, db-backed sessions`

---

### Task 4: Stalwart credential exchange — verify password, mint/destroy per-session API keys

**Files:**
- Create: `mailosh/security/exchange.py`
- Modify: `mailosh/stalwart_admin.py` (`create_api_key`, `destroy_api_key`), `mailosh/jmap/client.py` (`connect_bearer`)
- Test: `tests/unit/test_exchange.py`, extend `tests/unit/test_stalwart_admin.py`, `tests/integration/test_live_auth_flow.py` (part 1)

**Interfaces:** per the Interfaces block. `try_mint_user_token` stays for the CLI probe but delegates to `create_api_key`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_exchange.py
import json, pathlib, respx
from mailosh.security.exchange import verify_password
SESSION = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())

@respx.mock
async def test_valid_credentials_return_account():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    acct = await verify_password("http://s", "demo@mailosh.test", "pw")
    assert acct and acct.username == SESSION["username"] and acct.account_id == SESSION["primaryAccounts"]["urn:ietf:params:jmap:mail"]

@respx.mock
async def test_anonymous_session_is_rejected():
    anon = dict(SESSION, username="", accounts={}, primaryAccounts={})
    respx.get("http://s/.well-known/jmap").respond(json=anon)      # Stalwart returns 200 for anonymous
    assert await verify_password("http://s", "demo@mailosh.test", "wrong") is None

@respx.mock
async def test_401_is_rejected_not_raised():
    respx.get("http://s/.well-known/jmap").respond(401)
    assert await verify_password("http://s", "u", "p") is None
```

```python
# tests/unit/test_stalwart_admin.py (append) — shape from p0-findings SPK-3
@respx.mock
async def test_create_and_destroy_api_key(admin_url_mock):
    ... # mock POST /jmap: x:ApiKey/set create {"k0": {"name": "mailosh-session", "secrets": [...]?}} — use the EXACT request the spike recorded in
        # docs/spikes/p0-findings.md SPK-3 (copy the JSON); response created {"k0": {"id": "key-1", "secret": "API_abc"}}
    key = await admin.create_api_key("demo@mailosh.test", "mailosh-session")
    assert key.id == "key-1" and key.secret == "API_abc"
    await admin.destroy_api_key("key-1")                 # x:ApiKey/set destroy ["key-1"], raises JmapError on notDestroyed
```

```python
# tests/integration/test_live_auth_flow.py (part 1; marker integration; skips when MAILOSH_DEMO_PASSWORD unset)
async def test_password_verify_and_api_key_lifecycle():
    s = Settings(); admin = StalwartAdmin(s.stalwart_url, s.stalwart_admin_user, s.stalwart_admin_secret)
    assert await verify_password(s.stalwart_url, s.demo_user, "definitely-wrong") is None
    acct = await verify_password(s.stalwart_url, s.demo_user, s.demo_password); assert acct
    key = await admin.create_api_key(s.demo_user, "itest")
    c = await JmapClient.connect_bearer(s.stalwart_url, key.secret); boxes = await c.get_mailboxes(); assert boxes; await c.close()
    await admin.destroy_api_key(key.id)
    with pytest.raises(TransportError):                  # bearer now invalid → 401
        await JmapClient.connect_bearer(s.stalwart_url, key.secret)
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** `verify_password`: httpx GET `/.well-known/jmap` with `auth=(username, password)`, `follow_redirects=True`, timeout 10 s; on 401/403 → None; on 200 parse JSON; require non-empty `username` and a mail primary account; return `VerifiedAccount(username, account_id, email=username)`; any `TransportError`-class failure raises `TransportError` (so the login route can show "mail server unreachable" instead of "wrong password"). `create_api_key`: build the `x:ApiKey/set` create exactly like the spike (account-scoped to the target user — read SPK-3 for the field that scopes it), name `mailosh-session-<8 hex>`, return `ApiKey`; `destroy_api_key`: `x:ApiKey/set destroy`. `connect_bearer`: same as `connect` but `headers={"Authorization": f"Bearer {token}"}` and no basic auth. Live test must pass; record any surprise (e.g. per-account key cap) in `docs/spikes/p1a-findings.md` (create the file with headings: Auth exchange · Design system · Rows & list · Keyboard & palette · Live updates · Budgets · Browser QA).
- [ ] **Step 4: `make test` + `make itest` green.** **Step 5: Commit** — `feat(auth): stalwart password verification and per-session api keys`

---

### Task 5: Login, logout, session middleware, per-session JMAP clients

**Files:**
- Create: `mailosh/web/auth.py`, `mailosh/web/templates/auth/login.html`, `mailosh/jmap/pool.py`
- Modify: `mailosh/web/deps.py`, `mailosh/web/app.py` (lifespan: engine/sessionmaker, `ClientPool`, admin client; middleware; routers), `docker-compose.yml` (nothing new), `mailosh/cli.py` (import-mbox keeps using demo creds when set)
- Test: `tests/unit/test_auth_routes.py`, `tests/unit/test_pool.py`

**Interfaces:**
- Produces dependencies in `deps.py`: `get_db()`, `current_session(request, db) -> SessionRow | None` (reads cookie; loads via `sessions.load_session`), `require_session(...) -> SessionRow` (401 → for HX requests set `HX-Redirect: /login?next=…` and return 401; for full pages `RedirectResponse(303)`), `current_user(session, db) -> AppUser`, `csrf_protect(request, session)` (calls `csrf.validate`; reads form field `csrf_token` when the request is a form post without the header), `client_for(request, session) -> JmapClient` (via `request.app.state.pool`), `prefs_for(user, db) -> UiPref`. `create_app()` gains `app.state.pool: ClientPool`, `app.state.admin: StalwartAdmin`, `app.state.settings`, `app.state.sessionmaker`; `start_listener` param removed (listeners are per-user, Task 7). Old `get_client` (demo user) is deleted; routes that still use it are ported in Task 8 — until then the P0 routes are mounted only when `Settings.demo_user` is set (temporary shim, removed in Task 8).
- Routes: `GET /login` (bare layout form: username, password, "Keep me signed in", `next`), `POST /login` (rate-limit → verify → get_or_create_user → mint api key → create_session → set cookie → `audit login.ok` → 303 to `next` or `/`; failures: `audit login.fail`, `record_failure`, re-render with generic error "Wrong email or password" and, if the mail server is unreachable, "Can't reach the mail server right now"), `POST /logout` (destroy api key, revoke session, clear cookie, 303 `/login`), `POST /logout/all`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_auth_routes.py
import pytest
from fastapi.testclient import TestClient
from mailosh.web.app import create_app
from mailosh.security.exchange import VerifiedAccount
from mailosh.stalwart_admin import ApiKey

class FakeAdmin:
    def __init__(self): self.created = []; self.destroyed = []
    async def create_api_key(self, username, name): self.created.append(username); return ApiKey(id=f"k{len(self.created)}", secret="API_x")
    async def destroy_api_key(self, key_id): self.destroyed.append(key_id)

@pytest.fixture
def app(monkeypatch, sqlite_url):
    async def fake_verify(url, u, p): return VerifiedAccount(u, "acc1", u) if p == "right" else None
    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    app = create_app(settings=test_settings(sqlite_url))       # test_settings() in conftest: secret_key, cookie_secure=False, database_url=sqlite
    app.state.admin = FakeAdmin()
    return app

def test_login_page_renders(app):
    r = TestClient(app).get("/login"); assert r.status_code == 200 and 'name="password"' in r.text

def test_login_success_sets_cookie_and_redirects(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": "d@x", "password": "right", "next": "/mail/inbox"})
    assert r.status_code == 303 and r.headers["location"] == "/mail/inbox" and "sid=" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and app.state.admin.created == ["d@x"]

def test_login_failure_is_generic_and_rate_limited(app):
    c = TestClient(app)
    for _ in range(5):
        r = c.post("/login", data={"username": "d@x", "password": "wrong"}); assert r.status_code == 200 and "Wrong email or password" in r.text
    r = c.post("/login", data={"username": "d@x", "password": "right"})
    assert r.status_code == 429 and "Try again in" in r.text

def test_protected_route_redirects_html_and_hx(app):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/mail/inbox").status_code == 303
    r = c.get("/mail/inbox", headers={"HX-Request": "true"}); assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/login")

def test_logout_destroys_key_and_cookie(app):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "d@x", "password": "right"})
    token = c.get("/login").text  # any page renders the meta tag; extract content="..."
    import re; csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/mail/inbox", headers={"HX-Request": "true"}).text or token)
    r = c.post("/logout", headers={"X-CSRF-Token": csrf.group(1) if csrf else ""})
    assert r.status_code in (303, 403)
    # write the assertion against your real csrf plumbing: with the right token -> 303 and admin.destroyed == ["k1"]; with a wrong token -> 403

def test_post_without_csrf_is_403(app):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "d@x", "password": "right"})
    assert c.post("/logout").status_code == 403
```

```python
# tests/unit/test_pool.py
async def test_pool_builds_one_client_per_session_and_evicts_idle(monkeypatch): ...  # fake JmapClient.connect_bearer counting calls; two gets same session -> 1 connect; drop() closes; stop_idle(0) closes all
```

Note for the implementer: `create_app(settings: Settings | None = None)` must accept an injected `Settings` (unit tests pass an aiosqlite URL) and create tables via `Base.metadata.create_all` when the URL is sqlite (tests) while production relies on Alembic. Add `sqlite_url` and `test_settings` fixtures to `tests/conftest.py`.

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement** per Interfaces. Login template uses `layouts/bare.html` (centered 360 px card, wordmark, fields with visible labels, error region `role="alert"`, hidden `csrf_token` is NOT needed on login — the session does not exist yet; protect login from CSRF by `Sec-Fetch-Site` check + rate limit). Cookie set with `sessions.cookie_params` and `max_age` only when "remember" is checked. Store `ip` from `request.client.host` (honour `X-Forwarded-For` only when `settings.trust_proxy` — add that bool, default False). `ClientPool`: dict session_id → (client, last_used); `get` decrypts the api key secret and `connect_bearer`s once; `drop` closes; background task every 5 min calls `stop_idle(1800)`.
- [ ] **Step 4: `make test` green.** **Step 5: Commit** — `feat(auth): login/logout with db sessions, csrf, rate limiting, per-session jmap clients`

---

### Task 6: Formatting helpers and view-model services (TDD)

**Files:**
- Create: `mailosh/services/__init__.py`, `mailosh/services/mailbox_tree.py`, `mailosh/services/thread_list.py`
- Modify: `mailosh/ui/format.py`, `mailosh/jmap/client.py` (`query_page`, `set_mailboxes`, `set_keywords`)
- Test: `tests/unit/test_format.py`, `test_mailbox_tree.py`, `test_thread_list.py`, extend `test_jmap_mail.py`

**Interfaces:** exactly per the Interfaces block.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_format.py
from datetime import datetime, timezone
from mailosh.ui.format import format_date, format_senders, initials, avatar_color
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
def test_dates():
    assert format_date(datetime(2026, 9, 2, 10, 42, tzinfo=timezone.utc), NOW) == "10:42 AM"
    assert format_date(datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc), NOW) == "Sep 1"
    assert format_date(datetime(2025, 9, 1, 8, 0, tzinfo=timezone.utc), NOW) == "9/1/25"
def test_senders_gmail_style(make_header):     # make_header(from_name, from_email) fixture -> EmailHeader
    hs = [make_header("Aisha Rahman", "a@x"), make_header("Tom Reyes", "t@x"), make_header("Manish Sharma", "me@x"), make_header("Aisha Rahman", "a@x")]
    assert format_senders(hs, me="me@x") == "Aisha, Tom, me (4)"
    assert format_senders(hs[:1], me="me@x") == "Aisha Rahman"
    assert format_senders([make_header(None, "noreply@github.com")], me="me@x") == "noreply@github.com"
def test_initials_and_color_stable():
    assert initials("Daniel Okafor", "d@x") == "DO" and initials(None, "priya@x") == "P"
    assert avatar_color("d@x") == avatar_color("d@x") and 0 <= avatar_color("d@x") < 12
```

```python
# tests/unit/test_mailbox_tree.py
async def test_nav_roles_counts_and_label_tree(fake_client_with_mailboxes):   # fixture: Mailboxes inbox(role, unread 12), sent, drafts(total 2), trash, junk, archive, "Work"(unread 3), "Work/Design"(parent Work), "Receipts"
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta={"m-work": LabelMeta(color="indigo", visibility="show")})
    assert [i.key for i in nav.system] == ["inbox", "starred", "sent", "drafts"]          # no "snoozed"
    assert [i.key for i in nav.more] == ["all", "archive", "spam", "trash"]
    assert nav.system[0].count == 12 and nav.system[0].active
    work = next(l for l in nav.labels if l.name == "Work"); assert work.color == "indigo" and work.children[0].name == "Design"
    assert resolve_mailbox(nav, "inbox") == nav.inbox_id and resolve_mailbox(nav, "m-work") == "m-work" and resolve_mailbox(nav, "starred") is None
```

```python
# tests/unit/test_thread_list.py
async def test_page_rows_from_batched_query(client, api_mock):   # respx fixture; assert ONE http call; response fixture QUERY_PAGE_RESPONSE in conftest
    nav = ...; page = await build_page(client, mailbox_key="inbox", nav=nav, position=0, limit=50, me="me@x", now=NOW)
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)["methodCalls"]
    assert [m[0] for m in body] == ["Email/query", "Email/get", "Thread/get", "Email/get"]
    assert body[0][1]["collapseThreads"] is True and body[0][1]["calculateTotal"] is True
    row = page.rows[0]
    assert row.senders == "Aisha, Tom, me (3)" and row.count == 3 and row.unread and row.chips[0].name == "Work" and row.date_display == "10:42 AM"
    assert page.total == 1284 and page.next_position == 50
async def test_starred_is_a_keyword_query(...):  # mailbox_key="starred" -> filter {"hasKeyword": "$flagged", "inMailboxOtherThan": [trash, spam]}
async def test_all_mail_excludes_spam_and_trash(...):  # mailbox_key="all" -> filter {"inMailboxOtherThan": [...]}
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement.** `format_date`: local time of `now`'s tz (pass tz-aware datetimes; convert both to the same tz); today → `%-I:%M %p`; same year → `%b %-d`; else `%-m/%-d/%y`. `format_senders`: unique by email preserving first-appearance order of the *oldest→newest* messages; use first name (before space) when > 1 sender, full name (or address) when single; "me" for `me`; append ` (N)` with N = message count when N > 1. `query_page` builds the RFC 8621 §4.10 chain with `properties` for the final `Email/get` = `["id","threadId","mailboxIds","keywords","from","subject","receivedAt","preview","hasAttachment"]` and `#ids` back-references; filters per mailbox key: inbox/sent/drafts/archive/spam/trash → `inMailbox`; `starred` → `hasKeyword $flagged` + `inMailboxOtherThan [spam, trash]`; `all` → `inMailboxOtherThan [spam, trash]`; sort `receivedAt desc`. `build_page` computes per thread: unread = any email lacks `$seen`; starred = any `$flagged`; chips = user labels (non-role mailboxes) present on any email, ordered by nav order, max 3 (template shows 2 + "+N"); latest_email_id = newest `receivedAt`. `build_nav` orders labels alphabetically, nests by `parentId`, hides `visibility == "hide"` and (for `unread`) labels with 0 unread; counts = `unreadEmails` (Drafts uses `totalEmails`).
- [ ] **Step 4: Green.** **Step 5: Commit** — `feat(services): nav tree, batched thread page, gmail-style formatting`

---

### Task 7: Per-user live updates — hub registry, `/events`, in-house SSE bridge

**Files:**
- Modify: `mailosh/sse.py` (add `HubRegistry`), `mailosh/web/app.py` (state.hubs; lifespan close), `NOTICE`
- Create: `mailosh/web/events.py`, `mailosh/web/static/js/sse.js`
- Test: `tests/unit/test_hub_registry.py`, extend `tests/unit/test_sse_hub.py`

**Interfaces:** `HubRegistry` per block; `GET /events` (requires session) streams the user's hub with `ping=25`, event name `mail`, `data` = JSON `{"types": [...]}`, `id` = latest JMAP state when known. `sse.js` (ES module, ours, MIT): connects `new EventSource("/events")`; `mail` → `htmx.trigger(document.body, "mail:changed", detail)` coalesced 400 ms; `open` → `Alpine.store("ui").offline = false` and triggers `mail:changed` once (catch-up); `error` → after 3 s without reopen sets `offline = true`; starts a 120 s polling `mail:changed` while offline. Templates listen with `hx-trigger="mail:changed from:body"`.

- [ ] **Step 1: Failing tests** — registry returns the same hub per user and different hubs per user; `ensure_listener` starts exactly one task per user (fake client with an `event_stream` that yields one StateChange then blocks); `stop_idle(0)` cancels tasks without warnings; `/events` without session → 401; with session → `text/event-stream` and the first frame is a comment/ping (TestClient streaming with a timeout). 
- [ ] **Step 2: FAIL. Step 3: Implement.** Listener lifecycle: started lazily by `/events` (and by the mail routes' first request) via `ensure_listener(user_id, client)`; each hub tracks `last_activity`; `stop_idle(1800)` runs from a lifespan background task every 5 min and also closes the pooled client for users with no hub subscribers. Remove `sse.js` (htmx-ext-sse) from the Makefile vendor list and NOTICE (already done in Task 2 — verify) and delete any `hx-ext="sse"` remnants.
- [ ] **Step 4: Green. Step 5: Commit** — `feat(rt): per-user sse hubs and in-house eventsource bridge`

---

### Task 8: App shell, nav, list page and rows (list-first)

**Files:**
- Create: `mailosh/web/mail.py`, templates `shell/topbar.html`, `shell/nav.html`, `list/page.html`, `list/toolbar.html`, `list/rows.html`, `list/row.html`, `list/empty.html`, `list/skeleton.html`, `thread/page.html` (P0 thread view moved under the new layout, minimal restyle), `mailosh/web/static/js/app.js` (Alpine stores `ui`, `list`)
- Modify: `mailosh/web/app.py` (mount router; delete P0 `/inbox*` routes and the demo shim), `styles/input.css` (row/nav component classes), delete `mailosh/web/templates/inbox.html`, `_rows.html`
- Test: `tests/unit/test_mail_routes.py` (replaces `test_web_inbox.py`), update `test_web_thread.py` paths

**Interfaces:**
- Routes: `GET /` → 303 `/mail/inbox`; `GET /mail/{key}` full page (or `#main` partial when `HX-Request`, `hx-push-url`); `GET /mail/{key}/rows?position=N&limit=50` → `rows.html` fragment ending with the sentinel `<div hx-get="…?position=N+limit" hx-trigger="intersect once root:#list" hx-swap="outerHTML">` **only when `next_position` is not None** (fixes the Phase 0 parked sentinel); `GET /t/{thread_id}` (moved P0 view). Template contract for `row.html`: `<div id="row-{{ r.thread_id }}" class="row {{ 'is-unread' if r.unread }}" role="row" tabindex="-1" data-id="{{ r.thread_id }}" data-email-ids="{{ r.email_ids|join(',') }}" aria-selected="false" hx-get="/t/{{ r.thread_id }}" hx-target="#main" hx-push-url="true" preload="mousedown">` containing: `button.row-check` (aria-label "Select conversation"), `button.row-star` (`aria-pressed`), `.row-from`, `.row-text` (`.row-subject` + `.row-preview`), `.row-chips`, paperclip icon when `has_attachment`, `.row-date` (`<time datetime>`), and `.row-actions` (archive / delete / mark read|unread buttons with `title="Archive (e)"`, shown on hover/focus via CSS, replacing the date). List container: `<div id="list" role="grid" aria-multiselectable="true" hx-swap="morph:innerHTML show:none">`.
- `app.js`: `Alpine.store("ui", {theme, density, offline:false, toast(msg, undoToken)})`, `Alpine.store("list", {focusId:null, selected:new Set(), select(id), toggle(id), clear(), move(delta), ensureFocus()})` — re-reads `[data-id]` rows on `htmx:afterSettle`, keeps focus on the nearest row when the focused one vanished, `scrollIntoView({block:"nearest"})`, sets `tabindex` 0/−1 and `.is-focused`.

- [ ] **Step 1: Failing tests** — `/` redirects; `/mail/inbox` renders nav (no "Snoozed"), label colours as CSS vars, `data-theme` from prefs, rows with the contract attributes (escape test with `<b>` subject from `FAKE_ROW`-style fixture), sentinel present when `next_position` and absent at the end, `/mail/nope` → 404 page, partial vs full page by `HX-Request`, `preload="mousedown"` on rows, `role="grid"`. Fake services via a `FakeClient` exposing `get_mailboxes()` and `query_page()`.
- [ ] **Step 2: FAIL. Step 3: Implement** with the mockup as the visual reference (`docs/design/mockups/layout.html` / `key-moments.html` §1): top bar 52 px (menu, wordmark, search pill with `/` kbd, help, gear, avatar); nav 224 px (Compose button with `c` hint; system items; More ▾ disclosure; Labels header with `+`; label rows with colour dot and count; `aria-current="page"` on active); list toolbar (select-all tri-state checkbox, refresh, ⋮, "1–50 of 1,284", ‹ ›); rows per density (`--row-h`; Comfortable renders subject and preview on two lines, others one line with " – "). Read rows `--read`, unread `--surface` + weight 600 on sender/subject/date. Empty state copy per spec §5.4. Skeleton fragment served by `hx-trigger="load"` only when the first page takes > 300 ms (`hx-indicator` with `transition-delay: 300ms`). Thread page: P0 view under the new layout with the back arrow (`u` hint), action bar, `n of total` placeholder ("—" until 1B), unchanged rendering.
- [ ] **Step 4: Green; `make css`; `docker compose up -d --build mailosh`; open `/mail/inbox` after logging in — screenshot both themes and all three densities (Quick settings arrives in Task 12; toggle via `?density=` query param supported only in DEBUG for QA).** **Step 5: Commit** — `feat(shell): list-first gmail frame — top bar, nav, rows, thread page under the new layout`

---

### Task 9: Actions with optimistic UI and undo (TDD)

**Files:**
- Create: `mailosh/services/actions.py`, `mailosh/services/undo.py`, `mailosh/web/actions.py`, `mailosh/web/static/js/actions.js`, templates `fragments/toast.html`
- Modify: `mailosh/web/app.py` (router), `list/row.html` (wire buttons), `app.js` (toast store + undo timer)
- Test: `tests/unit/test_undo.py`, `tests/unit/test_actions.py`

**Interfaces:**
- Routes (all POST, CSRF, form or JSON body `ids` = email ids, repeated): `/a/archive`, `/a/delete`, `/a/spam`, `/a/star` (+`on`), `/a/read` (+`on`), `/a/undo` (`token`). Success → `204` with `HX-Trigger: {"om:done": {"toast": "Archived", "undo": "<token>", "removed": ["thread ids"], "counts": {"inbox": -1}}}`. `undo` → `204` with `HX-Trigger: {"om:done": {"toast": "Undone", "refresh": true}}`. JMAP failure → the global handler (Task 11) turns it into an error toast + `om:revert`.

> **AS-BUILT (amends the line above; recorded after Task 9's review).** The header carries a byte budget, `MAX_TRIGGER_BYTES = 3840`, sized to clear nginx's default 4 KB `proxy_buffer_size`. Oversized payloads shed fields, so **four** shapes reach the client and their *keys* differ, not just their values:
>
> | shape | keys | notes |
> |---|---|---|
> | undo kept | `counts, removed, toast, undo` | |
> | rows shed | `counts, refresh, removed, toast, undo` | `removed=[]`, `refresh=true` |
> | undo shed | `counts, refresh, removed, toast, undo, undo_unavailable` | `undo=null`, code `"too_many"` |
> | nothing changed | `counts, removed, toast, undo, undo_unavailable` | `undo=null`, code `"no_change"` |
>
> Two drifts from the text above: **`refresh: true` can now appear on a forward action**, not only on `/a/undo`'s own response; and `undo_unavailable` is a new field carrying a **stable code**, never display copy (the wording is owned client-side, so i18n does not mean string-matching server prose).
>
> Three consumption rules, each flagged by review as easy to get wrong: `undo_unavailable` is **absent, not null**, when undo is present — test key presence, never `=== null`; the codes are identifiers, not text; and `"no_change"` **contradicts the `toast` beside it** (starring an already-starred message emits `toast: "Starred"` with `undo_unavailable: "no_change"`), so surface an explanation only for `"too_many"`.
- Semantics (spec §6.3, JMAP-correct): **archive** = remove the Inbox id; if an email would end with zero mailboxes, add the Archive-role mailbox. **delete** = set mailboxIds to `{trash: true}` (remember previous ids in the UndoSpec). **spam** = `{junk: true}` + keyword `$junk`. **star**/**read** = keywords `$flagged`/`$seen`. Undo reverses exactly (restore previous `mailboxIds` per email — the spec carries `prev: dict[email_id, list[mailbox_id]]`, not just add/remove).
- `actions.js`: `om.act(kind, ids, extra)` — optimistic: for archive/delete/spam the rows collapse (160 ms, `.is-leaving`) and are removed; star/read toggle classes immediately; posts with `htmx.ajax("POST", url, {values, swap:"none"})`; on `om:done` shows the toast with Undo (10 s) and updates nav counts from `counts`; on failure (`htmx:responseError`/`sendError`) reverts by re-fetching `#list` (`htmx.trigger("#list","mail:changed")`) and shows the error toast. `z` (Task 10) calls `om.undoLast()` which posts the last token while its window is open.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_undo.py
from mailosh.services.undo import UndoSpec, sign, verify
def test_sign_verify_roundtrip_and_expiry():
    spec = UndoSpec(kind="archive", email_ids=["e1"], prev={"e1": ["inbox", "work"]}, keyword=None, on=None, toast="Archived")
    tok = sign(spec, "k" * 40, now=1000.0)
    assert verify(tok, "k" * 40, now=1030.0) == spec
    with pytest.raises(ValueError): verify(tok, "k" * 40, now=1000.0 + 61)
    with pytest.raises(ValueError): verify(tok + "x", "k" * 40, now=1001.0)
```

```python
# tests/unit/test_actions.py  (FakeClient records set_mailboxes / set_keywords calls; logged-in TestClient fixture from Task 5 with csrf header helper)
def test_archive_removes_inbox_and_falls_back_to_archive_mailbox(authed, fake):
    fake.emails = {"e1": {"inbox"}, "e2": {"inbox", "work"}}
    r = authed.post("/a/archive", data={"ids": ["e1", "e2"]})
    assert r.status_code == 204
    trig = json.loads(r.headers["HX-Trigger"])["om:done"]
    assert fake.set_mailboxes_calls == [(["e1"], {"archive"}, {"inbox"}), (["e2"], set(), {"inbox"})]   # e1 needs a home; e2 keeps Work
    assert trig["toast"] == "Archived" and trig["counts"] == {"inbox": -2} and trig["undo"]
def test_undo_restores_previous_mailboxes(authed, fake): ...   # POST /a/undo with the token -> set_mailboxes(["e1"], add={"inbox"}, remove={"archive"})
def test_delete_moves_to_trash_and_star_read_toggle(authed, fake): ...
def test_actions_require_csrf(authed_no_csrf): assert authed_no_csrf.post("/a/archive", data={"ids": ["e1"]}).status_code == 403
def test_bulk_over_100_requires_confirm(authed, fake): ...   # 101 ids without confirm=1 -> 409 with HX-Trigger om:confirm
```

- [ ] **Step 2: FAIL. Step 3: Implement.** `undo.sign` = `base64url(json(spec) + "." + hmac_sha256(derive_key(secret,"undo"), json))` with `exp`; `verify` checks signature (compare_digest) and expiry. `services/actions.py` uses `client.set_mailboxes`/`set_keywords` in the fewest JMAP calls (one `Email/set` with per-id patches — extend `set_mailboxes` to accept per-id patches: `set_mailboxes_patch(patches: dict[email_id, dict[str, bool|None]])`). Counts delta computed from what actually changed. Nav counts in the DOM update via `om:done.counts` (Alpine reads `[data-count-key]` badges).
- [ ] **Step 4: Green. Step 5: Commit** — `feat(actions): archive/delete/spam/star/read with signed undo and optimistic ui`

---

### Task 10: Keyboard registry, selection, `?` overlay (JS) + selection toolbar

**Files:**
- Create: `mailosh/web/static/js/keys.js`, templates `shell/shortcuts_dialog.html`, `list/toolbar_selected.html`
- Modify: `app.js`, `list/toolbar.html`, `list/row.html` (checkbox → `$store.list.toggle`), `styles/input.css`
- Test: `tests/unit/test_mail_routes.py` (toolbar renders both states; dialog markup present); JS is verified in Task 14's browser QA — write the manual checklist into `docs/spikes/p1a-findings.md` now.

**Interfaces:**
- `keys.js` exports `registry` (array of `{id, keys, scope, group, label, run}`), `registerDefaults(store)`, `dispatch(event)`; scopes `global|list|thread|compose|dialog`; two-key sequences with 1000 ms timeout; ignore when `event.target` matches `input, textarea, select, [contenteditable]` or `isComposing`, except `Escape` and chords with `metaKey/ctrlKey`; rejected key in the current scope → `document.activeElement.animate(shake)` 150 ms. Default map (spec §6.1): list scope `j k o Enter u x Shift+J Shift+K e # ! s Shift+I Shift+U l v [ ] . Esc`, sequences `g i|s|t|d|a|l`, `* a|n|r|u|s|t`; global `c / z ? Cmd+K`; thread scope adds `n p ; :` and `r a f`. `l`, `v`, `g l`, `c`, `r/a/f`, `n/p`, `;/:` exist in the registry with `run` stubs that open the palette in label/move mode (Task 11) or are marked `available:false` until 1B/1C — **unavailable entries are hidden from the overlay and no-op silently (no shake)**, so the UI never advertises a broken key.
- `?` overlay: native `<dialog id="shortcuts">` filled from the registry grouped by `group` ("Navigation", "Actions", "Selection", "Application", "Conversation"), `kbd` chips, closes on `Esc`/click-out.
- Selection toolbar: when `$store.list.selected.size > 0`, the toolbar swaps (Alpine `x-show`) to: select-all checkbox (tri-state), Archive, Spam, Delete, Mark read/unread, Labels (opens palette label mode), Move to, More; count "3 selected"; `Esc` clears.

- [ ] **Step 1: Failing route tests** for toolbar markup (both toolbars present with `x-show`), dialog element present, rows have `data-id`. **Step 2: FAIL. Step 3: Implement** keys.js (~150 lines, no deps), wire in `app.js` (`window.addEventListener("keydown", dispatch)`), selection store behaviours (`x` toggles focused, `Shift+J/K` extend, `* a` selects all loaded rows, `* n` none, `* r/u/s/t` by row classes), bulk actions call `om.act(kind, idsOfSelected)`. Tooltips: every action button's `title` includes the key from the registry (`Archive (e)`).
- [ ] **Step 4: Green; manual check in the browser: `j/k` moves focus with the accent edge, `x` selects, `e` archives with undo toast, `z` undoes, `?` shows the overlay grouped like Gmail's.** **Step 5: Commit** — `feat(keys): keyboard registry, selection model, shortcuts overlay, selection toolbar`

---

### Task 11: ⌘K command palette v1

**Files:**
- Create: `mailosh/web/palette.py`, `mailosh/web/static/js/palette.js`, templates `shell/palette.html`
- Modify: `app.js`, `keys.js` (Cmd+K, `l`, `v`, `g l` open modes), `layouts/app.html` (include dialog)
- Test: `tests/unit/test_palette.py`

**Interfaces:**
- `GET /palette/index` → JSON `{"actions":[{"id":"archive","label":"Archive conversation","keys":["e"],"kind":"action","needs":"selection"}, …], "goto":[{"id":"goto:inbox","label":"Inbox","keys":["g","i"],"href":"/mail/inbox"}, …labels…], "labels":[{"id":"m-work","label":"Work","color":"indigo"}], "settings":[{"id":"prefs:theme","label":"Theme: dark","post":"/prefs","values":{"theme":"dark"}}]}` (labels/goto built from `build_nav`; actions from a Python list mirrored from the JS registry — single source: the JS registry ids; Python only adds labels/goto/settings).
- `palette.js`: `<dialog id="palette">` with input + results; loads `/palette/index` on first open (cached 60 s; refreshed on `mail:changed`); modes `command` (default), `label` (multi-apply to selection/open thread; type-to-create → `POST /labels` — **not in 1A**: show "Create label “x”" only when 1D ships; in 1A the create option is hidden), `move`, `goto`; scoring via `commandScore(label + " " + aliases, query)` (vendored), threshold 0.001, recents boost (+0.2, last 20 in `localStorage`); results grouped with kbd chips; `↑↓`/`Ctrl+J/K` move, `Enter` runs (`run()` from the registry for actions, `htmx.ajax("GET", href)` for goto, `om.act("label", …)` for labels); free text with no match → "Search mail for “…”" → `/search?q=` (route lands in 1D; in 1A it navigates to `/mail/inbox?q=` and shows a toast "Search arrives with the next release" — remove in 1D). Focus trapped by the native dialog; `Esc` closes; opening from `l`/`v` preselects the mode.

- [ ] **Step 1: Failing test** — `/palette/index` (authed) returns the groups, `goto` includes labels from the fake nav and excludes hidden labels, no `snooze` action anywhere, settings toggles present. **Step 2: FAIL. Step 3: Implement.** **Step 4: Green; manual: `Cmd+K`, type "arch" → Archive first with `e` chip; `g l` opens goto in label mode.** **Step 5: Commit** — `feat(palette): cmd-k command palette with fuzzy actions, goto, labels`

---

### Task 12: Quick settings (theme, density, shortcuts) + prefs endpoint

**Files:**
- Create: `mailosh/web/prefs.py`, templates `shell/quick_settings.html`
- Modify: `shell/topbar.html` (gear opens the panel), `app.js` (`ui` store applies `data-theme`/`data-density` optimistically), `layouts/app.html`
- Test: `tests/unit/test_prefs.py`

**Interfaces:** `POST /prefs` (CSRF) form fields subset of `theme (system|light|dark)`, `density (compact|standard|comfortable)`, `shortcuts (true|false)` → 204 + `HX-Trigger: {"om:prefs": {...}}`; invalid → 422. Panel: right-side popover (280 px) with segmented controls for Theme (System/Light/Dark), Density (Compact/Standard/Comfortable), Keyboard shortcuts toggle; changes apply instantly to `<html>` dataset and persist.

- [ ] **Step 1: Failing tests** (valid/invalid values, persistence via `repo.get_prefs`, CSRF). **Step 2: FAIL. Step 3: Implement. Step 4: Green. Step 5: Commit** — `feat(prefs): quick settings for theme, density, shortcuts`

---

### Task 13: Error surface, states, offline banner, security headers

**Files:**
- Modify: `mailosh/web/app.py` (exception handlers, middleware for security headers), templates `fragments/toast.html`, `fragments/offline.html`, `fragments/error_page.html`, `layouts/app.html` (offline banner slot)
- Test: `tests/unit/test_errors.py`

**Interfaces:**
- `@app.exception_handler(JmapError)` and `(TransportError)`: HTMX request → `200` with `HX-Reswap: none` and `HX-Trigger: {"om:error": {"toast": "Couldn't reach the mail server — retrying", "retry": true}}` (never a bare 500); full page → `error_page.html` with status 502/500 and a Retry link. `HTTPException(401)` → `HX-Redirect` for HTMX else 303 (already from Task 5 — assert here). `RequestValidationError` → 422 with a toast for HTMX. Security headers middleware (spec §9 last bullet) on every response; the `/m/*` frame routes (1B) will override CSP.
- Offline banner: `<div id="offline" hidden>` under the top bar shown by `$store.ui.offline`; copy "Reconnecting to your mailbox…".

- [ ] **Step 1: Failing tests** — a route whose fake client raises `TransportError` returns 200 + `HX-Reswap: none` + `om:error` for HX and 502 page otherwise; CSP/`X-Content-Type-Options`/`Referrer-Policy` present on `/mail/inbox` and `/login`. **Step 2: FAIL. Step 3: Implement. Step 4: Green. Step 5: Commit** — `feat(web): global error surface, offline banner, security headers`

---

### Task 14: Integration flow, docs, browser QA, findings

**Files:**
- Create/extend: `tests/integration/test_live_auth_flow.py` (part 2), `docs/spikes/p1a-findings.md`, `README.md` (login story, `make db-upgrade`, env vars), `NOTICE` (final check)
- Modify: `Makefile` (`qa` target printing the manual checklist), `.env.example`

- [ ] **Step 1: Hermetic integration scenario** (self-cleaning, per-run ids): import 3 messages with per-run Message-IDs into Inbox via the P0 client → `TestClient`-style httpx against the live app? (The live app runs in Docker; drive it with `httpx.Client(base_url="http://localhost:8000")`): `POST /login` (demo creds) → `GET /mail/inbox` shows the 3 subjects → `POST /a/archive` one → `GET /mail/inbox` no longer shows it → `POST /a/undo` → back → `POST /logout` → `GET /mail/inbox` 303. Cleanup destroys the 3 emails.
- [ ] **Step 2: Browser QA (Chrome MCP), record in findings with screenshots:** login → inbox in light and dark, Compact/Standard/Comfortable; keyboard-only: `j/k`, `x`, `Shift+J`, `e` + `z`, `#` + `z`, `s`, `Shift+I`, `g s`, `g i`, `?`, `Cmd+K` "arch" → Enter; hover actions replace the date; selection toolbar; new mail via `scripts/send-test.py` appears without reload and the title shows `(N)`; kill Stalwart's network for 10 s → offline banner → reconnect; fresh clone `make up` works. Compare against `docs/design/mockups/`.
- [ ] **Step 3: Budgets:** `scripts/measure.py` extended with `GET /mail/inbox/rows` server time (p50/p95, 20 runs, logged in) and `web-vitals` (vendor `web-vitals.iife.js` in DEBUG only) INP/LCP read from the console after the keyboard session; targets spec §11; record numbers and misses.
- [ ] **Step 4: Findings doc complete** (headings from Task 4), executive summary: what 1B needs to know (auth exchange behaviour, listener idle policy, morph exclusions, any Stalwart quirk found). **Step 5: `make test && make itest`, ruff clean; commit** — `docs: p1a findings, integration flow, README` and tag `phase1a-complete`.

---

## Self-review (done at write time)

- **Spec coverage:** §4 tokens/type/motion → Task 2; §5 frame/nav/rows/states → Tasks 6, 8, 13; §6.1–6.2 keyboard/palette → Tasks 10–11; §6.3 undo/optimistic → Task 9; §6.4 prefetch/morph → Tasks 2, 8 (`preload="mousedown"`, `hx-ext="morph, preload"`); §6.5 live updates → Task 7; §9 auth/sessions/CSRF/rate-limit/API keys/security headers → Tasks 1, 3, 4, 5, 13; §10 Quick settings subset → Task 12 (full settings in 1D); §12 structure → File structure; §13 testing → per task + Task 14; §14 1A exit criteria → Task 14 QA. Deferred by design: search route, labels CRUD (1D), conversation rebuild (1B), compose (1C).
- **Placeholder scan:** Task 5's logout test intentionally tells the implementer to assert against their real CSRF plumbing (two named outcomes) — acceptable; Task 11's 1A-only palette behaviours ("Search arrives…", hidden create) are explicit temporary decisions with their removal task named.
- **Type consistency:** `UndoSpec` gained `prev: dict[str, list[str]]` in Task 9 — the Interfaces block's `add/remove` fields are superseded by `prev`; implementer uses `prev` (kind, email_ids, prev, keyword, on, toast). `query_page` in Task 6 vs `set_mailboxes_patch` added in Task 9 — both live in `client.py`. `HubRegistry`, `ClientPool`, `SessionRow`, `build_nav`, `build_page` names match across Tasks 5–11.
