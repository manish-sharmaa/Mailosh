# Phase 1A findings

Findings recorded per topic, filled in by whichever Phase 1A task closes each one
out (mirrors `docs/spikes/p0-findings.md`'s own "filled in by the task
that closes it out" convention). See
`docs/plans/2026-09-02-phase1a-foundation.md` for the task list.

"Auth exchange" was written by Task 4 as it ran. Every other section below was
written by **Task 14**, the closing task, from live browser QA and live
measurement against the running stack — not from the code alone, and not from
what the intervening tasks' own reports claimed. Where Task 14 could not verify
something, it says so rather than inheriting a claim.

---

## Executive summary — what Phase 1B needs to know

Phase 1A works. The list, triage, undo, keyboard, palette, live updates, error
handling and session lifecycle were all exercised against the real
`docker compose` stack in a real browser, and the failure drills (mail server
down, app down, signed out in another tab) all behave honestly rather than
silently. 608 unit tests and 4 live integration tests pass.

Seven things 1B should not have to rediscover:

1. **`StalwartAdmin.destroy_api_key` takes `(username, key_id)`, and the
   account has a hard quota of 5 `x:ApiKey` objects.** Task 4's section below
   is the authority; nothing since has changed it. The app mints **one** key
   per user and reuses it across sessions (`_mint_or_reuse_key`), destroying it
   only when the last session goes — so the quota is not currently a live risk,
   but any 1B feature that mints per-device or per-app credentials meets it
   immediately.
2. **Listener idle policy**: one `stalwart_listener` per *user* (not per tab),
   armed on the first `GET /events` after login, reconnecting to Stalwart with
   1→2→4…→30 s backoff, and cancelled by a 5-minute maintenance sweep once its
   hub has had no subscriber and no activity for 30 minutes. Pooled JMAP
   clients are evicted on the same sweep at the same 30-minute idle. See "Live
   updates".
3. **Morph exclusions are a template rule, not a configuration.** Every
   long-lived singleton — `#offline`, `#shortcuts`, `#palette`,
   `#quick-settings`, `#compose-dock`, `#status` — is included by
   `layouts/app.html` and deliberately **not** by `layouts/fragment.html`, so
   an htmx swap into `#main` can never leave a second one in the document. 1B's
   compose dock must keep obeying this. Details and the id-keying that makes
   morph preserve rows are under "Rows & list".
4. **Stalwart quirk, still live**: unauthenticated SMTP to port 2525 (what
   `scripts/send-test.py` does) is classified as spam and lands in **Junk**,
   not the Inbox. The SSE push still fires, so it is a valid live-update probe
   — but "run send-test.py and watch a row appear in the inbox" does not work
   and never did.
5. **Two seam regressions were found in `scripts/measure.py` by re-running it**
   — it had been silently broken since the tasks that changed what it measures
   (`/events` gained a session dependency → 401; the SSE event was renamed
   `new-mail` → `mail` → 0/10 frames every run, which reads exactly like a dead
   pipeline). Both fixed. The lesson for 1B: a measurement script nothing
   re-runs is not a safety net.
6. **Two real defects were found in browser QA and are NOT fixed** (Task 14's
   file ownership was tests, findings, `scripts/measure.py` and the Makefile's
   `qa` target only). Both are described precisely, with a suggested fix, under
   "Browser QA": (a) signing in after a session expires mid-stream lands the
   user on a **bare HTML fragment** instead of the inbox; (b) `keys.js` is
   loaded **twice as two separate module instances**, one of which has an empty
   registry and exists only to run duplicate listeners and waste ~30 KB.
7. **Two spec §11 byte budgets are missed** and one is comfortably met. Total
   JS is 92.3 KiB gz against a 90 KB budget (101.5 KiB as actually fetched,
   because of defect 6b) and the font is 97.4 KB against a 48 KB budget; CSS is
   8.7 KiB gz against 30 KB. Squire and DOMPurify are vendored but **not
   loaded** in 1A — the moment 1B's compose ships them the JS total goes to
   roughly 121 KiB gz. Numbers and the arithmetic are under "Budgets".

## Auth exchange

Task 4. Verified against the live `stalwartlabs/stalwart:v0.16.20` stack this
worktree's compose project runs (`stalwart`/`postgres`/`mailosh`, loopback-only
ports), via `mailosh/security/exchange.py` (`verify_password`),
`mailosh/stalwart_admin.py` (`StalwartAdmin.create_api_key`/`destroy_api_key`),
`mailosh/jmap/client.py` (`JmapClient.connect_bearer`), and
`tests/integration/test_live_auth_flow.py` (`make itest`, run repeatedly — 3
consecutive clean passes while writing this section, no leftover state between
runs). Builds directly on SPK-3 (`p0-findings.md`), which established that
admin-minted `x:ApiKey` credentials are viable at all; this task is the first to
exercise the full lifecycle (verify -> mint -> authenticate -> destroy -> confirm
revoked) end to end against the real server, not just a single mint probe.

### Headline: SPK-3's mechanism holds; its destroy-scope silence turned out to hide a real gap, corrected live

Password verification and key minting work exactly as SPK-3/design spec §9
anticipated. But `destroy_api_key` is **not** simply "the admin's own account id,
like every other admin-scoped call" — that assumption (baked into this task's
plan-time Interfaces block, `destroy_api_key(self, key_id: str) -> None`, no
username) was written before this task ran it live, and it failed outright on the
first real attempt. See "3. destroy_api_key" below for the full transcript. There
is also a hard, previously-undocumented **cap of 5 `x:ApiKey` objects per
account** — see "4. Per-account key cap" — with real consequences for Task 5's
session design.

### 1. `verify_password`: exact shapes, both outcomes exercised live

`GET {stalwart_url}/.well-known/jmap` with HTTP Basic auth
(`auth=(username, password)`), `follow_redirects=True`, 10s timeout. Both
branches were exercised against the real server by
`test_password_verify_and_api_key_lifecycle`, not only by the respx-mocked unit
tests:

- **Right password**: HTTP 200, a real session body (`username`,
  `primaryAccounts["urn:ietf:params:jmap:mail"]` both populated). Cross-checked
  live: `verify_password`'s returned `account_id` was asserted equal to what an
  independent `JmapClient.connect` call with the same credentials resolves —
  not just "truthy", a genuine agreement check, and it held.
- **Wrong password**: confirmed live, exactly as SPK-3/SPK-5 described —
  Stalwart answers HTTP 200, not 401, with an *anonymous* session
  (`username: ""`, `accounts: {}`, `primaryAccounts: {}`). `verify_password`
  reads the raw response dict directly (never builds a `mailosh.jmap.models.
  Session`, whose `primary_account_id` field is required/non-nullable and would
  raise an unrelated `pydantic.ValidationError` on this exact anonymous shape)
  and returns `None`.
- **401/403 handling** (`verify_password` also treats these as invalid
  credentials, not a transport failure) is defensive, not live-observed — this
  Stalwart version never actually answers this way for bad Basic auth on
  `/.well-known/jmap`; kept in case a future version, or a fronting proxy, ever
  does reject at the HTTP layer instead.
- **5xx / connection failure** -> raises `TransportError` (controller decision
  #2), so a caller can distinguish "mail server unreachable" from "wrong
  password". Not separately live-tested (would require breaking the live stack
  mid-test); covered by respx unit tests instead
  (`test_server_error_raises_transport_error`,
  `test_connection_failure_raises_transport_error`).

### 2. `create_api_key`: SPK-3's shape, reconfirmed live, unchanged

Request, verbatim (redacted): `x:ApiKey/set` with `accountId` = the **target**
user's own account id (resolved via `_find_account_id`, an
`x:Account/query`+`get` round trip — see "5. Timing" below), `create: {"k0":
{"description": "<name>-<8 hex>"}}` — a bare one-field payload, nothing else,
matching SPK-3's own recorded shape exactly. Response:

```
{"methodResponses": [["x:ApiKey/set", {"accountId": "c",
  "created": {"k0": {"id": "b", "secret": "API_<redacted>"}}}, "c0"]], ...}
```

The returned `secret` was live-confirmed (via a plain, auth-header-only httpx
client — see the `connect_bearer` note below for why that matters) to
authenticate `GET /.well-known/jmap` as `username: "demo@mailosh.test"` — the
target account, not the admin. `JmapClient.connect_bearer(stalwart_url,
key.secret)` then listed real mailboxes for that account
(`test_password_verify_and_api_key_lifecycle`'s own assertion).

**Gotcha hit while probing this, worth naming so it isn't rediscovered**: an
httpx client constructed with a client-level `auth=(...)` (Basic) **silently
overrides** a per-request `headers={"Authorization": "Bearer ..."}` — httpx's
`BasicAuth.auth_flow` unconditionally rewrites the `Authorization` header
regardless of what was already set. A first draft of this section's own probe
script reused one admin-Basic-auth'd client for a "does this bearer token still
work" check and got back the *admin's own* identity both before and after a
supposed destroy, which briefly looked like `destroy_api_key` doing nothing at
all. `JmapClient.connect_bearer`'s real implementation never has this problem —
it builds its `httpx.AsyncClient` with only `headers=`, never `auth=` — but
it's a sharp edge worth flagging for anyone else writing a throwaway script
against this client library.

### 3. `destroy_api_key`: SPK-3 left this unpinned, and the obvious guess was wrong

SPK-3 recorded that api keys are destroyable ("used to clean up every probe
credential minted during this investigation") but never recorded which
`accountId` a destroy call needs. This task's first implementation guessed the
ADMIN's own account id — consistent with literally every other admin-scoped
call in `stalwart_admin.py` (`x:Domain`, `x:Account`, `x:DkimSignature` are all
global directories, fully readable/writable via the admin's own account
regardless of which specific object is addressed) — and matched the plan's
Interfaces block, which took no `username` parameter.

**Run for real, it failed on the very first live attempt:**

```
POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
{"methodCalls": [["x:ApiKey/set",
  {"accountId": "d333333", "destroy": ["b"]}, "c0"]]}     # d333333 = admin's own account id

-> {"methodResponses": [["x:ApiKey/set",
     {"accountId": "d333333", "notDestroyed": {"b": {"type": "notFound"}}}, "c0"]], ...}
```

— for a key that unquestionably existed (it had just been minted in the same
test run, and its secret was still live-authenticating as the demo account).
A direct follow-up probe explains why:

```
POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
{"methodCalls": [["x:ApiKey/query", {"accountId": "d333333"}, "q0"],
                  ["x:ApiKey/get", {"accountId": "d333333", "#ids": {...}}, "g0"]]}

-> {"methodResponses": [
     ["error", {"type": "forbidden", "description": "Account not found."}, "q0"],
     ["error", {"type": "invalidResultReference", ...}, "g0"]], ...}
```

The admin's own account id ("d333333", the `STALWART_RECOVERY_ADMIN`
bootstrap/recovery principal — see SPK-5 §1) isn't a real backing row for
`x:ApiKey/query` to enumerate at all: it returns a **batch-level `error`**
(`forbidden`/"Account not found"), not an empty list. Unlike `x:Domain`/
`x:Account`, `x:ApiKey` is a genuinely **per-account** resource (each account's
own credential set), not a server-wide directory the admin can browse or manage
through its own account id. The admin *can* still act on any account's api
keys — but only by setting `accountId` explicitly to that key's **owning**
account, the same shape `create_api_key` already used for its own create call.
Scoped that way, both query and destroy work exactly as expected:

```
POST /jmap  {"methodCalls": [["x:ApiKey/query", {"accountId": "c"}, "q0"],   # c = demo account
                              ["x:ApiKey/get", {"accountId": "c", "#ids": {...}}, "g0"]]}
-> lists the key correctly, alongside a second, previously-orphaned key
   ("itest-03ecf0eb") left over from this task's own first failed test run —
   itself a small confirming data point that the admin-account-scoped destroy
   really had been silently doing nothing.

POST /jmap  {"methodCalls": [["x:ApiKey/set", {"accountId": "c", "destroy": ["b"]}, "c0"]]}
-> {"methodResponses": [["x:ApiKey/set", {"accountId": "c", "destroyed": ["b"]}, "c0"]], ...}

# and, from a clean bearer-only client (see the auth_flow gotcha above):
GET /.well-known/jmap  Authorization: Bearer API_<redacted>
  before destroy -> 200, {"username": "demo@mailosh.test", ...}
  after  destroy -> 401
```

**Fix applied**: `destroy_api_key`'s signature changed to
`destroy_api_key(self, username: str, key_id: str) -> None` — a deliberate,
documented departure from the plan's Interfaces block, resolving `username` ->
account id via `_find_account_id` (the same helper `create_api_key` already
uses) before issuing the destroy. This is a real, load-bearing correction, not
a stylistic one: the original signature could not have worked correctly against
the live server no matter how it was implemented internally, since nothing
about `key_id` alone identifies which account's `x:ApiKey` namespace to search.
**Consequence for Task 5**: the logout route needs the session's owning user's
email (already available — `SessionRow.user_id` -> `AppUser.email`, no new
column needed) alongside `SessionRow.api_key_id` to call
`destroy_api_key(user.email, session.api_key_id)`. This is a one-line
adaptation at that call site, not a structural problem, but it is a real diff
from what the plan's Interfaces block promised, and should be treated as
already-corrected there rather than rediscovered.

### 4. Per-account key cap: exactly 5, confirmed live, not anticipated by SPK-3 or the plan

Not mentioned anywhere in SPK-3 or the plan — found by deliberately minting
past the limit (`StalwartAdmin.create_api_key` in a loop, no mocks):

```
create #1..#5: succeed
create #6:
  x:ApiKey/set create failed for 'demo@mailosh.test':
    {'type': 'overQuota', 'description': 'You have exceeded your quota of 5 API keys.'}
```

Reproduced verbatim as a regression test
(`test_create_api_key_raises_on_over_quota_matching_live_shape`, `tests/unit/
test_stalwart_admin.py`) so this exact shape stays covered without needing to
re-trip the live quota to prove it. `create_api_key` has no special handling
for `overQuota` specifically — it's just another `notCreated` reason and raises
`JmapError` the same as any rejected mint — but the *number* matters
operationally:

**This is a real design concern for Task 5, flagged here rather than worked
around in this task (out of scope — no session/cookie logic here).** Design
spec §9 mints one new `x:ApiKey` per login and destroys it on logout / "sign
out everywhere". A quota of 5 means:

- A user who logs in from a 6th browser/device/tab **without** first cleanly
  signing out of one of the other 5 will have that 6th login's
  `create_api_key` call raise `JmapError` outright — this needs a deliberate,
  user-visible handling path in Task 5's login route (e.g. a clear "you have
  reached the maximum number of active sessions — sign out of one first" error,
  or an LRU-eviction policy that destroys the oldest session's key
  automatically), not just an unhandled 500.
- More insidiously: **any session that never gets a clean logout** (browser
  crash, idle expiry, a tab just closed) leaves its Stalwart `x:ApiKey` alive
  until something explicitly destroys it — the Postgres `session` row expiring
  (idle/absolute) does **not**, by itself, destroy the Stalwart-side key unless
  Task 5 also builds an active reaper that calls `destroy_api_key` for
  expired/evicted sessions, not just lets their Postgres rows age out. Without
  that reaper, ordinary multi-device use (phone + laptop + a second browser
  profile + occasionally forgetting to sign out) can exhaust the quota of 5
  within days, for a single real user, with no misbehavior on their part.
  **Recommendation for Task 5**: implement the reaper (a scheduled/on-access
  sweep that destroys the Stalwart key for every session row it expires, not
  only unlinks the row) as a first-class part of the session-expiry path, not
  an afterthought — this is now a functional requirement, not a nice-to-have,
  given the confirmed quota of 5.

### 5. Timing

Measured via the real `StalwartAdmin.create_api_key`/`destroy_api_key` code
path (including each create's own internal `_find_account_id` round trip —
`create_api_key` does not cache this, so every call is genuinely 2 JMAP HTTP
round trips, not 1), 5 consecutive creates against the live stack:

- `create_api_key`: min 22.1ms, max 127.8ms, avg 48.3ms (n=5; the max was the
  first call in the run, plausibly a cold-connection effect — later calls
  clustered near the min).
- `destroy_api_key`: 6.1ms (single sample).

Both comfortably fast for an interactive login/logout path; not separately
budgeted against spec §6 by this task (that section's own numeric budgets
weren't re-read here — flagging as unmeasured against them rather than
claiming a pass).

### What's still open / not exercised by this task

- Whether `overQuota` is exactly 5 for every account type (group vs. user) or
  configurable server-side — only tested against the one demo `User` account
  this dev stack provisions.
- No live test of `x:AppPassword` (SPK-3's Basic-auth-shaped sibling) — out of
  scope; `x:ApiKey` remains the right choice for a Bearer-token session
  credential, unaffected by this task's findings.
- The reaper recommended in "4." above is not implemented anywhere yet — this
  section only names the requirement for whoever picks up Task 5.

### Consequence for design spec §9

Confirms §9's preferred path (admin-minted per-session `x:ApiKey`, no
session-vault fallback) remains viable — nothing found here invalidates that
choice. Two concrete amendments/additions for whoever implements Task 5:
(a) `StalwartAdmin.destroy_api_key` takes `(username, key_id)`, not `(key_id)`
alone — the plan's Interfaces block is superseded by this task's live-tested
signature; (b) session expiry (idle, absolute, and explicit logout) must
actively call `destroy_api_key`, not just delete/expire the Postgres row,
given the confirmed 5-key-per-account quota.

## Design system

Closed out by Task 14 (Task 2 built it and never wrote this section). Verified
in the browser against the running stack, not read off the stylesheet.

**Palette and themes.** Graphite & Blue (spec §4, mockup decision C). Three
theme settings, all three exercised live: `system` (follows
`prefers-color-scheme`), `light` and `dark`, switched from the gear's
quick-settings popover. The switch is applied to the DOM on click and persisted
by a `POST /prefs` afterwards, so it never waits on a round trip.

**Density.** Compact, Standard, Comfortable, all three exercised live and all
three visibly different: Compact and Standard collapse the row to one line
(`subject – preview` inline), Comfortable gives the two-line row the approved
mockups show. **Comfortable is the default** (the mockup decision) and was
restored as the account's setting when QA finished.

**Type and colour tokens.** One self-hosted font file, `inter-latin.woff2`,
`font-weight: 100 900` variable, `font-display: swap`, `<link rel="preload">`d
from both layouts, with an `Inter Fallback` `@font-face` that is metric-matched
to Arial (`size-adjust: 107.47%`, ascent/descent/line-gap overrides) so the swap
does not reflow. No CDN font, no CDN anything: the app's own CSP is
`default-src 'self'; script-src 'self'` with no `unsafe-eval`, which is also why
Alpine is the **CSP build** — see the Makefile's own long comment for what that
build cannot evaluate.

**Comparison against `docs/design/mockups/`.** Structure matches:
regions, row anatomy (checkbox, star, sender, subject + label chip, date), hover
actions replacing the date, the selection toolbar with its count, the label list
with colour dots and counts, the undo toast under the list. Deliberate,
spec-driven divergences from `layout.html`: no **Snoozed** nav item (spec §3 —
deferred features get no control at all), and **list-first** rather than the
mockup's reading-pane-on-the-right (spec §2's approach C decision; the reading
pane is the optional mode). One unintended divergence worth naming: the
mockup's undo toast reads "Conversation archived · Undo · [z]" with a keyboard
chip; the app's reads "Archived · Undo" with no chip. Pixel-level work against
`polish-v2.html` was done and recorded when the polish pass landed, with
screenshots; Task 14 re-checked structure, not pixels, and did not re-do it.

## Rows & list

Closed out by Task 14. All verified live on the 14-message dev account.

**The row is the contract.** `list/row.html` renders
`<div id="row-{thread_id}" role="row" data-id data-email-ids data-count
aria-selected>`. Three of those are load-bearing beyond rendering:

- `id="row-{thread_id}"` is what **idiomorph keys on**. A live update
  re-fetches `/mail/{key}/rows` and morphs it into `#list`; because every row
  carries a stable id, a row that was already on screen is *kept*, not replaced
  — which is why a pending undo, a selection and the roving tab stop all
  survive a live update (verified: see "Live updates").
- `data-email-ids` is the whole conversation and is what an action posts;
  `data-count` is the number of messages *in the mailbox being viewed*, which
  is deliberately not `len(email_ids)`.
- `aria-selected` is the selection model. The list is an ARIA grid with a
  roving tabindex: the row is the tab stop, its controls are `tabindex="-1"`
  cells reached with Arrow keys. Verified by keyboard alone.

**Toolbar and range.** The range readout lives *outside* `#list`, so
`/mail/{key}/rows` re-renders it out-of-band; observed correct across archive,
delete, spam and undo ("1–14 of 14" → "1–11 of 11" → back). The selection
toolbar replaces the default toolbar as soon as anything is selected and shows
`N selected`.

**Hover actions replace the date.** Confirmed visually: hovering (or focusing) a
row swaps its date for archive / delete / mark-read buttons. They are real
buttons that paint above the row's stretched anchor, so clicking one does not
also open the conversation.

**Archive semantics, verified against the live server.** Archive *subtracts the
Inbox* and keeps every other mailbox — a message labelled Work stays under Work
and simply leaves the Inbox. Only a message whose sole mailbox was the Inbox
goes to the Archive folder. This surprised QA at first (three archived rows,
only one of them found in Archive afterwards) and it is correct Gmail behaviour,
not a bug. `tests/integration/test_live_app_flow.py` asserts both halves.

**The Archive folder is created exactly once.** Stalwart provisions Inbox /
Deleted Items / Junk Mail / Drafts / Sent Items and *not* Archive, so archiving
an Inbox-only message on a stock account has nowhere to put the message and
`ensure_role_mailbox` creates the folder. Covered live by
`test_archive_mailbox_is_created_exactly_once`, which parks the dev account's
existing Archive (clears its `role` **and** renames it — RFC 8621 §2 makes both
role and sibling name unique, so leaving either in place would make the create
fail for the wrong reason), archives two Inbox-only messages in two separate
requests, asserts exactly one archive-role mailbox exists after each, and
restores the original in a `finally`. Verified by hand first: `Mailbox/set
update` with `{"role": null}` and back to `{"role": "archive"}` both return
`updated` on this Stalwart build, so the park/restore is safe.

## Keyboard & palette

Closed out by Task 14. **This is the manual keyboard checklist Task 10 was
supposed to write here and did not.** Every line below was run in Chrome
against the live stack; the result column is what actually happened, and the
`make qa` target prints the same list for a human to re-run.

The registry (`static/js/keys.js`) holds **43 entries, 31 available** in 1A; the
other 12 are compose/conversation actions that are declared but marked
unavailable, so the `?` overlay and the ⌘K palette can refuse to advertise a key
that does nothing.

| Keys | Expected | Verified |
|---|---|---|
| `j` / `k` | move the cursor down / up | yes — cursor index tracks |
| `x` | select the cursor row | yes — `aria-selected="true"`, toolbar appears |
| `Shift+J` | extend the selection down | yes — 1 → 3 selected |
| `e` then `z` | archive, then undo | yes — row leaves, title `(11)`→`(10)`, "Archived · Undo"; `z` restores the row *at its original position* and the title |
| `#` then `z` | delete, then undo | yes — "Deleted · Undo", message confirmed in Trash and nowhere else, `z` restores |
| `!` then `z` | report spam, then undo | yes — "Reported spam · Undo", message in Junk **with `$junk`**, `z` restores and clears the keyword |
| `s` | star / unstar | yes — `aria-pressed` toggles, "Starred" then "Unstarred" |
| `Shift+I` / `Shift+U` | mark read / unread | yes — `is-unread` and the title count follow |
| `g s` / `g i` | go to Starred / Inbox | yes — `/mail/starred`, `/mail/inbox`, titles follow |
| `?` | shortcuts overlay | yes — `<dialog id="shortcuts">` with Navigation / Selection / Actions / Application groups, only available keys listed |
| `Esc` | clear selection | yes |
| `⌘K`, type `arch`, `Enter` | run Archive conversation | yes — palette opens with the input focused, "arch" filters to *Archive conversation (e)* and the *Archive* mailbox, `Enter` runs the first and closes the palette |

**`delete` and `spam` had never been fired at real mail before this task.** They
share `archive`'s shape but not its code — `actions.delete`/`actions.spam`
*replace* `mailboxIds` where `archive` subtracts, and `spam` additionally sets
`$junk` — so "it works for `e`" proved nothing about them. Both are now covered
twice: in the browser (above) and over HTTP against the live app
(`test_live_app_flow.py`), including that undo clears `$junk` again rather than
only moving the message back.

**Harness caveats, so the next person does not read a false failure.** Driving
this from Chrome MCP is not the same as a human at a keyboard:

- Synthetic key events do not reach the page through the normal input path;
  they have to be dispatched as real `KeyboardEvent`s into `window` (which is
  where `app.js` binds `keydown`). The palette's own handler is bound to its
  `<input>`, so `Enter` has to be dispatched there, not on `window`.
- **The undo window is 10 s** (`UNDO_WINDOW_MS`, and the server-signed token
  agrees). One tool round trip can exceed that, so `e` and `z` must be issued
  from the *same* script or `z` correctly does nothing and looks like a broken
  undo. This cost a false "undo is broken" reading before it was understood.
- The tab reports `visibilityState: "hidden"` throughout, which freezes CSS
  transitions at t=0 and suppresses every paint/LCP/INP performance entry — see
  "Budgets".
- `<dialog>`'s `close` event never fires in this harness; `dialog.close()` still
  works and `dialog.open` is still truthful.

## Live updates

Closed out by Task 14. Mechanism first, then what was verified end to end.

**Shape.** Stalwart's own JMAP EventSource → one `stalwart_listener` task **per
user** → an `SseHub` → every `GET /events` that user has open. The browser-facing
event is **`mail`** (not `new-mail`; it was renamed and `scripts/measure.py` did
not follow — see "Budgets"), carrying `{"types": ["Email", "Mailbox"]}` with the
JMAP state string on the SSE `id:` line so a reconnecting client can replay.
`static/js/sse.js` coalesces frames for 400 ms and then fires one
`mail:changed`, which re-GETs `/mail/{key}/rows` and morphs it into `#list`,
carrying an out-of-band nav and a fresh `<title>` with it.

**Idle policy — the numbers 1B asked for.** `_MAINTENANCE_INTERVAL_SECONDS =
300`: every 5 minutes a background sweep runs. `_HUB_IDLE_SECONDS = 1800`: a hub
with no `/events` subscriber and no activity for 30 minutes has its listener
cancelled and is forgotten. `_POOL_IDLE_SECONDS = 1800`: same sweep evicts
pooled JMAP clients idle for 30 minutes. The listener itself reconnects to
Stalwart with exponential backoff `1, 2, 4, … 30 s`, reset on every successful
frame, and returns (rather than retrying) once its client has been closed.
Client-side: `OFFLINE_AFTER_MS = 6000` before the banner appears, `POLL_MS =
120000` for the fallback refetch-and-redial while offline.

**Verified end to end, in a real browser, against the real stack:**

- **New mail appears with no reload, and the title shows `(N)`.** A message
  imported into the Inbox while the tab sat open appeared at the top of the
  list within a second; row count 14 → 15, `<title>` `(14)` → `(15) Inbox —
  Mailosh`, range "1–15 of 15", sidebar badge 15. **Both** open tabs updated,
  which is the fan-out working.
- **`scripts/send-test.py` does *not* put a row in the inbox** — see the
  Stalwart quirk in the executive summary. The push fires and every count that
  should move moves; the message is in Junk. This is worth stating plainly
  because the plan's own QA step says to watch the inbox for it.
- **Undo survives a live update.** The seam nobody had tested: tab A archived a
  thread (undo pending, toast up); tab B then starred a *different* message,
  producing a genuine state change; tab A received it, re-fetched and morphed
  (the star appeared on a row tab A never touched, and the title changed); tab A
  then pressed `z` and the archived thread came back **at its original index**,
  title restored, "Undone". The undo token is client-held and the row identity
  is idiomorph-preserved, so the morph in between changes nothing.
- **Two tabs, one logout — the survivor recovers, it does not die silently.**
  This had previously only been modelled in Node. Live: signing out in tab B
  closed tab A's stream (`upstream_lost`), tab A's `EventSource` errored, and
  **the "Reconnecting to your mailbox…" banner appeared** — while the list
  stayed on screen showing its last known state rather than blanking or lying.
  `/events` answers 401 once the session is gone, and a refused stream is not
  one the browser retries, so recovery comes from sse.js's own 120 s poll tick:
  at that tick tab A re-fired `mail:changed`, the rows GET returned 401 +
  `HX-Redirect`, and the tab navigated to `/login`. Confirmed by waiting it out,
  not by reading the code. **It also surfaced a real bug in that redirect — see
  "Browser QA" defect 1.**

## Budgets

Closed out by Task 14. Targets are design spec §11. Everything below is
measured on this machine against the live stack; nothing is estimated.

### Latency

`.venv/bin/python scripts/measure.py`, run 2026-09-03T02:21Z against a 14-message
Inbox / 24-message account.

| Measurement | n | p50 | p95 | min | max | mean |
|---|---|---|---|---|---|---|
| inbox query (`query_inbox`, limit=50) | 20 | 1.2 ms | 2.7 ms | 1.0 | 17.5 | 2.0 |
| thread fetch (`get_thread`) | 20 | 1.0 ms | 2.5 ms | 0.9 | 13.6 | 1.8 |
| SMTP → SSE `mail` frame | 10 | 220.0 ms | 779.6 ms | 205.0 | 971.0 | 328.3 |
| **`GET /mail/inbox/rows`, logged in** | 20 | **7.6 ms** | **14.4 ms** | 6.9 | 15.7 | 9.0 |

The rows fragment is measurement (d), added by this task, and is the one spec
§11's "partial TTFB < 200 ms" is actually about: it is a full
`Email/query`+`Email/get` to Stalwart plus nav, prefs (a Postgres read) and Jinja
rendering. **PASS**, with two orders of magnitude of headroom. The cold first
request (35.1 ms, discarded from the table and reported separately by the
script) pays TCP connect, the session's first pooled JMAP client and Jinja's
first compile. Response body 70,542 bytes uncompressed over 14 messages.
Measured client-side on loopback, so these are server time *plus* a loopback
round trip and httpx overhead — upper bounds, not flattering ones.

One full-page navigation, single sample, from the browser's own Navigation
Timing: TTFB 137 ms, DOMContentLoaded 235 ms, load 367 ms, document 97,212 B
uncompressed.

**`scripts/measure.py` was broken and is now fixed.** Two independent stale
assumptions, both from the same cause — nothing re-ran it between the tasks that
changed what it measures:

1. It opened an **anonymous** `httpx` client at `/events`. That endpoint gained
   a session dependency after Task 11 wrote the script, so the SSE reader died
   on `401 Unauthorized` before the first send and aborted the whole run. Fixed
   by `_app_session`, a logged-in HTTP session (`POST /login` … `POST /logout`,
   CSRF included) shared by measurements (c) and (d).
2. It matched the SSE event name **`new-mail`**. The app publishes **`mail`**.
   Every run therefore reported "0/10 runs got a frame" — indistinguishable from
   a dead live-update pipeline — while a real browser tab open at the same
   moment was visibly refetching on every one of those same sends. Fixed, and
   the reader now logs any unexpected event name once instead of silently
   ignoring it.

### Bytes

> **Superseded — do not quote these figures.** Every number in this subsection
> is the state of the tree on 2026-09-03 and all three have since moved: the
> duplicate `keys.js` fetch was fixed, our own modules grew, and the font
> recipe was corrected so the font budget now passes at 40,752 B. Re-measured
> on 2026-09-05 — see `p1b-findings.md` ("Budgets"), which also records that
> `scripts/measure.py` does not measure byte budgets at all and never did.
> Kept here as the record of what Phase 1A found, not as current numbers.

`gzip -9` on the built assets; the file list is what the browser actually
fetches for `/mail/inbox`, taken from `performance.getEntriesByType('resource')`,
not from the template.

| Loaded on the inbox page | raw | gz |
|---|---|---|
| vendor/htmx.min.js | 51,238 | 16,576 |
| vendor/alpine.min.js (CSP build, 3.17.1) | 71,087 | 23,511 |
| vendor/idiomorph-ext.min.js | 10,153 | 3,483 |
| vendor/preload.js | 4,051 | 1,563 |
| vendor/command-score.js | 5,935 | 1,896 |
| js/keys.js | 30,648 | 9,405 |
| js/actions.js | 40,353 | 14,445 |
| js/app.js | 34,242 | 12,428 |
| js/palette.js | 22,538 | 8,179 |
| js/sse.js | 6,681 | 3,015 |
| **JS total (unique)** | **276,926** | **94,501** (92.3 KiB) |
| **JS total as actually fetched** | 307,574 | **103,906** (101.5 KiB) |
| app.css | 39,080 | 8,947 (8.7 KiB) |
| fonts/inter-latin.woff2 | 99,740 | — (already compressed) |

Vendored but **not loaded** in 1A: `squire.js` (59,913 / 18,394) and
`purify.min.js` (29,204 / 10,900) — both are compose dependencies, and compose
is 1B.

| Budget (spec §11) | Target | Measured | Verdict |
|---|---|---|---|
| Partial TTFB (`/mail/inbox/rows`) | < 200 ms | p50 7.6 / p95 14.4 ms | **PASS** |
| Total JS, gzipped | ≤ 90 KB | 92.3 KiB unique / 101.5 KiB fetched | **MISS** |
| CSS, gzipped | ≤ 30 KB | 8.7 KiB | **PASS** |
| One font file | 48 KB | 97.4 KB, one file | **MISS** |
| Swap paint | < 100 ms | not measurable — see below | **NOT MEASURED** |
| INP | < 200 ms | not measurable — see below | **NOT MEASURED** |
| First-load LCP on LAN | < 1.5 s | not measurable — see below | **NOT MEASURED** |

**Where the JS overrun is, precisely.** The spec's own breakdown budgets *our*
code at ≤ 15 KB gz. Ours is **46.4 KiB gz** (keys 9,405 + actions 14,445 + app
12,428 + palette 8,179 + sse 3,015) — 3.1× the budget, and the dominant term.
Two mitigating facts and one aggravating one, all of them real: our JS ships
**unminified** (there is no Node toolchain, by design, and no minifier in the
build), so some of that is comments and whitespace that a minifier would take;
Alpine's **CSP build** is 23.0 KiB gz against the 19.4 KB the budget assumed for
the standard build, which is a deliberate, security-driven +3.6 KiB; and the
duplicate `keys.js` fetch (defect 2 under "Browser QA") adds a further 9.2 KiB
gz of pure waste to every cold load. **Fixing that duplicate is the single
cheapest 9 KiB available**, and it is a one-line template change. Even so the
budget is missed on unique bytes alone, and 1B should expect roughly **121 KiB
gz** once Squire and DOMPurify load for compose — that is a budget conversation
1B has to have on purpose, not discover.

**The font.** One file is served (`inter-latin.woff2`), which is what the budget
asks for, but it is 97.4 KB against a 48 KB target — a 2.08× miss. It is the
Inter v4.1 variable font subset the Makefile's `pyftsubset` step produces from
`InterVariable.woff2`; the subset ranges are latin plus the punctuation,
currency and arrow glyphs this UI sets. Getting to 48 KB means either a
narrower unicode range or dropping the variable axis for two static weights.
Note also that the *build input* `InterVariable.woff2` (344 KB) sits inside the
served `/static/fonts/` directory — nothing references it and nothing fetches
it, but it is reachable.

**Why INP and LCP could not be measured, and what was measured instead.** The
brief allowed for `web-vitals` being awkward to vendor. It is worse than
awkward and vendoring it would not have helped: the Chrome-MCP tab reports
`visibilityState: "hidden"` and `document.hasFocus() === false` for its whole
life, and a page that has never been visible records **no paint entries at
all** — `performance.getEntriesByType('paint')` and
`…('largest-contentful-paint')` both come back **empty**, buffered
`PerformanceObserver` included. `web-vitals` reads exactly those entries, so it
would report nothing too. INP is equally unavailable: `PerformanceEventTiming`
records real user input, and CDP-dispatched synthetic events produce no `event`
entries (confirmed: empty with `durationThreshold: 16`). These three budgets
need a human at a visible browser with DevTools, and that is the honest status —
not "we could not vendor the library". What stands in their place is the
server-side partial TTFB distribution above (the input to swap paint), the
end-to-end push latency (p50 220 ms / p95 780 ms from SMTP accept to the browser
receiving the frame), and the navigation timing sample.

## Browser QA

Closed out by Task 14. Run in Chrome against `make dev` at
`http://localhost:8000`, on the 14-message dev account, 2026-09-03. Appearance,
keyboard, pointer and live-update results are recorded in the four sections
above; this section holds the **failure drills** and the **defects found**.

### The plan's own QA step is wrong about the offline banner

The plan says "kill Stalwart's network for 10 s → offline banner". **It does
not, and cannot.** `/events` is served by *our* app, not by Stalwart, so
stopping Stalwart leaves the browser↔app SSE stream perfectly healthy and the
banner never appears. Verified: with `stalwart` stopped the banner stayed
hidden the whole time.

The correct drill is to stop the **mailosh** container:

- `docker stop …-mailosh-1` → within ~9 s the banner **"Reconnecting to your
  mailbox…"** appears (6 s `OFFLINE_AFTER_MS` plus the failing dial),
  `$store.ui.offline === true`, and the list stays on screen showing its last
  known state.
- `docker start …-mailosh-1` → the banner **clears by itself**, with no page
  reload, once the `EventSource`'s own retry succeeds. `offline` back to false,
  list intact.

That asymmetry is itself a finding for 1B: **a user whose mail server is down
gets no signal at all** until they try an action. The listener retries Stalwart
behind a hub that still reports itself live, so nothing tells the browser. If 1B
wants "the mail server is unreachable" to be visible, it needs a distinct
signal — the current banner is strictly about the browser↔app stream.

### A failed action during a real mail-server outage reverts, and does not lie

With `stalwart` stopped, pressing `e` on a selected row: the row was
optimistically removed, the write failed, and the row **came back**, with the
toast **"Couldn't archive"** — a specific failure, not the success copy and not
silence. The selection was preserved so the action can be retried, and the title
count never moved. After `docker start …-stalwart-1` the same action succeeded
and `z` undid it, with no page reload — the pooled client and the listener both
recovered on their own.

### Defect 1 — signing in after an expired session lands on a bare HTML fragment

**Reproduced, not theorised.** Leave a tab on `/mail/inbox`, let the session end
(sign out elsewhere, or let it expire). At sse.js's next poll tick the tab
re-fires `mail:changed`; htmx GETs `/mail/inbox/rows?position=0&limit=50`, gets
401 + `HX-Redirect`, and navigates to:

```
/login?next=%2Fmail%2Finbox%2Frows%3Fposition%3D0%26limit%3D50
```

Signing in there redirects to `/mail/inbox/rows?position=0&limit=50` — the
**fragment endpoint** — so the user lands on unstyled partial HTML with no app
shell (a wall of text and a giant unsized SVG). Verified by signing in and
looking at it.

Cause: `mailosh/web/deps.py::_next_url` builds `next` from `request.url.path`,
which for an htmx-driven fragment request *is* the fragment path, and
`mailosh/web/auth.py::_safe_next` only guards against open redirects — it has
no notion of "is this a page?". Suggested fix (not applied — outside this task's
file ownership): in `create_app`'s `SessionRequired` handler, when the request
is an htmx request, take `next` from the **`HX-Current-URL`** header — the
browser's actual page URL, which `mailosh/web/mail.py::_referring_key` already
reads for exactly this kind of reason — falling back to `_DEFAULT_NEXT`. A
narrower belt-and-braces alternative is for `_safe_next` to reject any path a
page route does not serve.

### Defect 2 — `keys.js` is loaded twice, as two separate module instances

`layouts/app.html` loads it by tag, content-hashed:

```html
<script src="{{ static('js/keys.js') }}" type="module"></script>   <!-- /static/js/keys.js?v=2a32fab0 -->
```

…while `app.js`, `actions.js` and `palette.js` each import it **relatively**:

```js
import { dispatch, registerDefaults } from "./keys.js";            // /static/js/keys.js
```

ES modules are keyed by resolved URL, and those are two URLs. Both are fetched
on every page load (visible in the app's own access log and in the browser's
resource timing), and both module instances exist at once. Proved in the page:

```js
const a = await import('/static/js/keys.js');
const b = await import('/static/js/keys.js?v=2a32fab0');
// { sameModule:false, sameRegistryObject:false, sameDispatchFn:false,
//   registryLenA:43, registryLenB:0 }
```

The instance the tag loads has an **empty registry** — `registerDefaults()` is
called by `app.js`, which imported the *other* copy. So the tagged instance does
nothing except run keys.js's top-level side effects a second time, which binds
the shortcuts-`<dialog>` click and close-button listeners **twice** on the same
element. Nothing misbehaves today because both handlers are idempotent
(`dialog.close()`), but the next non-idempotent handler added there will
double-fire, and every cold load pays ~30 KB (9.2 KiB gz) for the privilege.

Suggested fix (not applied — outside this task's file ownership): **delete the
`<script src="{{ static('js/keys.js') }}" type="module"></script>` line.** It is
redundant — every consumer already imports keys.js, and module resolution does
not care about tag order. That removes the duplicate instance, the duplicate
listeners and the wasted bytes in one line.

A related, smaller point for 1B: relative imports bypass the `static()` helper
entirely, so `js/keys.js` and `vendor/command-score.js` are served **without**
the content-hash query the rest of the assets get. They still revalidate (304),
so this is not a staleness bug today, but spec §11's "content-hashed +
`immutable`" story does not currently hold for anything reached by a relative
`import`. Fixing that properly needs an import map or hashed filenames on disk.

### What this task deliberately did not verify

- **"Fresh clone `make up` works"** (plan Step 2's last item). Not run: this
  worktree's stack was already up and healthy throughout, and tearing it down to
  prove a cold start would have destroyed the state every other check in this
  section depends on. `make test`/`make itest`/`make up` all depend on the real
  vendor/icon/font/CSS file targets, so a fresh clone fetches them
  automatically, but that path is **unverified by this task** — treat it as
  open.
- **Mobile/responsive breakpoints** (spec §11's under-768 px rules). Out of the
  plan's Task 14 scope and not exercised.
- **Pixel-level mockup comparison** — done and recorded by the polish-v2 task;
  see "Design system".

### Housekeeping

Every message this QA created was destroyed afterwards and the account was
returned to exactly the 24 messages / 14 Inbox rows it started with, with theme
and density restored to System / Comfortable. Two `Mailosh send-test …` messages
dated 2026-09-02 remain in the Archive folder; they predate this task (Task 8's
manual QA left them) and were left alone rather than silently cleaned up.
