# Phase 1B — Reading Implementation Plan

> Implement this plan task by task, in order. Steps use checkbox (`- [ ]`) syntax so progress can be tracked in place.

**Goal:** Make `/t/{threadId}` a real conversation view that renders **attacker-controlled HTML mail safely**: a server-side sanitiser pipeline (tinycss2 for CSS, nh3 for markup), a CSP-sandboxed frame with a hash-pinned resize script, remote images blocked by default behind a proxy that never leaks the reader's IP, quoted text folded, attachments with previews, per-message actions, mark-read delay, auto-advance and print. Exit criterion (spec §14): open any real-world HTML mail (Gmail / Outlook / Apple Mail / newsletter corpus) safely and legibly in both themes.

**Architecture:** A new `mailosh/render/` package holds every pure function that touches mail content — `css_sanitize`, `html_sanitize`, `quote_trim`, `plain_text`, `frame_document`, `image_policy`, `fetch_guard`, `dark`. None of them import FastAPI, a DB session or a JMAP client; they take strings and dataclasses and return strings and dataclasses, which is what makes the adversarial test suites cheap to write and cheap to review. `mailosh/services/conversation.py` builds the view model over `JmapClient`. `mailosh/web/frames.py` is the only router that serves mail content, and every response it produces carries its own security headers. The conversation page itself renders **plain-text bodies inline** (escaped and linkified server-side, no attacker HTML anywhere near the app origin) and **HTML bodies inside a sandboxed iframe** whose document is generated in Python — not Jinja — because its CSP is a byte-exact `sha256` hash of the one inline script it carries.

**Tech Stack:** Python 3.12+, FastAPI, `nh3` 0.3.x (MIT; Rust `ammonia` bindings), `tinycss2` 1.5.x (BSD-3-Clause), httpx, Jinja2, htmx 2.0.10 + idiomorph 0.7.4 + preload 2.1.2, Alpine 3.17.1 **CSP build**, Tailwind 4 standalone CLI, SQLAlchemy 2 + Alembic, PostgreSQL 16, Stalwart v0.16.x.

**Spec:** `docs/specs/2026-09-02-phase1-webmail-design.md` — **§7 is this plan's scope**, and §14's 1B row is the exit criterion. §9 (app CSP, CSRF, sessions), §11 (a11y, budgets), §12 (module layout), §13 (testing) and §15 (the frame/resize verification gate) all bind. Phase 0 spec `2026-08-31-mailosh-design.md` still governs the JMAP client. What 1A learned: `docs/spikes/p1a-findings.md`. Visual reference: `docs/design/mockups/key-moments.html` §1 and `docs/design/polish-v2-spec.md`.

## Global Constraints

- Python ≥ 3.12; AGPL-3.0-or-later; no GPL/AGPL dependencies (nh3 is MIT, tinycss2 is BSD-3-Clause — both checked against their published metadata); no Redis; **no Node toolchain**; every frontend asset vendored and pinned in the Makefile and recorded in `NOTICE`; no runtime CDN.
- The app's own CSP stays `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'` — **no `unsafe-eval`, ever**. Mail frames carry their own stricter header, set on the route (the app middleware applies its set with `setdefault`, so a route header wins).
- **Alpine is the CSP build.** It accepts method calls *with arguments*, property paths, computed access, ternaries, arithmetic, comparison, `&&`/`||`, assignment and object/array literals in `x-data`. It rejects template literals, out-of-scope globals, multi-statement expressions, spread/destructuring, shorthand object keys, optional chaining and inline function expressions in `x-data`. htmx's `hx-on:`, `hx-vals='js:…'` and `hx-trigger="…[expr]"` are unavailable. Prefer htmx round trips and native `<details>`/`<dialog>` over new JS.
- Mail content never touches Postgres (spec §12). Postgres holds app state only.
- Every mutation is `POST` + CSRF and returns `204` with an `HX-Trigger` JSON header or a fragment; `HX-Request` alone is never trusted.
- **Tests assert behaviour and cardinality, not substring presence.** For sanitiser output this is not stylistic: `nh3` does **not** emit attributes in a stable order (the same input produced `target=… rel=…` and `rel=… target=…` across two runs of the same probe), so a test that compares a rendered tag as a string is flaky by construction. Parse the output (`html.parser`, via the `parse_attrs` helper Task 3 adds to `tests/helpers.py`) and assert on the parsed attributes, on counts, and on absence.
- `styles/input.css` is the source; the app serves the compiled `mailosh/web/static/app.css`. **Any task that touches CSS must run `make css`** before its verification step.
- **The two dark token blocks in `styles/input.css` must stay identical** — `:root[data-theme=dark]` and the `@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) }` block are two different activation conditions carrying the same values. A token added or changed in one MUST be written into the other, byte-identically.
- There is no JS test runner. JS is asserted two ways and only two ways: (a) Python tests that read the file and assert **control flow** — that a guard clause appears before the effect it guards, and that a handler is registered exactly once across the whole static tree; (b) live browser verification in the task's own step and again in Task 14's QA matrix.
- Zero-warning test output; `ruff check .` and `ruff format --check .` clean; conventional commits.
- **Commits carry no AI attribution of any kind** — no `Co-Authored-By` trailer, no session link, no "generated with" line, nothing naming a model or a tool. Every commit is authored solely by the person who wrote it.
- No UI control for a feature that does not work yet (spec §3, and 1A's own rule). Reply / reply-all / forward belong to 1C: 1B's per-message ⋮ menu ships **only** the items 1B implements. `keys.js` entries for 1C keys stay `available:false` and keep no-op-ing silently.

---

## Reference: Phase 1A code you build on

`mailosh/jmap/client.py` — `JmapClient.connect_bearer`, `_call`, `_call_raw`, `get_mailboxes`, `query_page(mailbox_id=…, position, limit, exclude_mailbox_ids, has_keyword) -> QueryPage`, `get_thread(thread_id) -> list[EmailBody]`, `set_keywords`, `set_mailboxes`, `set_mailboxes_patch`, `get_email_states`, `upload`, `event_stream`; module constants `USING`, `_EMAIL_LIST_PROPS`, `_EMAIL_BODY_PROPS`, `_MAX_BODY_VALUE_BYTES`, `_THREAD_ROW_PROPS`. Models in `mailosh/jmap/models.py`: `JmapModel` (camelCase aliases via `to_camel`, `populate_by_name=True`), `Session` (`api_url`, `upload_url`, `event_source_url`, `primary_account_id`, `from_jmap`, `rebase`), `Address`, `Mailbox`, `EmailHeader`, `EmailBody` (adds `to`, `cc`, `text_body` resolved from `textBody`/`bodyValues` by a `model_validator`), `StateChange`. Errors: `JmapError`, `MethodError`, `TransportError`.

`mailosh/web/mail.py` — `GET /`, `GET /mail/{key}`, `GET /mail/{key}/rows`, `GET /t/{thread_id}`; helpers `_is_fragment`, `_apply_fragment`, `_base_context`, `_nav_for`, `_not_found`, `_valid_keys`, `_referring_key`, `_list_context`, `_range_label`, `_active_item`; constants `INBOX_URL`, `PAGE_SIZE = 50`, `MAX_PAGE_SIZE = 100`. `mailosh/web/deps.py` — `require_session`, `current_session`, `current_user`, `prefs_for`, `client_for`, `get_db`, `csrf_protect`, `SessionRequired`. `mailosh/web/app.py` — `create_app`, `_SECURITY_HEADERS` (applied with `setdefault`), the `TransportError`/`JmapError`/`RequestValidationError`/`SessionRequired` handlers, `_hx_trigger`, `_error_toast`, `_error_page`, `split_quoted`, `html_to_text`, and the router mounts. `mailosh/web/actions.py` — `POST /a/{archive,delete,spam,star,read,undo}`, all CSRF-protected, answering `204` + `HX-Trigger: {"om:done": {...}}`. `mailosh/db/models.py` — `AppUser`, `SessionRow`, `LabelMeta`, `UiPref`, `Contact`, `ImageSenderAllow(user_id, sender_email)`, `LoginAttempt`, `AuditLog`. `mailosh/db/repo.py` — `get_or_create_user`, `get_prefs`, `set_prefs`, `label_meta_map`, `audit`. `mailosh/ui/format.py` — `initials`, `avatar_color`, `label_color`, `format_date`, `format_senders`. `mailosh/ui/env.py::build_env` registers globals `icon`, `static`, `kbd` and filters `initials`, `avatar_color`, `label_color`. Templates: `layouts/app.html`, `layouts/fragment.html` (title + main + oob only), `layouts/bare.html`, `thread/page.html`. Static JS: `app.js` (stores `ui`, `list`), `keys.js` (`registry`, `registerDefaults`, `dispatch`, scopes `global|list|thread|compose|dialog`), `actions.js` (`window.om.act/undoLast/targets`, `[data-action]` delegation, `data-role="back"`), `palette.js`, `sse.js`.

## What the 1A findings already settled (do not re-derive)

`docs/spikes/p1a-findings.md`, executive summary. The four items that bind this plan:

1. **`UiPref` already has every column 1B needs** — `conversation_view`, `mark_read_delay`, `auto_advance`, `remote_images`, `dark_restyle`, `font_size` — and `ImageSenderAllow(user_id, sender_email)` already exists. No migration is needed for the remote-image gate. Only the dark-restyle per-sender memory needs a new table (Task 12).
2. **Morph exclusions are a template rule, not configuration.** Every long-lived singleton (`#offline`, `#shortcuts`, `#palette`, `#quick-settings`, `#status`, `#toasts`) is included by `layouts/app.html` and deliberately **not** by `layouts/fragment.html`. 1B's new singletons — the attachment preview `<dialog>` — obey the same rule: `layouts/app.html` only.
3. **Rows are keyed by `id="row-{thread_id}"` so idiomorph preserves them.** 1B's message cards get `id="msg-{email_id}"` for the same reason: a live update that re-renders the conversation must not collapse an expanded message or reload its iframe.
4. **Budgets are already tight.** Total JS was measured at 92.3 KiB gz against spec §11's 90 KB, and 1C's Squire + DOMPurify will push it to ~121 KiB. 1B adds exactly one new module (`frame.js`, target ≤ 3 KB raw) and loads **neither Squire nor DOMPurify** — the sanitiser is entirely server-side. `NOTICE` currently claims DOMPurify "belongs to … the HTML-mail sanitiser pipeline (Phase 1B)"; that sentence is wrong and Task 3 corrects it (DOMPurify's only consumer is 1C's Squire).

## Departures from spec §7, decided up front

Each of these is a deliberate, reasoned change to the letter of §7. Nothing else in §7 is departed from; anything not listed here is implemented as written.

1. **The frame CSP's `sandbox` directive gains `allow-scripts`.** §7 writes `sandbox allow-popups allow-popups-to-escape-sandbox` in the header while the iframe attribute carries `allow-scripts`. A document under two sandbox policies gets the **intersection** of their permissions, so as written the CSP would strip script execution and the hash-pinned resize script — the whole reason the header names a hash — would never run. The header must be `sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox`. `allow-same-origin` is still absent from both, which is the property that matters.
2. **`img-src` gains `'self'` and never gains `http:`/`https:`.** §7's own `cid:` rewrite target (`/m/{id}/cid/{cid}`) is same-origin, so `img-src data:` alone would block every inline image the pipeline produces. And because **every** allowed remote image is rewritten to the same-origin proxy `/img?u=<signed>`, the third-party schemes are never needed: the CSP is byte-identical for `remote=0` and `remote=1`, which means a sanitiser bug that left a raw `https://tracker/px.gif` in the document is still blocked by the browser rather than silently leaking the reader's IP. Final value: `img-src 'self' data:`.
3. **`cid:` is rewritten to an *absolute* same-origin URL, not a relative one.** Verified against nh3 0.3.7: `attribute_filter` runs **before** the URL-scheme and `url_relative` checks, so a filter that returns `/m/{id}/cid/{cid}` has its return value immediately deleted by `url_relative="deny"`. The rewrite target is `{origin}/m/{email_id}/cid/{cid}` where `origin` comes from `request.base_url`.
4. **The quoted-text `•••` pill lives *inside* the frame for HTML bodies** and is driven by the same hash-pinned script (one script, one hash), because the quote is part of the sandboxed document and expanding it must re-post the frame height. Plain-text bodies keep the parent-page `<details>` pill they have today. Both render as the same `•••` chip.
5. **The per-message ⋮ menu ships without reply / reply all / forward.** Those are 1C's, and spec §3's "nothing in the UI exists that does not work" outranks §7's enumeration of the menu.
6. **"Show original" and the print page keep the sandboxed iframe.** Inlining sanitiser output into the app origin for printing would discard the containment the rest of the plan is built on. Task 13 verifies iframe printing in three browsers and records the result honestly rather than trading the boundary for convenience.
7. **`theme` accepts `system` as well as `light|dark`, and the frame route takes two further parameters.** `system` lets the frame resolve light/dark from its own `prefers-color-scheme` instead of the server guessing, which is the same mechanism `styles/input.css` already uses for the app; `restyle=0|1` carries the per-sender "Show original" choice (Task 12) and `expand=1` renders the quoted half open with no toggle, for the print page (Task 13).
8. **The image proxy buffers rather than streams.** §7 says "streams". The proxy instead reads the upstream body in chunks with a hard `MAX_IMAGE_BYTES` abort and returns it whole, because a streamed response commits to a status and a `Content-Type` before the body has been size-checked — and the one thing this endpoint must never do is emit bytes it has not finished vetting. The cap (5 MB) makes the memory cost bounded and small.

## File structure (locked for 1B)

```
pyproject.toml                          + nh3>=0.3.7, tinycss2>=1.5
NOTICE                                  fix the DOMPurify sentence (it is 1C's, not 1B's)
Makefile                                + printer, image, image-off, download, maximize-2, chevrons-up-down, chevrons-down-up icons
mailosh/ui/icons.txt                    same seven names
migrations/versions/0002_reading.py     table sender_pref (Task 12)
mailosh/db/models.py                    + SenderPref
mailosh/db/repo.py                      + sender_pref helpers
mailosh/jmap/models.py                  + BodyPart; Session.download_url; EmailBody html/attachments/recipients/headers
mailosh/jmap/client.py                  + blob_url, stream_blob, fetch_blob; get_thread fetches html + attachments
mailosh/render/__init__.py
mailosh/render/css_sanitize.py          tinycss2: stylesheets and style attributes
mailosh/render/html_sanitize.py         nh3 config, attribute filter, URL policy
mailosh/render/quote_trim.py            ihasmail selector list + offset split; plain-text quote start
mailosh/render/plain_text.py            escape, linkify, depth classes
mailosh/render/frame_document.py        the frame document, its inline script and its CSP
mailosh/render/image_policy.py          remote-image decision + signed proxy URLs
mailosh/render/fetch_guard.py           SSRF guard for the image proxy
mailosh/render/dark.py                  dark restyle decision
mailosh/security/signing.py             generic HMAC payload signing (undo.py is left alone)
mailosh/services/conversation.py        ConversationView / MessageView / AttachmentView
mailosh/web/frames.py                   /m/{id}/html, /m/{id}/frame, /m/{id}/cid/{cid}, /m/{id}/att/{blob}, /m/{id}/source, /img
mailosh/web/mail.py                     /t/{thread_id} rebuilt; + /mail/{key}/at/{position}
mailosh/web/prefs.py                    + reading prefs
mailosh/web/app.py                      mount frames.router; drop split_quoted (moves to render/plain_text.py)
mailosh/web/templates/thread/page.html, header.html, message.html, frame.html, attachments.html, details.html, menu.html, print.html
mailosh/web/templates/fragments/preview_dialog.html
mailosh/web/static/js/frame.js          the parent half of the resize handshake
mailosh/web/static/js/actions.js        + auto-advance after a thread action
mailosh/web/static/js/keys.js           thread-scope keys become available
styles/input.css                        conversation view, frame, chips, banner, preview dialog
tests/helpers.py                         parse_attrs (html -> parsed tag/attribute map)
tests/conftest.py                        + fake, authed, authed_no_csrf, app_client, csrf, user,
                                           token_for, set_pref, fake_client, nav, download_mock,
                                           xss_corpus, quote_fixtures
tests/unit/test_jmap_bodies.py, test_css_sanitize.py, test_html_sanitize.py, test_frame_document.py,
tests/unit/test_frame_routes.py, test_quote_trim.py, test_plain_text.py, test_image_policy.py,
tests/unit/test_fetch_guard.py, test_img_proxy.py, test_conversation.py, test_thread_routes.py,
tests/unit/test_attachments.py, test_dark_restyle.py, test_print_route.py, test_reading_prefs.py
tests/fixtures/mail/*.html               real-world corpus (Gmail, Outlook, Apple Mail, newsletter)
tests/fixtures/xss/*.html                adversarial corpus
tests/integration/test_live_reading_flow.py
docs/spikes/p1b-findings.md
```

Interfaces every later task relies on (exact):

```python
# mailosh/jmap/models.py
class BodyPart(JmapModel):
    part_id: str | None = None
    blob_id: str | None = None
    size: int = 0
    type: str = "application/octet-stream"
    name: str | None = None
    cid: str | None = None
    disposition: str | None = None

class Session(JmapModel):          # + one field, carried through from_jmap and rebase
    download_url: str

class EmailBody(EmailHeader):      # + fields, all defaulted so 1A's constructions still validate
    bcc: list[Address] = []
    reply_to: list[Address] = []
    sent_at: datetime | None = None
    blob_id: str | None = None
    html_body: str | None = None
    text_truncated: bool = False
    html_truncated: bool = False
    attachments: list[BodyPart] = []
    return_path: str | None = None      # alias "header:Return-Path:asText"
    auth_results: str | None = None     # alias "header:Authentication-Results:asText"

# mailosh/jmap/errors.py
class BlobTooLarge(JmapError): ...

# mailosh/jmap/client.py
def blob_url(self, blob_id: str, *, mime_type: str, name: str) -> str
@asynccontextmanager
async def stream_blob(self, blob_id: str, *, mime_type: str, name: str) -> AsyncIterator[httpx.Response]
async def fetch_blob(self, blob_id: str, *, mime_type: str, name: str, max_bytes: int) -> bytes   # raises BlobTooLarge

# mailosh/render/css_sanitize.py
ALLOWED_PROPERTIES: frozenset[str]
ALLOWED_FUNCTIONS: frozenset[str]
ALLOWED_AT_RULES: frozenset[str]          # {"media"}
MAX_CSS_BYTES: int                        # 512 * 1024
def sanitize_declarations(css: str) -> str          # one style="" value; "" when nothing survives
def sanitize_stylesheet(css: str) -> str            # one <style> block

# mailosh/render/html_sanitize.py
ALLOWED_TAGS: frozenset[str]
ALLOWED_ATTRIBUTES: dict[str, set[str]]
CLEAN_CONTENT_TAGS: frozenset[str]
URL_SCHEMES: frozenset[str]     # {"http","https","mailto","cid","data"} — nh3's set is global,
                                # so the per-tag gating of cid/data happens in the attribute filter
DATA_IMAGE_TYPES: frozenset[str]                     # png gif jpeg jpg webp bmp — never svg+xml
@dataclass(frozen=True)
class SanitizeContext:
    email_id: str
    origin: str                                      # "https://mail.example.com", no trailing slash
    remote: bool
    cid_parts: dict[str, str]                        # bare content-id (no <>) -> blob id
    sign_image: Callable[[str], str] | None          # absolute http(s) url -> opaque token
@dataclass(frozen=True)
class SanitizeResult:
    html: str
    css: str
    blocked_remote: int
    remote_hosts: tuple[str, ...]
def extract_styles(html: str) -> list[str]
def sanitize_email_html(html: str, ctx: SanitizeContext) -> SanitizeResult

# mailosh/render/quote_trim.py
@dataclass(frozen=True)
class QuoteMatcher:
    tag: str | None = None
    klass: str | None = None
    id_exact: str | None = None
    id_prefix: str | None = None
    attr: tuple[str, str] | None = None
QUOTE_MATCHERS: tuple[QuoteMatcher, ...]
def split_html(html: str) -> tuple[str, str]         # (visible, quoted); quoted == "" when none
def find_quote_start(text: str) -> int | None
def split_plain(text: str) -> tuple[str, str]
def quote_depth(line: str) -> int                    # 0..4

# mailosh/render/plain_text.py
@dataclass(frozen=True)
class TextLine:
    html: Markup
    depth: int
def render_plain(text: str | None) -> tuple[list[TextLine], list[TextLine]]   # (visible, quoted)
def linkify(text: str) -> Markup      # escapes first, then scans the escaped string

# mailosh/render/frame_document.py
FRAME_SCRIPT: str                                    # the exact inline JS, no surrounding whitespace
FRAME_SCRIPT_HASH: str                               # "sha256-<base64>"
MIN_FRAME_HEIGHT: int = 200
MAX_FRAME_HEIGHT: int = 20_000
def csp_header() -> str
def render_frame(*, visible_html: str, quoted_html: str, mail_css: str,
                 theme: str, restyle: str, expand: bool = False) -> str
    # theme: "system" | "light" | "dark"; restyle: "none" | "color-scheme" | "invert"
    # expand=True renders the quoted half open with no toggle (the print page, Task 13)

# mailosh/security/signing.py
def sign_payload(payload: dict, *, secret_key: str, purpose: str, ttl: int, now: float | None = None) -> str
def verify_payload(token: str, *, secret_key: str, purpose: str, now: float | None = None) -> dict   # raises ValueError

# mailosh/render/image_policy.py
IMAGE_URL_TTL: int = 3600
@dataclass(frozen=True)
class ImageDecision:
    show: bool
    reason: str            # "override" | "policy_always" | "sender_allowed" | "contact" | "blocked"
async def decide(db, *, user_id: int, policy: str, sender_email: str | None, override: int | None) -> ImageDecision
async def allow_sender(db, *, user_id: int, sender_email: str) -> None
def sign_remote_url(url: str, *, secret_key: str, user_id: int, now: float | None = None) -> str
def verify_remote_url(token: str, *, secret_key: str, user_id: int, now: float | None = None) -> str

# mailosh/render/fetch_guard.py
class BlockedUrl(ValueError): ...
MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
MAX_REDIRECTS: int = 3
CONNECT_TIMEOUT: float = 5.0
TOTAL_TIMEOUT: float = 10.0
ALLOWED_IMAGE_TYPES: frozenset[str]
def check_ip(raw: str) -> None                       # raises BlockedUrl
def check_url(url: str) -> tuple[str, int]           # (host, port); raises BlockedUrl
async def resolve_public(host: str, port: int) -> list[str]      # raises BlockedUrl
async def fetch_image(url: str) -> tuple[str, bytes]             # (content_type, body); raises BlockedUrl

# mailosh/render/dark.py
def declares_color_scheme(html: str, css: str) -> bool
def background_is_light(html: str, css: str) -> bool
def restyle_mode(*, theme: str, enabled: bool, declares: bool, light: bool) -> str   # "none"|"color-scheme"|"invert"

# mailosh/services/conversation.py
@dataclass(frozen=True)
class AttachmentView:
    blob_id: str; name: str; mime: str; size: int; size_display: str; icon: str; preview: str | None
@dataclass(frozen=True)
class MessageView:
    id: str; from_name: str; from_email: str
    to: list[Address]; cc: list[Address]; bcc: list[Address]
    mailed_by: str | None; signed_by: str | None     # domains, not raw headers; None = omit the row
    received_at: datetime; date_display: str; date_full: str
    initials: str; avatar_color: int
    unread: bool; starred: bool; expanded: bool; snippet: str
    has_html: bool; truncated: bool
    visible_lines: list[TextLine]; quoted_lines: list[TextLine]
    attachments: list[AttachmentView]
@dataclass(frozen=True)
class ConversationView:
    thread_id: str; subject: str
    messages: list[MessageView]
    email_ids: list[str]; unread_ids: list[str]
    first_unread_id: str | None                      # scroll target; None -> the last message
    chips: list[LabelChip]                           # reused from mailosh.services.thread_list
async def build_conversation(client, *, thread_id: str, me: str, now: datetime,
                             label_meta: dict[str, LabelMeta], nav: NavModel) -> ConversationView | None
```

---

### Task 1: JMAP bodies — HTML parts, attachments, blob download, `downloadUrl`

**Files:**
- Modify: `mailosh/jmap/models.py`, `mailosh/jmap/client.py`, `mailosh/jmap/errors.py`, `tests/conftest.py` (the `client` respx fixture's session body gains `downloadUrl`)
- Test: `tests/unit/test_jmap_bodies.py`

**Interfaces:** `BodyPart`, `Session.download_url`, the seven new `EmailBody` fields, `blob_url`, `stream_blob`, `fetch_blob`, `BlobTooLarge` — exactly as in the Interfaces block. `_EMAIL_BODY_PROPS` becomes `[*_EMAIL_LIST_PROPS, "cc", "bcc", "replyTo", "sentAt", "blobId", "textBody", "htmlBody", "bodyValues", "attachments", "header:Return-Path:asText", "header:Authentication-Results:asText"]` and `get_thread`'s `Email/get` gains `"fetchHTMLBodyValues": True`. RFC 8621 has a single `maxBodyValueBytes` per call covering both text and HTML, so there is no separate HTML cap to add: raise `_MAX_BODY_VALUE_BYTES` from `256 * 1024` to `512 * 1024` (enough for essentially every real newsletter, and bounded at ~10 MB for a 20-message thread) and **surface** truncation through `text_truncated`/`html_truncated` rather than clipping silently.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_jmap_bodies.py
import json

import pytest
from conftest import THREAD_GET_PLUS_EMAIL_RESPONSE   # the repo's existing import style:
                                                      # tests/ is not a package, pytest puts
                                                      # tests/ on sys.path via its conftest
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import EmailBody, Session

RAW = {
    "id": "E1", "threadId": "T1", "mailboxIds": {"mb1": True}, "keywords": {},
    "from": [{"name": "A", "email": "a@x"}], "to": [{"email": "b@y"}], "cc": None, "bcc": None,
    "replyTo": [{"email": "r@x"}], "subject": "s", "receivedAt": "2026-09-01T10:00:00Z",
    "sentAt": "2026-09-01T09:59:00Z", "blobId": "B0", "preview": "p", "hasAttachment": True,
    "textBody": [{"partId": "1"}], "htmlBody": [{"partId": "2"}],
    "bodyValues": {
        "1": {"value": "hello", "isTruncated": False},
        "2": {"value": "<p>hello</p>", "isTruncated": True},
    },
    "attachments": [
        {"partId": "3", "blobId": "B3", "size": 12, "type": "image/png",
         "name": "logo.png", "cid": "logo@mail", "disposition": "inline"},
        {"partId": "4", "blobId": "B4", "size": 99, "type": "application/pdf",
         "name": "spec.pdf", "cid": None, "disposition": "attachment"},
    ],
    "header:Return-Path:asText": "<bounce@x>",
    "header:Authentication-Results:asText": "mx.test; dkim=pass header.d=x.test",
}

def test_email_body_resolves_html_and_attachments():
    body = EmailBody.model_validate(RAW)
    assert body.text_body == "hello" and body.text_truncated is False
    assert body.html_body == "<p>hello</p>" and body.html_truncated is True
    assert [a.blob_id for a in body.attachments] == ["B3", "B4"]
    assert body.attachments[0].cid == "logo@mail"
    assert body.attachments[0].disposition == "inline"
    assert body.blob_id == "B0"
    assert [a.email for a in body.reply_to] == ["r@x"]
    assert body.bcc == []
    assert body.return_path == "<bounce@x>"
    assert body.auth_results.startswith("mx.test")

def test_email_body_without_html_is_none_not_error():
    raw = {k: v for k, v in RAW.items() if k not in ("htmlBody", "bodyValues", "attachments")}
    body = EmailBody.model_validate(raw)
    assert body.html_body is None and body.text_body is None and body.attachments == []

def test_session_carries_and_rebases_download_url():
    session = Session.from_jmap({
        "apiUrl": "https://mail.test/jmap", "uploadUrl": "https://mail.test/upload/{accountId}",
        "downloadUrl": "https://mail.test/download/{accountId}/{blobId}/{name}?accept={type}",
        "eventSourceUrl": "https://mail.test/events",
        "primaryAccounts": {"urn:ietf:params:jmap:mail": "acct"},
    })
    rebased = session.rebase("http://stalwart:8080")
    assert rebased.download_url == "http://stalwart:8080/download/{accountId}/{blobId}/{name}?accept={type}"

async def test_get_thread_requests_html_values_and_headers(client, api_mock):
    # Same shape as tests/unit/test_jmap_mail.py::test_get_thread_single_roundtrip:
    # respond with a canned body, then read the request that was actually sent.
    api_mock.respond(json=THREAD_GET_PLUS_EMAIL_RESPONSE)   # existing conftest fixture
    await client.get_thread("t-1")
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    args = body["methodCalls"][1][1]
    assert args["fetchHTMLBodyValues"] is True and args["fetchTextBodyValues"] is True
    assert args["maxBodyValueBytes"] == 512 * 1024
    for prop in ("htmlBody", "attachments", "blobId", "bcc", "replyTo", "sentAt",
                 "header:Return-Path:asText", "header:Authentication-Results:asText"):
        assert prop in args["properties"], prop

async def test_blob_url_substitutes_all_four_placeholders(client):
    url = client.blob_url("B3", mime_type="image/png", name="a b.png")
    assert "{accountId}" not in url and "{blobId}" not in url
    assert "{type}" not in url and "{name}" not in url
    assert "a%20b.png" in url or "a+b.png" in url

async def test_fetch_blob_caps_size(client, download_mock):
    download_mock(content=b"x" * 100)
    with pytest.raises(JmapError):
        await client.fetch_blob("B3", mime_type="image/png", name="a.png", max_bytes=10)
    assert await client.fetch_blob("B3", mime_type="image/png", name="a.png", max_bytes=1000) == b"x" * 100
```

- [ ] **Step 2: Run → FAIL** — `make test` → `AttributeError: 'Session' object has no attribute 'download_url'`.
- [ ] **Step 3: Implement.** `Session.from_jmap` reads `downloadUrl`; `rebase` rewrites it with `_rebase_url` alongside the other three. `EmailBody` gains a `_resolve_html_body` `model_validator(mode="before")` mirroring `_resolve_text_body` — it reads `htmlBody[0]["partId"]` out of `bodyValues`, sets `htmlBody` to the flat string (or `None`), and records `isTruncated` for both parts into `text_truncated`/`html_truncated`. Do **not** merge the two validators: keep them separate so a malformed `textBody` cannot suppress the html resolution. `return_path`/`auth_results` use explicit `Field(alias="header:Return-Path:asText")` (the `to_camel` generator would otherwise mangle the JMAP header pseudo-property). `blob_url` substitutes all four RFC 8620 §6.2 placeholders with `urllib.parse.quote(..., safe="")`. `stream_blob` is an `@asynccontextmanager` wrapping `self._http.stream("GET", url)` with the same `_transport_error` translation as `upload`. `fetch_blob` iterates `aiter_bytes()` accumulating into a `bytearray`, raising `BlobTooLarge` the moment the running total exceeds `max_bytes` (never after buffering the whole body). Add `download_mock` to `tests/conftest.py` beside `upload_mock`.
- [ ] **Step 4: Run tests → PASS**, zero warnings; the existing `tests/unit/test_jmap_mail.py` and `test_web_thread.py` stay green (every new field is defaulted).
- [ ] **Step 5: Commit** — `feat(jmap): html bodies, attachments, blob download and downloadUrl`

---

### Task 2: CSS sanitiser (tinycss2) — stylesheets and style attributes

**Files:**
- Create: `mailosh/render/__init__.py`, `mailosh/render/css_sanitize.py`
- Modify: `pyproject.toml` (`tinycss2>=1.5`)
- Test: `tests/unit/test_css_sanitize.py`

**Interfaces:** `ALLOWED_PROPERTIES`, `ALLOWED_FUNCTIONS`, `ALLOWED_AT_RULES`, `MAX_CSS_BYTES`, `sanitize_declarations`, `sanitize_stylesheet` — exactly as in the Interfaces block.

`ALLOWED_PROPERTIES` is an explicit allow-list, not a deny-list: `background-color`, `border`, `border-*` (`top|right|bottom|left|width|style|color|radius|collapse|spacing`), `caption-side`, `clear`, `color`, `direction`, `display`, `empty-cells`, `float`, `font`, `font-family`, `font-size`, `font-style`, `font-variant`, `font-weight`, `height`, `letter-spacing`, `line-height`, `list-style`, `list-style-position`, `list-style-type`, `margin`, `margin-*`, `max-height`, `max-width`, `min-height`, `min-width`, `opacity`, `overflow-wrap`, `padding`, `padding-*`, `table-layout`, `text-align`, `text-decoration`, `text-indent`, `text-transform`, `vertical-align`, `visibility`, `white-space`, `width`, `word-break`, `word-spacing`. Notably absent and therefore dropped: `position`, `top`/`right`/`bottom`/`left`, `z-index`, `background`, `background-image`, `behavior`, `-moz-binding`, `filter`, `content`, `transform`, `animation`, `transition`, `cursor`, `pointer-events`, `src`, `all`.

`ALLOWED_FUNCTIONS` = `{"rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp", "var"}`. Any other `FunctionBlock` — `expression`, `url`, `attr`, `image-set`, `element`, `-moz-element`, `-webkit-image-set` — drops the whole declaration.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_css_sanitize.py
import pytest
from mailosh.render import css_sanitize as css

def parse(out):
    """Parse sanitiser output back into {property: value} so tests assert on
    declarations, not on serialisation whitespace."""
    import tinycss2
    return {
        d.lower_name: tinycss2.serialize(d.value).strip()
        for d in tinycss2.parse_blocks_contents(out, skip_comments=True, skip_whitespace=True)
        if getattr(d, "lower_name", None)
    }

@pytest.mark.parametrize("payload", [
    "position: fixed",
    "position: sticky",
    "width: expression(alert(1))",
    "behavior: url(#default#time2)",
    "-moz-binding: url(http://evil/x.xml)",
    "background-image: url(http://evil/px.gif)",
    "background: url('http://evil/px.gif')",
    "color: rgb(0,0,0); background-image: URL(http://evil/px.gif)",
    "content: '</style><script>alert(1)</script>'",
    "width: attr(data-x)",
    "background-image: image-set('http://evil/a.png' 1x)",
    "top: 0; left: 0; z-index: 99999",
])
def test_declaration_payloads_are_dropped(payload):
    out = css.sanitize_declarations(payload)
    decls = parse(out)
    assert "position" not in decls and "background-image" not in decls
    assert "behavior" not in decls and "-moz-binding" not in decls
    assert "content" not in decls and "top" not in decls and "z-index" not in decls
    assert "url(" not in out.lower()
    assert "expression" not in out.lower()
    assert "<" not in out

def test_safe_declarations_survive_with_important_and_case_folding():
    out = css.sanitize_declarations("COLOR: Red !important; Font-Weight: 700; margin:0 auto")
    decls = parse(out)
    assert decls["color"].lower() == "red"
    assert decls["font-weight"] == "700"
    assert decls["margin"] == "0 auto"
    assert len(decls) == 3

def test_empty_result_is_empty_string_not_whitespace():
    assert css.sanitize_declarations("position:fixed") == ""
    assert css.sanitize_declarations("") == ""
    assert css.sanitize_declarations("}}}garbage{{{") == ""

def test_stylesheet_drops_import_fontface_and_keeps_media():
    out = css.sanitize_stylesheet(
        '@charset "utf-8";'
        '@import url("http://evil/x.css");'
        "@font-face { font-family: E; src: url(http://evil/f.woff) }"
        "@namespace svg url(http://www.w3.org/2000/svg);"
        "@media screen and (max-width: 600px) { .a { color: red; position: fixed } }"
        "p { color: blue }"
    )
    assert "@import" not in out and "@font-face" not in out
    assert "@charset" not in out and "@namespace" not in out
    assert "@media screen and (max-width: 600px)" in out
    assert "position" not in out
    assert out.count("color") == 2

def test_stylesheet_cannot_break_out_of_the_style_element():
    # tinycss2's serializer decodes \3c back to a literal "<" and never
    # re-escapes it, so a string literal is a real </style> breakout vector.
    for payload in [
        'p { font-family: "</style><script>alert(1)</script>" }',
        'p { font-family: "a\\3c /style>b" }',
        'a[title="</style><img src=x onerror=alert(1)>"] { color: red }',
    ]:
        out = css.sanitize_stylesheet(payload)
        assert "<" not in out
        assert "script" not in out.lower()

def test_nested_media_is_recursed_not_passed_through():
    out = css.sanitize_stylesheet("@media print { @import url(http://evil/x.css); p { color: red } }")
    assert "@import" not in out and "evil" not in out
    assert "color" in out

def test_oversized_input_is_refused_whole():
    assert css.sanitize_stylesheet("p{color:red}" + "/*" + "x" * css.MAX_CSS_BYTES + "*/") == ""
    assert css.sanitize_declarations("color:red;" + "a" * css.MAX_CSS_BYTES) == ""

def test_a_breakout_swallows_the_rule_it_lands_in_and_nothing_else_leaks():
    # tinycss2 parses everything after `p{...}` as ONE qualified rule whose
    # prelude is `</style><script>alert(1)</script> div`. The `<` in that
    # prelude drops the whole rule -- including the `div` selector welded to
    # it -- which is the correct trade: losing one rule beats emitting a `<`.
    out = css.sanitize_stylesheet("p{color:red} </style><script>alert(1)</script> div{color:blue}")
    assert "<" not in out and "script" not in out.lower()
    assert out.count("color") == 1
    assert "div" not in out
```

- [ ] **Step 2: Run → FAIL** — `make test` → `ModuleNotFoundError: mailosh.render`.
- [ ] **Step 3: Implement.** Add `tinycss2>=1.5` to `pyproject.toml` and `.venv/bin/pip install -e '.[dev]'`. `sanitize_declarations`: bail to `""` when `len(css) > MAX_CSS_BYTES`; `tinycss2.parse_blocks_contents(css, skip_comments=True, skip_whitespace=True)`; keep only `ast.Declaration` nodes whose `lower_name` is in `ALLOWED_PROPERTIES`; reject a declaration when any component value is an `ast.URLToken`, or an `ast.FunctionBlock` whose `lower_name` is not in `ALLOWED_FUNCTIONS` (recursing into `FunctionBlock.arguments`, `ParenthesesBlock.content`, `SquareBracketsBlock.content` and `CurlyBracketsBlock.content` so a nested `url(` inside `calc(` is caught), or when `tinycss2.serialize(d.value)` contains `<`; re-emit as `f"{name}:{serialize(value).strip()}"` joined by `;`, appending `" !important"` when `d.important`. `sanitize_stylesheet`: same size bail; `tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)`; drop every `ast.ParseError`; for `ast.QualifiedRule` reject the rule when `serialize(prelude)` contains `<`, else emit `prelude + "{" + sanitize of its content + "}"` (reusing the declaration walker, so the two paths cannot diverge); for `ast.AtRule` keep only `at_keyword.lower() in ALLOWED_AT_RULES` — for `@media`, reject when the prelude contains `<`, then recurse with `tinycss2.parse_rule_list(rule.content, …)` and emit `@media <prelude>{ … }`. Finish with one belt-and-braces assertion: if `"<" in result`, return `""`. Write that last check as real code with a comment naming the `</style>` breakout it exists for — it is the difference between "we filtered the payloads we thought of" and "nothing that reaches a `<style>` element can contain a `<`".
- [ ] **Step 4: Run tests → PASS**, zero warnings, `ruff check .` clean.
- [ ] **Step 5: Commit** — `feat(render): tinycss2 css sanitiser for mail stylesheets and style attributes`

---

### Task 3: HTML sanitiser (nh3) — allow-lists, attribute filter, URL policy

This is the highest-consequence task in the plan and is reviewed on its own. Everything it produces is fed to a browser as a document; every rule below exists because a probe against nh3 0.3.7 showed the library does **not** do it for you.

**Files:**
- Create: `mailosh/render/html_sanitize.py`, `tests/helpers.py` (the `parse_attrs` helper), `tests/fixtures/xss/` (the adversarial corpus, one file per family)
- Modify: `pyproject.toml` (`nh3>=0.3.7`), `NOTICE` (fix the DOMPurify sentence), `tests/conftest.py` (add the `xss_corpus` fixture)
- Test: `tests/unit/test_html_sanitize.py`

**Interfaces:** `ALLOWED_TAGS`, `ALLOWED_ATTRIBUTES`, `CLEAN_CONTENT_TAGS`, `URL_SCHEMES`, `DATA_IMAGE_TYPES`, `SanitizeContext`, `SanitizeResult`, `extract_styles`, `sanitize_email_html` — exactly as in the Interfaces block.

Five nh3 behaviours this task must code against, all verified against 0.3.7:

1. **`attribute_filter` runs before the URL-scheme and `url_relative` checks.** A filter that returns a relative URL under `url_relative="deny"` has its result deleted. Every rewrite must return an absolute URL.
2. **The filter sees raw, un-normalised values.** `CID:ABC` and `"  cid:abc  "` both reach it verbatim, and both pass nh3's own (case- and whitespace-insensitive) scheme check. Compare on `value.strip().lower()`, never on `value`.
3. **The filter is also called for the attributes nh3 itself injects** — `("a", "target", "_blank")` and `("a", "rel", "noopener noreferrer nofollow")`. A filter that returns `None` for unrecognised attributes strips its own `rel`.
4. **`url_schemes` is global, not per-tag.** With `data` in the set, `<a href="data:text/html,…">` survives. `data:` must be gated to `img` *and* to an `image/*` type inside the filter.
5. **Three configurations are hard errors, one of them a Rust panic.** `rel` in `attributes["a"]` together with `link_rel` raises `ValueError`; a tag in both `tags` and `clean_content_tags` raises `ValueError`; `allowed_classes` together with `class` in `attributes` **panics the extension module** (`PanicException`, not a catchable-by-accident error). Never pass `allowed_classes`.

`ALLOWED_TAGS`: `a abbr acronym address article aside b bdi bdo big blockquote br caption center cite code col colgroup dd del details dfn dir div dl dt em figcaption figure font footer h1 h2 h3 h4 h5 h6 header hgroup hr i img ins kbd li main map mark menu nav ol p pre q rp rt ruby s samp section small span strike strong sub summary sup table tbody td tfoot th thead time tr tt u ul var wbr`.

`CLEAN_CONTENT_TAGS` (tag **and** contents removed, and disjoint from `ALLOWED_TAGS`): `script style title textarea noscript iframe frame frameset object embed applet form input button select option optgroup label fieldset legend base link meta template portal dialog canvas audio video source track math svg marquee plaintext xmp`.

`ALLOWED_ATTRIBUTES`:
```python
{
    "*": {"class", "id", "dir", "lang", "title", "style", "align", "valign",
          "bgcolor", "width", "height"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "table": {"border", "cellpadding", "cellspacing", "summary"},
    "td": {"colspan", "rowspan", "headers", "scope", "nowrap"},
    "th": {"colspan", "rowspan", "abbr", "headers", "scope", "nowrap"},
    "col": {"span"}, "colgroup": {"span"},
    "ol": {"start", "type", "reversed"}, "li": {"value"},
    "time": {"datetime"},
    "blockquote": {"cite", "type"},   # `type` is inert markup but carries Mozilla's
                                      # blockquote[type=cite] quote marker (Task 8)
    "q": {"cite"}, "del": {"cite", "datetime"}, "ins": {"cite", "datetime"},
    "details": {"open"},
}
```
`srcset`, `poster`, `background`, `formaction`, `ping`, `usemap` and every `on*` handler are absent by construction and must be asserted absent.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_html_sanitize.py
import pytest
from helpers import parse_attrs   # tests/helpers.py; html -> {tag: [ {attr: value}, ... ]}

from mailosh.render.html_sanitize import SanitizeContext, extract_styles, sanitize_email_html

CTX = SanitizeContext(
    email_id="E1", origin="https://mail.test", remote=False,
    cid_parts={"logo@mail": "B3"}, sign_image=None,
)
CTX_REMOTE = SanitizeContext(
    email_id="E1", origin="https://mail.test", remote=True,
    cid_parts={"logo@mail": "B3"}, sign_image=lambda url: "TOK",
)

def attrs(html, ctx=CTX):
    return parse_attrs(sanitize_email_html(html, ctx).html)

# --- script execution -------------------------------------------------
@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<div onmouseover='alert(1)'>x</div>",
    "<svg><script>alert(1)</script></svg>",
    "<svg><animate onbegin=alert(1) attributeName=x dur=1s>",
    "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
    "<object data='javascript:alert(1)'></object>",
    "<embed src='javascript:alert(1)'>",
    "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
    '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">',
    "<xmp><p title='</xmp><img src=x onerror=alert(1)>'>",
    "<template><script>alert(1)</script></template>",
])
def test_no_payload_survives_as_script_or_handler(payload):
    out = sanitize_email_html(payload, CTX).html
    assert "alert" not in out
    assert "<script" not in out.lower()
    for tag, instances in parse_attrs(out).items():
        for a in instances:
            assert not any(k.startswith("on") for k in a), (tag, a)

# --- URL schemes ------------------------------------------------------
@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)",
    "  javascript:alert(1)  ", "vbscript:msgbox(1)", "data:text/html,<b>hi",
    "data:image/svg+xml,<svg onload=alert(1)>", "file:///etc/passwd",
    "//evil.test/x", "/relative", "#anchor", "cid:logo@mail",
])
def test_dangerous_hrefs_leave_no_href_at_all(href):
    anchors = attrs(f'<a href="{href}">x</a>').get("a", [])
    assert len(anchors) == 1
    assert "href" not in anchors[0]

def test_safe_hrefs_survive_with_target_and_rel_intact():
    a = attrs('<a href="https://ok.test/p?q=1&amp;r=2">x</a>')["a"][0]
    assert a["href"] == "https://ok.test/p?q=1&r=2"
    assert a["target"] == "_blank"
    assert set(a["rel"].split()) == {"noopener", "noreferrer", "nofollow"}
    m = attrs('<a href="mailto:a@b.test">x</a>')["a"][0]
    assert m["href"] == "mailto:a@b.test"

# --- images -----------------------------------------------------------
def test_cid_is_rewritten_to_an_absolute_same_origin_url():
    img = attrs('<img src="cid:logo@mail">')["img"][0]
    assert img["src"] == "https://mail.test/m/E1/cid/logo%40mail"

def test_cid_case_and_whitespace_variants_are_rewritten_too():
    # nh3 hands the filter the raw value; it does not fold case or trim.
    assert attrs('<img src="CID:logo@mail">')["img"][0]["src"].endswith("/cid/logo%40mail")
    assert attrs('<img src="  cid:logo@mail  ">')["img"][0]["src"].endswith("/cid/logo%40mail")

def test_unknown_cid_leaves_no_src():
    assert "src" not in attrs('<img src="cid:missing@mail">')["img"][0]

def test_data_uri_allowed_only_on_img_and_only_for_image_types():
    assert attrs('<img src="data:image/png;base64,AAA">')["img"][0]["src"].startswith("data:image/png")
    assert "src" not in attrs('<img src="data:image/svg+xml,<svg>">')["img"][0]
    assert "src" not in attrs('<img src="data:text/html,x">')["img"][0]
    assert "href" not in attrs('<a href="data:image/png;base64,AAA">x</a>')["a"][0]

def test_remote_images_blocked_by_default_and_counted():
    result = sanitize_email_html(
        '<img src="https://track.test/a.gif"><img src="http://track.test/b.gif">'
        '<img src="https://cdn.test/c.png">', CTX)
    assert result.blocked_remote == 3
    assert set(result.remote_hosts) == {"track.test", "cdn.test"}
    assert all("src" not in i for i in parse_attrs(result.html)["img"])

def test_remote_images_go_through_the_proxy_when_allowed():
    result = sanitize_email_html('<img src="https://track.test/a.gif">', CTX_REMOTE)
    assert result.blocked_remote == 0
    assert parse_attrs(result.html)["img"][0]["src"] == "https://mail.test/img?u=TOK"

def test_srcset_background_and_poster_are_never_emitted():
    out = sanitize_email_html(
        '<img src="https://a.test/x.png" srcset="https://evil.test/y.png 2x">'
        '<table><tr><td background="https://evil.test/z.png">c</td></tr></table>',
        CTX_REMOTE).html
    for instances in parse_attrs(out).values():
        for a in instances:
            assert "srcset" not in a and "background" not in a and "poster" not in a
    assert "evil.test" not in out

# --- style, base, forms ----------------------------------------------
def test_style_attributes_are_css_sanitised_not_merely_property_filtered():
    d = attrs('<div style="position:fixed;top:0;color:red;background:url(http://evil/x)">y</div>')["div"][0]
    assert d["style"] == "color:red"
    assert "style" not in attrs('<div style="position:fixed">y</div>')["div"][0]

def test_base_and_form_are_removed_and_cannot_reparent_relative_urls():
    out = sanitize_email_html(
        '<base href="https://evil.test/"><form action="https://evil.test/steal">'
        '<input name="p"></form><a href="/x">y</a>', CTX).html
    assert "<base" not in out.lower() and "<form" not in out.lower()
    assert "evil.test" not in out
    assert "href" not in parse_attrs(out)["a"][0]

def test_mail_ids_reserved_for_frame_chrome_are_stripped():
    divs = attrs('<div id="mailosh-quote">x</div><div id="MAILOSH-Quote">y</div><div id="ok">z</div>')["div"]
    assert len(divs) == 3
    assert sorted(d.get("id") or "" for d in divs) == ["", "", "ok"]

# --- <style> extraction -----------------------------------------------
def test_style_blocks_are_extracted_and_the_element_never_reaches_the_output():
    html = ('<style>@import url(http://evil/x.css); p{color:red;position:fixed}</style>'
            '<STYLE TYPE="text/css">.a{color:blue}</STYLE ><p>hi</p>')
    result = sanitize_email_html(html, CTX)
    assert "<style" not in result.html.lower()
    assert "@import" not in result.css and "position" not in result.css
    assert result.css.count("color") == 2
    assert "<" not in result.css

def test_extract_styles_finds_every_block_including_nested_and_uppercase():
    blocks = extract_styles('<div><style>a{}</style></div><STYLE>b{}</STYLE ><style media="print">c{}</style>')
    assert len(blocks) == 3

# --- corpus -----------------------------------------------------------
def test_xss_corpus_files_all_come_out_inert(xss_corpus):
    # Asserted on the parsed tree, not on the text: a corpus file may
    # legitimately contain the word "javascript:" in a text node, and a
    # substring check would either fail on that or pass on a real leak.
    for name, html in xss_corpus:
        out = sanitize_email_html(html, CTX).html
        parsed = parse_attrs(out)
        assert "script" not in parsed and "iframe" not in parsed, name
        assert "object" not in parsed and "embed" not in parsed, name
        for tag, instances in parsed.items():
            for a in instances:
                assert not any(k.startswith("on") for k in a), (name, tag, a)
                for value in a.values():
                    v = value.strip().lower()
                    assert not v.startswith(("javascript:", "vbscript:", "data:text")), (name, v)
```

- [ ] **Step 2: Run → FAIL** — `make test` → `ModuleNotFoundError: mailosh.render.html_sanitize`.
- [ ] **Step 3: Implement.** Add `nh3>=0.3.7` to `pyproject.toml`, reinstall. `extract_styles` uses an `html.parser.HTMLParser` subclass that flips a flag on `<style>` and collects `handle_data` (HTMLParser puts `style` into CDATA mode, so the CSS arrives raw, and it accepts `</style >` and `</STYLE>`) — it never re-emits markup, so a divergence between its parse and html5ever's cannot inject anything; the only thing that crosses is CSS text, which `css_sanitize.sanitize_stylesheet` then re-parses and re-serialises. `sanitize_email_html` builds a closure `attribute_filter(tag, attr, value)`:
  - normalise once: `v = value.replace("\x00", "").strip()`, `lv = v.lower()`, `scheme = lv.split(":", 1)[0] if re.match(r"^[a-z][a-z0-9+.\-]*:", lv) else None`;
  - `attr == "style"` → `css_sanitize.sanitize_declarations(value) or None`;
  - `attr == "id"` and `lv.startswith("mailosh-")` → `None` (the frame's own chrome ids are reserved so a mail cannot impersonate the quote container);
  - `tag == "img" and attr == "src"`: `cid:` → look up `ctx.cid_parts[v[4:].strip("<>")]`, return `f"{ctx.origin}/m/{ctx.email_id}/cid/{quote(cid, safe='')}"` or `None` when unknown; `data:` → allow only when the media type before `;`/`,` is `image/<t>` with `t in DATA_IMAGE_TYPES`, else `None`; `http:`/`https:` → when `ctx.remote and ctx.sign_image` return `f"{ctx.origin}/img?u={ctx.sign_image(v)}"`, else record the host in a counter set and return `None`; anything else → `None`;
  - `tag == "a" and attr == "href"` → keep only when `scheme in {"http", "https", "mailto"}`, else `None`;
  - everything else → `value` **unchanged** (this branch is what keeps nh3's own injected `rel`/`target`).
  Call `nh3.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, clean_content_tags=CLEAN_CONTENT_TAGS, attribute_filter=filter, url_schemes=set(URL_SCHEMES), url_relative="deny", link_rel="noopener noreferrer nofollow", set_tag_attribute_values={"a": {"target": "_blank"}}, filter_style_properties=set(css_sanitize.ALLOWED_PROPERTIES), strip_comments=True)`. Do **not** pass `allowed_classes`. Return `SanitizeResult(html=…, css="\n".join(sanitize_stylesheet(b) for b in extract_styles(html)), blocked_remote=…, remote_hosts=tuple(sorted(hosts)))`. Write `tests/helpers.py` with `parse_attrs(html) -> dict[str, list[dict[str, str]]]` (an `HTMLParser` subclass with `convert_charrefs=True`, collecting every start and start-end tag) and add an `xss_corpus` fixture to `tests/conftest.py` yielding `(name, text)` pairs; the corpus is one file per family under `tests/fixtures/xss/` (`handlers.html`, `schemes.html`, `css.html`, `mxss.html`, `svg_math.html`, `forms.html`, `meta_base.html`). In `NOTICE`, rewrite the two-sentence paragraph about Squire and DOMPurify so it says both belong to the compose editor (Phase 1C) and that Phase 1B's sanitiser is server-side (`nh3` + `tinycss2`) and loads neither.
- [ ] **Step 4: Run tests → PASS**, zero warnings. Then run the corpus by hand once and read the output: `.venv/bin/python -c "from mailosh.render.html_sanitize import *; import pathlib; ..."` printing each file's sanitised form, and eyeball it. A green assertion is not the same as having looked at what the sanitiser produced.
- [ ] **Step 5: Commit** — `feat(render): nh3 email html sanitiser with adversarial corpus`

---

### Task 4: Frame document, `GET /m/{id}/html`, hash-pinned resize script (spec §15 gate)

Spec §15 names this as the first thing 1B must prove: the hash-pinned script works in Chrome, Firefox and Safari, and `allow-same-origin` is never needed.

**Files:**
- Create: `mailosh/render/frame_document.py`, `mailosh/web/frames.py`, `mailosh/web/templates/thread/frame.html`, `mailosh/web/static/js/frame.js`, `docs/spikes/p1b-findings.md` (with the ten headings Task 14 lists; this task fills in "Frame delivery" as it runs, and every later task appends its own section, exactly as 1A's Task 4 did)
- Modify: `mailosh/web/app.py` (mount `frames.router`), `mailosh/web/mail.py` (thread route passes `origin` and per-message frame urls), `mailosh/web/templates/thread/page.html`, `mailosh/web/templates/layouts/app.html` (load `frame.js`), `styles/input.css`, `tests/conftest.py` (the `fake` / `authed` / `csrf` fixtures below)
- Test: `tests/unit/test_frame_document.py`, `tests/unit/test_frame_routes.py`

**Test fixtures this task introduces** (every task from here on uses them, so they are defined once, here):
- `fake` — a `FakeClient` returned by `deps.client_for`, recording every call. Builders: `fake.message(email_id, *, html=None, text=None, sender="a@x.test", subject="s", seen=True, blob_id=None, attachments=())` registers one message; `fake.thread(thread_id, messages, *, subject="s", unread=(), attachments=())` registers a thread, where `messages` is either a list of email ids or a list of `(email_id, html, text)` tuples; `fake.blob(blob_id, data)` registers blob bytes; `fake.list(key, *, total, ids=(), thread_at=None)` backs `query_page`. Recorders: `fake.query_calls` (every `Email/query` issued) and `fake.raise_on_thread` (set to an exception instance to make `get_thread` raise).
- `authed` — an `httpx.AsyncClient` against the app with a live session cookie and the `X-CSRF-Token` header pre-set; `authed_no_csrf` is the same without the header; `csrf` is the header dict on its own; `app_client` is unauthenticated.

**Interfaces:**
- `FRAME_SCRIPT`, `FRAME_SCRIPT_HASH`, `MIN_FRAME_HEIGHT = 200`, `MAX_FRAME_HEIGHT = 20_000`, `csp_header()`, `render_frame(...)` per the Interfaces block. The document is assembled in **Python string concatenation, not Jinja**, because the CSP names a `sha256` of the script's exact bytes and a template's whitespace handling is not a contract.
- `csp_header()` returns exactly, in this order:
  `sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; frame-ancestors 'self'; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src '<FRAME_SCRIPT_HASH>'`
- `GET /m/{email_id}/html?remote=0|1&theme=system|light|dark&restyle=0|1&expand=0|1` → `text/html`, session-required, headers `Content-Security-Policy` (above), `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`. `restyle` is honoured from Task 12 onward and accepted (and ignored) before then; `expand` is the print page's (Task 13).
- `GET /m/{email_id}/frame?thread={thread_id}&remote=0|1` → the `thread/frame.html` partial. In this task it renders the `<iframe>` alone; Task 7 adds the remote-image banner above it. The partial exists from here so that Task 7's banner buttons can be plain htmx swaps of a target that already has a URL, with no bespoke JS.
- The iframe element, everywhere it is rendered: `<iframe class="mail-frame" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" loading="lazy" title="Message content" src="/m/{id}/html?…">`. **`allow-same-origin` appears nowhere in the repository.**

`FRAME_SCRIPT` is exactly this text — no leading or trailing whitespace, no template interpolation:

```js
(function () {
  var LAST = 0;
  function post() {
    var d = document.documentElement, b = document.body;
    var h = Math.max(d.scrollHeight, d.offsetHeight, b ? b.scrollHeight : 0, b ? b.offsetHeight : 0);
    if (h === LAST) return;
    LAST = h;
    parent.postMessage({ type: "mailosh:frame-height", height: h }, "*");
  }
  var t = document.querySelector("[data-mailosh-quote-toggle]");
  var q = document.querySelector("[data-mailosh-quote]");
  if (t && q) {
    t.addEventListener("click", function () {
      var opening = q.hasAttribute("hidden");
      if (opening) { q.removeAttribute("hidden"); } else { q.setAttribute("hidden", ""); }
      t.setAttribute("aria-expanded", opening ? "true" : "false");
      post();
    });
  }
  window.addEventListener("load", post);
  window.addEventListener("resize", post);
  if (window.ResizeObserver) { new ResizeObserver(post).observe(document.documentElement); }
  setTimeout(post, 0); setTimeout(post, 300); setTimeout(post, 1200);
})();
```

`frame.js` (parent side) registers **one** `message` listener for the whole app and, before touching anything, checks `event.origin === "null"` (a sandboxed document with no `allow-same-origin` has an opaque origin, which serialises as the string `"null"`), then finds the `iframe.mail-frame` whose `contentWindow === event.source`, then clamps `Number(data.height)` into `[MIN_FRAME_HEIGHT, MAX_FRAME_HEIGHT]` before assigning `style.height`. A message that fails any of those three checks returns without effect.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_frame_document.py
import base64
import hashlib

from mailosh.render import frame_document as fd

def test_hash_matches_the_script_bytes_exactly():
    digest = hashlib.sha256(fd.FRAME_SCRIPT.encode("utf-8")).digest()
    assert fd.FRAME_SCRIPT_HASH == "sha256-" + base64.b64encode(digest).decode()

def test_csp_is_exactly_the_agreed_policy():
    assert fd.csp_header() == (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
        "frame-ancestors 'self'; default-src 'none'; img-src 'self' data:; "
        "style-src 'unsafe-inline'; script-src '" + fd.FRAME_SCRIPT_HASH + "'"
    )

def test_document_embeds_the_script_verbatim_so_the_hash_holds():
    doc = fd.render_frame(visible_html="<p>x</p>", quoted_html="", mail_css="p{color:red}",
                          theme="light", restyle="none")
    start = doc.index("<script>") + len("<script>")
    assert doc.count("<script>") == 1
    embedded = doc[start:doc.index("</script>", start)]
    assert embedded == fd.FRAME_SCRIPT

def test_quoted_half_renders_hidden_behind_a_toggle_only_when_present():
    with_quote = fd.render_frame(visible_html="<p>a</p>", quoted_html="<p>b</p>",
                                 mail_css="", theme="light", restyle="none")
    assert with_quote.count("data-mailosh-quote-toggle") == 1
    assert 'data-mailosh-quote hidden' in with_quote or 'hidden data-mailosh-quote' in with_quote
    without = fd.render_frame(visible_html="<p>a</p>", quoted_html="",
                              mail_css="", theme="light", restyle="none")
    assert "data-mailosh-quote-toggle" not in without

def test_expand_renders_the_quote_open_with_no_toggle():
    doc = fd.render_frame(visible_html="<p>a</p>", quoted_html="<p>b</p>", mail_css="",
                          theme="light", restyle="none", expand=True)
    assert "data-mailosh-quote-toggle" not in doc
    assert "hidden" not in doc
    assert "<p>b</p>" in doc

def test_mail_css_is_placed_in_its_own_style_element_after_the_base_css():
    doc = fd.render_frame(visible_html="", quoted_html="", mail_css=".mail{color:red}",
                          theme="dark", restyle="none")
    assert doc.count("<style>") == 2
    assert doc.index("max-width") < doc.index(".mail{color:red}")
    assert '<meta name="color-scheme"' in doc
```

```python
# tests/unit/test_frame_routes.py
import base64, hashlib, pathlib, re

STATIC = pathlib.Path("mailosh/web/static")
TEMPLATES = pathlib.Path("mailosh/web/templates")

def test_allow_same_origin_appears_nowhere_in_the_repository():
    hits = [p for p in list(TEMPLATES.rglob("*.html")) + list(STATIC.rglob("*.js"))
            if "allow-same-origin" in p.read_text()]
    assert hits == []

def test_frame_js_registers_exactly_one_message_listener_app_wide():
    total = sum(p.read_text().count('addEventListener("message"')
                for p in STATIC.rglob("*.js"))
    assert total == 1

def test_frame_js_guards_precede_the_height_assignment():
    src = (STATIC / "js/frame.js").read_text()
    origin_check = src.index('event.origin !== "null"')
    source_check = src.index("contentWindow === event.source")
    clamp = src.index("Math.min")
    assign = src.index("style.height")
    assert origin_check < source_check < clamp < assign

def test_frame_js_clamps_with_the_documented_bounds():
    src = (STATIC / "js/frame.js").read_text()
    assert "200" in src and "20000" in src

async def test_html_route_carries_the_exact_csp_and_the_script_hash_matches_the_body(authed, fake):
    fake.message("E1", html="<p>hello</p>")
    r = await authed.get("/m/E1/html?remote=0&theme=light")
    assert r.status_code == 200
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "allow-same-origin" not in csp
    assert "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox" in csp
    assert "img-src 'self' data:" in csp
    assert "http:" not in csp and "https:" not in csp
    body = r.text
    script = body[body.index("<script>") + 8:body.index("</script>")]
    want = "sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src '{want}'" in csp

async def test_html_route_csp_is_identical_with_and_without_remote_images(authed, fake):
    fake.message("E1", html='<img src="https://track.test/a.gif">')
    a = await authed.get("/m/E1/html?remote=0")
    b = await authed.get("/m/E1/html?remote=1")
    assert a.headers["content-security-policy"] == b.headers["content-security-policy"]

async def test_html_route_requires_a_session(app_client):
    assert (await app_client.get("/m/E1/html")).status_code in (303, 401)

async def test_iframe_never_requests_same_origin(authed, fake):
    fake.message("E1", html="<p>x</p>")
    frag = (await authed.get("/m/E1/frame?thread=T1&remote=0")).text
    attrs = re.search(r'<iframe([^>]*)>', frag).group(1)
    assert "allow-same-origin" not in attrs
    assert 'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"' in attrs
    assert 'referrerpolicy="no-referrer"' in attrs
```

- [ ] **Step 2: Run → FAIL** — `make test` → `ModuleNotFoundError: mailosh.render.frame_document`.
- [ ] **Step 3: Implement.** `render_frame` concatenates, in order: the head —

```html
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
```

  — then the base `<style>`, which is exactly this. `{scheme}` is `light dark` whenever `restyle == "color-scheme"` (spec §7: a mail that declares its own scheme is simply handed both and left alone), and otherwise `light` / `dark` / `light dark` for `theme` `light` / `dark` / `system`. The two `filter` rules are emitted **only** when `restyle == "invert"`, wrapped in `@media (prefers-color-scheme: dark){…}` when `theme == "system"`:

```css
html{color-scheme:{scheme}}
body{margin:0;padding:12px 14px;font:14px/1.55 Inter,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;word-break:break-word}
*{max-width:100%}
img{max-width:100%;height:auto;border:0}
img:not([src]){display:inline-block;min-width:64px;min-height:24px;border:1px dashed currentColor;border-radius:4px;opacity:.45}
table{max-width:100%}
.mailosh-quote-toggle{display:inline-flex;align-items:center;height:18px;margin:8px 0;padding:0 9px;border:0;border-radius:999px;background:rgba(127,127,127,.22);color:inherit;font:inherit;font-weight:700;letter-spacing:.15em;cursor:pointer}
html{filter:invert(1) hue-rotate(180deg)}
img,[style*="background-image"]{filter:invert(1) hue-rotate(180deg)}
```

  The blocked-image placeholder needs no marker attribute: a blocked image is precisely one whose `src` the sanitiser removed, so `img:not([src])` selects exactly the right set. Then the mail's own `<style>` (second, so mail rules win over the base), then `</head><body>`, the visible half, and — when `quoted_html` is non-empty and `expand` is false — `<button type="button" class="mailosh-quote-toggle" data-mailosh-quote-toggle aria-expanded="false">&#8226;&#8226;&#8226;</button><div data-mailosh-quote hidden>` + the quoted half + `</div>`; with `expand=True` the quoted half is emitted bare, with no button and no `hidden`. Finally `<script>` + `FRAME_SCRIPT` + `</script></body></html>`. `FRAME_SCRIPT_HASH` is computed at import time. `mailosh/web/frames.py` gets `router = APIRouter(tags=["frames"])`; `GET /m/{email_id}/html` loads the message (a `_load_message(client, email_id)` helper doing one `Email/get` by id with `_EMAIL_BODY_PROPS`), sanitises with a `SanitizeContext` built from `str(request.base_url).rstrip("/")`, and returns `HTMLResponse(doc, headers=…)`. When the message has no HTML body, the route 404s (the conversation view renders plain text inline and never asks for a frame). Mount the router in `create_app` next to `mail.router`. `frame.js` is a plain ES module loaded from `layouts/app.html` alongside `app.js`. `thread/page.html` renders the frame partial for every message whose `has_html` is true and keeps the existing plain-text rendering otherwise. Run `make css`.
- [ ] **Step 4: Run tests → PASS**; then the **spec §15 gate**, recorded in `docs/spikes/p1b-findings.md` under "Frame delivery": `make up`, log in, open a thread containing an HTML mail, and in **Chrome, Firefox and Safari** confirm (a) the frame renders with no CSP violation in the console, (b) it auto-sizes to its content and re-sizes when the window changes, (c) the console shows no `Refused to execute inline script` (which would mean the hash is wrong), (d) an inline `cid:` image loads — this is the one thing `img-src 'self'` inside a sandboxed opaque-origin document has to prove, and if any browser refuses it, stop and record the fallback (inline the cid part as a `data:` URI, capped at 256 KB per part and 2 MB per message, and drop `'self'` from `img-src`) before continuing, and (e) from the parent page's console, `window.postMessage({type:"mailosh:frame-height",height:99999},"*")` leaves every frame's height unchanged. Screenshot all three browsers.
- [ ] **Step 5: Commit** — `feat(render): sandboxed mail frame with hash-pinned resize script`

---

### Task 5: Inline `cid:` parts — `GET /m/{id}/cid/{content_id}`

**Files:**
- Modify: `mailosh/web/frames.py`, `mailosh/render/html_sanitize.py` (nothing; the rewrite already exists — this task makes the URL resolve), `tests/conftest.py`
- Test: `tests/unit/test_frame_routes.py` (new cases)

**Interfaces:** `GET /m/{email_id}/cid/{content_id}` — session-required, streams the matching inline part. Matching is on the bare content id (the part's `cid` with any surrounding `<>` stripped, compared case-sensitively as RFC 2392 requires). The response carries `Content-Type` from a fixed allow-list (`image/png`, `image/gif`, `image/jpeg`, `image/webp`, `image/bmp`, `image/x-icon`) and **404s for anything else, `image/svg+xml` included**; plus `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`, `Content-Security-Policy: default-src 'none'; sandbox`, `Referrer-Policy: no-referrer`, `Cache-Control: private, max-age=3600`. Parts over `MAX_INLINE_BYTES = 5 * 1024 * 1024` are 404s, not truncated streams.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_frame_routes.py  (appended)
async def test_cid_part_streams_with_locked_down_headers(authed, fake):
    fake.message("E1", attachments=[{"blobId": "B3", "type": "image/png",
                                     "cid": "logo@mail", "disposition": "inline", "size": 3}])
    fake.blob("B3", b"PNG")
    r = await authed.get("/m/E1/cid/logo%40mail")
    assert r.status_code == 200 and r.content == b"PNG"
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in r.headers["content-security-policy"]

async def test_cid_matching_strips_angle_brackets_from_the_part(authed, fake):
    fake.message("E1", attachments=[{"blobId": "B3", "type": "image/png",
                                     "cid": "<logo@mail>", "disposition": "inline", "size": 3}])
    fake.blob("B3", b"PNG")
    assert (await authed.get("/m/E1/cid/logo%40mail")).status_code == 200

@pytest.mark.parametrize("mime", ["image/svg+xml", "text/html", "application/pdf",
                                  "application/octet-stream"])
async def test_non_image_cid_parts_are_404_not_served(authed, fake, mime):
    fake.message("E1", attachments=[{"blobId": "B3", "type": mime, "cid": "x@m",
                                     "disposition": "inline", "size": 3}])
    fake.blob("B3", b"<svg onload=alert(1)>")
    assert (await authed.get("/m/E1/cid/x%40m")).status_code == 404

async def test_unknown_cid_and_oversized_part_are_404(authed, fake):
    fake.message("E1", attachments=[{"blobId": "B3", "type": "image/png", "cid": "x@m",
                                     "disposition": "inline", "size": 99 * 1024 * 1024}])
    assert (await authed.get("/m/E1/cid/nope%40m")).status_code == 404
    assert (await authed.get("/m/E1/cid/x%40m")).status_code == 404

async def test_end_to_end_a_cid_image_in_a_frame_points_at_this_route(authed, fake):
    fake.message("E1", html='<img src="cid:logo@mail">',
                 attachments=[{"blobId": "B3", "type": "image/png", "cid": "logo@mail",
                               "disposition": "inline", "size": 3}])
    fake.blob("B3", b"PNG")
    doc = (await authed.get("/m/E1/html")).text
    src = re.search(r'<img[^>]*src="([^"]+)"', doc).group(1)
    assert src.endswith("/m/E1/cid/logo%40mail")
    assert (await authed.get(src.replace("http://testserver", ""))).status_code == 200
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** The route reuses `_load_message`, finds the part by `(p.cid or "").strip("<>")`, checks `p.type.split(";")[0].strip().lower()` against the allow-list and `p.size` against `MAX_INLINE_BYTES`, and streams through `client.stream_blob(p.blob_id, mime_type=p.type, name=p.name or "inline")` into a `StreamingResponse`. Build the `cid_parts` map fed to `SanitizeContext` in `GET /m/{id}/html` from the same normalisation (`{(p.cid or "").strip("<>"): p.blob_id for p in msg.attachments if p.cid and p.blob_id}`), so the rewrite and the lookup can never disagree — write them as one shared helper `cid_map(msg)`, not two expressions.
- [ ] **Step 4: Green.** Browser: reopen the newsletter with an inline logo and confirm it renders (this is the second half of Task 4's step-4 check (d), now against a real message).
- [ ] **Step 5: Commit** — `feat(frames): serve inline cid parts to the sandboxed mail frame`

---

### Task 6: Image proxy — `GET /img?u=<signed>` and the SSRF guard

The proxy is a server that fetches an attacker-chosen URL. Treat it as such. It lands **before** the banner and the policy so that `?remote=1` is end-to-end real the moment the gate that offers it exists — a "Show images" button whose images cannot load is a broken branch, not an incremental one.

**Files:**
- Create: `mailosh/security/signing.py`, `mailosh/render/fetch_guard.py`, `mailosh/render/image_policy.py` (the signing half; Task 7 adds the decision half)
- Modify: `mailosh/web/frames.py` (wire `sign_image` into `GET /m/{id}/html`), `tests/conftest.py` (add `token_for` — `lambda url: sign_remote_url(url, secret_key=settings.secret_key, user_id=<the authed user's id>)` — and a `_fake_addrinfo(addresses)` helper returning an async stand-in for `getaddrinfo`; `respx_mock` is respx's own pytest fixture and needs nothing added)
- Test: `tests/unit/test_fetch_guard.py`, `tests/unit/test_img_proxy.py`

**Interfaces:** `sign_payload`/`verify_payload`, `sign_remote_url`/`verify_remote_url`, `BlockedUrl`, `MAX_IMAGE_BYTES`, `MAX_REDIRECTS`, `CONNECT_TIMEOUT`, `TOTAL_TIMEOUT`, `ALLOWED_IMAGE_TYPES`, `check_ip`, `check_url`, `resolve_public`, `fetch_image` — exactly as in the Interfaces block. `mailosh/services/undo.py`'s own `sign`/`verify` are **left alone**; this plan does not refactor working security code to share an abstraction.

- Token: `base64url(json({"u": url, "e": exp, "s": user_id}))` + `"."` + `base64url(hmac_sha256(derive_key(secret_key, b"img"), payload))`, compared with `secrets.compare_digest`, TTL `IMAGE_URL_TTL = 3600`. `verify_remote_url` raises `ValueError` on a bad signature, an expired token, or a `user_id` that is not the caller's — a signed URL is not transferable between accounts.
- `GET /img?u=<token>` is session-required. On any `ValueError` from verification it returns `403` with an empty body; on any `BlockedUrl` or upstream failure it returns `502` with an empty body. It never returns a redirect, never returns a body it did not type-check, and never renders an error page (this URL only ever appears in an `<img>`).
- Outbound request: `httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT), headers={"User-Agent": "Mailosh", "Accept": "image/*"}, cookies={})` — a fresh client per request, so no cookie jar and no connection reuse across users. **No `Referer`, no `Authorization`, no forwarded request headers of any kind.**
- Redirects are followed manually, at most `MAX_REDIRECTS = 3`, and every hop re-runs `check_url` + `resolve_public` from scratch.
- Per-user fan-out cap: an `asyncio.Semaphore(6)` per user id in an `app.state` registry, so a mail with 200 remote images cannot open 200 sockets.
- Response to the browser: `Content-Type` from `ALLOWED_IMAGE_TYPES` (`image/png`, `image/gif`, `image/jpeg`, `image/webp`, `image/bmp`, `image/x-icon`) and nothing else — `image/svg+xml` is a `502`; plus `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`, `Content-Security-Policy: default-src 'none'; sandbox`, `Referrer-Policy: no-referrer`, `Cache-Control: private, max-age=86400`. Not one upstream header is forwarded.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_fetch_guard.py
import pytest
from mailosh.render.fetch_guard import BlockedUrl, check_ip, check_url, resolve_public, fetch_image

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.1.2.3", "0.0.0.0", "10.1.2.3", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "100.64.0.1", "224.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
])
def test_private_and_reserved_addresses_are_blocked(ip):
    with pytest.raises(BlockedUrl):
        check_ip(ip)

@pytest.mark.parametrize("ip", ["1.1.1.1", "93.184.216.34", "2606:4700::1111"])
def test_public_addresses_pass(ip):
    check_ip(ip)

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://x/", "ftp://x/", "http://[::1]/x", "http://127.0.0.1/x",
    "http://localhost/x", "https://192.168.0.1/x", "http://user:pw@evil.test/x",
    "http://evil.test:22/x", "http://evil.test:25/x", "http://", "not-a-url",
])
def test_hostile_urls_are_refused_before_any_socket(url):
    with pytest.raises(BlockedUrl):
        check_url(url)

def test_ordinary_urls_yield_host_and_default_port():
    assert check_url("https://cdn.test/a.png") == ("cdn.test", 443)
    assert check_url("http://cdn.test/a.png") == ("cdn.test", 80)
    assert check_url("http://cdn.test:8080/a.png") == ("cdn.test", 8080)

async def test_a_hostname_resolving_to_any_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr("mailosh.render.fetch_guard._getaddrinfo",
                        _fake_addrinfo(["93.184.216.34", "127.0.0.1"]))
    with pytest.raises(BlockedUrl):
        await resolve_public("split.test", 443)

async def test_redirect_to_a_private_host_is_blocked_at_the_hop(respx_mock):
    respx_mock.get("https://cdn.test/a.png").respond(302, headers={"location": "http://127.0.0.1/x"})
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")

async def test_redirect_chains_stop_at_the_limit(respx_mock):
    for i in range(6):
        respx_mock.get(f"https://cdn.test/{i}").respond(
            302, headers={"location": f"https://cdn.test/{i + 1}"})
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/0")

async def test_oversized_body_is_aborted_not_buffered(respx_mock):
    from mailosh.render.fetch_guard import MAX_IMAGE_BYTES
    respx_mock.get("https://cdn.test/big.png").respond(
        200, headers={"content-type": "image/png"}, content=b"x" * (MAX_IMAGE_BYTES + 4096))
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/big.png")

@pytest.mark.parametrize("ctype", ["text/html", "image/svg+xml", "application/pdf", ""])
async def test_non_image_content_types_are_refused(respx_mock, ctype):
    respx_mock.get("https://cdn.test/x").respond(200, headers={"content-type": ctype}, content=b"..")
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/x")

async def test_no_cookie_or_referer_is_ever_sent(respx_mock):
    route = respx_mock.get("https://cdn.test/a.png").respond(
        200, headers={"content-type": "image/png"}, content=b"PNG")
    await fetch_image("https://cdn.test/a.png")
    sent = route.calls.last.request.headers
    assert "cookie" not in sent and "referer" not in sent and "authorization" not in sent
```

```python
# tests/unit/test_img_proxy.py
import pytest
from mailosh.render.image_policy import sign_remote_url, verify_remote_url

def test_signed_url_roundtrips_and_expires():
    tok = sign_remote_url("https://cdn.test/a.png", secret_key="k" * 40, user_id=7, now=1000.0)
    assert verify_remote_url(tok, secret_key="k" * 40, user_id=7, now=1500.0) == "https://cdn.test/a.png"
    with pytest.raises(ValueError):
        verify_remote_url(tok, secret_key="k" * 40, user_id=7, now=1000.0 + 3601)

def test_signature_is_bound_to_the_user_and_to_the_secret():
    tok = sign_remote_url("https://cdn.test/a.png", secret_key="k" * 40, user_id=7, now=1000.0)
    with pytest.raises(ValueError):
        verify_remote_url(tok, secret_key="k" * 40, user_id=8, now=1000.0)
    with pytest.raises(ValueError):
        verify_remote_url(tok, secret_key="j" * 40, user_id=7, now=1000.0)

def test_tampering_with_the_payload_is_rejected():
    tok = sign_remote_url("https://cdn.test/a.png", secret_key="k" * 40, user_id=7, now=1000.0)
    payload, sig = tok.split(".")
    with pytest.raises(ValueError):
        verify_remote_url(payload[:-2] + "AA." + sig, secret_key="k" * 40, user_id=7, now=1000.0)

async def test_proxy_serves_only_allowlisted_types_with_locked_headers(authed, respx_mock, token_for):
    respx_mock.get("https://cdn.test/a.png").respond(200, headers={
        "content-type": "image/png", "set-cookie": "a=b", "x-upstream": "leak"}, content=b"PNG")
    r = await authed.get("/img?u=" + token_for("https://cdn.test/a.png"))
    assert r.status_code == 200 and r.content == b"PNG"
    assert r.headers["content-type"] == "image/png"
    assert "set-cookie" not in r.headers and "x-upstream" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"

async def test_proxy_rejects_an_unsigned_or_foreign_token(authed):
    assert (await authed.get("/img?u=nonsense")).status_code == 403
    assert (await authed.get("/img")).status_code == 422

async def test_proxy_requires_a_session(app_client, token_for):
    assert (await app_client.get("/img?u=" + token_for("https://cdn.test/a.png"))).status_code in (303, 401)

async def test_proxy_answers_502_and_no_body_on_a_blocked_target(authed, token_for):
    r = await authed.get("/img?u=" + token_for("http://127.0.0.1/x"))
    assert r.status_code == 502 and r.content == b""
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** `check_ip` uses `ipaddress.ip_address` and rejects on any of `is_private`, `is_loopback`, `is_link_local`, `is_reserved`, `is_multicast`, `is_unspecified`; for IPv6 it additionally recurses into `.ipv4_mapped` when present, because `::ffff:127.0.0.1` is none of those as an IPv6 address. `check_url` rejects a scheme outside `{http, https}`, a URL with userinfo, an empty host, a host that parses as an IP failing `check_ip`, and a port outside `{80, 443, 8080, 8443}`. `resolve_public` calls `asyncio.get_running_loop().getaddrinfo` (wrapped as a module-level `_getaddrinfo` so tests can patch one name) and raises `BlockedUrl` if **any** returned address fails `check_ip` — any, not all, so a split-horizon record with one public and one loopback answer is refused. `fetch_image` loops up to `MAX_REDIRECTS`, re-validating each hop, uses `client.stream("GET", url)`, checks the `Content-Type` before reading a byte, then reads `aiter_bytes()` into a `bytearray`, raising `BlockedUrl` the moment the running total exceeds `MAX_IMAGE_BYTES`. After the response headers arrive and before the body is read, read `response.extensions.get("network_stream")` and, when present, `get_extra_info("server_addr")`, and run `check_ip` on the peer address — this is the DNS-rebinding backstop, and it is best-effort by design (it is absent under HTTP/2 and behind a proxy), so it supplements `resolve_public` rather than replacing it; say so in a comment. `GET /m/{id}/html` wires `sign_image=lambda url: sign_remote_url(url, secret_key=settings.secret_key, user_id=user.id)` into its `SanitizeContext`, which is the piece that makes `?remote=1` actually load anything; Task 7's `GET /m/{id}/frame` wires the identical lambda from the same helper (`_sanitize_context(request, msg, user, settings, remote=…)`), so the two routes cannot end up signing differently.
- [ ] **Step 4: Green.** Browser: open a newsletter with real remote images and hit `/m/{id}/html?remote=1` directly; confirm in DevTools' Network panel that every image request goes to `/img?u=…` on the app's own origin and **not one request leaves for the sender's host**.
- [ ] **Step 5: Commit** — `feat(frames): signed remote-image proxy with ssrf guard`

---

### Task 7: Remote-image gate — banner, policy, per-sender allow list

**Files:**
- Modify: `mailosh/render/image_policy.py` (add the decision half beside Task 6's signing half), `mailosh/web/templates/thread/frame.html` (add the banner block), `mailosh/web/frames.py`, `mailosh/db/repo.py`, `styles/input.css`, `tests/conftest.py` (add a `user` fixture — the `AppUser` the `authed` session belongs to, so a DB-level test and a route-level test in the same module agree on whose rows they are reading)
- Test: `tests/unit/test_image_policy.py`, `tests/unit/test_frame_routes.py`

**Interfaces:**
- `decide(db, *, user_id, policy, sender_email, override) -> ImageDecision`. Precedence, highest first: `override` (the reader clicked "Show images" for this message — `1` shows, `0` hides, `None` defers), then `policy == "always"`, then an `ImageSenderAllow` row for `sender_email`, then `policy == "contacts"` with a `Contact` row for `sender_email`, else blocked. `reason` records which branch fired.
- `allow_sender(db, *, user_id, sender_email)` upserts an `ImageSenderAllow` row (idempotent).
- `POST /m/{email_id}/images/allow` (CSRF) — form field `sender`, must equal the message's own From address or the request is `403`; inserts the row and returns the `thread/frame.html` partial with `remote=1`. A reader must not be able to allow-list an arbitrary address by editing a form.
- `GET /m/{email_id}/frame?thread=…&remote=0|1` — `remote` is the per-message override; the route calls `decide` and renders accordingly. The banner's "Show images" is `hx-get="/m/{id}/frame?remote=1"` with `hx-target="closest .msg-frame"` `hx-swap="outerHTML"`; "Always show from {sender}" is `hx-post="/m/{id}/images/allow"` with the same target. **No JavaScript is added by this task** — both controls are htmx swaps, which is also what keeps them working under the Alpine CSP build.
- Banner copy, spec §7 verbatim: `This message has {n} remote image{s} · Show images · Always show from {sender}`. It renders only when `blocked_remote > 0` and the decision was `blocked`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_image_policy.py
import pytest
from mailosh.db import models
from mailosh.render.image_policy import decide, allow_sender

async def test_override_beats_every_policy(db, user):
    assert (await decide(db, user_id=user.id, policy="ask", sender_email="a@x", override=1)).show
    assert not (await decide(db, user_id=user.id, policy="always", sender_email="a@x", override=0)).show

async def test_policy_always_shows_and_ask_blocks(db, user):
    assert (await decide(db, user_id=user.id, policy="always", sender_email="a@x", override=None)).show
    d = await decide(db, user_id=user.id, policy="ask", sender_email="a@x", override=None)
    assert not d.show and d.reason == "blocked"

async def test_allow_listed_sender_shows_under_ask(db, user):
    await allow_sender(db, user_id=user.id, sender_email="A@X.test")
    d = await decide(db, user_id=user.id, policy="ask", sender_email="a@x.test", override=None)
    assert d.show and d.reason == "sender_allowed"

async def test_allow_sender_is_idempotent(db, user):
    await allow_sender(db, user_id=user.id, sender_email="a@x")
    await allow_sender(db, user_id=user.id, sender_email="a@x")
    rows = (await db.execute(models.ImageSenderAllow.__table__.select())).all()
    assert len(rows) == 1

async def test_contacts_policy_reads_the_harvested_table(db, user):
    d = await decide(db, user_id=user.id, policy="contacts", sender_email="a@x", override=None)
    assert not d.show
    db.add(models.Contact(user_id=user.id, email="a@x", count=1))
    await db.commit()
    d = await decide(db, user_id=user.id, policy="contacts", sender_email="a@x", override=None)
    assert d.show and d.reason == "contact"

async def test_missing_sender_never_shows_under_ask_or_contacts(db, user):
    for policy in ("ask", "contacts"):
        assert not (await decide(db, user_id=user.id, policy=policy,
                                 sender_email=None, override=None)).show
```

```python
# tests/unit/test_frame_routes.py  (appended)
async def test_banner_counts_and_offers_both_controls_when_blocked(authed, fake):
    fake.message("E1", html='<img src="https://t.test/a.gif"><img src="https://t.test/b.gif">',
                 sender="news@t.test")
    frag = (await authed.get("/m/E1/frame?thread=T1")).text
    assert "2 remote images" in frag
    assert frag.count("Show images") == 1
    assert frag.count("Always show from") == 1
    assert "remote=1" in frag

async def test_no_banner_when_there_is_nothing_blocked(authed, fake):
    fake.message("E1", html="<p>text only</p>", sender="news@t.test")
    frag = (await authed.get("/m/E1/frame?thread=T1")).text
    assert "remote image" not in frag

async def test_allow_route_rejects_a_sender_that_is_not_the_message_sender(authed, fake, csrf):
    fake.message("E1", html='<img src="https://t.test/a.gif">', sender="news@t.test")
    r = await authed.post("/m/E1/images/allow", data={"sender": "attacker@evil.test"}, headers=csrf)
    assert r.status_code == 403

async def test_allow_route_requires_csrf(authed_no_csrf, fake):
    fake.message("E1", html='<img src="https://t.test/a.gif">', sender="news@t.test")
    assert (await authed_no_csrf.post("/m/E1/images/allow",
                                      data={"sender": "news@t.test"})).status_code == 403

async def test_allowing_a_sender_rerenders_the_frame_with_images_on(authed, fake, csrf):
    fake.message("E1", html='<img src="https://t.test/a.gif">', sender="news@t.test")
    frag = (await authed.post("/m/E1/images/allow",
                              data={"sender": "news@t.test"}, headers=csrf)).text
    assert "remote image" not in frag and "remote=1" in frag
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** `decide` compares addresses case-insensitively (`sender_email.strip().lower()`) on both the allow-list and the contacts lookup; `allow_sender` stores the lowercased address and uses SQLAlchemy's dialect-neutral `session.merge` so the sqlite unit fixture and Postgres behave the same. `POST /m/{id}/images/allow` loads the message first, compares the form's `sender` to `msg.from_[0].email` case-insensitively, and raises `HTTPException(403)` on a mismatch. **CSRF is declared per route, not on the router** — `frames.router` serves mostly GETs (`/m/{id}/html`, `/cid/`, `/img`), and a router-level `Depends(deps.csrf_protect)` would reject every one of them, so the two POSTs in this router each carry their own `dependencies=[Depends(deps.csrf_protect)]`. `GET /m/{id}/frame` builds its `SanitizeContext` through the same `_sanitize_context(request, msg, user, settings, remote=…)` helper `GET /m/{id}/html` uses, so the frame partial and the frame document sanitise identically. Add the banner block to `thread/frame.html` and its styling to `styles/input.css`; run `make css`.
- [ ] **Step 4: Green.** Browser: open a newsletter, confirm the banner counts correctly, "Show images" swaps the frame in place without a page navigation, and "Always show from" survives a reload of the same sender's next message.
- [ ] **Step 5: Commit** — `feat(reading): remote-image gate with per-sender allow list`

---

### Task 8: Quote trimming — HTML selectors and plain-text heuristics

**Files:**
- Create: `mailosh/render/quote_trim.py`, `mailosh/render/plain_text.py`, `tests/fixtures/mail/quotes/` (one file per client family: `gmail.html`, `outlook_web.html`, `outlook_desktop.html`, `apple.html`, `thunderbird.html`, `yahoo.html`, `protonmail.html`), plus a `quote_fixtures` fixture in `tests/conftest.py` yielding `(name, html, expected_visible_snippet)` — the snippet is a distinctive phrase from each file's reply half, written into the file as `<p>REPLY-{name}</p>`
- Modify: `mailosh/web/app.py` (delete `split_quoted`; it moves into `plain_text`), `mailosh/web/mail.py` (drop the cross-module import of it), `tests/unit/test_web_thread.py` (import moves), `styles/input.css` (the four quote-depth colours)
- Test: `tests/unit/test_quote_trim.py`, `tests/unit/test_plain_text.py`

**Interfaces:** `QuoteMatcher`, `QUOTE_MATCHERS`, `split_html`, `find_quote_start`, `split_plain`, `quote_depth`, `TextLine`, `render_plain`, `linkify` — exactly as in the Interfaces block.

`QUOTE_MATCHERS`, from spec §7 verbatim, in this order: `.gmail_quote`, `blockquote[type=cite]`, `.moz-cite-prefix`, `#divRplyFwdMsg`, `.yahoo_quoted`, `div[id^=appendonsend]`, `.ms-outlook-mobile-reference-message`, `#OLK_SRC_BODY_SECTION`, `.protonmail_quote`, `.mailosh_quote`. Plus the two textual heuristics, applied only when no matcher hit: a `<div>`/`<p>` whose text content matches `^\s*On\b.{0,300}\bwrote:\s*$`, and one matching `^\s*-{2,}\s*Original Message\s*-{2,}\s*$` (case-insensitive).

`split_html` works by **offset slicing, never re-serialisation**: an `HTMLParser` subclass walks the already-sanitised fragment, and at the first start tag matching a `QuoteMatcher` records the byte offset of that tag's `<` (computed from `getpos()` against a precomputed line-start table). `visible = html[:offset]`, `quoted = html[offset:]`. Nothing is re-emitted, so the split cannot introduce markup the sanitiser did not already approve; the two halves may each be unbalanced, which the browser closes for us inside the sandboxed frame. This matches ihasmail's own behaviour: a quote marker means "everything from here down is the quote".

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_quote_trim.py
import pathlib, pytest
from mailosh.render.quote_trim import split_html, find_quote_start, split_plain, quote_depth

@pytest.mark.parametrize("marker", [
    '<div class="gmail_quote">',
    '<blockquote type="cite">',
    '<div class="moz-cite-prefix">',
    '<div id="divRplyFwdMsg">',
    '<div class="yahoo_quoted">',
    '<div id="appendonsend-1">',
    '<div class="ms-outlook-mobile-reference-message">',
    '<div id="OLK_SRC_BODY_SECTION">',
    '<div class="protonmail_quote">',
    '<div class="mailosh_quote">',
])
def test_every_spec_selector_splits_the_body(marker):
    visible, quoted = split_html(f"<p>my reply</p>{marker}<p>old text</p></div>")
    assert visible == "<p>my reply</p>"
    assert quoted.startswith(marker[:8])
    assert "old text" in quoted

def test_a_body_with_no_quote_returns_an_empty_second_half():
    visible, quoted = split_html("<p>just a reply</p>")
    assert visible == "<p>just a reply</p>" and quoted == ""

def test_the_first_marker_wins_when_several_are_present():
    visible, quoted = split_html(
        '<p>a</p><div class="gmail_quote">g</div><div class="yahoo_quoted">y</div>')
    assert visible == "<p>a</p>"
    assert quoted.count("gmail_quote") == 1 and "yahoo_quoted" in quoted

def test_a_class_list_containing_the_marker_still_matches():
    visible, _ = split_html('<p>a</p><div class="x gmail_quote y">q</div>')
    assert visible == "<p>a</p>"

def test_a_nested_marker_splits_at_its_own_offset():
    visible, quoted = split_html('<div><p>a</p><div class="gmail_quote">q</div></div>')
    assert "a" in visible and "gmail_quote" in quoted

def test_splitting_never_introduces_markup_the_sanitiser_did_not_produce():
    src = '<p>a</p><div class="gmail_quote"><p>b</p></div>'
    visible, quoted = split_html(src)
    assert visible + quoted == src

def test_client_fixtures_split_where_expected(quote_fixtures):
    for name, html, expected_visible_snippet in quote_fixtures:
        visible, quoted = split_html(html)
        assert expected_visible_snippet in visible, name
        assert quoted, name
        assert expected_visible_snippet not in quoted, name

# --- plain text -------------------------------------------------------
@pytest.mark.parametrize("body,head", [
    ("hi\n\nOn Sep 1, 2026 at 8:41 PM, Dan <d@x> wrote:\n> old", "hi"),
    ("hi\n\n-----Original Message-----\nFrom: Dan", "hi"),
    ("hi\n\n________________________________\nFrom: Dan\nSent: Monday", "hi"),
    ("hi\n\nFrom: Dan <d@x>\nSent: Monday, 1 Sep\nTo: me", "hi"),
    ("hi\n\n> old line\n> older line", "hi"),
])
def test_find_quote_start_locates_each_family(body, head):
    idx = find_quote_start(body)
    assert idx is not None
    assert body[:idx].strip() == head

def test_no_quote_returns_none_and_split_keeps_everything_visible():
    assert find_quote_start("just a note\nwith two lines") is None
    visible, quoted = split_plain("just a note")
    assert visible == "just a note" and quoted == ""

def test_the_earliest_marker_wins():
    body = "hi\n\nOn Sep 1 Dan wrote:\n\n-----Original Message-----\n"
    assert find_quote_start(body) == body.index("On Sep 1")

@pytest.mark.parametrize("line,depth", [
    ("text", 0), ("> a", 1), (">> a", 2), ("  >  >  > a", 3), (">>>>>>> a", 4),
])
def test_quote_depth_counts_and_caps_at_four(line, depth):
    assert quote_depth(line) == depth
```

```python
# tests/unit/test_plain_text.py
from markupsafe import Markup
from mailosh.render.plain_text import render_plain, linkify

def test_text_is_escaped_before_anything_else_happens():
    visible, _ = render_plain("<script>alert(1)</script> & <b>x</b>")
    joined = "".join(str(line.html) for line in visible)
    assert "<script" not in joined and "&lt;script&gt;" in joined
    assert "&amp;" in joined and "<b>" not in joined

def test_links_become_anchors_with_the_same_rel_as_sanitised_mail():
    out = str(linkify("see https://ok.test/a?b=1&c=2 now"))
    assert 'href="https://ok.test/a?b=1&amp;c=2"' in out
    assert 'target="_blank"' in out
    assert set(out.split('rel="')[1].split('"')[0].split()) == {"noopener", "noreferrer", "nofollow"}
    assert out.count("<a ") == 1

def test_a_url_cannot_break_out_of_the_href_it_lands_in():
    # linkify scans the ALREADY-ESCAPED string, so a quote in the source text
    # is "&quot;" by the time the regex sees it and can never close href="".
    out = str(linkify('go to https://ok.test/x"onmouseover=alert(1) now'))
    assert out.count("<a ") == 1
    assert 'onmouseover="' not in out
    assert "&quot;onmouseover" in out

def test_mailto_and_bare_www_behaviour_is_explicit():
    assert 'href="mailto:a@b.test"' in str(linkify("write to mailto:a@b.test"))
    assert "<a " not in str(linkify("visit www.example.test"))   # http/https/mailto only, per spec §7

def test_depth_classes_ride_on_the_lines_not_on_a_wrapper():
    visible, quoted = render_plain("reply\n\n> a\n>> b")
    assert [line.depth for line in visible] == [0, 0]
    assert [line.depth for line in quoted] == [1, 2]
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** `find_quote_start` evaluates every pattern and returns the smallest match index (never the first pattern that happens to match): the attribution regex, the `-----Original Message-----` divider, the Outlook `_{10,}` rule followed within three lines by `^From:`, a bare `^From:\s` followed within three lines by `^(Sent|Date):\s`, and the start of the maximal trailing run of `>`-quoted lines. `linkify` escapes first with `markupsafe.escape`, then scans the **escaped** string with `re.compile(r"\b(?:https?://|mailto:)[^\s<>\"]+")` and wraps each hit — scanning after escaping is what makes it impossible for a URL to close the attribute it lands in, and the test above pins that ordering. `render_plain` splits on `\n`, calls `quote_depth` per line, and returns `(visible_lines, quoted_lines)` cut at `find_quote_start`. Move `split_quoted` out of `mailosh/web/app.py` into `plain_text` as the internal `>`-run grouper, update `tests/unit/test_web_thread.py`'s import, and delete the old definition and the cross-module import comment in `mailosh/web/mail.py`. In `styles/input.css`, add `.q1`–`.q4` quote-depth colours stepping from `--fg-2` toward `--fg-3` with a 2 px left rule in four label colours (`--label-sky`, `--label-emerald`, `--label-violet`, `--label-slate`), and add the same four to **both** dark token blocks if any new token is introduced — the two blocks must stay identical.
- [ ] **Step 4: Green;** `make css`. **Step 5: Commit** — `feat(render): quote trimming for html and plain-text bodies`

---

### Task 9: Conversation view — service, cards, expansion, details, per-message menu

**Files:**
- Create: `mailosh/services/conversation.py`, templates `thread/header.html`, `thread/message.html`, `thread/details.html`, `thread/menu.html`
- Modify: `mailosh/web/mail.py` (`/t/{thread_id}` rebuilt), `mailosh/web/frames.py` (`GET /m/{id}/source`), `mailosh/web/templates/thread/page.html`, `styles/input.css`, `mailosh/ui/icons.txt` + `Makefile` (icons `printer image image-off download maximize-2 chevrons-up-down chevrons-down-up`), `tests/conftest.py` (add the `fake_client` and `nav` fixtures the service tests use: a bare object with `get_thread(thread_id)` returning whatever `fake_client.thread(...)` registered, and a `NavModel` with one Inbox item and one "Work" label)
- Test: `tests/unit/test_conversation.py`, `tests/unit/test_thread_routes.py`

**Interfaces:** `AttachmentView`, `MessageView`, `ConversationView`, `build_conversation` — exactly as in the Interfaces block; `build_conversation` returns `None` when the thread has no messages, so the route renders the existing `_not_found` page.

- Expansion rule (spec §7): `expanded = wasUnread ∪ {last} ∪ (single)`. The `wasUnread` snapshot is taken from the messages as fetched by this GET, *before* any mark-read POST — which is safe by construction, because mark-read is a separate POST that answers `204` and the client never re-renders the conversation from it. Say so in the docstring; it is the thing that stops a message collapsing under the reader.
- Collapsed rows show avatar, name, snippet and date on one line and expand on click (`<details>`/`<summary>` semantics; the whole summary row is the control). `;` expands all, `:` collapses all (Task 10 binds the keys).
- Per message: avatar (initials on `avatar_color`), name + address, a `to me ▾` `<details>` popover carrying From / To / Cc / Bcc / Date / mailed-by / signed-by, timestamp, a star button posting `/a/star`, and a ⋮ menu holding **mark unread from here**, **show original**, **delete message**. Three items, not four: **Print** is added by Task 13, which is the task that builds the print route — a menu item that 404s between two tasks is exactly the kind of thing spec §3's rule exists to prevent. Reply / reply all / forward are 1C's (departure 5).
- `mailed_by` is the domain of the `Return-Path` header when present; `signed_by` is the `header.d=` value parsed out of `Authentication-Results` when a `dkim=pass` appears in it. Either row is omitted when its source header is absent — an empty row is worse than no row.
- A message whose body Stalwart truncated (`MessageView.truncated`, from `EmailBody.text_truncated`/`html_truncated`) renders one quiet line under the body: `This message was too large to show in full · Show original`, linking to `/m/{id}/source`. Rendering nothing there would leave a reader silently looking at half a message.
- The conversation opens scrolled to the first unread message (spec §7). The service exposes `first_unread_id` (falling back to the last message's id when nothing is unread), the template puts `data-first-unread` on that card, and Task 10's `htmx:afterSettle` handler scrolls it into view. Task 9 renders the attribute; Task 9's own tests assert it is on the right card.
- `GET /m/{email_id}/source` streams the message's own `blob_id` as `text/plain; charset=utf-8` with `Content-Disposition: inline`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`. It is capped at `MAX_SOURCE_BYTES = 2 * 1024 * 1024`.
- Message card ids are `id="msg-{email_id}"` so idiomorph keeps an expanded card expanded and does not reload its iframe on a live update (1A findings, item 3).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_conversation.py
import itertools
from datetime import UTC, datetime, timedelta

from mailosh.jmap.models import Address, BodyPart, EmailBody
from mailosh.services.conversation import build_conversation

_clock = itertools.count()

def msg(email_id, *, seen=True, html=None, text="body", attachments=(),
        return_path=None, auth_results=None):
    """One EmailBody, with receivedAt increasing per call so the service's
    oldest-first ordering is actually exercised rather than accidentally
    matching construction order."""
    return EmailBody(
        id=email_id, thread_id="T1", mailbox_ids={"mb-inbox"},
        keywords={"$seen"} if seen else set(),
        from_=[Address(name="Dan", email="d@x.test")], to=[Address(email="me@x")],
        subject="s", received_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=next(_clock)),
        preview="p", has_attachment=bool(attachments),
        text_body=text, html_body=html,
        attachments=[BodyPart(**a) for a in attachments],
        return_path=return_path, auth_results=auth_results,
    )

async def test_expansion_is_unread_union_last(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=True), msg("E2", seen=False), msg("E3", seen=True)])
    view = await build_conversation(fake_client, thread_id="T1", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav)
    assert [m.id for m in view.messages] == ["E1", "E2", "E3"]     # oldest first
    assert [m.expanded for m in view.messages] == [False, True, True]
    assert view.unread_ids == ["E2"]
    assert view.first_unread_id == "E2"

async def test_a_single_message_thread_is_always_expanded(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=True)])
    view = await build_conversation(fake_client, thread_id="T1", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav)
    assert view.messages[0].expanded is True

async def test_an_all_read_thread_expands_only_the_last_and_scrolls_there(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=True), msg("E2", seen=True)])
    view = await build_conversation(fake_client, thread_id="T1", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav)
    assert [m.expanded for m in view.messages] == [False, True]
    assert view.unread_ids == [] and view.first_unread_id == "E2"

async def test_cid_parts_are_excluded_from_the_attachment_chips(fake_client, nav):
    fake_client.thread("T1", [msg("E1", attachments=[
        {"blobId": "B1", "type": "image/png", "cid": "logo@m", "disposition": "inline",
         "name": "logo.png", "size": 10},
        {"blobId": "B2", "type": "application/pdf", "cid": None, "disposition": "attachment",
         "name": "spec.pdf", "size": 2048},
    ])])
    view = await build_conversation(fake_client, thread_id="T1", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav)
    assert [a.name for a in view.messages[0].attachments] == ["spec.pdf"]
    assert view.messages[0].attachments[0].size_display == "2 KB"

async def test_signed_by_and_mailed_by_come_from_headers_and_are_optional(fake_client, nav):
    fake_client.thread("T1", [msg("E1", return_path="<b@bounce.test>",
                                  auth_results="mx.test; dkim=pass header.d=news.test")])
    view = await build_conversation(fake_client, thread_id="T1", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav)
    assert view.messages[0].mailed_by == "bounce.test"
    assert view.messages[0].signed_by == "news.test"
    fake_client.thread("T2", [msg("E9")])
    view2 = await build_conversation(fake_client, thread_id="T2", me="me@x",
                                     now=datetime.now(UTC), label_meta={}, nav=nav)
    assert view2.messages[0].mailed_by is None and view2.messages[0].signed_by is None

async def test_a_dkim_fail_does_not_produce_a_signed_by_claim(fake_client, nav):
    fake_client.thread("T1", [msg("E1", auth_results="mx.test; dkim=fail header.d=news.test")])
    view = await build_conversation(fake_client, thread_id="T1", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav)
    assert view.messages[0].signed_by is None

async def test_missing_thread_returns_none(fake_client, nav):
    fake_client.thread("T9", [])
    assert await build_conversation(fake_client, thread_id="T9", me="me@x",
                                    now=datetime.now(UTC), label_meta={}, nav=nav) is None
```

```python
# tests/unit/test_thread_routes.py
import pathlib
import re

import pytest

async def test_thread_page_renders_one_card_per_message_with_stable_ids(authed, fake):
    fake.thread("T1", ["E1", "E2", "E3"])
    html = (await authed.get("/t/T1")).text
    assert html.count('id="msg-E') == 3
    assert 'id="msg-E1"' in html

async def test_html_messages_get_a_frame_and_text_messages_do_not(authed, fake):
    fake.thread("T1", [("E1", "<p>rich</p>", None), ("E2", None, "plain")])
    html = (await authed.get("/t/T1")).text
    assert html.count("<iframe") == 1
    assert "/m/E1/html" in html and "/m/E2/html" not in html
    assert "plain" in html

async def test_subject_and_body_from_a_hostile_message_are_escaped_in_the_shell(authed, fake):
    fake.thread("T1", [("E1", None, "<script>alert(1)</script>")],
                subject="<img src=x onerror=alert(1)>")
    html = (await authed.get("/t/T1")).text
    assert "<script>alert(1)</script>" not in html
    assert "onerror=alert" not in html

async def test_menu_offers_only_the_actions_this_task_implements(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    for present in ("Mark unread from here", "Show original", "Delete message"):
        assert html.count(present) == 1
    for absent in ("Reply all", "Forward", "Print"):
        assert absent not in html

async def test_every_message_has_a_star_control_pointing_at_the_action_route(authed, fake):
    fake.thread("T1", ["E1", "E2"])
    html = (await authed.get("/t/T1")).text
    assert html.count('data-action="star"') == 2

async def test_the_first_unread_card_is_the_scroll_target(authed, fake):
    fake.thread("T1", [("E1", None, "a"), ("E2", None, "b")], unread=["E2"])
    html = (await authed.get("/t/T1")).text
    assert html.count("data-first-unread") == 1
    marked = [m.group(1) for m in re.finditer(r'<article[^>]*\bid="msg-(E\d)"[^>]*\bdata-first-unread', html)]
    assert marked == ["E2"]

async def test_details_popover_omits_rows_whose_header_is_missing(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    assert "mailed-by" not in html and "signed-by" not in html

async def test_a_jmap_failure_reaches_the_global_error_surface_not_a_traceback(authed, fake):
    from mailosh.jmap.errors import TransportError
    fake.raise_on_thread = TransportError("stalwart unreachable")
    page = await authed.get("/t/T1")
    assert page.status_code == 502 and "<html" in page.text.lower()
    frag = await authed.get("/t/T1", headers={"HX-Request": "true"})
    assert frag.status_code == 200 and "om:error" in frag.headers["HX-Trigger"]

async def test_source_route_is_plain_text_and_capped(authed, fake):
    fake.message("E1", blob_id="B0")
    fake.blob("B0", b"From: a@x\r\n\r\nbody")
    r = await authed.get("/m/E1/source")
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert b"From: a@x" in r.content

async def test_fragment_vs_full_page_split_is_preserved(authed, fake):
    fake.thread("T1", ["E1"])
    full = await authed.get("/t/T1")
    frag = await authed.get("/t/T1", headers={"HX-Request": "true"})
    assert "<!doctype html>" in full.text.lower()
    assert "<!doctype html>" not in frag.text.lower()
    assert frag.headers["HX-Push-Url"] == "/t/T1"
```

- [ ] **Step 2: FAIL.** **Step 3: Implement** against `docs/design/mockups/key-moments.html` §1. `build_conversation` sorts by `received_at` ascending, computes the expansion set, and builds `MessageView`s using `mailosh.ui.format`'s `initials`/`avatar_color`/`format_date` and `mailosh.render.plain_text.render_plain` for text bodies. `first_unread_id` is the first ascending message lacking `$seen`, or the last message's id when none is unread. `size_display` formats bytes as `123 B` / `2 KB` / `1.4 MB` (binary units, one decimal above 1 MB). `mailed_by` is the domain after the last `@` in `return_path` with `<>` stripped; `signed_by` is `re.search(r"dkim=pass[^;]*?header\.d=([A-Za-z0-9.\-]+)", auth_results)` — the `dkim=pass` prefix is load-bearing, since a `dkim=fail` result also carries a `header.d=` and reporting it would be a claim the message does not support. `chips` reuses `LabelChip` from `mailosh.services.thread_list` so the conversation header and the list rows cannot disagree about a label's colour. Plain-text bodies keep the existing `<details><summary class="quote-toggle">•••</summary>` pill from `thread/page.html`, now fed by `quoted_lines` instead of `split_quoted`, and each line renders `class="q{depth}"` when `depth > 0` with `white-space: pre-wrap` unchanged. The route replaces the current inline `split_quoted` rendering, keeps the existing list-style action toolbar, `_referring_key`, `_not_found`, `_is_fragment`, `Cache-Control: private, max-age=60` and `Vary: HX-Request` exactly as they are, and adds `origin` to the context. Add the seven icons to `mailosh/ui/icons.txt` and to the Makefile's `ICON_FILES`; run `make icons css`.
- [ ] **Step 4: Green;** `make css`; browser: open a 6-message thread and confirm collapsed rows read as one line, the newest and every unread one start open, the `to me ▾` popover opens with the keyboard, and the ⋮ menu contains four items.
- [ ] **Step 5: Commit** — `feat(thread): conversation cards, expansion rules, details popover, message menu`

---

### Task 10: Mark-read delay, auto-advance, prev/next position, thread keys

**Files:**
- Modify: `mailosh/web/mail.py` (`/t/{thread_id}` takes `key`/`pos`; new `GET /mail/{key}/at/{position}`), `mailosh/web/templates/list/row.html` (rows carry their position), `thread/header.html`, `mailosh/web/static/js/actions.js`, `mailosh/web/static/js/keys.js`, `styles/input.css`, `tests/conftest.py` (add `set_pref(**fields)` — an async helper that calls `repo.set_prefs` for the `authed` user and returns nothing)
- Test: `tests/unit/test_thread_routes.py`, `tests/unit/test_mail_routes.py`

**Interfaces:**
- `GET /t/{thread_id}?key={mailbox_key}&pos={n}` — `key` is validated against the nav exactly like `/mail/{key}` (an unvalidated key must never reach `Email/query`); `pos` is clamped to `>= 0`. The header renders `{pos+1} of {total}` with ‹ › linking to `/mail/{key}/at/{pos-1}` and `/mail/{key}/at/{pos+1}`; the first and last positions render their arrow `disabled` rather than hiding it.
- `GET /mail/{key}/at/{position}` resolves position → thread id with one `query_page(position=position, limit=1)` and renders the conversation directly (no redirect), so a `‹`/`›` click is one round trip. Position past the end → the list's `_not_found` page.
- Mark read on open: the conversation page carries `data-mark-read-url="/a/read"`, `data-mark-read-ids="…"` and `data-mark-read-delay="{seconds}"`. `actions.js` schedules one `POST /a/read` after the delay; `0` fires immediately, `-1` never fires. A prefetched GET (`HX-Preloaded: true`) still renders the page but the timer is only armed on a real navigation — the attribute is rendered either way and `actions.js` arms it from `htmx:afterSettle`, not from parse.
- Auto-advance: the page carries `data-advance-url` (the next conversation per `prefs.auto_advance`: `older` → `pos+1`, `newer` → `pos-1`, `list` → the list URL). After a successful archive/delete/spam **from the thread view**, `actions.js` navigates there instead of to `data-role="back"`'s target. Undo from the toast still works because the token is in the toast, not in the page.
- `keys.js`: the thread-scope entries `n`, `p`, `;`, `:`, `Shift+U` flip from `available:false` to real handlers (next/previous message card, expand all, collapse all, mark unread from here). `r`, `a`, `f` stay `available:false` — they are 1C's.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_thread_routes.py  (appended)
async def test_position_header_and_arrows(authed, fake):
    fake.list("inbox", total=1284)
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1?key=inbox&pos=3")).text
    assert "4 of 1,284" in html
    assert "/mail/inbox/at/2" in html and "/mail/inbox/at/4" in html

async def test_first_and_last_positions_disable_rather_than_hide_an_arrow(authed, fake):
    fake.list("inbox", total=2)
    fake.thread("T1", ["E1"])
    first = (await authed.get("/t/T1?key=inbox&pos=0")).text
    last = (await authed.get("/t/T1?key=inbox&pos=1")).text
    assert first.count("disabled") == 1 and last.count("disabled") == 1

async def test_an_unknown_key_is_ignored_and_never_reaches_the_query(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1?key=../../etc&pos=0")).text
    assert fake.query_calls == []          # no Email/query was issued at all
    assert "thread-position" not in html   # and no position readout was rendered
    assert 'href="/mail/inbox"' in html    # back still goes somewhere real

async def test_at_route_resolves_a_position_to_a_conversation(authed, fake):
    fake.list("inbox", total=3, thread_at={1: "T7"})
    fake.thread("T7", ["E7"])
    html = (await authed.get("/mail/inbox/at/1")).text
    assert 'id="msg-E7"' in html
    assert "2 of 3" in html

async def test_at_route_past_the_end_is_the_404_page(authed, fake):
    fake.list("inbox", total=1)
    assert (await authed.get("/mail/inbox/at/9")).status_code == 404

@pytest.mark.parametrize("delay,expected", [(0, "0"), (1, "1"), (3, "3"), (-1, "-1")])
async def test_mark_read_delay_is_rendered_from_prefs(authed, fake, set_pref, delay, expected):
    await set_pref(mark_read_delay=delay)
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    assert f'data-mark-read-delay="{expected}"' in html
    assert 'data-mark-read-ids="E1"' in html

@pytest.mark.parametrize("mode,target", [("older", "/mail/inbox/at/4"),
                                         ("newer", "/mail/inbox/at/2"),
                                         ("list", "/mail/inbox")])
async def test_auto_advance_target_follows_the_pref(authed, fake, set_pref, mode, target):
    await set_pref(auto_advance=mode)
    fake.list("inbox", total=10)
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1?key=inbox&pos=3")).text
    assert f'data-advance-url="{target}"' in html
```

```python
# tests/unit/test_mail_routes.py  (appended)
async def test_rows_carry_their_absolute_position_for_the_thread_header(authed, fake):
    # A row's link hands the conversation its place in the list; without it
    # the thread header would have to re-query just to learn "4 of 1,284".
    fake.list("inbox", total=200, ids=["T1", "T2", "T3"])
    html = (await authed.get("/mail/inbox/rows?position=50&limit=3")).text
    assert html.count("pos=") == 3
    assert "/t/T1?key=inbox&amp;pos=50" in html
    assert "/t/T3?key=inbox&amp;pos=52" in html
```

```python
# tests/unit/test_thread_routes.py  (js control flow, appended)
def _registry_entry(src: str, entry_id: str) -> str:
    """The source text of one keys.js registry object, from its `id:` to the
    start of the next entry — so an assertion about one entry cannot
    accidentally read a neighbour's `available` flag."""
    start = src.index(f'id: "{entry_id}"')
    nxt = src.find("id: \"", start + 10)
    return src[start:nxt if nxt != -1 else len(src)]

def test_actions_js_prefers_advance_over_back_only_in_the_thread():
    src = pathlib.Path("mailosh/web/static/js/actions.js").read_text()
    assert src.index("data-advance-url") < src.index('data-role="back"')

def test_keys_js_marks_1c_keys_unavailable_and_thread_nav_available():
    src = pathlib.Path("mailosh/web/static/js/keys.js").read_text()
    for entry in ("next-message", "prev-message", "expand-all", "collapse-all", "mark-unread-from-here"):
        assert "available: false" not in _registry_entry(src, entry), entry
    for entry in ("reply", "reply-all", "forward"):
        assert "available: false" in _registry_entry(src, entry), entry

def test_the_scroll_to_first_unread_runs_from_settle_not_from_parse():
    src = pathlib.Path("mailosh/web/static/js/actions.js").read_text()
    settle = src.index("htmx:afterSettle")
    assert settle < src.index("data-first-unread")
    assert settle < src.index("data-mark-read-delay")
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** The `at` route and `/t/{id}` share one `_render_conversation(request, …)` helper so the two paths cannot drift on headers, context keys or the fragment split. `_thread_position(client, nav, key, pos)` returns `(total, prev_url, next_url)` from one `query_page(position=pos, limit=1)` — `QueryPage.total` is the server's own `calculateTotal`, never a length. In `actions.js`, the existing `[data-action]` success path gains one branch: when the acting element is inside `[data-advance-url]` and the action is archive/delete/spam, navigate to that URL with `htmx.ajax("GET", url, {target: "#main", swap: "morph:innerHTML"})` and push it; otherwise keep today's `data-role="back"` behaviour unchanged. Arm the mark-read timer from a `htmx:afterSettle` handler that reads the three data attributes and, for `delay >= 0`, `setTimeout`s one `om.act("read", ids, {on: true})`; clear any pending timer on the next settle so leaving a conversation early cannot mark it read behind the reader. The same handler scrolls `[data-first-unread]` into view with `scrollIntoView({block: "start"})` — one settle handler owns both, because both are "the conversation just landed in the DOM" concerns and splitting them would give them two chances to disagree about which conversation is on screen.
- [ ] **Step 4: Green;** browser, keyboard only: `j` `o` opens, `n`/`p` walk the cards, `;`/`:` expand and collapse all, `e` archives and lands on the next conversation, `z` undoes and the row returns, `u` goes back, `Shift+U` marks unread from a card. Confirm with a 3 s mark-read delay that leaving within 3 s leaves the conversation unread.
- [ ] **Step 5: Commit** — `feat(thread): mark-read delay, auto-advance, prev/next position, thread keys`

---

### Task 11: Attachments — chips, download, preview dialog

**Files:**
- Create: `mailosh/web/templates/thread/attachments.html`, `mailosh/web/templates/fragments/preview_dialog.html`
- Modify: `mailosh/web/frames.py` (`GET /m/{id}/att/{blob_id}`), `mailosh/web/templates/layouts/app.html` (the dialog is a singleton, app layout only — 1A findings item 2), `thread/message.html`, `styles/input.css`
- Test: `tests/unit/test_attachments.py`

**Interfaces:**
- `GET /m/{email_id}/att/{blob_id}?inline=0|1` — session-required. The `blob_id` must belong to one of *this* message's attachments or the response is `404`; a blob id is not a capability. `inline=0` (default) serves `Content-Disposition: attachment; filename="…"; filename*=UTF-8''…` (RFC 5987) with `Content-Type: application/octet-stream`. `inline=1` serves `Content-Disposition: inline` and the real type **only** when it is in `PREVIEW_TYPES = {"image/png","image/gif","image/jpeg","image/webp","image/bmp","application/pdf","text/plain"}`; anything else falls back to the download shape. `text/html` is never served inline, under any flag. Every response carries `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, `Referrer-Policy: no-referrer`, `Cache-Control: private, max-age=3600`.
- `AttachmentView.preview` is `"image"`, `"pdf"`, `"text"` or `None`. Every chip shows icon, name and size, and carries two real buttons revealed on hover **and on focus** (`.attachment-chip:hover .chip-actions, .attachment-chip:focus-within .chip-actions { opacity: 1 }`): Download always, Open only when `preview` is non-`None`. They are buttons, not hover-only affordances — spec §11 requires every hover action to be keyboard-operable, and 1A already fixed exactly this class of bug on list rows.
- The preview dialog is one native `<dialog id="attachment-preview">` in `layouts/app.html`. A chip opens it with `hx-get` into the dialog's body and `x-on:click="$store.ui.openDialog('attachment-preview')"` (a method call with an argument — inside what the Alpine CSP build evaluates). PDFs render in `<iframe sandbox="allow-scripts" referrerpolicy="no-referrer">`; images in `<img>`; text is fetched server-side, escaped and shown in `<pre>`. Escape closes and returns focus to the chip.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_attachments.py
import pytest

async def test_download_is_octet_stream_with_an_rfc5987_filename(authed, fake):
    fake.message("E1", attachments=[{"blobId": "B2", "type": "application/pdf",
                                     "name": "rapport été.pdf", "size": 9, "disposition": "attachment"}])
    fake.blob("B2", b"%PDF-1.7 ")
    r = await authed.get("/m/E1/att/B2")
    assert r.headers["content-type"] == "application/octet-stream"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert "filename*=UTF-8''" in cd
    assert r.headers["x-content-type-options"] == "nosniff"

@pytest.mark.parametrize("mime,inline_type", [
    ("image/png", "image/png"), ("application/pdf", "application/pdf"),
    ("text/plain", "text/plain"),
])
async def test_previewable_types_are_served_inline_with_their_real_type(authed, fake, mime, inline_type):
    fake.message("E1", attachments=[{"blobId": "B2", "type": mime, "name": "f", "size": 3,
                                     "disposition": "attachment"}])
    fake.blob("B2", b"abc")
    r = await authed.get("/m/E1/att/B2?inline=1")
    assert r.headers["content-type"].startswith(inline_type)
    assert r.headers["content-disposition"].startswith("inline")

@pytest.mark.parametrize("mime", ["text/html", "image/svg+xml", "application/xhtml+xml",
                                  "application/x-msdownload"])
async def test_dangerous_types_are_never_inline_even_with_the_flag(authed, fake, mime):
    fake.message("E1", attachments=[{"blobId": "B2", "type": mime, "name": "f", "size": 3,
                                     "disposition": "attachment"}])
    fake.blob("B2", b"<script>alert(1)</script>")
    r = await authed.get("/m/E1/att/B2?inline=1")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")

async def test_a_blob_id_from_another_message_is_404(authed, fake):
    fake.message("E1", attachments=[{"blobId": "B2", "type": "image/png", "name": "a",
                                     "size": 3, "disposition": "attachment"}])
    fake.message("E2", attachments=[{"blobId": "B9", "type": "image/png", "name": "b",
                                     "size": 3, "disposition": "attachment"}])
    assert (await authed.get("/m/E1/att/B9")).status_code == 404

async def test_chips_exclude_inline_cid_parts_and_carry_size_and_preview_kind(authed, fake):
    fake.thread("T1", [("E1", None, "x")], attachments=[
        {"blobId": "B1", "type": "image/png", "cid": "logo@m", "disposition": "inline",
         "name": "logo.png", "size": 10},
        {"blobId": "B2", "type": "application/pdf", "cid": None, "disposition": "attachment",
         "name": "spec.pdf", "size": 2048},
    ])
    html = (await authed.get("/t/T1")).text
    assert html.count("attachment-chip") == 1
    assert "spec.pdf" in html and "logo.png" not in html
    assert "2 KB" in html
    assert html.count("Download spec.pdf") == 1
    assert html.count("Open spec.pdf") == 1

async def test_a_chip_with_no_preview_kind_offers_download_only(authed, fake):
    fake.thread("T1", [("E1", None, "x")], attachments=[
        {"blobId": "B3", "type": "application/zip", "cid": None, "disposition": "attachment",
         "name": "bundle.zip", "size": 4096},
    ])
    html = (await authed.get("/t/T1")).text
    assert html.count("Download bundle.zip") == 1
    assert "Open bundle.zip" not in html

async def test_the_preview_dialog_is_a_singleton_in_the_app_layout_only(authed, fake):
    fake.thread("T1", ["E1"])
    full = (await authed.get("/t/T1")).text
    frag = (await authed.get("/t/T1", headers={"HX-Request": "true"})).text
    assert full.count('id="attachment-preview"') == 1
    assert 'id="attachment-preview"' not in frag
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** The route loads the message, looks the blob up in `msg.attachments` and streams it via `client.stream_blob`. Filenames go through a `_content_disposition(name, inline)` helper that emits both the ASCII-folded `filename=` and the `filename*=UTF-8''` form and strips CR/LF from the name before either (a header-injection guard, not a formatting nicety). `text/plain` previews are fetched with `client.fetch_blob(..., max_bytes=256*1024)`, decoded `utf-8` with `errors="replace"`, and escaped by Jinja. Add the icon mapping (`file-text` for text/pdf, `image` for images, `file` otherwise) in `conversation.AttachmentView`. Run `make css`.
- [ ] **Step 4: Green;** browser: download a PDF and an image, preview both plus a `.txt`, confirm Escape closes the dialog and focus returns to the chip, and confirm a `.html` attachment downloads rather than rendering.
- [ ] **Step 5: Commit** — `feat(thread): attachment chips, downloads and preview dialog`

---

### Task 12: Dark restyle

**Files:**
- Create: `mailosh/render/dark.py`, `migrations/versions/0002_reading.py`
- Modify: `mailosh/db/models.py` (`SenderPref`), `mailosh/db/repo.py`, `mailosh/render/frame_document.py`, `mailosh/web/frames.py`, `thread/frame.html`, `styles/input.css`
- Test: `tests/unit/test_dark_restyle.py`

**Interfaces:**
- `SenderPref(user_id, sender_email)` primary key with `dark_restyle: bool | None`; `repo.sender_pref(db, user_id, sender_email) -> SenderPref | None` and `repo.set_sender_restyle(db, user_id, sender_email, value)`.
- `restyle_mode(*, theme, enabled, declares, light) -> str`: `"none"` when the effective theme is light, or when `enabled` is false; `"color-scheme"` when the mail declares a colour scheme (a `<meta name="color-scheme">` in the source, or a `color-scheme` declaration in its CSS); `"invert"` when it does not and its background reads light; `"none"` otherwise (a mail that is already dark is left alone).
- `background_is_light(html, css)` reads, in order: a `color-scheme` hint, the `body`/first `table` `bgcolor` attribute, a `background-color` declaration on `body`/`table` in the sanitised CSS. It parses `#rgb`, `#rrggbb` and the sixteen HTML colour names, computes relative luminance, and returns `True` above `0.5`. **A mail with no background at all returns `True`** — mail defaults to white paper, and treating "unknown" as dark would leave black text on a black frame.
- `"invert"` emits, in the frame's base CSS: `html{filter:invert(1) hue-rotate(180deg)}` and `img,[style*="background-image"]{filter:invert(1) hue-rotate(180deg)}` — spec §7 verbatim.
- The banner gains a "Show original" toggle when `restyle != "none"`; it posts `POST /m/{id}/restyle` (CSRF, form field `sender` validated against the message's own From exactly like the image allow route) which writes `SenderPref.dark_restyle = False` and re-renders `thread/frame.html` with `restyle=0`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_dark_restyle.py
import pytest
from mailosh.render.dark import background_is_light, declares_color_scheme, restyle_mode

@pytest.mark.parametrize("theme,enabled,declares,light,expected", [
    ("light", True,  False, True,  "none"),
    ("dark",  False, False, True,  "none"),
    ("dark",  True,  True,  True,  "color-scheme"),
    ("dark",  True,  False, True,  "invert"),
    ("dark",  True,  False, False, "none"),
])
def test_the_decision_table_is_exactly_the_spec(theme, enabled, declares, light, expected):
    assert restyle_mode(theme=theme, enabled=enabled, declares=declares, light=light) == expected

def test_color_scheme_is_detected_from_meta_or_css():
    assert declares_color_scheme('<meta name="color-scheme" content="dark light">', "")
    assert declares_color_scheme("", ":root{color-scheme:light dark}")
    assert not declares_color_scheme("<p>x</p>", "p{color:red}")

@pytest.mark.parametrize("html,css,light", [
    ("<body bgcolor=\"#ffffff\">", "", True),
    ("<body bgcolor=\"#111111\">", "", False),
    ("<body bgcolor=\"black\">", "", False),
    ("", "body{background-color:#fff}", True),
    ("", "body{background-color:#0d1015}", False),
    ("", "table{background-color:#eee}", True),
    ("<p>no background anywhere</p>", "", True),      # mail defaults to white paper
])
def test_background_luminance(html, css, light):
    assert background_is_light(html, css) is light

def test_invert_emits_both_spec_rules_and_only_when_asked():
    from mailosh.render.frame_document import render_frame
    doc = render_frame(visible_html="", quoted_html="", mail_css="", theme="dark", restyle="invert")
    assert doc.count("invert(1) hue-rotate(180deg)") == 2
    assert "html{filter:invert(1) hue-rotate(180deg)}" in doc.replace(" ", "")
    plain = render_frame(visible_html="", quoted_html="", mail_css="", theme="dark", restyle="none")
    assert "invert(1)" not in plain
    declared = render_frame(visible_html="", quoted_html="", mail_css="", theme="dark",
                            restyle="color-scheme")
    assert "invert(1)" not in declared
    assert "color-scheme:light dark" in declared.replace(" ", "")

async def test_show_original_is_remembered_per_sender(authed, fake, csrf):
    fake.message("E1", html="<p>x</p>", sender="news@t.test")
    await authed.post("/m/E1/restyle", data={"sender": "news@t.test"}, headers=csrf)
    fake.message("E2", html="<p>y</p>", sender="news@t.test")
    frag = (await authed.get("/m/E2/frame?thread=T1")).text
    assert "restyle=0" in frag
    fake.message("E3", html="<p>z</p>", sender="other@t.test")
    other = (await authed.get("/m/E3/frame?thread=T1")).text
    assert "restyle=0" not in other

async def test_restyle_route_rejects_a_foreign_sender_and_needs_csrf(authed, authed_no_csrf, fake, csrf):
    fake.message("E1", html="<p>x</p>", sender="news@t.test")
    assert (await authed.post("/m/E1/restyle", data={"sender": "x@evil.test"},
                              headers=csrf)).status_code == 403
    assert (await authed_no_csrf.post("/m/E1/restyle",
                                      data={"sender": "news@t.test"})).status_code == 403
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** Write `migrations/versions/0002_reading.py` with `down_revision = "0001_foundation"` creating `sender_pref(user_id FK, sender_email, dark_restyle BOOLEAN NULL)` with the composite primary key, and a matching `downgrade`. `declares_color_scheme` runs on the **raw** message HTML, not the sanitised output — `<meta>` is in `CLEAN_CONTENT_TAGS`, so by sanitisation time the `<meta name="color-scheme">` evidence is gone; the frame route therefore keeps the raw body around long enough to ask. The route computes `enabled = prefs.dark_restyle and (sender_pref.dark_restyle is not False) and (restyle_param != 0)`, then calls `restyle_mode`. Theme resolution: `prefs.theme == "dark"` computes the mode directly; `prefs.theme == "system"` computes the same mode but the invert rules are emitted inside `@media (prefers-color-scheme: dark)`, because the server cannot know which way a `system` reader's OS is set and an unconditional invert would hand a system-light reader a photographic negative. Run `make db-upgrade` and `make css`.
- [ ] **Step 4: Green;** browser: in dark mode open a white-background newsletter (inverted, images not double-inverted), a mail declaring `color-scheme` (untouched), and an already-dark mail (untouched); click "Show original" and confirm the next mail from that sender opens un-inverted.
- [ ] **Step 5: Commit** — `feat(reading): dark restyle with per-sender show-original memory`

---

### Task 13: Print, and the reading preferences in Quick settings

**Files:**
- Create: `mailosh/web/templates/thread/print.html`
- Modify: `mailosh/web/mail.py` (`GET /t/{thread_id}/print`), `mailosh/web/templates/thread/menu.html` (add Print), `mailosh/web/prefs.py`, `mailosh/web/templates/shell/quick_settings.html`, `mailosh/web/static/js/frame.js`, `styles/input.css`
- Test: `tests/unit/test_print_route.py`, `tests/unit/test_reading_prefs.py`

**Interfaces:**
- `GET /t/{thread_id}/print` renders `layouts/bare.html` with every message expanded, every plain-text quote rendered open (no `<details>`), and one iframe per HTML message pointed at `/m/{id}/html?expand=1` (spec §7's containment is not traded away for print fidelity — departure 6). It loads `frame.js` only, calls `window.print()` once every frame has reported a height or after 1500 ms, whichever comes first, and carries `Cache-Control: private, no-store`. `?remote=1` is honoured so a reader who already showed images prints them. The ⋮ menu's **Print** item — held back by Task 9 precisely so it never pointed at a missing route — is added here.
- `POST /prefs` gains `mark_read_delay` (`Literal["0","1","3","-1"]`), `remote_images` (`Literal["ask","always","contacts"]`), `dark_restyle` (`Literal["true","false"]`), `auto_advance` (`Literal["older","newer","list"]`) and `conversation_view` (`Literal["true","false"]`) alongside the three it already takes, each `Form`-optional so a one-control request stays a partial update. The full settings **pages** are still 1D's; this task adds a "Reading" group to the existing quick-settings popover only.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_print_route.py
import pathlib

async def test_print_page_expands_everything_and_keeps_the_frames_sandboxed(authed, fake):
    fake.thread("T1", [("E1", "<p>a</p>", None), ("E2", None, "b\n\n> quoted")])
    html = (await authed.get("/t/T1/print")).text
    assert html.count("<iframe") == 1
    assert "allow-same-origin" not in html
    assert "expand=1" in html                     # the HTML frame opens its own quote
    assert "quoted" in html and "<details" not in html   # the text quote is already open
    assert "list-toolbar" not in html and 'aria-label="Primary"' not in html

async def test_print_page_honours_the_remote_flag(authed, fake):
    fake.thread("T1", [("E1", '<img src="https://t.test/a.gif">', None)])
    assert "remote=1" in (await authed.get("/t/T1/print?remote=1")).text
    assert "remote=1" not in (await authed.get("/t/T1/print")).text

async def test_print_page_is_not_cached(authed, fake):
    fake.thread("T1", ["E1"])
    assert "no-store" in (await authed.get("/t/T1/print")).headers["cache-control"]

async def test_the_menu_gains_print_now_that_the_route_exists(authed, fake):
    fake.thread("T1", ["E1"])
    html = (await authed.get("/t/T1")).text
    assert html.count("/t/T1/print") == 1

def test_print_trigger_waits_for_heights_then_falls_back_on_a_timer():
    src = pathlib.Path("mailosh/web/static/js/frame.js").read_text()
    ready = src.index("framesReady")
    timer = src.index("1500")
    call = src.index("window.print()")
    assert ready < call and timer < call
```

```python
# tests/unit/test_reading_prefs.py
import pytest

@pytest.mark.parametrize("field,posted,stored", [
    ("mark_read_delay", "3", 3),
    ("mark_read_delay", "-1", -1),
    ("remote_images", "always", "always"),
    ("dark_restyle", "false", False),
    ("auto_advance", "list", "list"),
    ("conversation_view", "false", False),
])
async def test_each_reading_pref_persists_with_its_column_type(authed, csrf, db, user,
                                                               field, posted, stored):
    from mailosh.db import repo
    r = await authed.post("/prefs", data={field: posted}, headers=csrf)
    assert r.status_code == 204
    prefs = await repo.get_prefs(db, user.id)
    assert getattr(prefs, field) == stored
    assert isinstance(getattr(prefs, field), type(stored))

@pytest.mark.parametrize("field,value", [
    ("mark_read_delay", "7"), ("remote_images", "never"), ("auto_advance", "sideways"),
])
async def test_illegal_values_are_422_and_change_nothing(authed, csrf, db, user, field, value):
    from mailosh.db import repo
    before = await repo.get_prefs(db, user.id)
    snapshot = (before.mark_read_delay, before.remote_images, before.auto_advance)
    assert (await authed.post("/prefs", data={field: value}, headers=csrf)).status_code == 422
    after = await repo.get_prefs(db, user.id)
    assert (after.mark_read_delay, after.remote_images, after.auto_advance) == snapshot

async def test_a_one_field_post_leaves_the_others_alone(authed, csrf, db, user):
    from mailosh.db import repo
    await authed.post("/prefs", data={"theme": "dark", "mark_read_delay": "3"}, headers=csrf)
    await authed.post("/prefs", data={"remote_images": "always"}, headers=csrf)
    prefs = await repo.get_prefs(db, user.id)
    assert (prefs.theme, prefs.mark_read_delay, prefs.remote_images) == ("dark", 3, "always")

async def test_quick_settings_renders_every_reading_control_once(authed, fake):
    fake.list("inbox", total=0)
    html = (await authed.get("/mail/inbox")).text
    for label in ("Mark as read", "Remote images", "Dark mail", "After archiving", "Conversation view"):
        assert html.count(label) == 1
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** `mark_read_delay` arrives as a string `Literal` and is stored as `int(value)` — the `Literal` is what makes FastAPI reject `7` before the handler body runs, and the cast is what keeps the column an integer. `frame.js` gains a `framesReady` counter used only on the print page (guarded by `document.body.dataset.print === "1"`, so the ordinary conversation page never installs a print timer). Run `make css`.
- [ ] **Step 4: Green;** browser, all three engines: print-preview a thread containing one long HTML mail and one plain-text mail and record what each browser does with a tall sandboxed iframe — if any of them clips it, **record the measurement and the clipping threshold in `docs/spikes/p1b-findings.md`** rather than quietly widening the containment boundary.
- [ ] **Step 5: Commit** — `feat(reading): printable conversation and reading preferences`

---

### Task 14: Live integration, real-world corpus, browser QA, findings

**Files:**
- Create: `tests/integration/test_live_reading_flow.py`, `tests/fixtures/mail/` (the real-world corpus)
- Modify: `docs/spikes/p1b-findings.md` (created by Task 4; completed here), `Makefile` (`qa` target lists 1B's checks), `README.md` (reading section), `scripts/measure.py` (frame TTFB)
- Test: the integration module itself

- [ ] **Step 1: Hermetic live integration scenario** (self-cleaning, per-run ids, one scenario, `make itest`). Import six messages into the dev account's Inbox with per-run `Message-ID`s via the Phase 0 client: (1) a Gmail-shaped reply with `<div class="gmail_quote">`, (2) an Outlook-shaped reply with `<div id="divRplyFwdMsg">`, (3) an Apple Mail reply with `<blockquote type="cite">`, (4) a newsletter with three remote images and one inline `cid:` logo, (5) a message with a PDF attachment, (6) the adversarial payload (`<script>`, `javascript:` href, `expression()`, `<base>`, an SVG script, an inline `<style>` carrying `@import` and a `</style>` breakout string). Then, over `httpx.Client(base_url="http://localhost:8000")`: log in → `GET /mail/inbox` shows all six → open each `/t/{id}` → for each, `GET /m/{id}/html` and assert the CSP header is byte-identical to `frame_document.csp_header()`, that the payload message's frame contains no `<script` other than the one hash-pinned block, no `javascript:`, no `on*` attribute and no `@import`, that the newsletter frame carries no `src` on its three remote images at `remote=0` and three `/img?u=` sources at `remote=1`, and that its `cid:` image resolves `200` → archive one, undo it → `POST /logout`. Clean up all six emails in a `finally`.
- [ ] **Step 2: Browser QA (Chrome MCP), recorded in `docs/spikes/p1b-findings.md` with screenshots.** The matrix: each corpus message in **Chrome, Firefox and Safari**, in light and dark, at all three densities. Per message record whether it rendered legibly, whether the frame auto-sized, and any console error. Then the six drills: (a) the payload message — the console is clean and nothing executed; (b) a hostile `postMessage` from the parent console leaves every frame's height unchanged; (c) `remote=0` produces zero network requests to the sender's hosts (Network panel, filtered by domain) and `remote=1` routes every one through `/img?u=`; (d) "Always show from" survives a reload and a new message from the same sender; (e) dark restyle across a white newsletter, a `color-scheme`-declaring mail and an already-dark mail; (f) keyboard-only: `o n p ; : e z u Shift+U` and the attachment dialog's focus return.
- [ ] **Step 3: Budgets.** Extend `scripts/measure.py` with `GET /m/{id}/html` server time (p50/p95, 20 runs, logged in) and record it against spec §11's 200 ms partial-TTFB budget. Re-measure total JS gz (`frame.js` is the only addition; the 1A number was 92.3 KiB against 90 KB) and CSS gz, and state plainly whether 1B moved either. Re-run `scripts/measure.py` end to end rather than trusting its last recorded output — 1A found it had been silently broken for two separate reasons at exactly this point in the cycle.
- [ ] **Step 4: Findings doc complete.** Headings: `Frame delivery` · `Sanitiser` · `Remote images and the proxy` · `Quote trimming` · `Conversation view` · `Attachments` · `Dark restyle` · `Print` · `Budgets` · `Browser QA`. The executive summary states what 1C should not have to rediscover — at minimum: the five nh3 behaviours from Task 3, the CSP that actually shipped and why it differs from spec §7, whether `img-src 'self'` held inside a sandboxed opaque-origin document in all three engines, the per-browser print result, and any Stalwart quirk found around `htmlBody`, `attachments` or blob download. Where a check could not be run, say so rather than inheriting a claim from a task report.
- [ ] **Step 5: `make test && make itest`, `ruff check . && ruff format --check .` clean; commit** — `docs: p1b findings, reading integration flow, budgets` and tag `phase1b-complete`.
