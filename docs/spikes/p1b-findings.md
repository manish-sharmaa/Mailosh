# Phase 1B findings

Findings recorded per topic, mirroring `docs/spikes/p1a-findings.md` and
`docs/spikes/p0-findings.md`'s "filled in by the task that closes it out"
convention. See `docs/plans/2026-09-03-phase1b-reading.md` for the task list.

The plan expected Task 4 to create this file and every later task to add its own
section as it ran. **That did not happen** — the file did not exist when Task 14
started. Everything below was therefore written by **Task 14**, the closing
task, from what Task 14 itself ran: a real-world mail corpus
(`tests/fixtures/mail/corpus/`, six client shapes), a live integration flow
against the running `docker compose` stack
(`tests/integration/test_live_reading_flow.py`), and Chrome browser QA against
`http://localhost:8000` on 2026-09-05.

**Where Task 14 could not verify something, it says so.** That applies to whole
headings — `Conversation view`, `Attachments` and `Print` are marked
**not independently verified** rather than filled in from the intervening tasks'
own reports. (`Budgets` was in that list and no longer is: it was re-measured
on 2026-09-05 and now carries only numbers produced on that date.) A finding inherited from a task report is a claim about a report,
not about the system, and this branch has already been bitten twice by exactly
that (`p1a-findings.md` executive summary, items 5 and 6).

---

## Executive summary — what Phase 1C needs to know

Phase 1B's sanitisers hold. The corpus, the live flow and the browser all agree:
a hostile message is inert and a real message is readable. **1772 unit tests and
5 live integration tests pass.**

But the phase's exit criterion is *open real-world HTML mail safely and
legibly*, and against real mail the reading experience has **one serious defect
and two legibility gaps**. All three were found by this task; none is fixed
here, because none of the files that would fix them belong to it.

1. **Inline `cid:` images and proxied remote images do not load in a real
   browser.** This is the big one, and it is invisible to every test in the
   suite. The frame document has an **opaque origin** (the CSP `sandbox`
   directive carries no `allow-same-origin`, by design), so a subresource
   request it makes is not same-site for cookie purposes; the `SameSite=Lax`
   session cookie is not attached; `/m/{id}/cid/{cid}` and `/img?u=…` both
   answer **`303 → /login`**, the browser follows the redirect, receives
   `text/html`, and the image fails. The reader sees a broken-image icon.
   Verified in Chrome with a same-page A/B (identical URL, identical session:
   from the frame `303`, from the top document `200`) and a positive control
   (`fetch(…, {credentials:'omit'})` from the top document reproduces the exact
   `303`). `httpx` sends cookies regardless of `SameSite`, which is why
   `make itest` passes on the same route in the same second. **See "Browser QA
   → Defect 1" for the evidence and four candidate fixes.** This is the spec §15
   gate item the brief flagged as having a documented fallback: it **failed**,
   though not for the CSP reason anyone expected — `img-src 'self'` is fine, the
   request is made and reaches the server.

2. **The two-level border longhands really do hurt real mail** — the suspect
   recorded during the phase is confirmed, with numbers. `border-bottom-color`,
   `border-top-width` and their ten siblings are not in `ALLOWED_PROPERTIES`,
   while `border`, `border-bottom`, `border-color`, `border-style` and
   `border-width` all are. Four of the six corpus messages write rules that way:
   Word's table underlines, Outlook-on-the-web's signature-image rule, the table
   newsletter's 3px masthead rule and 1px story dividers, and the modern
   newsletter's dark-mode header rule. **Twelve declarations lost across the
   corpus, all of them visible layout.** Allowing them grants **no new
   capability**: every one is a component of a shorthand already on the list, and
   `_unsafe_values` rejects `url()` and every non-allow-listed function *before*
   the property name is consulted. See "Sanitiser".

3. **A modern Outlook-for-Windows reply is never quote-trimmed.** Current Word
   builds open the quoted history with an unmarked
   `<div style="border:none;border-top:solid #E1E1E1 1.0pt;…">` and a
   `From:/Sent:/To:/Subject:` block — no class, no id, and no
   `-----Original Message-----`. `QUOTE_MATCHERS` covers neither, and the HTML
   heuristics only look for `On … wrote:` or that divider. The whole history
   renders inline, confirmed on screen in Chrome. The *plain-text* path already
   has the missing heuristic (`quote_trim._header_block_start` reads exactly this
   header block); the HTML path does not. See "Quote trimming".

4. **`MAX_CSS_BYTES` really is per `<style>` block, and the aggregate really can
   exceed it — but the body cap bounds the damage.** Both body-fetch paths
   (`JmapClient.get_thread` and `frames._load_message`) pass
   `maxBodyValueBytes = 512 KB`, which is exactly `MAX_CSS_BYTES`. So the worst
   case is ~512 KB of CSS spread over many blocks, each individually under the
   cap: measured at **11 blocks × 48 KB = 528 KB in, 555 KB of CSS out, 256 ms
   of synchronous CPU** in an async route. Not an outage (the nh3 nesting bug was
   18 s), but 256 ms of blocked event loop and half a megabyte of CSS inlined
   into one frame document. A running total across blocks is a cheap fix. See
   "Sanitiser".

5. **Outlook's and Word's comment-wrapped stylesheets survive intact** — worth
   knowing because the failure would have been total and silent. Both wrap
   `<style>` contents in `<!-- … -->`; CDO/CDC tokens are legal at the top level
   of a stylesheet, tinycss2 consumes them, and the `<`-check runs on the
   *output*, not the input. Word's 23-declaration sheet comes through with 16
   declarations (the 7 losses are all `mso-*` and `@page`). If this had gone the
   other way, every Word and Outlook message ever sent would have rendered
   unstyled and no existing test would have noticed.

6. **`frame.js`'s `LAST = 0` sentinel can strand a frame at its default height.**
   `post()` returns early when the measured height equals `LAST`, and `LAST`
   starts at `0` — so a frame that measures `0` on its first pass records nothing
   and has no trigger left (`load`, the three `setTimeout`s and the initial
   `ResizeObserver` callback have all fired). Observed reproducibly in Chrome for
   one message: no height ever assigned across 16 s and a reload. **Could not be
   separated from a harness artifact** — this tab reports
   `visibilityState: "hidden"`, which throttles exactly those timers — so it is
   recorded as a latent bug with a visible mechanism, not a confirmed one.

7. **Firefox and Safari were not tested.** They cannot be driven from this
   harness. Spec §15's browser gate is therefore **one third complete**. The
   `img-src 'self'` question in particular has a *different* answer per engine
   in principle, and defect 1 above may well present differently in Safari, whose
   cookie policy is stricter still.

### The plan's own "what 1C should not have to rediscover" list, answered

The plan named five things this summary must state. Answered here, including
where the answer is "not known":

| the plan's ask | answer |
|---|---|
| The five nh3 0.3.7 behaviours from Task 3 | **Unchanged and still load-bearing**, and the corpus exercises four of them against real mail rather than synthetic payloads. They are written out in `mailosh/render/html_sanitize.py`'s module docstring and that remains the authority; this task did not re-derive them from the extension. Summarised: (1) `attribute_filter` sits *between* two URL checks — a relative rewrite is deleted, so every rewrite returns an absolute URL, and a returned `javascript:` would be emitted verbatim, so the filter never returns an attacker string; (2) the filter sees raw, un-normalised values (`'  CID:ABC  '`); (3) it is also called for nh3's *own* injected `target`/`rel`, so the default branch must return `value` unchanged; (4) `url_schemes` is global, not per tag, and `cite` is not scheme-checked by nh3 at all; (5) three configurations are hard errors, one of them a Rust `PanicException` deriving from `BaseException`. |
| The CSP that actually shipped, and why it differs from spec §7 | See "Frame delivery" — the header verbatim, the hash pin confirmed live, and the two departures (`allow-scripts` added, `img-src` narrowed to a constant `'self' data:`) with the reasoning. Spec §7 as written pins a script it also forbids. |
| Whether `img-src 'self'` held inside a sandboxed opaque-origin document in all three engines | **Chrome: yes, `img-src` is not the problem** — the request is made and reaches the server. But the image still fails, on cookies rather than CSP (defect 1). **Firefox and Safari: unknown, not tested.** |
| The per-browser print result | **Unknown, in all three engines.** `GET /t/{id}/print` was not opened by this task. See "Print". |
| Any Stalwart quirk around `htmlBody`, `attachments` or blob download | **One confirmed, one absent.** Confirmed: a `multipart/alternative` → `multipart/related` message imported through JMAP comes back with the inline image in `attachments` (not in `htmlBody`), its `cid` **retaining the RFC 2392 angle brackets** — `<logo.9f2c1b@mailosh.test>` — which is exactly what `frames._bare_cid` strips, and which would silently 404 every inline image if any caller ever compared the raw value. The declared `type` survives as `image/png` and the blob round-trips byte for byte. Absent: no truncation, re-encoding or reordering quirk was observed at these sizes. 1A's own recorded quirk (unauthenticated SMTP to port 2525 lands in **Junk**, not Inbox) still stands and is why this task imports through JMAP rather than sending. |

---

## Frame delivery

Verified live, in `tests/integration/test_live_reading_flow.py` and in Chrome.

**The CSP that actually ships**, byte for byte, from `frame_document.csp_header()`
and asserted byte-identical on the live response for every message the
integration flow opens:

```
sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox;
frame-ancestors 'self'; default-src 'none'; img-src 'self' data:;
style-src 'unsafe-inline'; script-src 'sha256-BhxL4wngaNen0LuJckDEyMcOXwOwl+UYMti50hYlCro='
```

224 bytes, and the browser confirms the same 224. Two things about it 1C should
have written down:

- **The hash pin is real and it holds in Chrome.** `script-src` carries
  `'sha256-…'` and **no** `'unsafe-inline'`; the hash is computed from
  `FRAME_SCRIPT`'s exact bytes at import time (`FRAME_SCRIPT_HASH`), never
  hardcoded. The script executes, verified by the height handshake — which is a
  positive proof rather than an absent error, because nothing but that script
  posts a height. Change one byte of `FRAME_SCRIPT` and Chrome refuses it
  silently; the observable symptom would be frames stuck at 200 px, not a
  console error the user would ever see. The `_CSP_DIRECTIVES` tuple in
  `frame_document.py` does **not** contain `script-src` — `csp_header()` appends
  it — so reading the tuple alone is misleading, and this task read it that way
  first.
- **`img-src 'self' data:` is deliberately identical for `?remote=0` and
  `?remote=1`.** The remote-image decision is expressed by whether a `src`
  exists at all, never by widening the policy, so a bug in the decision cannot
  become a bug in the containment.

**How it differs from spec §7, and why.** Spec §7 (`docs/specs/
2026-09-02-phase1-webmail-design.md`, "Delivery") writes the header as
`sandbox allow-popups allow-popups-to-escape-sandbox; frame-ancestors 'self';
default-src 'none'; img-src data: [https: http: when remote=1];
style-src 'unsafe-inline'; script-src 'sha256-<resize>'`. Two real departures:

1. **`allow-scripts` is added to the `sandbox` directive.** Without it the
   spec's own hash-pinned resize script cannot run *at all* — a sandboxed
   document with no `allow-scripts` executes nothing, whatever `script-src`
   permits. Spec §7's CSP as written pins a script it also forbids. The shipped
   header resolves that; the iframe element's `sandbox` attribute carries the
   identical list, and neither has `allow-same-origin`.
2. **`img-src` is `'self' data:`, constant, instead of `data:` widened to
   `https: http:` at `remote=1`.** Strictly tighter, and necessary in both
   directions: `'self'` is what lets the `cid:` route and the `/img?u=` proxy
   load at all (spec's `data:`-only would have blocked inline images outright),
   and never naming a remote host is what keeps `remote=1` from being a policy
   change. This is the better design and should be written back into the spec.

`Referrer-Policy: no-referrer` is on the response (`_FRAME_DOC_HEADERS`) *and*
on the iframe element, which is belt and braces rather than redundancy: the
element attribute governs the frame's own subresource requests, and the header
governs navigations out of it. Blob responses (`/cid`, `/att`, `/source`) carry
their own `default-src 'none'; sandbox` on top of the app-wide CSP.

- `X-Content-Type-Options: nosniff` is present on the frame document, on
  `/m/{id}/cid/{cid}` and on `/m/{id}/att/{blob}`. Asserted live.
- The iframe element's `sandbox` attribute and the CSP `sandbox` directive carry
  the **same** list, and a document under both gets the intersection. Confirmed
  experimentally, and it matters: adding `allow-same-origin` to the *element*
  does not grant it, because the header does not. An A/B that differed only in
  the element's sandbox attribute produced identical behaviour.
- `referrerpolicy="no-referrer"` on the element, `loading="lazy"`, and
  `hx-trigger="intersect once"` on the wrapper. In a hidden tab the
  IntersectionObserver never fires, so browser QA had to fire it by hand
  (`htmx.trigger(wrap, 'intersect')`) — a harness note, not a defect.
- **Auto-size works.** The parent assigned real heights from the real handshake:
  880 px (table newsletter), 588 px (Word reply), 503 px then 253 px (Gmail reply,
  before and after the remote images failed to load). Every height message
  arrived with `event.origin === "null"`, which is the opaque origin the frame
  is supposed to have and the first thing `frame.js` checks.
- `?expand=1` (the print path) renders the quote open with **no** toggle and no
  hidden container. Asserted live.
- A frame requested by a signed-out session redirects to `/login` rather than
  rendering. Asserted live at the end of the integration flow.

## Sanitiser

The corpus is `tests/fixtures/mail/corpus/`, six real-client message shapes,
read by `tests/unit/test_mail_corpus.py` (68 tests). Unlike
`tests/fixtures/xss/`, every assertion here fails **closed** — it passes only
when something survived, so a sanitiser that returned `""` for everything would
fail almost all of them while scoring perfectly on the adversarial corpus.

| fixture | bytes | `<style>` decls kept | `style=""` decls kept | `cid:` | blocked remote |
|---|---:|---:|---:|---:|---:|
| `gmail_reply.html` | 1 909 | — (no sheet) | 12 / 12 | 1 | 2 |
| `outlook_web_reply.html` | 1 799 | 2 / 2 | 17 / 21 | 1 | 0 |
| `outlook_desktop_reply.html` | 4 695 | 16 / 23 | 24 / 34 | 1 | 0 |
| `apple_mail_reply.html` | 1 929 | — (no sheet) | 12 / 20 | 1 | 1 |
| `newsletter_table.html` | 7 071 | 56 / 69 | 74 / 84 | 1 | 2 |
| `newsletter_modern.html` | 4 639 | 69 / 75 | 11 / 13 | 1 | 2 |

Read the losses, not the ratios. Sorted by what they cost a reader:

**Layout actually lost — the border longhands.** Twelve declarations across four
messages, every one of them a visible rule:

| message | lost | what the reader loses |
|---|---|---|
| `newsletter_table` (sheet) | `border-bottom-{width,style,color}`, `border-top-{width,style,color}` | the 3px masthead rule and the 1px divider between stories |
| `newsletter_table` (inline) | the same six again | the same rules, written twice as templates do |
| `outlook_desktop` (inline) | `border-bottom-{width,style,color}` ×2 | the second row of the figures table loses its underline while the first (written as the `border-bottom` shorthand) keeps it — visible side by side in the Chrome screenshot |
| `outlook_web` (inline) | `border-bottom-{width,style,color}` | the signature image's bottom rule |
| `newsletter_modern` (sheet) | `border-bottom-color` | the card header's rule in dark mode only |

**The recommendation, with the argument, not the conclusion.** Add the twelve
`border-{top,right,bottom,left}-{color,style,width}` longhands to
`ALLOWED_PROPERTIES`. This is not a widening of the security surface:

- Each is a *component of a shorthand already on the list*. `border-bottom: 1px
  solid #ccc` is allowed today and expresses exactly what the three longhands do.
  The current list is internally inconsistent, not deliberately narrow.
- The value check runs first and is property-agnostic. `_unsafe_values` rejects
  any `URLToken`, any `FunctionBlock` outside `ALLOWED_FUNCTIONS`, and any
  `ParseError`, regardless of which property the value belongs to. Verified
  directly against a patched allow-list:
  `border-bottom-color:expression(alert(1))` → `""`,
  `border-top-width:url(javascript:alert(1))` → `""`,
  `border-left-color:url("https://track.example/p.gif")` → `""`,
  `border-bottom-color:rgb(226,226,226)` → kept.
- `tests/unit/test_mail_corpus.py::test_gap_two_level_border_longhands_are_dropped`
  asserts the current behaviour, and asserts the shorthand/longhand
  inconsistency alongside it, so the change fails that test by name.

**A second, narrower judgement call: `background`.** The `background` shorthand
is dropped whole, which costs `newsletter_table` its page ground
(`body { background: #f4f4f4 }`) and one inline rule. The exclusion is
deliberate and documented (`background` can carry `url()` and fetch silently).
But the value check already handles that: with `background` allow-listed,
`background:url(https://track.example/p.gif)` → `""` and
`background:transparent url(x) no-repeat` → `""`, while `background:#f4f4f4`
survives. Unlike the border longhands this **is** a new capability (a colour
that cannot be set through `background` today), even though `background-color`
already sets the same pixel. In this corpus the cost is small — the newsletter's
`.wrapper { background-color: … }` class and its `bgcolor` attributes both
survive, so the page still has its grey ground. **Recommend leaving it alone**
and revisiting only if real mail shows the ground actually disappearing.

**Losses that are correct and should stay.** Everything else the corpus loses is
either vendor-private or deliberate, and
`test_nothing_standard_and_unaccounted_for_is_lost` will fail by name if a new
one appears:

- `mso-*` (7 across Word and the table newsletter), `-webkit-*`, `-ms-*` — hints
  no engine outside their vendor reads.
- `@page` (Word's print geometry, for a document we do not own), `color-scheme`
  and `supported-color-schemes` (`mailosh.render.dark` owns the frame's scheme),
  `outline`, `overflow`, `box-sizing`.
- `word-wrap`, `line-break`, `font-variant-caps` — typographic hints the frame's
  own base stylesheet already covers with `word-break: break-word`. Apple Mail's
  apparent 12/20 is almost entirely this: six of its eight losses are
  `word-wrap`/`-webkit-nbsp-mode`/`line-break`, each counted twice because it
  sits on `<body>`, which is unwrapped.
- `margin`/`padding`/`background` on `newsletter_table`'s `<body>`, and
  `display` on OWA's `<style style="display:none">`: lost **with their element**,
  not to a property policy. Worth stating because it makes the raw ratios look
  worse than the sanitiser is.

**`@import` costs a font, not a layout.** `newsletter_modern.html` ships two
`<style>` elements, the first nothing but an `@import` of a Google font.
`ALLOWED_AT_RULES` is `{"media"}`, so that block sanitises to `""` and the
second — the entire design, 69 of 75 declarations — is unaffected. The
`@media (prefers-color-scheme: dark)` block and Outlook.com's `[data-ogsc]`
selectors both survive, which matters: a message that dresses itself for dark
mode keeps its own colours *and* is exempted from the automatic restyle.

**Comment-wrapped stylesheets survive** — see executive summary item 5.

**`MAX_CSS_BYTES` in aggregate.** Confirmed per-block, and confirmed bounded:

| shape | result |
|---|---|
| one `<style>` of 540 KB | refused, `css=""`, 1 ms |
| 12 blocks × 480 KB (5.7 MB of comments) | all accepted, `css=155 B`, 28 ms |
| 11 blocks × 48 KB of dense declarations (550 KB body) | all accepted, **555 KB of CSS out, 256 ms** |

The 512 KB `maxBodyValueBytes` on both fetch paths is what keeps this from being
worse — it happens to equal `MAX_CSS_BYTES` exactly, which reads like a
coincidence rather than a decision. A running total across blocks would make the
bound explicit and cost one integer.

**One cosmetic re-serialisation artifact**, noted so nobody hunts it later:
tinycss2 re-emits `font-family:Aptos, Calibri, sans-serif` as
`font-family:Aptos , Calibri , sans-serif` (space before the comma) and
normalises `-.25in` to `-0.25in`, `rgb(102,102,102)` to `rgb(102, 102, 102)`.
All still valid CSS, all render identically. Only matters if a future test
asserts on exact declaration strings — several in
`tests/unit/test_mail_corpus.py` do, deliberately.

## Remote images and the proxy

Verified in the corpus, live, and in the browser.

- **`remote=0` strips every remote `src`** across the corpus and live. The
  sanitiser's `blocked_remote` counts *images*, not hosts, and `remote_hosts` is
  the deduplicated host list behind it — so both newsletters, which carry one
  hero image and one 1×1 open-tracking pixel on a *separate* host, report
  `2` and two hosts. The banner renders exactly that: **"This message has 2
  remote images / They would load from cdn.vendor.example,
  track.vendor.example."** Screenshotted in Chrome.
- **`remote=1` routes every remote image through `/img?u=<token>`** and no
  original host string survives into the document. Asserted live for a
  two-image, two-host message.
- **`/img?u=not-a-token` is `403`**, live — refused at the signature, before any
  fetch is attempted, which is why it is not `502`.
- **A `cid:` a message does not carry is `404`**, live. The lookup is scoped
  inside the message the reader already opened, so a guessed Content-ID cannot
  reach another message's part.
- **The inline part round-trips byte for byte.** A `multipart/alternative` →
  `multipart/related` message imported through JMAP comes back with exactly one
  `attachments` entry carrying a `cid`, Stalwart preserves the Content-ID with
  its angle brackets (`<logo.9f2c1b@mailosh.test>`, stripped by
  `frames._bare_cid` as designed), the declared type survives as `image/png`,
  and `GET /m/{id}/cid/{cid}` returns the original 89 bytes with
  `Content-Disposition: inline`. This is the seam nothing else tests, and it
  works.
- **…and none of it loads in a browser.** See "Browser QA → Defect 1". Both the
  `cid` route and the `/img` proxy answer `303 → /login` when the frame asks.

## Quote trimming

- **Five of six corpus messages split correctly**, at the client's own marker
  and not merely somewhere:
  `gmail_reply` at `<div class="gmail_quote gmail_quote_container">`,
  `outlook_web_reply` at `<div id="appendonsend"></div>`,
  `apple_mail_reply` at `<blockquote type="cite" class="">`. Both newsletters
  are correctly **not** split — a false positive there is worse than a missed
  one, because the reader would see a masthead and nothing else.
- **`outlook_desktop_reply` is not split at all.** Executive summary item 3.
  Confirmed on screen: the `From:/Sent:/To:/Subject:` block and the quoted
  "Can you make Thursday?" render inline, with no toggle. The existing
  `tests/fixtures/mail/quotes/outlook_desktop.html` passes only because it uses
  the `-----Original Message-----` shape, which is the one Word variant already
  covered — the corpus fixture uses the other one, which current Outlook for
  Windows actually emits.

  The fix is a `QuoteMatcher` that cannot key on a class or an id, because Word
  emits neither. The realistic options are a `From:`/`Sent:`/`To:`/`Subject:`
  header-block heuristic in the HTML path (the plain-text path already has one,
  `quote_trim._header_block_start`, and could be lifted), or a matcher on a
  `<div>` whose only content is that block. Either is a behaviour change to a
  shared module and is left to whoever owns `quote_trim.py`.
- The split remains a pure slice: `visible + quoted == input` is asserted for
  every corpus message, so no attribute, tag or entity can differ from what the
  sanitiser approved.
- Live, the toggle markup lands correctly: the reply text is *above*
  `class="mailosh-quote-toggle"`, the quoted half is inside
  `<div data-mailosh-quote hidden>` *below* it, and `?expand=1` produces
  neither.

## Conversation view

**Not independently verified by Task 14.** The live flow opens single-message
threads, so it exercises `GET /t/{id}` end to end (title, sender row, the frame
partial's swap target, the ⋮ menu's presence) but says nothing about
multi-message expansion, per-message menus, `details` rows, or auto-advance.
`tests/unit/test_conversation.py`, `test_thread_routes.py` and
`test_web_thread.py` are the authority; this task did not re-derive their
claims and does not repeat them here.

## Attachments

**Not independently verified by Task 14.** The live flow imports no message with
a non-inline attachment, and the browser QA never opened the preview dialog —
the harness's `<dialog>` `close` event does not fire, so a dialog drill would
have produced a result that means nothing. `tests/unit/test_attachments.py` is
the authority.

One adjacent fact that *was* verified live and is worth carrying forward:
`GET /m/{id}/source` returns the raw RFC 5322 message as `text/plain`, and the
adversarial payload (`alert(1)` and all) appears there **verbatim** — which is
correct, is the one place it is supposed to appear, and is why that route's
`text/plain` and `nosniff` are load-bearing rather than tidy.

## Dark restyle

**Partially verified.** What Task 14 saw in Chrome:

- A light Word message and a light Gmail reply both render **dark** inside the
  frame, legibly, with the reader's dark theme — text, table rules, list markers
  and the muted signature colour all readable. Screenshotted.
- A newsletter that ships its own `@media (prefers-color-scheme: dark)` block
  keeps that block through the sanitiser (see "Sanitiser"), which is the input
  `mailosh.render.dark`'s `declares_color_scheme` reads.

What Task 14 did **not** verify: the three-way restyle decision across a
`color-scheme`-declaring mail versus an already-dark mail versus a white one,
the per-sender "Original colours" memory, or the banner's swap.
`tests/unit/test_dark_restyle.py` is the authority.

## Print

**Not verified by Task 14.** `GET /t/{id}/print` was not opened, and no engine's
print output was inspected. The plan's executive-summary ask — "the per-browser
print result" — is therefore **unanswered**, in all three engines.
`tests/unit/test_print_route.py` is the authority for the route's shape. What
*is* verified is the ingredient the print page depends on: `?expand=1` renders
the quote open with no toggle, live.

## Budgets

Re-measured 2026-09-05. Targets are design spec §11. Every number below was
produced on this machine on this date; nothing is carried forward.

**First, a correction to how this section was framed.** `scripts/measure.py`
does **not** measure the byte budgets and never has — it measures four
live-stack latencies, and its own closing line says so ("Spec §11's other
budgets are browser-side … or static-asset sizes; this script measures
neither"). The JS/CSS/font figures in `p1a-findings.md` were produced
separately, by `gzip -9` over the built assets. Anyone told to "re-run
`measure.py` to check the asset budgets" is being sent to the wrong tool; the
byte method is reproduced below so the next person does not have to guess it.

### Latency — `scripts/measure.py`, run clean end to end

Run against the live compose stack, 28-message Inbox / 30-message account.

| Measurement | n | p50 | p95 | min | max | mean |
|---|---|---|---|---|---|---|
| inbox query (`query_inbox`, limit=50) | 20 | 2.4 ms | 5.0 ms | 1.5 | 12.3 | 2.9 |
| thread fetch (`get_thread`) | 20 | 2.0 ms | 2.7 ms | 1.3 | 3.2 | 1.9 |
| SMTP → SSE `mail` frame | 5 | 230.1 ms | 797.7 ms | 191.1 | 871.2 | 401.2 |
| **`GET /mail/inbox/rows`, logged in** | 20 | **8.4 ms** | **12.3 ms** | 7.1 | 12.4 | 9.0 |

Partial TTFB **PASSES** < 200 ms with more than an order of magnitude spare.
Cold first request 35.7 ms, discarded by the script and reported separately;
response body 93,145 B uncompressed over 28 messages.

Two honest caveats on this run, neither of which the script hides:

- **SMTP → SSE got a frame on only 5 of 10 sends**; the other five hit the
  15 s frame timeout and were excluded. The p95 above is therefore over n=5.
  Not diagnosed here — the stack had other agents working against it at the
  time — but it is not a clean result and should not be quoted as one.
- **The first invocation aborted outright.** `_send_smtp_message` raised
  `SMTPServerDisconnected: Connection unexpectedly closed: timed out` on run
  1/10 of measurement (c), which killed the whole process — including
  measurement (d), the *only* one spec §11's partial-TTFB budget is actually
  about, and the budget table itself. An immediate manual SMTP send to the
  same port succeeded in 0.7 s, so the failure was transient, and the second
  invocation (the one reported above) completed. **This is a robustness
  defect, not a stale-assumption defect**: measurements (a), (b) and (d) do
  not depend on (c), yet a single flaky SMTP connection discards all of them.
  Left as-is deliberately — it was not this task's file to redesign — but it
  is the third time in three phases that this script has failed at exactly
  this point, and (c) should be made non-fatal before the next phase relies
  on it.

### Bytes

Method, so it is reproducible: `gzip -9` (zlib, no filename header — the CLI
`gzip -9 -c` adds ~10 B per file and will not reproduce these) over the built
assets. The file list is what `layouts/app.html` actually causes a browser to
fetch for `/mail/inbox`, which now includes two modules that have **no
`<script>` tag** and are reached by `import` instead — `keys.js` (from
`actions.js`, `app.js`, `palette.js`) and `command-score.js` (from
`palette.js`).

| Loaded on the inbox page | how | raw | gz |
|---|---|---|---|
| vendor/htmx.min.js | tag | 51,238 | 16,576 |
| vendor/idiomorph-ext.min.js | tag | 10,153 | 3,483 |
| vendor/preload.js | tag | 4,051 | 1,563 |
| js/actions.js | tag | 47,818 | 17,005 |
| js/palette.js | tag | 22,533 | 8,174 |
| js/sse.js | tag | 6,677 | 3,010 |
| js/frame.js | tag | 4,203 | 1,941 |
| js/app.js | tag | 34,238 | 12,428 |
| vendor/alpine.min.js | tag | 71,087 | 23,511 |
| js/keys.js | import | 36,800 | 11,479 |
| vendor/command-score.js | import | 5,935 | 1,896 |
| **JS total** | | **294,733** | **101,066** (98.7 KiB) |
| app.css | | 49,289 | 10,665 (10.4 KiB) |
| fonts/inter-latin.woff2 | | **40,752** | — (already compressed) |

Vendored but **not loaded**: `squire.js` (59,913 / 18,394) and
`purify.min.js` (29,204 / 10,900), both compose dependencies; `js/auth.js`
(3,541 / 1,583) loads on `/login` only, via `layouts/bare.html`.

| Budget (spec §11) | Target | Measured | Verdict |
|---|---|---|---|
| Partial TTFB (`/mail/inbox/rows`) | < 200 ms | p50 8.4 / p95 12.3 ms | **PASS** |
| Total JS, gzipped | ≤ 90 KB | 101,066 B (98.7 KiB / 101.1 KB) | **MISS** |
| — of which ours | ≤ 15 KB | 54,037 B (52.8 KiB) | **MISS, 3.6×** |
| CSS, gzipped | ≤ 30 KB | 10,665 B (10.4 KiB) | **PASS** |
| One font file | 48 KB | **40,752 B (39.8 KiB)** | **PASS** |
| Swap paint / INP / first-load LCP | — | not measurable here | **NOT MEASURED** |

**The font budget is now met, and the miss was the build, not the budget.**
It stood at 99,740 B — 2.08× — and the cause was `pyftsubset` being handed
the whole of `InterVariable.woff2`. Spec §4.2 asks for "Inter variable
(`wght` 400–700, latin subset ≈ 48 KB woff2)"; InterVariable ships **two**
axes, `wght` 100–900 *and* `opsz` 14–32, and the recipe kept both along with
`--layout-features='*'`, which retains all 39 OpenType features and the 462
alternate glyphs that exist only to be reached through them. Neither `opsz`
nor 35 of those 39 features is reachable from anything this UI does. The
Makefile's font rule now pins `opsz` and names the feature set; the four
combinations, all measured on the same unicode range:

| | `--layout-features='*'` | tight feature set |
|---|---|---|
| **both axes** | 99,740 B (what shipped) | 63,524 B |
| **`wght` only** | 64,068 B | **40,752 B** |

Note the off-diagonal: **neither lever alone reaches 48 KB.** Keeping `opsz`
caps the best case at 63,524 B even with every optional feature stripped, so
the two changes are not independent and the axis is the one that had to go.

**What was checked before changing it, against the old subset:**

- **Coverage is identical** — 283 codepoints reachable from plain text
  before and after, none lost, none gained.
- **Advance widths are identical** for all 283, at the default instance.
- **Kerning is identical across all 80,089 ordered pairs** of those 283
  characters. `kern` is retained; so is `tnum`, which is not optional here —
  `styles/input.css` sets `font-variant-numeric: tabular-nums` in four
  places and two templates use Tailwind's `tabular-nums`, so dropping it
  would have silently unaligned the list-range counter. Tabular digits still
  share one advance (1328 units) after the change.
- The 35 dropped features are all opt-in — small caps, fractions,
  superiors/inferiors, ordinals, oldstyle and proportional figures,
  discretionary ligatures, `cv01`–`cv13`, `ss01`–`ss08` — and are reachable
  only through `font-feature-settings` / `font-variant-*`, which this
  stylesheet sets nowhere except the `tabular-nums` above.

**The one real cost, stated plainly: pinning `opsz` is not free.** With
`font-optical-sizing` at its initial `auto`, a browser drives `opsz` from the
used font size. This UI's type ramp is 10.5–19px and the axis starts at 14,
so **every size from 10.5px to 14px already rendered at `opsz=14`** and is
byte-identical after the change — which is the list rows, the chrome, the
labels, and nearly all of the text on screen. Three sizes do change:
`.thread-subject` (19px/600), `.auth-title` (17px/600) and the 15px band.
Measured on the string "Inbox Archive Settings Compose Reply Snooze
Yesterday 0123456789 Wavy Ta To AV":

| size | width before | width after | delta |
|---|---|---|---|
| 19px / 600 | 784.38 px | 799.12 px | **+1.9 %** |
| 17px / 600 | 708.50 px | 714.00 px | +0.8 % |
| 15px / 400 | 616.12 px | 616.75 px | +0.1 % |

That is the difference between Inter's 14 pt and 19 pt optical designs: the
smaller one is fractionally looser, by at most 0.39 px on any single glyph's
advance at 19px. It is a real change and it is defensible rather than
invisible — but `.thread-subject` already hand-tunes its tracking
(`letter-spacing: -.012em`), so this is the same order of adjustment the
design is already making by hand, and it buys a 59 % smaller font file. If
anyone judges the 19px subject to have loosened visibly, the fix is a
letter-spacing tweak on that one rule, not the `opsz` axis.

**Not adopted: spec §4.2's literal `wght 400–700`.** It builds at 29,476 B,
a further 11 KB.

The reason first given here was wrong, and is corrected rather than left to
be quoted later: it said `styles/input.css` "sets `font-weight: 100`", so the
spec was narrower than real usage and §4.2 should be corrected. That reads
the `@font-face` **range descriptor** (`font-weight: 100 900`, which declares
what the variable font supports) as though it were a usage. It is not one.

Grepped for weights used as actual values: **500 (×5), 600 (×21), 700 (×4)**,
plus the implicit 400 and Tailwind's `font-medium`/`font-semibold` in
templates. No rule anywhere asks for a weight outside 400–700. **§4.2 is
correct and must not be "corrected".**

So narrowing to 400–700 is in fact safe for every weight in use, and is a
real ~11 KB (27%) saving still on the table. It is not taken now for two
reasons, neither of them rendering risk: the font is already **40,752 B
against a 48 KB budget**, so the bytes buy nothing today; and the change
needs `styles/input.css`'s `@font-face` descriptor narrowed to
`font-weight: 400 700` in the same commit, which is another agent's file
while compose is in flight. A descriptor promising 100–900 over a font that
only carries 400–700 is worse than either end state — the browser would
synthesise the missing weights.

**The JS budget is missed and should not be moved.** 101,066 B gz against
90 KB. Three things the arithmetic has to keep straight:

- **The 1A figure (94,501 B) was not stale in the direction anyone assumed.**
  1A recorded 92.3 KiB unique / 101.5 KiB as-fetched, the gap being a
  duplicate `keys.js` tag. **That duplicate has since been fixed** — `keys.js`
  and `command-score.js` now have no tags and are imported — so unique and
  fetched are the same number again. But the 9.2 KiB that fix recovered has
  already been spent: `actions.js` grew +2,560 B gz, `keys.js` +2,074 B, and
  `frame.js` (+1,941 B) is new. Net, the honest unique total went **up**,
  from 94,501 to 101,066.
- **1B's prediction was wrong and should be retired.** This section
  previously said `frame.js` "moves the 1A headline by well under a
  kilobyte". `frame.js` alone is 1,941 B gz, and the module set around it
  moved by 6,565 B.
- **The budget's own breakdown already accounts for compose.** §11 spells it
  out: htmx 16 + idiomorph 3.4 + preload 1.5 + Alpine 19.4 + Squire 18 +
  DOMPurify 10.6 + command-score 1 + ours ≤ 15 = 84.9 KB, inside 90. Squire
  and DOMPurify are **not loaded yet**, so the like-for-like comparison is
  101,066 B against the 61.4 KB the breakdown allows for the parts actually
  on the page — a **1.65×** overrun. Load compose and the total is 130,360 B
  gz, **1.45×** the whole budget.

The overrun is entirely ours, and it is not close: **ours is 54,037 B gz
against a ≤ 15 KB line, 3.6×.** Vendor code is 47,029 B against the 61.4 KB
the breakdown allows it — under, even with Alpine's CSP build costing
+4.1 KB over the 19.4 KB assumed for the standard build. Do **not** raise
the 90 KB number to make this pass: the budget was constructed from a
part-by-part breakdown that still holds for every part except ours, and a
budget that moves when it is missed is not a budget. The available levers,
in order of cost: our JS ships **unminified** and there is no minifier in the
build (deliberately — no Node toolchain), which is the largest single
recoverable chunk and the one that requires a real decision; after that it is
actual code removal from `actions.js` (17.0 KB gz) and `keys.js` (11.5 KB
gz), the two biggest files we own.

## Browser QA

Chrome only, against `make dev` at `http://localhost:8000`, 2026-09-05, using
three seeded corpus messages plus one adversarial message, each with a per-run
`Message-ID`, all destroyed afterwards (`leftover: 0` verified twice).

**Firefox and Safari were not tested and are not implied to pass.** They cannot
be driven from this harness. Spec §15's gate is one engine of three.

### The gate, item by item

| spec §15 item | Chrome | evidence |
|---|---|---|
| No CSP violation in the console | **PASS** | zero `securitypolicyviolation` events across four messages; console carried nothing but a browser extension's own logs |
| No "Refused to execute inline script" | **PASS** | stronger than an empty console: the frame *posted its height*, which only the inline script does |
| The frame auto-sizes | **PASS** (with a caveat) | 880 px, 588 px, 503 px, 253 px assigned from real `postMessage`s, every one with `origin === "null"`. Caveat: defect 2 below |
| A `cid:` image loads under `img-src 'self'` in an opaque-origin document | **FAIL** | defect 1 below — but *not* because of `img-src`; the request is made and reaches the server |
| A hostile `postMessage` from the parent console changes nothing | **PASS** | 7 payload shapes (oversized, string height, negative, missing, non-object, and a synthetic `MessageEvent` with `origin:"null"`), plus one bounced through `frame.contentWindow`; height unchanged at 880 px throughout |
| Firefox | **UNVERIFIED** | cannot drive |
| Safari | **UNVERIFIED** | cannot drive |

The hostile-`postMessage` result is worth one sentence of *why*, because it is a
property of the design rather than luck: `frame.js` requires
`event.origin === "null"` **and** `event.source` to be identical to one of our
frames' `contentWindow`s. A `postMessage` typed into the parent console always
carries the page's real origin, and cannot forge `event.source`. Both checks are
load-bearing and neither is sufficient alone.

### Defect 1 — inline and proxied images do not load: the frame's requests are unauthenticated

**Reproduced, not theorised.** Not fixed: the files that would fix it
(`mailosh/web/frames.py`, `mailosh/security/sessions.py`) are not this task's.

Open any message with a `cid:` image. The reader gets a broken-image icon and
the alt text. Screenshotted. The app log, same second, same session, same URL:

```
GET /m/luaaaac0/html?remote=0                                  200 OK   <- the frame
GET /m/luaaaac0/cid/dispatch-mark%40newsletter.example          303 See Other   <- from inside the frame
GET /login?next=%2Fm%2Fluaaaac0%2Fcid%2Fdispatch-mark%40…       200 OK
GET /m/luaaaac0/cid/dispatch-mark%40newsletter.example?probe=topdoc  200 OK   <- from the top document
```

The same happens at `remote=1` for every proxied image:

```
GET /img?u=eyJlIjoxNzg4NTg2MDQ0…   303 See Other
GET /login?next=%2Fimg%3Fu%3D…      200 OK
```

So **"Show images" shows nothing**, and an inline logo never renders.

**Mechanism.** The frame document has an opaque origin — the CSP `sandbox`
directive carries no `allow-same-origin`, deliberately, and the element's
`sandbox` attribute matches. A subresource request from an opaque-origin
document is not same-site for cookie purposes, so Chrome does not attach the
`SameSite=Lax` session cookie (`mailosh/security/sessions.py`:
`{"httponly": True, "samesite": "lax", …}`). `deps.current_user` finds no
session and redirects.

Three controls, all run:

1. **Positive control for "no cookie ⇒ 303":** `fetch(url, {credentials:'omit'})`
   from the top document reproduces the exact `303`, while
   `{credentials:'same-origin'}` and `{credentials:'include'}` both give `200`.
2. **Negative control for "the route is fine":** an `<img>` pointed at the same
   URL from the top document loads (`naturalWidth === 8`).
3. **The element's `sandbox` attribute cannot rescue it:** an iframe with
   `sandbox="allow-scripts allow-same-origin"` behaves identically, because the
   response's own CSP `sandbox` directive is intersected with it.

**Why no test caught this.** `httpx` attaches cookies regardless of `SameSite`;
it has no concept of a document origin. `tests/integration/test_live_reading_flow.py`
asserts `200` on the same route in the same second and is *correct* — the route
works. Only a browser can see this, which is the argument for the browser gate
existing at all.

**Four candidate fixes**, none of them this task's to choose:

1. **Serve small inline images as `data:` URIs** inside the frame document.
   `img-src` already permits `data:`, no request is made, no cookie is needed.
   Costs body size, so it wants a threshold well under `MAX_INLINE_BYTES`'s 5 MB
   — a 256 KB cut-off covers the signature logos that are 95% of real `cid:`
   usage, with a fallback for the rest.
2. **Authenticate the two frame-subresource routes by signed token instead of by
   session.** `/img?u=` already carries a token bound to the reader's `user_id`
   (`image_policy.sign_remote_url`) and does not actually need the cookie;
   dropping `UserDep` there and trusting the token would fix the proxy outright.
   `/m/{id}/cid/{cid}` has no token today and would need one minted into the
   rewritten URL by `html_sanitize._cid_src` — the sanitiser already receives a
   `sign_image` callable, so the shape exists.
3. **`SameSite=None; Secure` on the session cookie.** Fixes both routes, requires
   HTTPS everywhere, and trades away a CSRF defence the app currently leans on
   (`csrf.py` treats `Sec-Fetch-Site: cross-site` as hostile). Not recommended.
4. **A second, narrowly-scoped cookie** with `SameSite=None; Secure`, valid only
   for the two subresource paths. More moving parts than option 2 for the same
   result.

Option 2 is the one that matches the design already in the code: the frame is
supposed to be a document with no ambient authority, and giving its subresources
explicit, per-URL, per-reader capabilities is exactly that posture.

### Defect 2 — `frame.js`'s zero-height sentinel can strand a frame

`FRAME_SCRIPT`'s `post()` opens with `var LAST = 0;` and `if (h === LAST) return;`.
A frame whose first measurement is `0` therefore posts nothing *and* records
nothing, and its remaining triggers — `load`, `resize`, the initial
`ResizeObserver` callback and `setTimeout(post, 0/300/1200)` — have all already
fired by then. A frame laid out later has nothing left to re-measure it.

Observed for one of four messages (the short adversarial one): no height ever
assigned across 16 s of sampling and a full page reload, while the other three
sized normally. The document, its CSP header and its script bytes were verified
byte-identical to a message that *did* size, so it is not a content-dependent
CSP failure.

**Honest status: not confirmed as a product defect.** This tab reports
`visibilityState: "hidden"` for its whole life, which throttles exactly the
timers and observers involved, so a harness artifact is at least as likely. The
mechanism is nonetheless real and visible in three lines of code, and the fix is
one character: `var LAST = -1`. Worth one deliberate check on a foreground tab
before 1C ships anything that depends on frame sizing.

### The adversarial message, live in a browser

Imported through SMTP-shaped MIME → JMAP → Stalwart → `Email/get`, then opened
in Chrome. The served document:

- **one** `<script`, byte-identical to `FRAME_SCRIPT`;
- **zero** occurrences of `alert(`, `javascript:`, `@import`, `expression(`,
  `<base`, `<iframe`, `<form`, or `attacker.invalid`;
- **zero** `on*` attributes (regex over the whole document);
- the readable sentence still readable.

No `window.alert` fired, no page error was raised, no CSP violation was
recorded. Nothing in the import → storage → retrieval path re-decodes or
re-wraps a payload the unit tests already kill — which was the only question a
live run could answer that the 450 000-iteration fuzz review could not.

### Harness artifacts encountered, for the next agent

- `visibilityState` is `"hidden"` for the tab's whole life. Timers and
  `ResizeObserver` are throttled; frame heights can take >10 s to arrive.
- `hx-trigger="intersect once"` never fires on its own — call
  `htmx.trigger(wrap, 'intersect')`.
- **Screenshots *did* composite sandboxed-iframe content** in this session,
  contrary to the warning carried forward from earlier tasks. The message body,
  the broken inline image and the quote toggle were all visible and correctly
  positioned. Screenshot coordinates matched click coordinates 1:1.
- **Clicks could not be delivered into the opaque-origin frame.** A click on the
  quote toggle at coordinates confirmed correct by two zoom captures produced no
  toggle and no height change, while a click on the parent's "Show images"
  button worked immediately. Whether the toggle *works* in Chrome is therefore
  **unverified**; the button and its handler are present in the served document
  and covered by unit tests.
- A same-origin copy of the frame document (`document.write` into an iframe you
  control) is **not** a usable substitute: the app shell's own CSP is
  `script-src 'self'`, which the copy inherits, so the frame's inline script is
  refused and the copy behaves like a document whose script never ran. Two
  probes were wasted on this.
- `javascript_tool` returns `[BLOCKED: Cookie/query string data]` for any
  expression whose result contains a header, a cookie or a URL with a query
  string. Compute hashes and lengths instead of printing the value.

### Housekeeping

All seeded messages destroyed, cleanup self-verified (`leftover: 0`, twice). The
browser tab was returned to the page it was on. All containers left running and
healthy.
