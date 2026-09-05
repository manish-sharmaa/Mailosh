# Phase 0 Spike Implementation Plan

> Implement this plan task by task, in order. Steps use checkbox (`- [ ]`) syntax so progress can be tracked in place.

**Goal:** Prove the Mailosh thesis end to end: FastAPI + an in-house async JMAP client + HTMX/SSE render a live-updating inbox and thread view against a Dockerised Stalwart, `Email/import` preserves multi-mailbox labels, a Squire compose prototype settles the HTMX gate, and the six SPK questions get written answers.

**Architecture:** Modular monolith `mailosh/` package. All mail data flows through `mailosh/jmap` (httpx + Pydantic) to Stalwart; the web layer renders Jinja2 partials swapped by HTMX; a per-user EventSource listener bridges Stalwart push into an SSE hub. Postgres ships in compose but no app code touches it in P0.

**Tech Stack:** Python 3.12+, FastAPI, httpx, Pydantic v2, Jinja2, sse-starlette, Typer, pytest + pytest-asyncio + respx, HTMX 2.0.10 + htmx-ext-sse 2.2.4 + Alpine 3 + Squire 2.4.8 + DOMPurify 3 (vendored), Tailwind 4 standalone via pytailwindcss, Docker Compose, Stalwart v0.16.x, PostgreSQL 16.

**Spec:** `docs/specs/2026-08-31-mailosh-design.md` (read it first; §6 budgets, §14 SPK gates)

## Global Constraints

- Python ≥ 3.12; license AGPL-3.0-or-later.
- No GPL/AGPL Python *dependencies* (`jmapc`, `aioimaplib` banned); no Redis; no Node toolchain.
- Mail content never touches Postgres; all mail data via JMAP only.
- Frontend assets are vendored into `mailosh/web/static/vendor/` (no runtime CDN).
- Budgets to measure (spec §6): inbox render 1 JMAP round trip; SMTP→browser < 2 s.
- Conventional commits, carrying no attribution trailers of any kind. Every commit is authored solely by the repository owner.
- Stalwart admin secret and demo credentials come from `.env`; never hardcode secrets.
- Spike findings are recorded in `docs/spikes/p0-findings.md` as tasks complete — each SPK answer is part of its task's deliverable, not an afterthought.

---

## Reference implementation (read before coding): ihasmail

Clone read-only (gitignored): `git clone --depth 1 https://github.com/Coffey-Labs/ihasmail .reference/ihasmail` (add `.reference/` to `.gitignore` in Task 1). AGPL-3.0-or-later — porting is allowed; any ported logic gets a `# Derived from ihasmail (https://github.com/Coffey-Labs/ihasmail), © Coffey Labs, AGPL-3.0-or-later` comment plus a `NOTICE` entry (create `NOTICE` at first port). Port ideas into Python; never vendor TypeScript.

| Their file | Feeds our task |
|---|---|
| `KNOWN-ISSUES.md`, `FEATURES.md` | Tasks 2–5, 8 — required reading; live-verified Stalwart quirks (synthetic-ID renumbering, per-account capability advertising, gzip/content-length proxy trap, futureRelease silently dropped, 2047-byte signature cap) |
| `web/src/jmap/types.ts` | Task 3 — field-by-field basis for `mailosh/jmap/models.py` |
| `web/src/jmap/client.ts` | Tasks 3–4 — capability-aware `using` lists, `maxObjectsInGet/Set` chunking, batching, SetError surfacing |
| `server/src/upstream.ts` | Task 3 (`Session.rebase`) and Task 10 — session URL rewriting, `urn:stalwart:jmap` registry detection |
| `web/src/jmap/push.ts` + `server/src/app.ts` `/api/events` | Task 8 — EventSource relay + backoff/visibility reconnect semantics |
| `server/src/crypto.ts`, `sessions.ts` | P1/P2 (spec §9) — credential sealing; not built in P0 |
| `web/src/lib/html.ts`, `server/src/imageproxy.ts` | P2 (spec §8) — sanitizer policy + SSRF-safe proxy; not built in P0 |
| `server/src/mock/index.ts` | Behavioral spec when we build a Python mock (P1+); do not run it |
| `web/src/lib/search.ts` | P2 — Gmail-operator translation cross-check |

## File structure (locked)

```
pyproject.toml                      project metadata + deps (hatchling)
Makefile                            venv, vendor, css, test, compose helpers
.env.example                        all env vars with safe defaults
.gitignore
LICENSE                             AGPL-3.0-or-later text
docker-compose.yml                  stalwart, postgres, mailosh, worker(profile)
docker/mailosh.Dockerfile
scripts/stalwart-init.sh            create test domain + demo account via mgmt API
scripts/send-test.py                SMTP send for latency measurement
scripts/measure.py                  SPK-6 measurements
mailosh/__init__.py
mailosh/config.py                  pydantic-settings Settings
mailosh/jmap/__init__.py
mailosh/jmap/models.py             Session, Mailbox, EmailHeader, EmailBody, Thread, StateChange
mailosh/jmap/client.py             JmapClient (request builder + typed helpers + event stream)
mailosh/jmap/errors.py             JmapError, MethodError
mailosh/stalwart_admin.py          StalwartAdmin (mgmt API: domain, account, dkim, oauth probe)
mailosh/sse.py                     SseHub (per-user fanout) + stalwart_listener task
mailosh/web/app.py                 create_app(), routes, DI of JmapClient
mailosh/web/deps.py                get_client() dependency (env-credential demo user)
mailosh/web/templates/base.html
mailosh/web/templates/inbox.html   full page
mailosh/web/templates/_rows.html   thread-row partial (HTMX swap target)
mailosh/web/templates/thread.html
mailosh/web/templates/compose.html Squire prototype
mailosh/web/static/vendor/         htmx.min.js, sse.js, alpine.min.js, squire.js, purify.min.js
mailosh/web/static/app.css         built by tailwind (input: styles/input.css)
styles/input.css
mailosh/cli.py                     typer app: `mailosh dev`, `mailosh import-mbox`
tests/conftest.py                   fixtures: settings, respx session mock, sample payloads
tests/fixtures/session.json         recorded Stalwart session object
tests/fixtures/sample.mbox          3-message thread
tests/unit/test_jmap_request.py
tests/unit/test_jmap_models.py
tests/unit/test_jmap_mail.py
tests/unit/test_submission.py
tests/unit/test_web_inbox.py
tests/unit/test_web_thread.py
tests/integration/test_live_stalwart.py   (marker: integration; needs compose up)
docs/spikes/p0-findings.md    SPK-1..6 answers (grown task by task)
```

Interface contract used throughout (defined in Task 3/4, consumed by web/CLI):

```python
class JmapClient:
    @classmethod
    async def connect(cls, base_url: str, username: str, password: str) -> "JmapClient"
    async def close(self) -> None
    @property
    def account_id(self) -> str
    async def get_mailboxes(self) -> list[Mailbox]                       # Mailbox: id,name,parent_id,role,total_unread,total_emails,sort_order
    async def query_inbox(self, mailbox_id: str, *, limit: int = 50, position: int = 0) -> list[EmailHeader]
        # ONE HTTP request: Email/query(collapseThreads) + backref Email/get
        # EmailHeader: id, thread_id, mailbox_ids: set[str], keywords: set[str],
        #              from_: list[Address], subject: str|None, received_at: datetime,
        #              preview: str, has_attachment: bool
    async def get_thread(self, thread_id: str) -> list[EmailBody]
        # ONE HTTP request: Thread/get + backref Email/get(bodyValues, fetchTextBodyValues)
        # EmailBody = EmailHeader + to,cc: list[Address], text_body: str|None
    async def set_keyword(self, email_id: str, keyword: str, on: bool) -> None
    async def move(self, email_id: str, add: set[str] = frozenset(), remove: set[str] = frozenset()) -> None
    async def upload(self, data: bytes, content_type: str) -> str        # returns blobId
    async def import_email(self, blob_id: str, mailbox_ids: set[str], keywords: set[str], received_at: datetime | None) -> str
    async def send(self, *, to: list[str], subject: str, text: str, html: str | None) -> str
    async def event_stream(self) -> AsyncIterator[StateChange]           # StateChange: changed: dict[str, dict[str, str]]
```

---

### Task 1: Repo scaffolding, license, tooling

**Files:**
- Create: `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, `LICENSE`, `styles/input.css`, `mailosh/__init__.py`, `mailosh/config.py`, `tests/unit/test_config.py`, `tests/conftest.py` (minimal)

**Interfaces:**
- Produces: `mailosh.config.Settings` (pydantic-settings) with fields `stalwart_url: str = "http://localhost:8080"`, `stalwart_admin_user: str = "admin"`, `stalwart_admin_secret: str`, `demo_user: str = "demo@mailosh.test"`, `demo_password: str`, `smtp_port: int = 2525`, env prefix `MAILOSH_`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config.py
from mailosh.config import Settings

def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "s3cret")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "pw")
    s = Settings()
    assert s.stalwart_url == "http://localhost:8080"
    assert s.stalwart_admin_secret == "s3cret"
    assert s.demo_user == "demo@mailosh.test"
```

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mailosh"
version = "0.0.1"
description = "Open-source, self-hosted mail platform: Stalwart engine, Gmail-class webmail"
license = "AGPL-3.0-or-later"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "httpx>=0.27",
  "pydantic>=2.8",
  "pydantic-settings>=2.4",
  "jinja2>=3.1",
  "sse-starlette>=2.1",
  "typer>=0.12",
  "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.21", "pytailwindcss>=0.2", "ruff>=0.6"]

[project.scripts]
mailosh = "mailosh.cli:app"

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["integration: needs docker compose stack running"]
addopts = "-m 'not integration'"

[tool.ruff]
line-length = 100
```

- [ ] **Step 3: Create `mailosh/__init__.py` (empty), `mailosh/config.py`**

```python
# mailosh/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAILOSH_", env_file=".env", extra="ignore")
    stalwart_url: str = "http://localhost:8080"
    stalwart_admin_user: str = "admin"
    stalwart_admin_secret: str
    demo_user: str = "demo@mailosh.test"
    demo_password: str
    smtp_port: int = 2525
```

- [ ] **Step 4: Create `.env.example` (mirror every Settings field: `MAILOSH_STALWART_ADMIN_SECRET=changeme`, `MAILOSH_DEMO_PASSWORD=changeme`, others commented with defaults), `.gitignore` (`.venv/ __pycache__/ .env node_modules/ mailosh/web/static/vendor/ mailosh/web/static/app.css .pytest_cache/ dist/`), `LICENSE` (full AGPL-3.0 text from https://www.gnu.org/licenses/agpl-3.0.txt), `styles/input.css` (`@import "tailwindcss";`), minimal `tests/conftest.py` (empty for now), `Makefile`:**

```makefile
VENDOR := mailosh/web/static/vendor
venv:
	python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
vendor:
	mkdir -p $(VENDOR)
	curl -fsSL -o $(VENDOR)/htmx.min.js   https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js
	curl -fsSL -o $(VENDOR)/sse.js        https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/dist/sse.min.js
	curl -fsSL -o $(VENDOR)/alpine.min.js https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js
	curl -fsSL -o $(VENDOR)/squire.js     https://cdn.jsdelivr.net/npm/squire-rte@2.4.8/dist/squire.js
	curl -fsSL -o $(VENDOR)/purify.min.js https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js
css:
	.venv/bin/tailwindcss -i styles/input.css -o mailosh/web/static/app.css --minify
test:
	.venv/bin/pytest
itest:
	.venv/bin/pytest -m integration
up:
	docker compose up -d
```

(Indent recipe lines with real tabs. If a jsdelivr version 404s, list available versions with `curl -s https://data.jsdelivr.com/v1/packages/npm/<pkg>` and pin the closest 2.0.x/2.2.x/3.x/2.4.x — record the final pins in the findings doc.)

- [ ] **Step 5: Run test to verify it passes**

Run: `make venv && .venv/bin/pytest tests/unit/test_config.py -v`
Expected: PASS (settings read env). Also run `make vendor` once and confirm five files exist.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: scaffold mailosh package, tooling, AGPL license"
```

---

### Task 2: Compose stack + Stalwart bootstrap (SPK-5 first contact)

**Files:**
- Create: `docker-compose.yml`, `docker/mailosh.Dockerfile`, `scripts/stalwart-init.sh`, `docs/spikes/p0-findings.md` (skeleton with six SPK headings)

**Interfaces:**
- Produces: running Stalwart with domain `mailosh.test`, account `demo@mailosh.test` (password from env), mgmt API base `http://localhost:8080/api`, JMAP session at `http://localhost:8080/.well-known/jmap`; SMTP exposed on `localhost:2525`.

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
services:
  stalwart:
    image: stalwartlabs/stalwart:v0.16.20
    ports: ["8080:8080", "2525:25", "1587:587", "1143:143"]
    volumes: ["stalwart-data:/opt/stalwart"]
    environment:
      - STALWART_ADMIN_SECRET=${MAILOSH_STALWART_ADMIN_SECRET:-changeme}
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8080/healthz/live"]
      interval: 5s
      retries: 20
  postgres:
    image: postgres:16-alpine
    environment: {POSTGRES_USER: mailosh, POSTGRES_PASSWORD: mailosh, POSTGRES_DB: mailosh}
    volumes: ["pg-data:/var/lib/postgresql/data"]
volumes: {stalwart-data: {}, pg-data: {}}
```

- [ ] **Step 2: Verify image + admin bootstrap reality.** Run `docker compose pull stalwart`. If the tag 404s, check `https://hub.docker.com/r/stalwartlabs/stalwart/tags` and pin the newest v0.16.x. Start it, then read the container log for the generated admin credential (Stalwart prints/accepts an admin secret on first boot — reconcile with the env var above; consult https://stalw.art/docs/install/docker). Adjust compose until `curl -fsS http://localhost:8080/healthz/live` succeeds and `curl -u "admin:$SECRET" http://localhost:8080/api/oauth` (or the documented mgmt ping route) returns non-401. **Record the actual bootstrap mechanism and mgmt auth in `p0-findings.md` under SPK-5.**

- [ ] **Step 3: Write `scripts/stalwart-init.sh`** — idempotent bootstrap using the management API paths confirmed in Step 2 (documented candidates below; correct them against reality and leave the final ones in the script):

```bash
#!/usr/bin/env bash
set -euo pipefail
BASE="${STALWART_URL:-http://localhost:8080}"; AUTH="admin:${MAILOSH_STALWART_ADMIN_SECRET:?}"
# 1. create domain (candidate route per stalw.art docs: POST /api/domain/{name})
curl -fsS -u "$AUTH" -X POST "$BASE/api/domain/mailosh.test" || true
# 2. create individual account (candidate: POST /api/principal  type=individual)
curl -fsS -u "$AUTH" -X POST "$BASE/api/principal" -H 'content-type: application/json' -d '{
  "type": "individual", "name": "demo",
  "secrets": ["'"${MAILOSH_DEMO_PASSWORD:?}"'"],
  "emails": ["demo@mailosh.test"], "quota": 0}' || true
# 3. read DKIM public key for the domain (candidate: GET /api/dkim/mailosh.test)
curl -fsS -u "$AUTH" "$BASE/api/dkim/mailosh.test" || echo "DKIM route differs; record actual"
```

- [ ] **Step 4: Verify end to end**

Run: `bash scripts/stalwart-init.sh` then
`curl -fsS -u "demo@mailosh.test:$MAILOSH_DEMO_PASSWORD" http://localhost:8080/.well-known/jmap | python3 -m json.tool | head -40`
Expected: a JMAP Session JSON with `primaryAccounts` containing `urn:ietf:params:jmap:mail`. Save the full output to `tests/fixtures/session.json` (scrub any secrets). Send a first SMTP message: `python3 - <<'PY'` … `smtplib.SMTP("localhost",2525).sendmail("test@example.org","demo@mailosh.test","Subject: hello\r\n\r\nworld")` … and confirm it lands: the JMAP `Email/query` via curl returns one id (exact curl in Task 5's integration test; a quick manual check is enough here).

- [ ] **Step 5: Write `docker/mailosh.Dockerfile`** (python:3.12-slim, `pip install -e .`, `CMD ["uvicorn","mailosh.web.app:create_app","--factory","--host","0.0.0.0","--port","8000"]`) — built but not yet in compose (added in Task 6).

- [ ] **Step 6: Record SPK-5 findings** (which routes existed, auth mechanism, anything renamed) in `docs/spikes/p0-findings.md`; note open questions for the wizard task (Task 10).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: compose stack with stalwart+postgres, bootstrap script, session fixture"
```

---

### Task 3: JMAP core — models, request envelope, errors (TDD)

**Files:**
- Create: `mailosh/jmap/__init__.py`, `mailosh/jmap/models.py`, `mailosh/jmap/errors.py`, `mailosh/jmap/client.py` (connect/close/request only)
- Test: `tests/unit/test_jmap_models.py`, `tests/unit/test_jmap_request.py`

**Interfaces:**
- Produces: `JmapClient.connect(base_url, username, password)` (fetches session via `GET {base_url}/.well-known/jmap`, basic auth, follows `apiUrl`/`uploadUrl`/`eventSourceUrl` from it); `await client._call(method_calls: list[tuple[str, dict, str]]) -> dict[str, dict]` mapping call-id → response args, raising `MethodError(type, call_id)` on `error` responses; `client.account_id`.
- Models (`models.py`, all `pydantic.BaseModel` with `populate_by_name=True` aliases for camelCase): `Session(api_url, upload_url, event_source_url, primary_account_id)` (parsed with a custom `from_jmap(dict)` that resolves `primaryAccounts["urn:ietf:params:jmap:mail"]`), `Address(name: str|None, email: str)`, `Mailbox(id, name, parent_id, role, sort_order, total_emails, unread_emails)`, `EmailHeader(...)`, `EmailBody(...)`, `StateChange(changed: dict[str, dict[str, str]])` — field list exactly as in the contract block at the top.

- [ ] **Step 1: Write failing model tests**

```python
# tests/unit/test_jmap_models.py
import json, pathlib
from mailosh.jmap.models import Session

def test_session_parses_fixture():
    raw = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())
    s = Session.from_jmap(raw)
    assert s.api_url.startswith("http")
    assert s.primary_account_id
    assert s.event_source_url
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_jmap_models.py -v` → FAIL (no module).

- [ ] **Step 3: Implement `models.py` + `errors.py`** (cross-check field names/optionality against `.reference/ihasmail/web/src/jmap/types.ts`) (`JmapError(Exception)`, `MethodError(JmapError)` with `type` and `call_id` attributes). `Session.from_jmap` must substitute Stalwart's advertised URLs verbatim (they may point at the container-internal host; add `Session.rebase(base_url)` that swaps scheme+host to the configured base — Stalwart behind Docker often advertises its own hostname; this bit is required for the app to work from the host).

- [ ] **Step 4: Write failing request-envelope tests using respx**

```python
# tests/unit/test_jmap_request.py
import respx, httpx, json, pathlib, pytest
from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import MethodError

SESSION = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())

@respx.mock
async def test_connect_and_batched_call():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    api = respx.post("http://s/jmap").respond(json={
        "methodResponses": [["Mailbox/get", {"list": []}, "c0"]], "sessionState": "x"})
    c = await JmapClient.connect("http://s", "u", "p")
    out = await c._call([("Mailbox/get", {"accountId": c.account_id}, "c0")])
    assert out["c0"] == {"list": []}
    body = json.loads(api.calls[0].request.content)
    assert body["using"] == ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
    assert body["methodCalls"][0][0] == "Mailbox/get"

@respx.mock
async def test_method_error_raises():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    respx.post("http://s/jmap").respond(json={
        "methodResponses": [["error", {"type": "unknownMethod"}, "c0"]], "sessionState": "x"})
    c = await JmapClient.connect("http://s", "u", "p")
    with pytest.raises(MethodError):
        await c._call([("Nope/get", {}, "c0")])
```

(Adjust the mocked `apiUrl` path to whatever `session.json` + `rebase` produce — the test should mock exactly `c._session.api_url`.)

- [ ] **Step 5: Implement `client.py` connect/close/_call** — one `httpx.AsyncClient(auth=(user, pw), http2=False, timeout=30)`; `_call` posts `{"using": [...], "methodCalls": [...]}`, maps responses by call id, first `error` tuple raises `MethodError`.

- [ ] **Step 6: Run all unit tests** — `pytest tests/unit -v` → PASS.

- [ ] **Step 7: Commit** — `git commit -am "feat(jmap): session, models, batched request envelope with typed errors"`

---

### Task 4: JMAP mail methods (TDD)

**Files:**
- Modify: `mailosh/jmap/client.py`
- Test: `tests/unit/test_jmap_mail.py`

**Interfaces:**
- Produces (exact signatures from the contract block): `get_mailboxes`, `query_inbox`, `get_thread`, `set_keyword`, `move`, `upload`, `import_email`.

- [ ] **Step 1: Write failing tests — the two batched reads assert ONE http call each**

```python
# tests/unit/test_jmap_mail.py  (respx-mocked like Task 3; helpers in tests/conftest.py)
async def test_query_inbox_single_roundtrip(client, api_mock):
    api_mock.respond(json=EMAIL_QUERY_PLUS_GET_RESPONSE)   # fixture dict in conftest
    rows = await client.query_inbox("mb-inbox", limit=50)
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    q, g = body["methodCalls"][0], body["methodCalls"][1]
    assert q[0] == "Email/query" and q[1]["collapseThreads"] is True
    assert q[1]["sort"] == [{"property": "receivedAt", "isAscending": False}]
    assert g[0] == "Email/get"
    assert g[1]["#ids"] == {"resultOf": q[2], "name": "Email/query", "path": "/ids"}
    assert rows[0].thread_id and rows[0].preview

async def test_get_thread_single_roundtrip(client, api_mock):
    api_mock.respond(json=THREAD_GET_PLUS_EMAIL_RESPONSE)
    msgs = await client.get_thread("t-1")
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    t, g = body["methodCalls"][0], body["methodCalls"][1]
    assert t[0] == "Thread/get" and t[1]["ids"] == ["t-1"]
    assert g[1]["#ids"]["path"] == "/list/*/emailIds/*"   # correct backref shape per RFC 8620 §3.7
    assert g[1]["fetchTextBodyValues"] is True
    assert msgs[0].text_body

async def test_set_keyword_patch_syntax(client, api_mock):
    api_mock.respond(json=EMPTY_SET_RESPONSE)
    await client.set_keyword("e1", "$seen", True)
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][1]["update"] == {"e1": {"keywords/$seen": True}}

async def test_import_email_multi_mailbox(client, api_mock, upload_mock):
    upload_mock.respond(json={"blobId": "b1", "type": "message/rfc822", "size": 3})
    api_mock.respond(json=IMPORT_RESPONSE)
    blob = await client.upload(b"raw", "message/rfc822")
    eid = await client.import_email(blob, {"mb-inbox", "mb-label1"}, {"$seen"}, None)
    body = json.loads(api_mock.calls[0].request.content)
    creation = body["methodCalls"][0][1]["emails"]["i0"]
    assert creation["mailboxIds"] == {"mb-inbox": True, "mb-label1": True}
    assert eid == "e-imported"
```

Fixture responses live in `tests/conftest.py` as plain dicts shaped per RFC 8621 examples (`EMAIL_QUERY_PLUS_GET_RESPONSE` has `Email/query`→`ids` and `Email/get`→`list` with two rows; `THREAD_GET_PLUS_EMAIL_RESPONSE` has `Thread/get` list with `emailIds` and `Email/get` with `bodyValues`; `IMPORT_RESPONSE` has `Email/import` `created: {"i0": {"id": "e-imported"}}`). Write them out fully — ~60 lines of dict literals.

- [ ] **Step 2: Run to verify failures** — `pytest tests/unit/test_jmap_mail.py -v` → FAIL (methods missing).

- [ ] **Step 3: Implement the methods** (consult `.reference/ihasmail/web/src/jmap/client.ts` for chunking and `using` handling; honour quirks from `KNOWN-ISSUES.md`). `query_inbox` builds `filter={"inMailbox": mailbox_id}`; `get_thread` maps `Email/get` args `properties=[...]`, `fetchTextBodyValues=True`, `maxBodyValueBytes=256*1024`, and merges `bodyValues` into `EmailBody.text_body` (first `textBody` part). `upload` POSTs raw bytes to `session.upload_url` (substitute `{accountId}`). `move` translates add/remove into `mailboxIds/<id>: true|null` patches.

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** — `git commit -am "feat(jmap): mailboxes, batched inbox query, thread fetch, flags, upload+import"`

---

### Task 5: Live integration test + mbox import (SPK-2)

**Files:**
- Create: `tests/fixtures/sample.mbox`, `tests/integration/test_live_stalwart.py`, `mailosh/cli.py` (`import-mbox` command)

**Interfaces:**
- Consumes: `JmapClient` (Task 4). Produces: `mailosh import-mbox tests/fixtures/sample.mbox --label Imported` CLI; written SPK-2 answer.

- [ ] **Step 1: Create `tests/fixtures/sample.mbox`** — three messages forming one thread (same base subject; msg2/msg3 carry `In-Reply-To`/`References` to msg1's `Message-ID: <t1@example.org>`; RFC 5322-complete headers, `From ` separators). Write it out fully in the file.

- [ ] **Step 2: Write the integration test**

```python
# tests/integration/test_live_stalwart.py
import mailbox, os, pytest, datetime
from mailosh.config import Settings
from mailosh.jmap.client import JmapClient

pytestmark = pytest.mark.integration

async def test_import_thread_and_multilabel():
    s = Settings()
    c = await JmapClient.connect(s.stalwart_url, s.demo_user, s.demo_password)
    boxes = {m.role or m.name: m for m in await c.get_mailboxes()}
    inbox = boxes["inbox"]
    # create a second mailbox to act as a label (Mailbox/set create)
    label_id = await c.create_mailbox("SpikeLabel")          # add this helper in this task
    ids = []
    for msg in mailbox.mbox("tests/fixtures/sample.mbox"):
        blob = await c.upload(bytes(msg), "message/rfc822")
        ids.append(await c.import_email(blob, {inbox.id, label_id}, set(), None))
    rows = await c.query_inbox(inbox.id)
    spike = [r for r in rows if "spike thread" in (r.subject or "").lower()]
    assert len(spike) == 1, "collapseThreads must fold 3 messages into one row"
    msgs = await c.get_thread(spike[0].thread_id)
    assert len(msgs) == 3, "threading via References must hold after import"
    assert {inbox.id, label_id} <= set(msgs[0].mailbox_ids), "multi-mailbox membership survives import"
```

- [ ] **Step 3: Add `create_mailbox` to the client** (unit test first in `test_jmap_mail.py`: asserts `Mailbox/set` create body; then implementation).

- [ ] **Step 4: Run it against the live stack** — `make up && bash scripts/stalwart-init.sh && make itest` → PASS. If threading or multi-label fails, that is a SPIKE RESULT, not a test to delete: record exact behaviour in `p0-findings.md` SPK-2 and adapt the design note.

- [ ] **Step 5: Implement `mailosh/cli.py`** with Typer: `import-mbox PATH --label NAME` doing the same loop via `asyncio.run`, printing imported count.

- [ ] **Step 6: Record SPK-2 answer** (receivedAt honoured? threads computed? multi-mailbox ok?) in the findings doc. **Step 7: Commit** — `git commit -am "feat: mbox import CLI + live integration test proving threads and multi-label import"`

---

### Task 6: Web app skeleton + inbox page

**Files:**
- Create: `mailosh/web/app.py`, `mailosh/web/deps.py`, `mailosh/web/templates/base.html`, `inbox.html`, `_rows.html`
- Test: `tests/unit/test_web_inbox.py`
- Modify: `docker-compose.yml` (add `mailosh` service, build from `docker/mailosh.Dockerfile`, `ports: ["8000:8000"]`, `env_file: .env`, `depends_on: stalwart`)

**Interfaces:**
- Produces: `create_app() -> FastAPI`; routes `GET /` (redirect `/inbox`), `GET /inbox` (full page), `GET /inbox/rows?position=N` (partial `_rows.html`); `app.state.client` created on lifespan startup from `Settings` (demo user), closed on shutdown; `get_client()` dependency in `deps.py` returns it (tests override).

- [ ] **Step 1: Failing test with dependency override**

```python
# tests/unit/test_web_inbox.py
from fastapi.testclient import TestClient
from mailosh.web.app import create_app
from mailosh.web import deps

class FakeClient:
    account_id = "a1"
    async def get_mailboxes(self): return [FAKE_INBOX]          # fixtures in conftest
    async def query_inbox(self, mid, limit=50, position=0): return [FAKE_ROW]

def test_inbox_renders_rows_and_escapes():
    app = create_app(start_listener=False)
    app.dependency_overrides[deps.get_client] = lambda: FakeClient()
    r = TestClient(app).get("/inbox")
    assert r.status_code == 200
    assert "Spike &lt;b&gt;subject&lt;/b&gt;" in r.text     # FAKE_ROW subject is "Spike <b>subject</b>" — must be escaped
    assert 'hx-get="/inbox/rows?position=50"' in r.text
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement** `create_app(start_listener=True)` (Jinja2Templates, static mount, lifespan that skips client creation when overridden/`start_listener=False`), `inbox.html` extends `base.html`: header (search box stub), left rail (mailboxes with unread counts), row list including `<div id="rows">{% include "_rows.html" %}</div>` and a "load more" `hx-get="/inbox/rows?position={{ next }}" hx-target="#rows" hx-swap="beforeend"`. `_rows.html` renders each `EmailHeader`: unread → font-bold, `preview`, relative time, `hx-get="/thread/{{ r.thread_id }}" hx-target="#main" hx-push-url="true"`. `base.html` loads `/static/vendor/htmx.min.js`, `sse.js`, `alpine.min.js`, `/static/app.css` (run `make css`).
- [ ] **Step 4: Run test → PASS.** Then `docker compose up -d --build mailosh` and eyeball `http://localhost:8000/inbox` showing the imported spike thread.
- [ ] **Step 5: Commit** — `git commit -am "feat(web): app factory, inbox page with HTMX row paging"`

---

### Task 7: Thread view + actions

**Files:**
- Create: `mailosh/web/templates/thread.html`; Modify: `mailosh/web/app.py`
- Test: `tests/unit/test_web_thread.py`

**Interfaces:**
- Produces: `GET /thread/{thread_id}` (partial or full page via `HX-Request` header check); `POST /email/{id}/keyword` form fields `keyword`, `on` → 204 + `HX-Trigger: refresh-rows`; `POST /email/{id}/archive` (remove inbox id) → same.

- [ ] **Step 1: Failing tests** — thread page shows all three fixture messages' `text_body` escaped, collapsed quoting behind an Alpine `x-show` toggle; archive posts call `FakeClient.move` with `remove={"mb-inbox"}` (FakeClient records calls; assert).
- [ ] **Step 2: Run → FAIL. Step 3: Implement.** Mark-as-read on open: thread route fires `set_keyword(id, "$seen", True)` for unread messages (FakeClient asserts). **Step 4: Run → PASS. Step 5: Commit** — `git commit -am "feat(web): thread view with read-on-open, archive and star actions"`

---

### Task 8: Real-time — SSE hub + Stalwart listener (SPK-6 half)

**Files:**
- Create: `mailosh/sse.py`; Modify: `mailosh/web/app.py` (lifespan starts listener when `start_listener=True`), `inbox.html` (`hx-ext="sse" sse-connect="/events" sse-swap="new-mail"` on the rows container → prepend), `scripts/send-test.py`
- Test: `tests/unit/test_sse_hub.py`

**Interfaces:**
- Produces: `SseHub` with `subscribe() -> AsyncIterator[ServerSentEvent]`, `publish(event: str, data: str)`; `stalwart_listener(client, hub)` task that iterates `client.event_stream()` and on any `Email` state change publishes `("new-mail", rendered_rows_html_or_signal)`; route `GET /events` returning `EventSourceResponse(hub.subscribe())`. P0 simplification: one global hub (single demo user).

- [ ] **Step 1: Failing hub test** — `publish` reaches two concurrent subscribers; slow subscriber (full queue, maxsize=100) is dropped not deadlocked (`asyncio.wait_for` bounded).
- [ ] **Step 2: Run → FAIL. Step 3: Implement hub** (dict of `asyncio.Queue`, `try_put_nowait` with eviction). Implement `client.event_stream()`: `httpx` stream GET on `session.event_source_url` (substitute `{types}=Email,Mailbox`, `{closeafter}=no`, `{ping}=30`), parse `text/event-stream` frames minimally (`event:`/`data:` accumulation, dispatch on blank line), yield `StateChange` on `event: state`; auto-reconnect loop with `1,2,4,…30s` capped backoff lives in `stalwart_listener`, not the stream parser. Listener strategy on change: publish `new-mail` with **empty data** and let the browser row container also carry `hx-get="/inbox/rows?position=0" hx-trigger="sse:new-mail"` — re-fetch instead of server-rendered push (one code path, idempotent).
- [ ] **Step 4: Unit tests pass; live check:** `scripts/send-test.py` (smtplib to `localhost:2525`, unique subject, prints ISO timestamp) while `/inbox` is open — the row must appear without reload. Measure: run send-test 5×, note wall-clock to visible row (crude stopwatch is fine at spike level; scripted variant in Task 11). Record in findings SPK-6.
- [ ] **Step 5: Commit** — `git commit -am "feat(rt): jmap eventsource listener, sse hub, live inbox refresh"`

---

### Task 9: Compose prototype with Squire (SPK-1 gate) + send

**Files:**
- Create: `mailosh/web/templates/compose.html`; Modify: `mailosh/web/app.py`, `mailosh/jmap/client.py` (`send`, `get_identity`)
- Test: `tests/unit/test_submission.py`

**Interfaces:**
- Produces: `GET /compose` page; `POST /compose` fields `to,subject,html` → sanitised (`DOMPurify` client-side pre-clean; server trusts nothing: strip to text + keep html as-is for P0 — **server-side nh3 arrives in P2 per spec §8; P0 sends only, never renders sent html**) → `client.send(...)` → redirect `/inbox?sent=1`. `client.send` builds ONE request: `Identity/get` (cached after first call) + `Email/set create` (draft: `mailboxIds={drafts:true}`, `keywords={"$draft":true,"$seen":true}`, `bodyStructure` multipart/alternative with `textBody` part `p1` + `htmlBody` part `p2`, `bodyValues={p1:{value:text},p2:{value:html}}`) + `EmailSubmission/set create {emailId:"#d0", identityId}` with `onSuccessUpdateEmail` moving to Sent (`mailboxIds` patch) and clearing `$draft`.
- [ ] **Step 1: Failing unit test** asserting exactly that method-call shape (respx; check `onSuccessUpdateEmail` uses `"#s0"` key form) and that html→text fallback strips tags.
- [ ] **Step 2: Run → FAIL. Step 3: Implement** `send` + `compose.html`: Alpine component wrapping vendored Squire (`<div id="editor">` + toolbar buttons bold/italic/link/quote wired to `squire.bold()` etc.; hidden `<input name="html">` filled with `DOMPurify.sanitize(squire.getHTML())` on submit; To/Subject inputs; `hx-post="/compose"`).
- [ ] **Step 4: Tests pass; live check** — send to `demo@mailosh.test` from the UI; it must arrive in the live inbox via SSE (nice dogfood loop). Send outward to an external inbox only if you have relay creds; else skip (P1 covers real deliverability).
- [ ] **Step 5: SPK-1 verdict** — 30-minute DX assessment against the checklist: toolbar state reflects selection? paste from Word survives? quoting on reply feasible? Alpine/Squire lifecycle fights HTMX swaps? Write **go / no-go + evidence** in findings; if no-go, note the Datastar/Preact-island fallback decision for P2 (do not rebuild now).
- [ ] **Step 6: Commit** — `git commit -am "feat: squire compose prototype with jmap submission"`

---

### Task 10: Wizard skeleton — Stalwart admin client (SPK-3, SPK-5 close-out)

**Files:**
- Create: `mailosh/stalwart_admin.py`; Modify: `mailosh/cli.py` (`setup` command)
- Test: `tests/unit/test_stalwart_admin.py` (respx, using recorded shapes from Task 2) + extend `tests/integration/test_live_stalwart.py`

**Interfaces:**
- Produces: `StalwartAdmin(base_url, admin_user, admin_secret)` with `async create_domain(name) -> None`, `async create_account(email, display_name, password) -> None`, `async get_dkim_record(domain) -> str` (returns the TXT value), `async try_mint_user_token(email) -> str | None` (SPK-3 probe: attempt whatever token/OAuth facility Task 2 discovered; return None + log if unsupported). CLI `mailosh setup --domain X --email Y` drives them and prints the DNS records block (MX/SPF/DKIM/DMARC values templated from spec §11).
- [ ] **Step 1: Failing respx tests** for the three calls using the routes recorded in Task 2 (update fixtures to reality). **Step 2: Run → FAIL. Step 3: Implement. Step 4: Integration run** against live container: `mailosh setup --domain spike2.test --email a@spike2.test` succeeds, DNS block prints, `try_mint_user_token` outcome recorded → **SPK-3 answer in findings** (this decides spec §9's credential design). **Step 5: Commit** — `git commit -am "feat: stalwart admin client + setup CLI printing DNS records"`

---

### Task 11: Measurements, findings, gate review (SPK-4, SPK-6, wrap)

**Files:**
- Create: `scripts/measure.py`; Modify: `docs/spikes/p0-findings.md` (complete all six SPK sections + final go/no-go)

- [ ] **Step 1: Write `scripts/measure.py`** — (a) inbox query: 20× `query_inbox` via client, print p50/p95 ms; (b) thread fetch same; (c) SMTP→SSE: subscribe to the hub via a raw `httpx` SSE GET on `/events`, `smtplib` send with unique subject, measure delta until `new-mail` frame, 10 runs, p50/p95. Print a Markdown table.
- [ ] **Step 2: Run against the compose stack; paste the table into findings SPK-6.** Compare against spec §6 budgets (<400 ms query, <2 s SMTP→browser); flag misses with hypotheses.
- [ ] **Step 3: SPK-4 decision** — read Stalwart's TLS/ACME docs (https://stalw.art/docs/server/tls/acme, or current path) + what Task 2's config showed; write the P1 decision: recommended split (mail-protocol TLS via Stalwart ACME; web TLS via Caddy container unless Stalwart can cleanly co-terminate) with the config sketch.
- [ ] **Step 4: Findings doc completeness pass** — every SPK section has: answer, evidence (commands/output), consequence for the spec (§ references). Add a ≤10-line executive summary at top: proceed to P1? any spec §4/§9 amendments?
- [ ] **Step 5: Full test run + commit + tag**

```bash
make test && make itest
git add -A && git commit -m "docs: p0 findings — budgets measured, SPK gates answered"
git tag spike-p0-complete
```

---

## Self-review (done at write time)

- **Spec coverage:** §14 SPK-1→Task 9, SPK-2→Task 5, SPK-3→Task 10, SPK-4→Task 11, SPK-5→Tasks 2+10, SPK-6→Tasks 8+11; §6 pipeline→Task 8; §7 parser and §8 sanitizer are P2 by spec — deliberately absent here.
- **Placeholder scan:** management-API route names are marked as candidates *to be corrected against the live server in Task 2* — that is the spike's purpose, not a placeholder; all other steps carry concrete code/commands.
- **Type consistency:** `JmapClient` contract block at top matches every consuming task (`query_inbox`, `get_thread`, `set_keyword`, `move`, `upload`, `import_email`, `create_mailbox` added in Task 5, `send` in Task 9, `event_stream` in Task 8).
