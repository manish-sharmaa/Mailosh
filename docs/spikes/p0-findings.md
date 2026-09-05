# Phase 0 spike findings

Six spike questions tracked across the phase-0 plan. Each is filled in by the task
that closes it out; see `docs/plans/2026-08-31-phase0-spike.md` for the
task list.

## Executive summary

**Proceed to P1: yes** — six for six, no fallback invoked, no gate left open.

- **SPK-1** HTMX gate: **GO** — Squire+Alpine compose island works; no fallback needed.
- **SPK-2** `Email/import`: **GO** — multi-mailbox, `receivedAt`, References-threading all hold live.
- **SPK-3** per-user tokens: **GO** — admin-minted `x:ApiKey` works; no session-vault fallback needed.
- **SPK-4** ACME split: **DECIDED** — Stalwart native ACME (DNS-01) for mail protocols; Caddy for web TLS.
- **SPK-5** management API: **GO, corrected** — real surface is JMAP `x:*` methods, not the brief's guessed REST routes.
- **SPK-6** budgets: **GO, directional** — all 3 measured latencies sit inside spec §6's budgets at spike scale; 100k-message scale is unmeasured.

**Spec amendments:** §4's TLS row gets a concrete refinement (Caddy as the default web-TLS
front; Stalwart's ACME challenge type = DNS-01), not a contradiction — see SPK-4. §9 needs
none; SPK-3 confirms exactly what it already anticipated (preferred per-user-token path viable).

**Known gaps for Phase 1** (no auth, no error surface, no SSE fan-out, and more) are listed under "Known gaps carried into Phase 1" near the end of this document — none block this P0 verdict.

## SPK-1 HTMX gate

Task: 9. Verified against the live stack (`docker compose up -d --build mailosh`,
stalwart/postgres never stopped/recreated) via `GET/POST /compose`
(`mailosh/web/templates/compose.html`, `mailosh/jmap/client.py`'s new
`get_identity`/`send`), plus a real Chrome browser session driven by
CDP-based browser automation for the interactive parts curl can't judge.

### Verdict: GO — Squire 2.4.8 + Alpine 3 compose island inside the HTMX page works

No fallback needed. Every item on the brief's checklist was exercised, either live in a
real browser or by direct, evidence-backed code inspection where the automation harness
itself couldn't reach (noted below, with what still wants a human's own hands).

### 1. Toolbar state reflects selection — confirmed, live, in a real browser

`compose.html`'s Alpine component listens for Squire's own `pathChange` event and
re-reads `hasFormat('B')`/`hasFormat('I')`/`hasFormat('A')` into `canBold`/`canItalic`/
`canLink`, which drive each button's `:class` (active = `bg-indigo-100 text-indigo-700`)
and `:aria-pressed`. Live in Chrome: typed a line, selected it (`Home`/`Shift+End`),
clicked **B** — the text visibly went bold *and* the button highlighted in the same
screenshot; unfocusing/moving the cursor away dropped the highlight again. Squire's own
custom event set (`pathChange`, `select`, `input`, `pasteImage`, `undoStateChange`,
confirmed by grepping the vendored source) is exactly what a toolbar needs — no polling.

**Bug found and fixed live**: the first pass wired `bold()`/`italic()` to *always* call
`editor.bold()`/`editor.italic()` (matching the brief's literal "wired to `bold()`,
`italic()`" wording) — clicking Bold on already-bold text was a no-op instead of a
toggle, confirmed by clicking B a second time and watching the text stay bold. Fixed to
check `canBold`/`canItalic` first and call `removeBold()`/`removeItalic()` when already
active — verified via direct calls against the live Alpine component: `canBold: false`
→ click → `<b>...</b>`, `canBold: true` → click → back to plain text, `canBold: false`.
A real toolbar button needs to toggle; this was worth fixing in the prototype itself; not
deferred as a finding for later.

### 2. Paste behavior — Squire sanitizes via DOMPurify *before* our own submit-time pass

Found in the vendored source (`squire.js`'s `_makeConfig`): Squire's default config
already sets `sanitizeToDOMFragment: n => DOMPurify.sanitize(n, {RETURN_DOM_FRAGMENT:
true, ...})`, and both `setHTML()` (initial/programmatic content) and `insertHTML()`
(what real paste handling calls internally, confirmed by grepping the `willPaste`
event's call site) route through it. Live-tested by calling `editor.insertHTML(...)`
directly (the same code path a real clipboard paste triggers) with a deliberately
dangerous payload:

```html
<p>Pasted <b>bold</b> text <script>window.__xss_marker__=1;</script>
<img src=x onerror="window.__xss_marker__=2">
<a href="javascript:window.__xss_marker__=3">bad link</a>
<a href="https://example.com">good link</a></p>
```

Result: `window.__xss_marker__` stayed `0` — the `<script>` element was removed
entirely, `onerror` was stripped off the `<img>`, and the `javascript:` URL was stripped
off its `<a href>` (left as a bare `<a>bad link</a>`), while the legitimate `<b>` and
`https://` link both survived untouched. So paste safety is **two independent layers**
before this task's own explicit `DOMPurify.sanitize(editor.getHTML())` in the submit
handler ever runs: Squire's `sanitizeToDOMFragment` fires on every paste/insert/`setHTML`
call already. Combined with the P0 rule that the server never renders sent html back to
any user (design spec §8; server-side `nh3` is P2), sent html crosses three independent
guards before reaching a recipient's own client: paste-time DOMPurify, submit-time
DOMPurify, and (eventually, P2) server-side `nh3`.
**Needs a human check**: this proves the *mechanism* is wired and effective against a
synthetic dangerous payload; it does not prove a real Word/Google-Docs clipboard paste
(deeply nested `<span style=...>`/MSO conditional-comment soup) comes out *visually
clean* after Squire's `blockTag: 'DIV'` normalization — only that it comes out *safe*.
Only a human with a real clipboard and a real paste gesture can judge the former; this
harness's synthetic `insertHTML` call and Chrome's own clipboard-permission model both
stood in the way of a genuine paste gesture.

### 3. Quoting for replies — structurally viable

`increaseQuoteLevel()` wraps the current selection in a real `<blockquote>` (verified
live: selecting `<div>Original message text to quote.</div>` and calling it produced
`<blockquote><div>...</div></blockquote>`). P0's compose page is send-only — there's no
"Reply" entry point wired to it yet (out of this task's scope) — but the primitive a
reply feature would need (pre-populate the editor with the original body wrapped in one
more quote level) is confirmed to work exactly as RFC-adjacent mail clients expect.

### 4. Alpine/Squire lifecycle vs. HTMX swaps — the swap-lifecycle problem doesn't arise here, by design

`GET /compose` is a full page load (a real `<a href="/compose">` in `inbox.html`'s
header, not `hx-get`), and `POST /compose`'s `<form>` has no `hx-post` — it's a *normal*
form submission that 303-redirects the browser to `/inbox?sent=1` natively. This means
Alpine's `init()` (which constructs `new Squire(...)`) runs exactly once per real
navigation to `/compose`, and is torn down by the browser's own normal navigation
teardown on submit — there is no htmx `hx-swap` anywhere in this flow that could replace
the editor's container out from under a live Squire instance while Alpine's own
directive-teardown lags behind (the classic failure mode the brief's checklist item is
asking about). This was a deliberate choice, not an accident: the task brief's earlier
draft literally said `hx-post="/compose"`; the controller-resolved requirements this task
implemented against explicitly call for "a normal form POST" instead, precisely to
sidestep this. **If a future task ever wants compose as an htmx-swapped panel** (e.g. a
Gmail-style bottom-right compose popup that stays open while the inbox behind it is
still live), the lifecycle question would need to be revisited for real — Alpine
`x-data` components generally survive being swapped in file *fresh* (a brand-new
`init()` per swap) but need an explicit teardown for anything holding a live
non-Alpine object (a Squire instance, an event listener) if the *same* container is
swapped a second time without a full page reload in between; nothing in this task
exercises that path, since compose never becomes a swap target.

### 5. End-to-end live send — fully verified, twice (curl and a real browser)

Sent from `demo@mailosh.test` to itself with bold text and a link, two ways:

**Via `curl` directly against `POST /compose`** (simulating exactly what the form
submits):
```
$ curl -sS -D - -X POST http://localhost:8000/compose \
    --data-urlencode "to=demo@mailosh.test" \
    --data-urlencode "subject=Mailosh compose-test 20260901T073000Z e91d7c3a" \
    --data-urlencode 'html=<div>Hello <b>bold</b> world! Check out <a href="https://example.com">this link</a>.</div>'
HTTP/1.1 303 See Other
location: /inbox?sent=1
```

**Via the actual browser UI** (Chrome, driven end-to-end: typed/inserted content into
the real Squire editor, filled To/Subject, clicked the real Send button) — landed on
`http://localhost:8000/inbox?sent=1`, the new subject appeared at the top of the inbox
list "just now", and opening the thread showed both copies (Sent + the self-received
Inbox copy) rendering the derived plain text cleanly, HTML-escaped, no raw markup
executing — consistent with the P0 rule that sent html is never rendered back.

JMAP-level confirmation (direct query, both sends independently verified this way):

```
mailbox 'e' role='sent' name='Sent Items'
mailbox 'a' role='inbox' name='Inbox'
...
--- message (Sent Items) ---
keywords: {'$seen': True}                    # note: NO '$draft' — onSuccessUpdateEmail cleared it
mailboxes: ['Sent Items(sent)']
html value: <div>Hello <b>bold</b> world! Check out <a href="https://example.com">this link</a>.</div>
text value: Hello bold world! Check out this link.

--- message (Inbox) ---
mailboxes: ['Inbox(inbox)']                   # lands Inbox, NOT Junk
```

Both copies landed exactly where expected — the Sent copy in Sent Items with `$draft`
cleared (the `onSuccessUpdateEmail` patch worked), and the self-received copy in
**Inbox**, not Junk: confirms the controller's prediction that internal JMAP submission
(unlike Task 8's anonymous external SMTP test mail) doesn't trip Stalwart's spam
heuristic. SSE: a `curl -N` capture of `/events`, timestamped per line, showed the
`new-mail` frame arriving within about 10ms of the POST completing (two frames, ~1s
apart, one per Email/Mailbox state change — the Sent-mailbox move and the Inbox
delivery are two separate `StateChange`s) — the same live dogfood loop Task 8 verified
for SMTP-delivered mail now holds for JMAP-submitted mail too.

### Two real JMAP protocol bugs found and fixed live along the way

Neither is a Squire/Alpine/HTMX finding, but both blocked the live send entirely, so
they're recorded here rather than only in the commit history — this is exactly the
"found live, not by a mocked unit test" category Task 8 also hit:

1. **Missing capability**: `JmapClient.USING` only declared `core`+`mail`; RFC 8621 §7.1
   puts `Identity`/`EmailSubmission` under `urn:ietf:params:jmap:submission`, which the
   client never declared. Symptom: `MethodError: unknownMethod` on `Identity/get`.
   Fixed by adding the capability to the client's fixed `USING` list.
2. **Duplicate call id from an implicit method call**: Stalwart's `onSuccessUpdateEmail`
   -triggered implicit `Email/set` update reuses the *same* call id as its triggering
   explicit `EmailSubmission/set` call (confirmed by inspecting the raw
   `methodResponses` array — three entries, the last two both labeled `"s0"`).
   `JmapClient._call`'s `results[call_id] = args` silently let the later (implicit)
   response clobber the earlier (explicit, actually useful) one, so `send()` saw no
   `created` map and raised a spurious `JmapError` even though the send had genuinely
   succeeded (`onSuccessUpdateEmail`'s own patch had already applied). RFC 8620 §5.3
   guarantees implicit-call responses are appended *after* every explicit one, so the
   fix is `results.setdefault(call_id, args)` — keep the first (explicit) response for
   a given id. Both are covered by dedicated regression tests
   (`test_jmap_request.py::test_call_keeps_first_response_for_a_duplicate_call_id`,
   `test_submission.py::test_send_survives_stalwart_reusing_the_submission_call_id_for_its_implicit_update`),
   reproducing the exact response shapes captured live.

### What still needs a human check

- **Mouse-click-to-focus on the Squire `<div id="editor">` was unreliable through
  CDP-based browser automation**: `document.elementFromPoint`
  confirmed a synthetic click at the editor's on-screen coordinates *does* hit the right
  element, but `document.activeElement` sometimes stayed `<body>` afterward — typing
  then landed nowhere until focus was forced via `element.focus()` in the page's own JS
  console. Once focus was established (by any means), typing, selection, and every
  toolbar button worked without issue. This reads as a known category of CDP-synthesized-
  click limitation against `contenteditable` regions specifically, not a page bug — but
  it was inconsistent enough (worked on the first attempt, failed on a later one with
  the same steps) that it should be confirmed with an actual mouse in an actual
  un-automated browser window before treating it as fully closed.
- **The Link button's `window.prompt()`**: `editor.makeLink(url)` itself was verified to
  work correctly when called directly; the prompt-then-`makeLink` *wiring* in
  `compose.html`'s `link()` method was only verified by reading the code, since a native
  OS modal dialog is outside what browser automation can drive or answer on the page's
  behalf. Low risk (it's four lines of straightforward Alpine code), but unexercised.
- **A genuine paste gesture** (real OS clipboard, real Word/Docs source formatting) —
  see item 2 above; the sanitization mechanism is proven, the resulting visual tidiness
  of real-world clipboard HTML isn't.

### Fallback (not needed — recorded per the brief for completeness)

Verdict is GO, so no fallback is being invoked. Per spec §14/§15, the named fallback if a
future phase's compose needs (attachments, inline images, more toolbar surface) outgrow
this approach is Datastar or one Preact island, with the server contract (`POST
/compose` → `client.send(...)`) unchanged either way — nothing about this task's `send`/
`get_identity` design is Squire-specific, so that swap, if ever needed, is isolated to
`compose.html` alone.

### Consequence for the spec

**§4** ("Templates/UI... HTMX 2 'supported indefinitely'; gate in P0") — gate passed, no
change to the tech-stack table needed. **§14** ("SPK-1: ... Fallback: Datastar or one
Preact island; server contract unchanged") — confirmed still true and now evidence-backed
rather than assumed; no amendment needed. **§8** (rendering/content security) — the
paste-time-DOMPurify + submit-time-DOMPurify two-layer finding (§2 above) is additional
defense-in-depth on top of what §8 already specifies (server never renders *sent* html
back; server-side `nh3` is P2) — confirms §8's P0 scope is sufficient, no amendment needed.

## SPK-2 Email/import

Task: 5. Verified against the same live `stalwartlabs/stalwart:v0.16.20` stack SPK-5
bootstrapped (`demo@mailosh.test` on `mailosh.test`), via
`tests/integration/test_live_stalwart.py::test_import_thread_and_multilabel`
(marker `integration`; `make itest`). Fixture: `tests/fixtures/sample.mbox`, a
real 3-message RFC 5322 thread (`References`/`In-Reply-To` chain back to
`Message-ID: <t1@example.org>`).

### Headline: all three spec §14 sub-questions hold — no surprises this time

Unlike SPK-5, nothing about `Email/import` needed correcting from the brief.
`test_import_thread_and_multilabel` passed on the first run against the live
server and reproducibly on every run since (3 consecutive `make itest` runs
in this task alone, plus a manual `mailosh import-mbox` CLI run — all green,
no assertion weakened to get there). Answering the spec's three named
sub-questions in turn:

### 1. Multi-`mailboxIds`: intact

A single `Email/import` call with two ids set `true` in `mailboxIds` files
the message under both simultaneously — no separate `Email/set` patch needed
afterward. Confirmed on every one of the 3 imported messages:

```json
{
  "id": "ceaaaaar",
  "mailboxIds": { "a": true, "m": true },
  ...
}
```

(`a` = Inbox, `m` = the `create_mailbox`-created label — see §3 below.)

### 2. `receivedAt`: honoured exactly, not derived from the message's own `Date:` header

The integration test deliberately imports each message with a
client-supplied `receivedAt` *different* from that message's own `Date:`
header (fixture dates: 2026-08-20 09:0x UTC; test's `receivedAt`: 2026-08-15
12:0x UTC — a "migrated 5 days earlier" scenario), so a passing round-trip
can only mean the field was actually respected, not that Stalwart coincidentally
parsed the same value out of the header. It was:

```json
{
  "id": "ceaaaaar",
  "receivedAt": "2026-08-15T12:00:00Z"    // == what the client sent, not the
}                                          // Date: header's 2026-08-20T09:00:00Z
```

All 3 messages round-tripped their exact supplied `receivedAt`
(`12:00:00Z`/`12:05:00Z`/`12:10:00Z`). This matters directly for SPK-2's
stated purpose ("needed for Takeout import later"): a bulk historical import
can preserve genuine original receive times instead of everything showing
"now".

### 3. Threading: computed from References at import time, per-message, not batched

The test imports the 3 messages via 3 **separate** `Email/import` calls (one
HTTP round trip each, via `upload` + `import_email` in a loop — not one
batched call with 3 creations). Despite that, Stalwart assigned all three the
same `threadId` immediately, synchronously, on each individual import — no
delayed re-indexing, no need to import in a single batch for threading to
apply:

```json
// Email/import response, one per message (call-by-call):
{"created": {"i0": {"id": "ceaaaaar", "threadId": "r", ...}}}   // msg1 (root)
{"created": {"i0": {"id": "ceaaaaas", "threadId": "r", ...}}}   // msg2 (In-Reply-To msg1)
{"created": {"i0": {"id": "ceaaaaat", "threadId": "r", ...}}}   // msg3 (In-Reply-To msg2)
```

`Email/query` with `collapseThreads: true` then correctly folds all 3 into
one row — and picks the **newest-by-receivedAt** message (`ceaaaaat`, the
sort order requested) as that row's representative id, not the thread root:

```json
{"ids": ["ceaaaaat"]}   // Email/query, collapseThreads=true, receivedAt desc
```

`Thread/get` on that row's `threadId` confirms all 3 member ids, and a
follow-up `Email/get` on those ids shows the `references`/`inReplyTo` chain
Stalwart parsed straight out of the raw message headers:

```json
{"id": "ceaaaaar", "references": null,              "inReplyTo": null}
{"id": "ceaaaaas", "references": ["t1@example.org"], "inReplyTo": ["t1@example.org"]}
{"id": "ceaaaaat", "references": ["t1@example.org", "t2@example.org"], "inReplyTo": ["t2@example.org"]}
```

i.e. threading is genuinely `References`-based (RFC 5322 chain), not
subject-line matching — msg1/msg2/msg3 share no thread-defeating quirks, and
the chain reconstructs correctly even though every message arrived via its
own independent `Email/import` call.

### `create_mailbox` (added this task, consumed by the test above)

`JmapClient.create_mailbox(name) -> str` — a plain `Mailbox/set` create
(`{"create": {"m0": {"name": name}}}`), checked against the response's
`created`/`notCreated` maps the same way `import_email` already checks its
own creation (unit-tested first: `tests/unit/test_jmap_mail.py::
test_create_mailbox_request_and_response` / `::test_create_mailbox_not_created_raises`).
No surprises against the live server either — `Mailbox/set create` behaved
exactly per RFC 8620 §5.3.

### Verification (reproduced fresh while writing this section)

```
$ .venv/bin/python -m pytest -m integration -v
tests/integration/test_live_stalwart.py::test_import_thread_and_multilabel PASSED

$ .venv/bin/mailosh import-mbox tests/fixtures/sample.mbox --label CliImported
Imported 3 message(s) from tests/fixtures/sample.mbox into Inbox + 'CliImported' (k)
```

Self-cleaning: the integration test destroys the Email objects it created
and the label mailbox in a `finally` block (via `JmapClient._call` directly —
`destroy` is deliberately **not** added to the public client contract, per
this task's brief), so repeated `make itest` runs stay green instead of
accumulating "SpikeLabel-*" mailboxes and duplicate "Spike thread" rows.
Verified idempotent: ran 3 times back to back, PASSED every time, mailbox
list and Inbox count identical (`0` total, no stray `SpikeLabel-*`) before
and after each run. The manual CLI run above isn't self-cleaning (it's a
real user-facing command, not a test) — cleaned up by hand afterward via the
same `Email/set destroy` + `Mailbox/set destroy` calls for this spike
session; a real user would just accumulate real imported mail, which is the
intended behavior.

### Open items for later tasks

- `import_email`'s `Email/import` response includes a server-assigned
  `threadId` directly in the `created` map (see §3 above) that
  `JmapClient.import_email` currently discards (returns only the new
  `id`). Not needed by anything in P0 — `query_inbox`/`get_thread` both
  re-derive thread membership anyway — but worth knowing it's already on
  the wire if a later task wants to avoid a round trip (e.g. Task 9's
  `send`, if it ever needs the thread id of what it just sent/appended).
- Not exercised here: `Email/import` importing the **same** `blobId` twice
  (duplicate-detection semantics), or importing into a mailbox id that
  doesn't exist (error shape). Neither blocks P0; flagging in case a later
  task (e.g. a real Takeout importer, P1+) needs idempotent re-import.

### Consequence for the spec

**§5** (data model: "label = Mailbox... thread = JMAP `Thread`") — confirmed exactly as
modelled: multi-mailbox membership and thread collapsing both work through the plain JMAP
primitives §5 already assumes, no schema surprises. **§14** ("SPK-2: `Email/import`
semantics... needed for Takeout import later; verify now while modelling") — gate passed;
a future Takeout importer (P5 per §13) can rely on `receivedAt` being honoured and
References-based threading applying per-message, without needing to batch imports or
post-process thread membership itself.

**Caveat found and fixed during Task 11** (recorded here since it lives in this SPK's own
test): a 3-message copy of this section's own fixture (`tests/fixtures/sample.mbox`,
Message-IDs `t1`–`t3@example.org`) was found live-persisted in a manually-created "Spike"
mailbox, left over from earlier work on this test and never cleaned up — since Stalwart's
threading is purely References-based (confirmed above), any fresh `Email/import` of the
same fixture always gets folded into the same thread as that leaked copy, which made
`test_import_thread_and_multilabel`'s `assert len(msgs) == 3` see 6 instead and fail. Not
a code or spec problem — a stale live-data hygiene issue. Removed via a one-time,
live-verified `Email/set destroy` + `Mailbox/set destroy` (task-11-report.md has the full
before/after evidence); `make itest` is green and was re-run twice more to confirm.

## SPK-3 per-user tokens

Task: 10. Verified against the same live `stalwartlabs/stalwart:v0.16.20` stack,
via `StalwartAdmin.try_mint_user_token` (`mailosh/stalwart_admin.py`) and a
standalone throwaway script exercising that exact method live.

### Verdict: preferred path viable — minting a per-user token is a real, working operation

Design spec §9 asked whether an admin credential can mint a per-user access token
directly (the preferred path) or whether P0 needs a session-vault fallback
(storing/reusing each user's own login instead). **The preferred path is viable.**
No session-vault fallback is required for P0.

### What exists on the live server

Three candidate mechanisms were found and probed, by downloading `GET /api/schema`
(the same self-describing schema Task 2 used for SPK-5) and grepping its `objects`
map for anything auth/token/credential-shaped:

| Mechanism | Schema evidence | Works for silent per-user minting? |
|---|---|---|
| `x:ApiKey` | tagged-union variant of `x:Credential` (`schemaName: x:SecondaryCredential`), also its own top-level object (`permissionPrefix: sysApiKey`) | **Yes** — see below |
| `x:AppPassword` | same shape as `x:ApiKey` (`permissionPrefix: sysAppPassword`) | Yes, but Basic-auth-shaped, not a Bearer token — see below |
| `x:OAuthClient` + a real OAuth2 authorization server | `permissionPrefix: sysOAuthClient`; live `GET /.well-known/oauth-authorization-server` | **No** — structurally requires human interaction, see below |

### 1. The naive approach — adding a credential via `x:Account/set update` — is explicitly rejected

SPK-5 already found `x:Credential` is a tagged union (`Password` | `AppPassword` |
`ApiKey`) used by `x:Account.credentials` (an `objectList`, index-string-keyed map).
The obvious next step — patch an existing account's `credentials` property the same
way a `Password` is added at create time — fails immediately and explicitly:

```
POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
{"methodCalls": [["x:Account/set", {"accountId": "<admin accountId>",
  "update": {"<account id>": {"credentials/1": {"@type": "ApiKey", "description": "..."}}}}, "c0"]]}

-> {"notUpdated": {"<account id>": {"type": "invalidProperties",
     "description": "Secondary credentials cannot be set directly.",
     "properties": ["credentials"]}}}
```

(`AppPassword`/`ApiKey` share `schemaName: "x:SecondaryCredential"` — hence
"**Secondary** credentials cannot be set directly", a server-side rule covering
both, not something specific to `ApiKey`.)

### 2. The real mechanism — `x:ApiKey`/`x:AppPassword` as independently-settable top-level objects, scoped to the TARGET user's own accountId

`GET /api/schema`'s `objects` map lists `x:ApiKey`/`x:AppPassword`/`x:OAuthClient`
with their own `permissionPrefix`, the same way `x:Domain`/`x:Account` are listed —
a hint they're independently `/set`-able, not merely nested inside `x:Account`.
Trying `x:ApiKey/set create` directly, scoped to the **target user's own JMAP
account id** (not the admin's — every other call in this whole spike, SPK-5
included, uses the admin's `accountId`), confirms it:

```
POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
{"methodCalls": [["x:ApiKey/set", {"accountId": "<TARGET user's account id, e.g. 'd'>",
  "create": {"k0": {"description": "spk3-probe"}}}, "c0"]]}

-> {"methodResponses": [["x:ApiKey/set", {"accountId": "d",
     "created": {"k0": {"id": "b", "secret": "API_<redacted, shown once — this exact
       key was destroyed via x:ApiKey/set destroy immediately after this probe>"}}}, "c0"]],
    "sessionState": "..."}
```

The server generates and returns a real secret directly in the create response
(`secret` is `update: "serverSet"` in the schema — same "shown once, redacted on
any later read" shape as `x:DkimSignature`'s `privateKey.secret`, SPK-5 §6). This
secret **works as a Bearer credential for exactly that user's own JMAP session**,
confirmed live both by hand and via `StalwartAdmin.try_mint_user_token` itself:

```
$ curl -s -H "Authorization: Bearer API_<redacted>" \
    http://localhost:8080/jmap/session | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["username"], list(d["accounts"]))'
admin@spike2.test ['d']
```

— not the admin's identity, not a 401, exactly the target account. `x:AppPassword/set
create` was also probed and works identically (`secret` shaped like
`app_<redacted, lowercase-prefixed>`, vs. `ApiKey`'s `API_`-prefixed one) — but
authenticates via HTTP **Basic** auth
(`user:secret` = `admin@spike2.test:app_...`), matching its schema description
("App password for third-party applications", i.e. meant for pairing with an
IMAP/SMTP client's login form) rather than a Bearer token. `try_mint_user_token`
uses `x:ApiKey`, the closer semantic and mechanical match for "token".

Both credential types support `/set destroy` (confirmed live, used to clean up
every probe credential minted during this investigation) — minting is cheap and
reversible, not a one-way commitment.

### 3. What's ruled out: Stalwart's OAuth surface is real, but structurally can't do this

Stalwart also exposes a genuine, working OAuth 2.0 authorization server, found via
the JMAP session capabilities and a well-known endpoint:

- The admin's own `GET /jmap/session` capabilities list has **no OAuth-related
  capability at all** (`urn:ietf:params:jmap:oauth` or similar doesn't exist in
  v0.16.20's capability set) — OAuth isn't advertised through JMAP itself.
- `GET /.well-known/oauth-authorization-server` returns 200 with full RFC 8414
  metadata:
  ```json
  {"issuer": "https://mail.mailosh.test",
   "token_endpoint": "https://mail.mailosh.test/auth/token",
   "authorization_endpoint": "https://mail.mailosh.test/login",
   "device_authorization_endpoint": "https://mail.mailosh.test/auth/device",
   "registration_endpoint": "https://mail.mailosh.test/auth/register",
   "grant_types_supported": ["authorization_code", "refresh_token",
     "urn:ietf:params:oauth:grant-type:device_code"],
   "scopes_supported": ["openid", "offline_access",
     "urn:ietf:params:oauth:scope:mail", "urn:ietf:params:oauth:scope:contacts",
     "urn:ietf:params:oauth:scope:calendars"]}
  ```
- `x:OAuthClient` (schema: `clientId`, `secret`, `redirectUris`, `contacts`, ...)
  is how an OAuth *client application* (e.g. "Thunderbird") gets registered — not
  a per-user token itself.

Every grant type this server advertises (`authorization_code`: browser redirect;
`device_code`: RFC 8628 device-code entry page; `refresh_token`: only usable once
one of the other two has already happened once) requires an **interactive human
step**. There is no `client_credentials`-style grant or admin-assertion flow that
would let a script silently mint an access token for an arbitrary `email` the way
`try_mint_user_token`'s contract needs — this is by design (RFC 6749's whole point
is that an app can't get a user's token without that user's consent) and was
confirmed by inspecting the metadata rather than assumed. Ruled out for this
method on structural grounds, not because it doesn't work.

### Consequence for design spec §9

**Preferred path (admin mints a per-user token) is viable — no session-vault
fallback needed for P0.** `StalwartAdmin.try_mint_user_token(email)` mints an
`x:ApiKey` scoped to the target account and returns its secret, usable
immediately as `Authorization: Bearer <secret>` against that user's own JMAP
session — a real, admin-driven, non-interactive, revocable per-user credential.
This directly replaces whatever session-vault/stored-user-password design §9 was
hedging against needing.

## SPK-4 ACME split

Task: 11. This is a **written P1 decision, not an implementation** — per the brief,
ACME is not enabled on the dev stack (Task 2's bootstrap-time `requestTlsCertificate:
false` stays as-is; see SPK-5). Verified against Stalwart's own live self-describing
schema (`GET /api/schema`, the same mechanism SPK-3/SPK-5 already used to ground their
answers in the real server rather than docs alone) plus `docs/hosting.md` (facts
verified 2026-08-31 directly from stalw.art's own docs) and design spec §3/§4's
architecture.

### Verdict: split — Stalwart's native ACME for its own mail-protocol listeners; Caddy in front of Mailosh's own web process for browser-facing TLS. Recommended Stalwart challenge type: **DNS-01**.

Design spec §4 already named this as the default with the split "to settle in P0"; this
task settles it, backed by live schema evidence rather than assumption, and adds one
concrete refinement `docs/hosting.md`'s own one-liner on this (sizing section, "Native ACME
TLS with HTTP-01, DNS-01, DNS-PERSIST-01, TLS-ALPN-01 challenges") didn't cover: which
challenge type Stalwart should actually use, and why, given the two ACME clients
(Stalwart's own, and Caddy's) will typically share one host/IP.

### 1. What Stalwart's schema actually offers (live-verified)

`docker compose exec stalwart cat /etc/stalwart/config.json` (read-only peek, per the
brief) was checked first: it's a 128-byte pointer to the RocksDB backend
(`{"@type":"RocksDb","path":"/var/lib/stalwart/",...}`), not a flat TLS/ACME config file —
confirming SPK-5's own finding that real configuration lives in the object store, managed
through the JMAP-shaped `x:*` methods, not a file this task could `cat` its way through.
`GET /api/schema` (Basic admin auth, `curl -L --compressed`) lists real, independently
`/set`-able objects for this — not just the four challenge-type names `docs/hosting.md`
already cited from stalw.art's docs (HTTP-01, DNS-01, DNS-PERSIST-01, TLS-ALPN-01,
confirmed present verbatim as `AcmeChallengeType` enum values below):

| Object | Purpose | Key fields (from live schema) |
|---|---|---|
| `x:AcmeProvider` | One ACME account/provider config, reusable across domains | `directory` (ACME directory URL; default `https://acme-v02.api.letsencrypt.org/directory`), `challengeType` (enum, default `TlsAlpn01`), `contact` (email set), `eabKeyId`/`eabHmacKey` (External Account Binding, for CAs like ZeroSSL/Google Trust Services that require it), `renewBefore` (default `R23` = renew at 2/3 of remaining validity), `reuseKey`, `accountKey`/`accountUri` (server-set after registration) |
| `x:Domain.certificateManagement` | Per-domain toggle | Tagged union `Manual` \| `Automatic` (`x:CertificateManagementProperties`: `acmeProviderId` + `subjectAlternativeNames`, e.g. `mta-sts`, `autoconfig`, or the bare apex for a wildcard) |
| `x:SystemSettings.defaultCertificateId` | Fallback cert for non-SNI clients | "Default TLS certificate to use when no SNI is provided by the client" — confirms Stalwart resolves the *serving* cert per-connection via SNI against each domain's `certificateManagement`, with this as the one explicit non-SNI fallback |
| `x:NetworkListener` | Per-listener bind/protocol/TLS config | `protocol` enum (`NetworkListenerProtocol`: `smtp`, `lmtp`, **`http`**, `imap`, `pop3`, `manageSieve`), `useTls`/`tlsImplicit`/`tlsTimeout`/cipher restrictions. **No per-listener certificate-id field** — confirms cert selection is global/SNI-driven (via the two rows above), not bound per-listener |
| `x:DnsServer` (`DnsServerBootstrapType`/`DnsServerType` enums) | DNS provider credentials, for Stalwart's own automatic DNS-record publishing (`x:Domain.dnsManagement`, `x:DnsManagementProperties`: `dnsServerId`, `publishRecords` incl. `dkim`/`spf`/`dmarc`/`mx`/`caa`/`mtaSts`/...) | 60+ built-in providers: Cloudflare, Route53, DigitalOcean, Hetzner, OVH, GoDaddy, Vultr, DNSimple, Netcup, Linode, Namecheap, ... (full list captured in task-11-report.md) |

`AcmeChallengeType` enum, verbatim from the live schema (matches `docs/hosting.md`'s own
citation exactly): `TlsAlpn01` (default), `DnsPersist01`, `Dns01`, `Http01`.

**Inference, not directly verified**: `x:DnsServer`'s provider list exists primarily for
publishing zone records automatically, but the same per-domain DNS-provider credential is
the natural (and, in every general-purpose ACME client we're aware of, the standard) way
to also automate DNS-01's `_acme-challenge` TXT record — this task did not live-test a
DNS-01 issuance (would need a real public domain + real DNS provider credentials + public
port reachability, none of which exist in this dev sandbox, and the brief explicitly says
not to enable ACME here). Flagging this as inference so it doesn't read as more verified
than it is.

### 2. Why "Stalwart terminates web TLS too" doesn't fit Mailosh's architecture

Structurally, Stalwart's `x:NetworkListener` *can* run a `protocol: http` listener with an
ACME-issued cert (the enum lists `http` as a first-class listener protocol alongside
`smtp`/`imap`/etc.) — so the schema alone doesn't rule this branch out. But design spec §3
draws Mailosh's web app as its own process (`Browser --HTTP/2--> mailosh (FastAPI)`),
architecturally separate from `stalwart` — a different container, different codebase,
talking to Stalwart only via the JMAP client (`mailosh.jmap`) and the admin API
(`mailosh.stalwart_admin`), never the reverse. Two problems with routing browser traffic
for the webmail hostname through Stalwart's own `http` listener instead:

- **No evidence Stalwart's `http` listener is a general reverse proxy.** Every `http`-typed
  admin surface this spike has touched (SPK-5's `/api/*`, `/jmap`, the webui) is Stalwart's
  *own* API/UI — `x:NetworkListener`'s fields (this task's own schema dump) have no
  upstream/backend-URL property that would let it forward to an unrelated FastAPI process.
  Making it front Mailosh's webmail would require capabilities not evidenced anywhere in
  the schema this spike explored.
- **It would weld Mailosh's web-TLS lifecycle to Stalwart's process**, contradicting §3's
  explicit seam ("nothing except `jmap/` + `admin/` talks to Stalwart") — the whole point
  of that seam (named in §3 as "the seam the Cloud reuses") is that Mailosh's web layer
  stays swappable/independently deployable from the mail engine.

Caddy, already named in §4 as the candidate ("Caddy (optional container) or user's own
proxy"), needs no schema capability from Stalwart at all — it terminates TLS for exactly
the one hostname the webmail UI is served from and reverse-proxies plaintext to the
`mailosh` container internally, which is a complete, independent, zero-coupling answer to
"who terminates the browser's TLS."

### 3. The one real wrinkle: port 80/443 contention on a single-IP box, and why DNS-01

Spec §12's reference deployment is one small VPS — typically one public IP. Stalwart's
mail-protocol ports (25/465/587/993/995/4190) never collide with Caddy's (80/443), so
there is no conflict for *those*. The conflict is specific to **how Stalwart's own ACME
client validates domain ownership**: `Http01` needs port 80 reachable to Stalwart itself;
`TlsAlpn01` (the schema's own default) needs port 443. If Caddy is also running its own
default automatic-HTTPS on the same box (which by default also wants 80/443 for the web
hostname), the two independent, uncoordinated ACME clients contend for the same ports.

**Recommendation: set Stalwart's `x:AcmeProvider.challengeType` to `Dns01`** (via a
configured `x:DnsServer` for the mail domain's DNS provider — see table above). This lets
Stalwart prove domain ownership over DNS alone, never binding 80/443 for its own
certificate issuance, so Caddy can own 80/443 exclusively for the web hostname with its own
default HTTP-01/TLS-ALPN-01, zero coordination needed between the two. This is a stronger
reason than "DNS-01 supports wildcards" (though it does) — it's specifically what removes
the *only* structural conflict between the two ACME clients this split otherwise creates.

**Named fallback, for an operator who won't configure DNS provider API credentials**: keep
Stalwart on `Http01`, and have Caddy (which is listening on 80 for every hostname on the
box anyway) reverse-proxy just the `/.well-known/acme-challenge/*` path for the *mail*
hostname through to Stalwart internally, while still handling its own cert issuance for the
webmail hostname directly. More moving parts; worth naming as Option B rather than the
default.

### 4. Recommended P1 config sketch (not executed — see brief; sketch only)

Stalwart side (JMAP `x:AcmeProvider`/`x:Domain` calls, same shape as SPK-5's/Task 10's
already-verified `x:Domain/set create` and `x:Account/set create` calls — admin-scoped
`accountId`, tagged-union `"@type"` discriminator, exactly the pattern this spike already
proved works):

```
POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
{"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"],
 "methodCalls": [
   ["x:DnsServer/set", {"accountId": "<admin accountId>",
     "create": {"d0": {"@type": "Cloudflare",
       "description": "primary DNS", "...": "<provider credential fields, see caveat below>"
     }}}, "c0"],
   ["x:AcmeProvider/set", {"accountId": "<admin accountId>",
     "create": {"a0": {"directory": "https://acme-v02.api.letsencrypt.org/directory",
       "challengeType": "Dns01", "contact": ["admin@example.com"]}}}, "c1"],
   ["x:Domain/set", {"accountId": "<admin accountId>",
     "update": {"<domain id>": {
       "dnsManagement": {"@type": "Automatic", "dnsServerId": "#d0"},
       "certificateManagement": {"@type": "Automatic", "acmeProviderId": "#a0"}
     }}}, "c2"]
 ]}
```

The `x:AcmeProvider`/`x:Domain` calls above use field names confirmed directly from this
task's own `fields['x:AcmeProvider']`/`fields['x:Domain']` schema dump — the same
evidentiary standard SPK-5's `x:Bootstrap/set`/`x:Account/set` calls used. **Caveat on the
`x:DnsServer/set` call**: this task's `/api/schema` dump returned `fields['x:DnsServer']`
as `null` (unlike the other three objects above, which all returned full field lists) —
the object and its 60+ `DnsServerType` provider variants (table above) are confirmed live,
but its exact per-provider create-payload shape (API token field name, etc.) is not; the
`"..."` above is a placeholder, not a verified field name. Verify against a fresh
`/api/schema` pull or the webui's own DNS-provider form before implementing.

Caddy side (a new `caddy` service in `docker-compose.yml`'s P1 profile, fronting the
`mailosh` container — `mailosh`'s own `8000:8000` host port publish goes away once Caddy
exists, matching what this dev compose file exposes today only because there is no Caddy
in front of it yet):

```caddyfile
mail.example.com {
    reverse_proxy mailosh:8000
}
```

No extra ACME config needed on the Caddy side for the common case — automatic HTTPS via
its own default HTTP-01 is Caddy's headline feature, and it owns 80/443 uncontested once
Stalwart is on DNS-01 (§3 above).

### Consequence for the spec

**§4**'s TLS row ("Proxy/TLS (web): Caddy (optional container) or user's own proxy;
mail-protocol TLS via Stalwart ACME; settle exact split in P0 (SPK-4)") — the split is now
settled, not contradicted: recommend Caddy become the *documented default* (still
swappable for "user's own proxy," e.g. an operator already running nginx/Traefik), and add
one concrete detail §4's one-liner didn't have: Stalwart's ACME challenge type should
default to **DNS-01** specifically to avoid port contention with Caddy, not left
unspecified (which would default to the schema's own `TlsAlpn01`, and collide). **§11**
(setup wizard) should surface an ACME-provider step for the mail domain (directory URL,
contact email, DNS provider selection if `Dns01` is chosen) alongside the existing
domain/hostname/storage/SMTP-mode steps — not itself a P0 concern, but the concrete object
this task found (`x:AcmeProvider`/`x:DnsServer`) is what a P1 wizard step would drive.

## SPK-5 management API

Task: 2. Verified against `stalwartlabs/stalwart:v0.16.20` running locally via
`docker compose`, cross-checked with the real `webui.zip` release bundle (the same
one the container downloads on first boot) and the live `/api/schema` self-description.

### Headline: the brief's candidate REST routes do not exist

`POST /api/domain/{name}`, `POST /api/principal`, `GET /api/dkim/{domain}`, and the
`STALWART_ADMIN_SECRET` env var were all guesses in the task brief. None of them
work on v0.16.20:

- `STALWART_ADMIN_SECRET=...` is silently ignored.
- `POST /api/domain/mailosh.test`, `POST /api/principal`, `GET /api/dkim/mailosh.test`
  all return `404` at every stage (bootstrap mode and after).

The real mechanism is below. `docker-compose.yml` and `scripts/stalwart-init.sh`
in this repo implement the corrected version.

### 1. Admin credential: `STALWART_RECOVERY_ADMIN`, not `STALWART_ADMIN_SECRET`

Without any admin env var, a fresh container starts in **bootstrap mode** and prints
a random one-time admin password to stderr:

```
🔑 Stalwart bootstrap mode - temporary administrator account
   username: admin
   password: <random, shown once>
...
This password is shown only once. To pin a credential instead, set
STALWART_RECOVERY_ADMIN=admin:<password> in the env file.
```

Setting `STALWART_RECOVERY_ADMIN=admin:${MAILOSH_STALWART_ADMIN_SECRET}` (compose
`environment:`) pins a known credential instead. Empirically verified this is a
**persistent recovery/break-glass credential**, not just a bootstrap-phase login: it
authenticates before setup, immediately after `x:Bootstrap/set` completes, and after
the subsequent container restart — coexisting with (and independent of) the separate
permanent admin account (`admin@mailosh.test`, random password) that the setup
step itself provisions. `docker-compose.yml` and `scripts/stalwart-init.sh` always
authenticate as `admin:$MAILOSH_STALWART_ADMIN_SECRET` for exactly this reason: it
is stable across the whole lifecycle, so the script never has to capture/parse a
server-generated secret.

### 2. Bootstrap mode is a real, distinct server state

A fresh container with no `/etc/stalwart/config.json` starts with **only port 8080
(plain HTTP) listening** ("Port 8080 is open for initial setup" in the log); SMTP,
IMAP, submission, etc. are not started yet. JMAP itself works in this state (the
bootstrap admin gets a working, if minimal, JMAP account), but the real management
surface (domains, accounts, DKIM, `/api/schema`) is not available until setup
completes.

### 3. There is no separate REST management API — it's JMAP-shaped custom methods

Nearly everything, including initial setup, rides the **same `POST /jmap` endpoint
and HTTP Basic auth** as regular mail JMAP traffic, using custom methods on
`x:`-prefixed object types (capability `urn:stalwart:jmap`):

| Object | Methods used | Purpose |
|---|---|---|
| `x:Bootstrap` | `get`/`set`, singleton id `"singleton"` | initial setup (domain, hostname, storage, TLS, DKIM defaults) |
| `x:Domain` | `get`/`query` | mail domains (auto-created by bootstrap's `defaultDomain`) |
| `x:Account` | `get`/`query`/`set` | user/group mailboxes |
| `x:DkimSignature` | `get`/`query` | DKIM signing keys (public key, selector, algorithm) |

These were discovered by downloading the exact webui bundle the container fetches
(`https://github.com/stalwartlabs/webui/releases/latest/download/webui.zip`) and
reading its API client (`assets/client-*.js`): every admin action reduces to the
same generic `${type}/get`, `${type}/set`, `${type}/query` JMAP call pattern used
for mail, posted to `/jmap`. The full object/field schema is self-described at
runtime (see below) and matches the CLI's generic `describe`/`query`/`create`
commands documented at `stalw.art/docs/management/cli/`.

The only two plain REST endpoints found:

- `GET /api/schema` — full schema for every object type (objects, fields, forms,
  enums). **Gzip-compressed and 302-redirects to a content-hashed URL** — fetch with
  `curl -L --compressed`. This is what `x:*` method/field names above were confirmed
  against, not guesswork.
- `GET /api/account` — `{"permissions": [...], "edition": "...", "locale": "..."}`
  for the caller. Cheap authenticated ping: bootstrap-mode admin has exactly
  `["sysBootstrapGet", "sysBootstrapUpdate"]`; a fully set-up admin has hundreds of
  `sys*`/`jmap*`/`imap*`/... permissions. `scripts/stalwart-init.sh` uses the
  `x:Bootstrap/get` empty-list-vs-not check instead (more direct), but `/api/account`
  is a good substitute for the brief's guessed `/api/oauth` ping route.

### 4. Completing setup: `x:Bootstrap/set`, then a mandatory restart

```
POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
{"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"],
 "methodCalls": [["x:Bootstrap/set", {"accountId": "<admin accountId>",
   "update": {"singleton": {
     "defaultDomain": "mailosh.test",
     "serverHostname": "mail.mailosh.test",
     "requestTlsCertificate": false
   }}}, "c0"]]}
```

Notes:
- `defaultDomain` **auto-creates the domain** (with DKIM keys, since
  `generateDkimKeys` defaults to `true`) — there is no separate domain-create call
  in the bootstrap path.
- `serverHostname` must be a real-looking FQDN; the container's hex hostname is
  rejected (`"Invalid server hostname"`). `mail.mailosh.test` was chosen
  deliberately distinct from the `mailosh.test` mail domain — this mirrors real
  mail topology (MX for a domain points at a separate server hostname) and matches
  what Stalwart's own generated DNS zone file expects (see below).
- `requestTlsCertificate: false` avoids Stalwart trying to obtain a Let's Encrypt
  cert for a non-public test domain during bootstrap (see SPK-4 note above).
- The response returns the server-generated **permanent** admin
  (`{"username": "admin@mailosh.test", "secret": "<random>"}>`) — informational
  only; nothing depends on capturing it, since the recovery admin credential
  keeps working (see §1).
- **The container must be restarted** after this call for the persisted config to
  load and the full protocol listeners + management surface to activate ("restart
  Stalwart for the new configuration to take effect", from the webui's own copy).
  `scripts/stalwart-init.sh` calls `docker compose restart stalwart` and re-polls
  `/healthz/live` before continuing. Verified end to end: a completely fresh
  `docker compose down -v && make up && bash scripts/stalwart-init.sh` reaches a
  working demo account in ~4 seconds.

### 5. Creating the demo account: `x:Account/set`, two non-obvious encoding rules

```
POST /jmap
{"using": [...], "methodCalls": [["x:Account/set", {"accountId": "<admin accountId>",
  "create": {"demo": {
    "@type": "User",
    "name": "demo",
    "domainId": "<id from x:Domain/query>",
    "credentials": {"0": {"@type": "Password", "secret": "<password>"}}
  }}}, "c0"]]}
```

Found by trial and error against the live server's validation errors:
- `x:Account` is a tagged union (`User` vs `Group`); the create payload needs an
  `"@type"` discriminator matching the schema's variant name. Same pattern for
  nested union types like `x:Credential` (`"@type": "Password"`).
- `credentials` is an `objectList`-typed property. A plain JSON array failed
  (`invalidPatch` / "Invalid value for object property"); an arbitrarily-keyed map
  also failed (`invalidPatch` / "Invalid key for object property"). An
  **index-string-keyed map** (`{"0": {...}}`) succeeded. Likely generalizes to any
  `objectList` property in this schema system, not just `credentials`.
- Idempotency check: `x:Account/query` with no filter returns *all* accounts;
  `x:Account/get` on those ids includes a server-computed `emailAddress`
  (`name@domain`) to match against.

### 6. Reading DKIM keys: `x:DkimSignature`, not `/api/dkim/{domain}`

```
POST /jmap
{"using": [...], "methodCalls": [
  ["x:DkimSignature/query", {"accountId": "<admin accountId>"}, "c0"],
  ["x:DkimSignature/get", {"accountId": "<admin accountId>",
    "#ids": {"resultOf": "c0", "name": "x:DkimSignature/query", "path": "/ids"},
    "properties": ["selector", "domainId", "publicKey", "@type"]}, "c1"]
]}
```

Returns one entry per algorithm (v0.16.20 defaults to both RSA and Ed25519), each
with `selector`, `@type` (`Dkim1RsaSha256` / `Dkim1Ed25519Sha256`), and `publicKey`
(base64). `privateKey.secret` is correctly redacted as `"****"` in read responses.

A second, equally valid way to get DKIM data: `x:Domain/get` includes a `dnsZoneFile`
field — a complete, ready-to-publish DNS zone (SPF, DKIM TXT records for both
selectors, DMARC, MX, MTA-STS, autoconfig/autodiscover, TLS-RPT) as one text blob.
**Open question for Task 10** (`get_dkim_record(domain) -> str`, "returns the TXT
value"): decide whether to extract the DKIM line(s) out of `dnsZoneFile` (already
formatted as `v=DKIM1; k=...; p=...`) or reconstruct the TXT value from
`x:DkimSignature/get`'s structured `@type` + `publicKey` fields. The former is less
code but is a full zone file to parse out of; the latter needs an
algorithm-to-`k=` mapping (`Dkim1RsaSha256` → `k=rsa`, `Dkim1Ed25519Sha256` →
`k=ed25519`) that isn't spelled out anywhere in the schema. Neither was chosen here
since Task 2 only needed to *read* the key, not format a single TXT value.

### Other corrections made in `docker-compose.yml` while getting this far

- **Volumes**: the image declares `/etc/stalwart` (config) and `/var/lib/stalwart`
  (RocksDB data, log path `/var/log/stalwart/` is unmounted/ephemeral), confirmed via
  `docker image inspect` — not `/opt/stalwart` as the brief's candidate compose had.
  Compose now mounts both `/etc/stalwart` and `/var/lib/stalwart` as named volumes.
- **Healthcheck**: the image has **no `wget`** (Debian trixie base, `curl` only,
  confirmed by shelling into the image) — the brief's `wget -qO- ...` healthcheck
  test would always fail with "exec: wget: not found". Changed to
  `curl -fsS http://localhost:8080/healthz/live`. The image's own baked-in
  `HEALTHCHECK` (which compose's `healthcheck:` overrides) is
  `curl -fsSk https://127.0.0.1:443/healthz/live || curl -fsS http://127.0.0.1:8080/healthz/live`
  — confirms the plain-HTTP :8080 path is a supported fallback, consistent with the fix.
- **Image tag**: `v0.16.20` (newest `v0.16.x` at time of writing) pulled without
  issue; no fallback tag was needed.
- **Ports**: 8080/2525/1587/1143 were all free on the host; no port changes were
  needed.

### Cross-checks that matter for Task 3 (JmapClient)

- `GET /.well-known/jmap` returns **HTTP 307** to `/jmap/session` (RFC 8620
  well-known indirection). The brief's literal Step 4 curl (`curl -fsS ...` with no
  `-L`) gets an **empty 307 body**, not the session JSON — needed `-L` here to get
  real output. For Task 3's `JmapClient.connect`, this means the `httpx.AsyncClient`
  **must set `follow_redirects=True`** (httpx's default is `False`); otherwise
  `GET {base_url}/.well-known/jmap` silently returns an empty redirect response
  instead of the session, and `Session.from_jmap(...)` will blow up on empty/missing
  JSON rather than a clear connection error.
- Once authenticated as `demo@mailosh.test`, the session's `apiUrl`/`uploadUrl`/
  `eventSourceUrl` are `https://mail.mailosh.test/jmap/...` — the **configured**
  `serverHostname` over HTTPS, not the request's actual `http://localhost:8080`.
  `mail.mailosh.test` doesn't resolve from the host and the container's TLS cert
  is self-signed, so these URLs are **not directly reachable** without `Session.rebase()`
  swapping scheme+host back to the configured base URL. This is exactly the behavior
  Task 3's brief already anticipated and required `Session.rebase(base_url)` for —
  this task independently reproduces and confirms it against the real container
  rather than it being speculative.

### Verification (fresh reproduction)

```
$ docker compose down -v && make up && bash scripts/stalwart-init.sh
...
waiting for http://localhost:8080/healthz/live ...
server is in bootstrap mode; completing initial setup (domain=mailosh.test, hostname=mail.mailosh.test)
{"methodResponses":[["x:Bootstrap/set",{"accountId":"d333333","updated":{"singleton":{"username":"admin@mailosh.test","secret":"..."}}},"c0"]],"sessionState":"..."}
restarting stalwart so the persisted configuration takes effect
waiting for http://localhost:8080/healthz/live ...
domain mailosh.test -> id b
creating demo@mailosh.test
{"methodResponses":[["x:Account/set",{"accountId":"d333333","created":{"demo":{"id":"c"}}},"c0"]],"sessionState":"..."}
  DKIM selector=v1-rsa-20260831 type=Dkim1RsaSha256 publicKey=MIIBIjANBgkqhkiG9w0BAQEF...
  DKIM selector=v1-ed25519-20260831 type=Dkim1Ed25519Sha256 publicKey=...
stalwart-init: done (demo@mailosh.test ready on domain mailosh.test)

$ curl -fsS -L -u "demo@mailosh.test:$MAILOSH_DEMO_PASSWORD" http://localhost:8080/.well-known/jmap | python3 -m json.tool | head -5
# primaryAccounts["urn:ietf:params:jmap:mail"] == account id "c"; saved to tests/fixtures/session.json

$ python3 -c 'import smtplib; smtplib.SMTP("localhost", 2525).sendmail("test@example.org", "demo@mailosh.test", "Subject: hello\r\n\r\nworld")'
# then Email/query for account "c" -> one id, Email/get -> subject "hello"
```

Re-running `bash scripts/stalwart-init.sh` against the already-bootstrapped stack
prints "server already bootstrapped; skipping initial setup" and
"demo@mailosh.test already exists ...; skipping create" — idempotent.

### Task 10 close-out

All four open questions below are now resolved, verified live against the same
`stalwartlabs/stalwart:v0.16.20` stack. `mailosh/stalwart_admin.py`
(`StalwartAdmin`) and `mailosh setup --domain X --email Y` implement the
resolved answers; `tests/unit/test_stalwart_admin.py` covers them with respx
fixtures shaped from the verbatim captures below.

- **`StalwartAdmin.create_domain(name)` — RESOLVED, first real exercise.**
  The hypothesis held exactly: a bare `{"name": "<domain>"}` create payload,
  no `"@type"` discriminator, succeeds and auto-generates DKIM keys
  immediately (same `dkimManagement: Automatic` default `x:Bootstrap/set`'s
  `defaultDomain` relies on). Verbatim first-ever call and response:

  ```
  POST /jmap  (Basic admin:$MAILOSH_STALWART_ADMIN_SECRET)
  {"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"],
   "methodCalls": [["x:Domain/set", {"accountId": "d333333",
     "create": {"d0": {"name": "spike2.test"}}}, "c0"]]}

  -> {"methodResponses": [["x:Domain/set", {"accountId": "d333333",
       "created": {"d0": {"id": "c"}}}, "c0"]], "sessionState": "f20d174b"}
  ```

  A duplicate-name retry (same call again) comes back as a normal 200, not an
  `error`-named response or a 4xx:

  ```
  -> {"methodResponses": [["x:Domain/set", {"accountId": "d333333",
       "notCreated": {"d0": {"type": "primaryKeyViolation", "properties": ["name"],
         "objectId": {"object": "Domain", "id": "c"}}}}, "c0"]], "sessionState": "f20d174b"}
  ```

  `create_domain` tolerates exactly that shape as a no-op (idempotent by
  *catching the duplicate*, not by querying first — see the module's own
  docstring for why). `create_account`'s duplicate-email retry mirrors this
  exactly, just `"properties": ["email"]` instead of `["name"]`.

- **`StalwartAdmin.get_dkim_record(domain)` — RESOLVED: reconstruct from
  `x:DkimSignature` fields, not `dnsZoneFile`.** The zone file interleaves
  DKIM TXT lines with SPF/MX/DMARC/SRV/CNAME/MTA-STS records for the whole
  domain and DNS-wraps a long RSA key's `p=` value across multiple quoted
  strings that would need rejoining — the structured `x:DkimSignature/get`
  fields (`selector`, `@type`, `publicKey`) give the same information already
  parsed, confirmed to match the zone file's own DKIM lines byte-for-byte
  live. `@type` -> `k=` mapping: `Dkim1RsaSha256` -> `rsa`,
  `Dkim1Ed25519Sha256` -> `ed25519`; `h=sha256` is reproduced from every live
  zone-file DKIM line observed (both algorithms hash SHA-256; there's no
  separate "hash algorithm" schema field to read it from instead). Returns a
  `(host, value)` pair as a small frozen dataclass (`DkimRecord`), not a bare
  `str` as the brief's stub signature said — the controller-resolved
  requirement for this task explicitly asked for both halves.

  **New finding, not anticipated by this open question:** a domain's DKIM keys
  are generated **asynchronously** after `x:Domain/set create` returns, not
  synchronously as part of it. Confirmed by creating a domain and querying
  `x:DkimSignature` in the *same batched request*: zero keys existed yet for
  the brand-new domain, even though both keys existed for it a few seconds
  later. A query moments later can also catch a *partial* state — the
  faster-to-generate Ed25519 key already present, the slower RSA key not yet
  — which is what live-tested `get_dkim_record`, called immediately after
  `create_domain` in the CLI's own call order, actually hit on its first
  real run: it silently returned the Ed25519 record instead of the intended
  RSA one. Fixed with a small bounded poll (`_DKIM_POLL_ATTEMPTS = 10`,
  `_DKIM_POLL_DELAY_SECONDS = 0.3`, ~3s worst case) for the preferred
  algorithm specifically, not just "any algorithm" — costs nothing on the
  common path (an already-provisioned domain's RSA key is found on the very
  first attempt). Re-verified reliable across three more fresh domains after
  the fix (`spike5.test`/`spike6.test`/`spike7.test`, CLI-driven, all
  returned RSA on the first `mailosh setup` invocation).

- **`try_mint_user_token(email) -> str | None` — RESOLVED, full write-up
  moved to its own section: see SPK-3 above.** Summary: the `AppPassword`/
  `ApiKey` breadcrumb this question flagged was the right lead, but the
  mechanism isn't "add to `x:Account.credentials` via update" (explicitly
  rejected server-side, "Secondary credentials cannot be set directly") —
  it's `x:ApiKey/set create` as an independently-settable top-level object,
  scoped to the *target* user's own account id.

- **Admin `accountId` resolution — RESOLVED, confirmed still dynamic.** Still
  `"d333333"` in this task's environment, and still resolved live via
  `GET /jmap/session` (`next(iter(session["accounts"]))`) exactly as
  recommended — `StalwartAdmin._admin_account_id` does this once per client
  instance and caches it (verified via a respx call-count assertion in
  `test_admin_account_id_is_resolved_once_and_cached`), never hardcodes it.

### Consequence for the spec

**§11** (setup wizard: "Creates Stalwart domain + DKIM keys via management API") — the
concrete mechanism is now known and implemented (`StalwartAdmin.create_domain`/
`create_account`/`get_dkim_record`/`try_mint_user_token`, `mailosh setup` CLI), replacing
the brief's guessed REST routes with the real JMAP-shaped `x:*` surface; no change to §11's
own wording needed, since it never named the specific routes — it described the *outcome*
(domain + DKIM keys created), which holds. **§3** ("Stalwart's HTTP management API for
domains/users/DKIM/queue") — "REST" was an implicit assumption in that phrasing; the real
surface is JMAP method calls over the same `/jmap` endpoint mail traffic uses, not a
separate REST API. Worth a small terminology fix next time §3 is touched (say "management
JMAP methods" rather than "HTTP management API"), but doesn't change the architecture
diagram or the seam itself — `admin/` still talks only to Stalwart, exactly as drawn.

## SPK-6 budgets

Task: 8 (pipeline implementation + preliminary live observations) and 11 (formal
measurement + budget comparison). Verified against the live `docker compose` stack
(stalwart + postgres + mailosh, all already running throughout this task — nothing
stopped or recreated) via `scripts/measure.py`, run twice back to back.

### Verdict: GO, directional — all three measured latencies sit comfortably inside spec §6's budgets at this store's tiny scale; the 100k-message inbox-render budget is honestly unmeasured (see caveat below), and the SMTP→SSE budget shows one well-understood cold-start miss out of 20 total runs across both script invocations, not a design problem.

### 1. Task 8's preliminary observations (folded in, as the controller flagged)

Task 8's own live verification (`task-8-report.md`, "Live verification" section) already
captured three SMTP→SSE deltas with a `curl -N` capture immediately after building the
feature: **send 1 (first connection right after a container rebuild): ~4s** ("first
connection after a container restart moments earlier; listener's upstream connection to
Stalwart was still warming up"); **sends 2 and 3 (same warm connection): ~0–1s** each. A
later same-task capture (idle past the 30s ping boundary, then a JMAP mailbox move) showed
sub-1s delivery even after 30+ seconds of connection idle, confirming the fix for the
read-timeout race described there. Task 8 also flagged, as a named concern for this task:
*"Backoff reset timing... resets only on an actual StateChange, not on bare successful
(re)connection — a long-idle-then-dropped healthy connection won't have reset its backoff
... flagging in case Task 11's latency measurement cares."*

**This task's own measurement reproduced exactly that pattern, unprompted.** The stack had
been idle (no SMTP/JMAP-mutation traffic) for roughly 90 minutes before this task's first
`scripts/measure.py` run — no container restart, but the same cold-connection effect Task 8
described:

| Run | Context | SMTP→SSE p50 | p95 | min | max | Note |
|---|---|---|---|---|---|---|
| **A** | First run this task, ~90 min idle beforehand | 234.1 ms | 2644.9 ms | 197.3 ms | **4295.8 ms** | Run 1/10 (the very first send) hit 4295.8 ms — over budget; runs 2–10 were 197–627 ms |
| **B** | Second run, immediately after A (listener now warm) | 232.0 ms | 655.7 ms | 194.1 ms | 726.5 ms | All 10/10 runs within the 2 s budget |

Run A's single outlier and Run B's clean sweep together are the same cold-vs-warm story
Task 8 predicted from first principles (backoff resets only on a real `StateChange`, so a
connection that quietly dropped during a long idle window pays a full reconnect on the
*next* event, not before) — now empirically confirmed with fresh evidence, not just
inferred. **Consequence:** this is a real, understood, low-impact-at-this-scale P0
simplification (exactly as Task 8 called it), not a new bug; a P1 fix (reset backoff on
successful reconnection too, not only on a StateChange) is cheap and named here for
whoever picks up real-time hardening post-P0.

### 2. Dataset size — honesty check before the budget comparison

Spec §6's inbox-render budget is stated **at 100k messages**. This store holds only
spike/test messages:

```
Inbox total: 10 messages (stable — measure.py's SMTP test mail lands in Junk, not Inbox)
Account-wide total (all mailboxes): 21, then 31, then 41 across three runs
```

The account-wide total climbing 21 -> 31 -> 41 across the first three runs was itself a
bug, not just a curiosity: the first two runs (below) predate a fix described in §6 —
each left its own 10 sent test messages live, uncleaned, hence the steady +10 per run.
Fixed and re-verified live (§6): a further run returned this to *exactly* 41 -> 41, not
51 — see there for the destroy-count evidence and an independent before/after check.

(via `Email/query` with `calculateTotal: true, limit: 0` — printed by `scripts/measure.py`
itself every run, so this number is never stale/hand-copied). **Three orders of magnitude
below the spec's stated budget scale.** The comparison below is a directional sanity check
("is anything pathologically slow at all") — it is explicitly **not** a conclusive
verification that Stalwart's `Email/query`+`Email/get` chain holds to 400 ms at 100k
messages. That would need a real bulk-import (or synthetic-data generator) run, which is
out of this task's scope (measurement tooling, not a load-test harness) — flagged as an
open item below.

### 3. Full measurement output (Run B, reproduced verbatim)

Both runs' full transcripts (progress log + report) are in task-11-report.md; this is the
cleaner of the two (fully warm — see §1 above for why Run A's first line differs):

```
# Mailosh P0 measurements -- 2026-09-01T09:33:02.473098+00:00

## Dataset size (context for the spec §6 budget comparison)

- Inbox (`a`) total: **10** messages
- Account-wide total (all mailboxes): **31** messages
- Spec §6's 400 ms inbox-render budget is stated **at 100k messages**; this store holds only spike/test messages, three orders of magnitude below that. The comparison below is directional (small-N sanity check that nothing is pathologically slow), **not** a conclusive verification of the 100k-message budget.

## Results

| Measurement | n | p50 (ms) | p95 (ms) | min (ms) | max (ms) | mean (ms) |
|---|---|---|---|---|---|---|
| inbox query (`query_inbox`, limit=50) | 20 | 1.2 | 3.4 | 1.0 | 6.6 | 1.7 |
| thread fetch (`get_thread`, thread_id=`bm`) | 20 | 1.1 | 2.2 | 0.9 | 3.9 | 1.3 |
| SMTP -> SSE (`/events` `new-mail` frame) | 10 | 232.0 | 655.7 | 194.1 | 726.5 | 326.7 |

### Raw runs (ms)

**inbox query:** [6.6, 1.7, 1.9, 1.4, 1.2, 1.1, 1.2, 1.2, 1.3, 1.1, 1.1, 1.0, 1.1, 1.1, 1.1, 3.2, 2.5, 1.0, 1.0, 2.2]

**thread fetch:** [3.9, 2.1, 1.6, 1.2, 1.4, 1.4, 1.6, 1.1, 1.1, 1.0, 1.5, 1.0, 1.0, 1.0, 0.9, 0.9, 1.0, 1.0, 1.1, 1.1]

**SMTP -> SSE:** [726.5, 220.0, 224.9, 209.7, 228.3, 235.7, 377.0, 194.1, 282.1, 569.1]
```

`get_thread` benchmarked against thread id `bm` (a genuine 2-message Sent+Inbox pair, from
Task 9's own SPK-1 live compose verification — `scripts/measure.py`'s thread-discovery
fallback; see `_resolve_thread_id`'s docstring for why the originally-intended 3-message
"Spike thread kickoff" fixture is no longer live — SPK-2's "Consequence for the spec" above
has the full story).

### 4. Budget comparison (spec §6)

| Budget (spec §6) | Target | Measured (this store's scale) | Verdict |
|---|---|---|---|
| Inbox render: one JMAP round trip, < 400 ms server time **at 100k messages** | < 400 ms | p50 1.2–1.4 ms, p95 3.4–3.8 ms, max 10.8 ms (range across both runs; each run n=20) | **Directional pass** — 40–100× headroom below budget at spike scale; 100k-scale genuinely unmeasured (see §2 above) |
| Open a thread: < 300 ms server time, one batched request | < 300 ms | p50 1.1–1.3 ms, p95 2.2–2.3 ms, max 4.9 ms (range across both runs; each run n=20) | **Directional pass** — same caveat; this store's threads are tiny (2 messages), a 100k-message *thread* (unlikely but possible) is also unmeasured |
| New mail visible in an open inbox: < 2 s from SMTP acceptance (expected typical < 500 ms) | < 2 s | Run A: 9/10 within budget (197–627 ms, one 4295.8 ms cold-start miss); Run B: 10/10 within budget (194–727 ms); warm mean ≈ 289–327 ms | **Pass, with one documented, understood miss** — see §1 above; typical/warm behavior matches the spec's own "expected typical < 500 ms" language closely |

No budget was silently waived: the one genuine miss (Run A's cold-start send) is reported,
not hidden, with a specific hypothesis (idle-then-reconnect, matching a concern Task 8
already named) rather than a vague "sometimes it's slow."

### 5. Open items for later tasks

- **100k-message-scale measurement is a real gap**, not merely a caveat — a P1/P2 task
  should generate synthetic bulk data (or run against a real large mailbox) and re-run
  `scripts/measure.py`'s inbox-query/thread-fetch measurements before treating §6's 400 ms/
  300 ms budgets as verified rather than directional.
- **Backoff-reset-on-idle** (§1 above): cheap P1 fix named, not implemented here — out of
  this task's scope (measurement + findings, not a code change to `mailosh/sse.py`).
- `scripts/measure.py` sends 10 new SMTP test messages per run (subject prefix
  `Mailosh measure-smtp-sse`) straight to Stalwart's port 2525, same as
  `scripts/send-test.py` — per the KNOWN QUIRK documented in both scripts' docstrings and
  here, these land in **Junk Mail**, not Inbox (Stalwart's own anonymous-sender heuristic,
  first found in Task 8, orthogonal to whether the push mechanism fires). **All 10 are
  destroyed by the script itself before it exits**, by default (`Email/set destroy`,
  resolved back by each message's own unique subject — see §6 below for the fix and its
  live proof). Pass `--keep` to skip that and leave them live for manual inspection
  instead — in which case they'd need to be found in Junk Mail (or removed by hand, the
  same `Email/set destroy` the script itself uses) rather than Inbox.

### 6. Cleanup fix, verified live (added during review)

A first version of `scripts/measure.py` sent its 10 SMTP test messages per run and never
removed any of them — caught in review, not by this task's own original self-review. The
account-wide totals already pasted in §2 above and §3's two transcripts show the bug
happening in real time: 21 (pristine baseline) -> 31 (after Run A's 10 uncleaned sends)
-> 41 (after Run B's 10 more) — a steady +10 per run that would have kept climbing
forever, exactly the class of stale-live-data problem this same task's own SPK-2 fix
(above) had to clean up elsewhere, just self-inflicted this time instead of inherited.

**Fix** (full mechanism in the script's own module docstring): after each send, resolve
the message's JMAP id back from its own unique subject (`Email/query`'s `subject` filter
— a safe substring match here, since every subject carries a uuid4 suffix), collect every
id found, and destroy all of them via `Email/set destroy` in a `finally` block (so an
interrupted or failed run still cleans up whatever it already created) — mirroring
`tests/integration/test_live_stalwart.py`'s own `_destroy` helper exactly (`destroy`
deliberately stays out of the public `JmapClient` contract, same reasoning that test file
gives). Any subject that can't be resolved is logged by name, not silently dropped. New
`--keep` flag (default off) skips cleanup for deliberate debugging only.

**Re-verified live** — ran `scripts/measure.py` once more (default: cleanup on), with an
independent before/after check *outside* the script itself (a separate, throwaway
`Email/query calculateTotal` call) so the proof doesn't rely solely on the script's own
self-report:

```
$ (independent check) Email/query calculateTotal -> account-wide total = 41

$ .venv/bin/python scripts/measure.py
...
running SMTP->SSE x10 (spacing=1.5s, drain-then-send, frame timeout=15.0s, cleanup=on) ...
  smtp->sse run 1/10: 1935.3 ms (subject='Mailosh measure-smtp-sse 0 8c76d7e7')
  [... runs 2-10, 197-552 ms, full list in task-11-report.md's fix-report addendum ...]
  cleanup: destroyed 10/10 sent test message(s)
post-cleanup dataset size: inbox=10 (was 10), account=41 (was 41)

$ (independent check, again) Email/query calculateTotal -> account-wide total = 41
```

Account-wide total returned to **exactly** its pre-run value (41 -> 41) — confirmed two
independent ways: the script's own before/after self-report (now printed on every run,
not just this one) and a separate read-only query run outside the script, both before and
after. All 10 sent messages resolved and destroyed; zero unresolved subjects. This run's
own latency numbers (inbox query p50 3.0 ms/p95 13.6 ms; thread fetch p50 1.9 ms/p95
4.9 ms; SMTP→SSE p50 238.3 ms/p95 1312.7 ms, one elevated 1935.3 ms run from the same
cold-listener-after-a-gap effect §1 describes — the stack had gone quiet again during the
time spent fixing and re-testing the script) tell the same qualitative story as Run A/Run
B in §3 (comfortably inside every §6 budget; one elevated SMTP→SSE outlier, explained) —
not a materially different picture, so the Run B table in §3 stays as the pasted
evidence rather than being replaced; this re-run's purpose was proving the cleanup fix,
not re-measuring. Full raw output is in task-11-report.md's fix-report addendum.

### Consequence for the spec

**§6** — the real-time design holds at spike scale; no change to the stated budgets
themselves. One addition worth making next time §6 is touched: its "exponential backoff
reconnect to Stalwart" line could note explicitly that backoff resets only on a genuine
`StateChange` (not bare reconnection) as a documented P0 simplification, matching what
`mailosh/sse.py`'s own `stalwart_listener` docstring already says — so a future reader of
the *spec* (not just the code) sees the same caveat. **§12** (deployment sizing) — nothing
contradicts the reference-box assumption; the 100k-message gap above is a testing-coverage
item, not a sizing concern.

## Known gaps carried into Phase 1

Added during the phase0 final whole-branch review (verdict: ready to merge with fixes, no
Critical issues). Everything above documents what P0 *proved*; this section is the honest
counterpart — what a user actually sees on failure, and what P0 deliberately doesn't cover
yet — so Phase 1 starts from an accurate picture rather than rediscovering these live.

1. **No error surface.** A `JmapError`/`TransportError` raised mid-request falls through to
   FastAPI's default 500 `text/plain` response; three of the four failing routes are HTMX
   partials, and htmx does not swap non-2xx responses into the DOM by default, so with
   Stalwart down the UI silently does nothing visible. No route-level test covers a failing
   client. Phase 1: an `@app.exception_handler` returning a swappable fragment instead
   (spec §4/§6).

2. **No authentication.** P0 serves the single demo mailbox configured in `.env` to anyone
   who can reach port 8000 — there is no login, no session, no per-user access control of
   any kind. Sessions, passkeys, and TOTP are spec §9 Phase-1 work; SPK-3 above only proved
   that minting a per-user *token* is possible, not that anything in this repo checks one.

3. **No polling fallback.** Spec §6's "graceful degradation to 30 s polling if the
   EventSource cannot connect" is not implemented — `inbox.html`'s `sse-connect="/events"`
   has no fallback path at all, so a browser/proxy that can't hold an SSE connection open
   simply never receives live updates, with no visible indication this happened.

4. **Integration test is not hermetic.** `tests/integration/test_live_stalwart.py` imports
   a fixture with fixed Message-IDs (`t1`–`t3@example.org`) and asserts on a global
   `"spike thread"` subject substring; any other copy of `sample.mbox` already living in the
   account — e.g. left by the documented `mailosh import-mbox` demo, which is not
   self-cleaning — folds into the same References-based thread and breaks the test's
   `len(msgs) == 3` assertion. This already broke `make itest` once (see SPK-2's "Caveat
   found and fixed during Task 11" above). Phase 1: rewrite each message's
   Message-ID/References with a per-run uuid before upload, instead of relying on the
   account being otherwise empty of this fixture.

5. **In-process SSE hub.** `mailosh/sse.py`'s `SseHub` is a single global, in-process
   pub/sub broker with no per-user routing — correct only because P0 serves one demo
   mailbox to everyone. Spec §12 sizes the reference deployment for two uvicorn workers,
   which would mean two independent upstream `stalwart_listener` connections and, once
   authentication exists, one user's mail events fanned out to every other connected user.
   Phase 1 must make the hub per-user and settle the multi-process story before this can
   serve more than one account.

6. **SPK-1 is conditionally closed.** `GET`/`POST /compose` is a full page load with a
   native form POST (SPK-1 §4 above), so the harder Alpine/Squire-lifecycle-versus-HTMX-swap
   question was sidestepped by design, not answered — SPK-1's own verdict already says this
   explicitly. A future swapped/overlay compose panel (e.g. a Gmail-style popup, P2 per spec
   §14/§15) needs its own lifecycle check before relying on this P0 finding.
