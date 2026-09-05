# Mailosh Phase 1 — Webmail Design Specification

- **Date:** 2026-09-02
- **Status:** Approved direction. Binding authority for the Phase 1 implementation plans (1A–1E).
- **Owner:** Manish Sharma
- **Supersedes:** roadmap ordering in the Phase 0 spec (§13) — Phase 1 is now the webmail; platform ops (wizard, DNS page, TLS, health panel) move to Phase 2.
- **Inputs:** `docs/research/2026-09-02-webmail-ux-research.md` (Superhuman, Gmail, ihasmail, HTMX techniques), `docs/spikes/p0-findings.md` (six spike gates), the Phase 0 spec (`2026-08-31-mailosh-design.md`, still authoritative for §3 architecture, §4 stack, §6 real-time, §8 rendering, §9 security unless amended here).

## 1. Goal and thesis

Ship a webmail that a Gmail user recognises in five seconds and a Superhuman user respects in five minutes: **Gmail's frame and affordances, Superhuman's manners** (approach C). Familiar regions, dense rows, labels, undo, a corner compose — rendered with restraint (one accent, hairlines, small radii, 120–150 ms motion, quiet chrome) and driven by a speed model (every action optimistic and reversible, every action on a key, ⌘K teaches the keys, prefetch makes opens instant).

**Definition of done (Phase 1):** a user logs in at `mail.example.com`, triages a 1,000-message inbox with the keyboard alone, reads HTML mail safely with images off by default, composes and sends rich text with attachments from a corner dock while the inbox keeps updating, organises with coloured nested labels, finds anything with Gmail operators, switches theme/density/reading-pane live from Quick settings, and does it all with sub-100 ms perceived latency on a 4 GB VPS. Nothing they do is irreversible without a confirmation, and nothing in the UI exists that does not work.

## 2. Decisions taken in the design sessions (2026-09-02)

| Decision | Choice | Alternatives shown | Why |
|---|---|---|---|
| Product shape | C — Gmail frame + Superhuman manners | A Gmail-faithful, B Superhuman-first | familiarity + speed + own identity |
| Default layout (≥ 1100 px) | **List-first**: conversation replaces the list, `u` back; reading pane right/below selectable in Quick settings | reading pane right (rec.), below | user preference; calmer screen, full-width rows; Gmail's own default |
| Palette | **Graphite & Blue** — neutral graphite, mail blue `#1a73e8`, dimmed to `#1E6FD6` in dark (retuned from `#1D6AE5`/`#8AB4F8`; see polish-v2-spec.md's amendment); Carbon-rule dark | Slate & Indigo (rec.), Paper & Teal | user preference; borrows Gmail/Outlook trust |
| Default density | **Comfortable** — 52 px two-line rows | Compact 36, Standard 44 (rec.) | user preference; easiest scan for casual users |
| Type | Inter 4.x variable, self-hosted latin subset | system stack | consistent metrics across OSes for dense rows |
| Keyboard | Gmail's map on by default + ⌘K, Shift+J/K, ⌘⇧↵ | — | familiarity; Superhuman's teaching model |
| Archive wording | "Archive" (`e`) | Superhuman's "Done" | Gmail vocabulary was the ask |
| Undo semantics | immediate commit + reverse op; delayed commit for send only | delayed commit everywhere | JMAP is truth; multi-device convergence |
| HTML rendering | separate URL + CSP `sandbox` header in `<iframe sandbox>` | `srcdoc`, shadow DOM | mox/Proton-grade isolation |
| SSE bridge | in-house ~40-line EventSource→htmx bridge | htmx-ext-sse | the extension publishes no license (NOTICE) |

## 3. Scope

### In Phase 1
Auth + sessions (§9); app shell and design system (§4); list-first mail views with selection/bulk/keyboard/palette (§5–6); conversation view with safe rendering (§7); compose dock (§8); labels, search, settings (§10); responsive layout, accessibility, performance pass, first-run coaching (§11–12).

### Deferred (not in Phase 1)
Snooze/reminders and schedule-send-without-server-support (need the job runner — Phase 3), Sieve filters UI and vacation responder (Phase 3), category tabs / Split Inbox / importance (later), calendar & contacts apps (Phase 4), multi-account & connectors (Phase 4), AI features (opt-in, later), offline/PWA-offline (later), tracker-blocking badge (later), passkeys/TOTP (Phase 2, with platform security), multi-worker fan-out (Phase 2). **Rule:** the UI must not show controls for deferred features — no "Snoozed" nav item until snooze works.

## 4. Design system

### 4.1 Tokens (Tailwind 4 CSS-first; plain CSS variables carry the theme, `@theme inline` maps them to utilities)

Light (`:root`):
`--bg #F4F6F8` page · `--surface #FFFFFF` · `--field #E9EEF4` inputs/search · `--read #F5F7FA` read rows · `--hover #EEF2F7` · `--line #E3E8EE` · `--line-2 #C9D2DC` strong borders/checkbox · `--fg #111827` · `--fg-2 #5A6472` · `--fg-3 #8A94A3` · `--accent #1A73E8` · `--accent-ink #1258C4` text on soft · `--accent-soft #E8F0FE` selection/active nav · `--on-accent #FFFFFF` · `--star #F59E0B` · `--danger #DC2626` · `--warn #B45309` · `--success #15803D` · `--toast #1F2937`/`--toast-fg #FFFFFF`.

Dark (`:root[data-theme=dark]` and `@media (prefers-color-scheme: dark) :root:not([data-theme=light])`), Carbon rules — no pure black, greys as depth, text at 90/65/42 %:
`--bg #111318` · `--surface #191C22` · `--field #1F232B` · `--read #14171D` · `--hover #1C2028` · `--line #282C35` · `--line-2 #3D4350` · `--fg rgba(255,255,255,.9)` · `--fg-2 rgba(255,255,255,.65)` · `--fg-3 rgba(255,255,255,.42)` · `--accent #1E6FD6` · `--accent-ink #A9C9F8` · `--accent-soft rgba(138,180,248,.16)` · `--on-accent #111318` · `--danger #F87171` · `--warn #FBBF24` · `--success #4ADE80` · `--toast #F3F4F6`/`--toast-fg #111827`.

Theme resolution: `data-theme` on `<html>` rendered **server-side** from the user's preference (no flash); `system` follows `prefers-color-scheme`; `color-scheme` set accordingly; `<meta name=theme-color>` matches `--bg`.

Label colours (12, light/dark): indigo `#6366F1/#818CF8`, emerald `#10B981/#34D399`, rose `#F43F5E/#FB7185`, amber `#F59E0B/#FBBF24`, sky `#0EA5E9/#38BDF8`, violet `#8B5CF6/#A78BFA`, teal `#14B8A6/#2DD4BF`, orange `#F97316/#FB923C`, pink `#EC4899/#F472B6`, lime `#84CC16/#A3E635`, slate `#64748B/#94A3B8`, red `#EF4444/#F87171`. Chip = colour at 14 % alpha background, text = the colour's ink shade (light) or the light shade (dark).

### 4.2 Type, shape, motion
- Inter variable (`wght` 400–700, latin subset ≈ 48 KB woff2, OFL 1.1) self-hosted with metric-matched fallback (`size-adjust 107.47%; ascent-override 90.14%; descent-override 22.44%`), `font-display: swap`, preloaded. `font-feature-settings: "tnum"` on dates/counts.
- Scale: 11 px chips/eyebrows (letter-spacing .06em, uppercase for section headers) · 12 px meta/dates · **13 px UI base** · 14 px message body (line-height 1.55, max 68ch) · 15 px wordmark · 19 px thread subject (weight 600, letter-spacing −.012em).
- Radii: 4 kbd · 6 nav items/buttons · 8 inputs/Compose/dock top corners · 10 cards/toasts · 12 palette · 99 chips/pills. Hairline borders everywhere (`--line`); shadows only on floating layers (dock `0 12px 40px rgba(15,23,42,.22)`, toast `0 8px 24px`, palette `0 24px 64px rgba(0,0,0,.5)`).
- Motion: `--default-transition-duration 120ms`, easing `cubic-bezier(.2,0,0,1)`; hover/press 100–150 ms; panels/dialogs 200 ms; archive row collapse 160 ms; keystroke-rejected shake 150 ms; `prefers-reduced-motion: reduce` sets all durations to 0. No decorative motion.
- Icons: Lucide (ISC) via a Jinja macro `{{ icon("archive", class="size-4") }}` inlining vendored SVGs (16 px in rows/toolbars, 20 px in the top bar, 1.75 stroke). `aria-hidden` unless the icon is the label.
- Focus: `:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px }`; never remove outlines. **`--focus`, not `--accent`** (amended in 1E, after measurement): `--accent` at `#1E6FD6` measures **2.83:1 on a hovered row and 2.98:1 on a selected row in dark**, below SC 1.4.11's 3:1, because those grounds are themselves accent-tinted. `--focus` is `#1A73E8` in light — identical to `--accent`, so light is unchanged — and `#4A8FE7` in dark, and it is re-pointed back to `--accent` inside `.toast`, whose surface is inverted. The brand accent itself is untouched.
  Rows additionally use `outline-offset: -2px`: `.row` spans the full width of a scrollport that clips both axes, so a positive offset drew the ring outside it and the left and right strokes were simply missing.
  A caution the 1E audit paid for: the ratios recorded in the old rule's comment were for the **pre-retune** `#8AB4F8` accent and had been stale since §4.1 moved to `#1E6FD6`. A contrast number written beside a colour is only true until the colour moves; `tests/unit/test_a11y.py` now recomputes every ratio from the tokens on each run, so the next retune fails a test instead of shipping.

### 4.3 Layout metrics
Top bar 52 px · left nav 224 px (collapses to a 64 px icon rail; state persisted) · main surface with 10 px top-left radius sitting on `--bg` · list toolbar 44 px · row heights **Compact 36 / Standard 44 / Comfortable 52 (two-line)** — settings label them exactly so; Comfortable is the shipped default via `--row-h`, `--pad-y` per `[data-density]` (never by rescaling `--spacing`) · sender column 170 px (140 px under 1100 px) · date column 66 px · compose dock 560 × 520 (min 280 × 44, full-screen inset 24 px) · palette 600 px wide at 84 px from top · reading pane (opt-in) list width 520 px default, min 320, splitter persisted.

Breakpoints: ≥ 1100 px nav + main (reading pane optional); < 1100 px nav collapses to rail; < 768 px nav becomes a drawer, list and conversation are separate screens (conversation slides over), compose is full-screen `100dvh`, action bar sticks to the bottom with safe-area padding. Touch affordances (swipe archive) are keyed on `(pointer: coarse)`, not width — deferred to 1E if time allows, never blocking.

## 5. Frame and list

### 5.1 Top bar
Menu (toggles nav rail) · wordmark (blue mark + "Mailosh") · centred search pill (max 640 px, 36 px, `/` kbd hint, filter icon opens the advanced panel) · right: help (`?` overlay), Quick settings (gear), avatar menu (account, settings, sign out). No product rail, no side-panel apps.

### 5.2 Left nav
Compose button (accent, 36 px, `c` hint on hover) · Inbox (unread count) · Starred · Sent · Drafts (count) · More ▾ → All mail (virtual: everything except Spam/Trash), Archive (the role mailbox that receives archived mail with no other label), Spam, Trash · **Labels** section (+ create) with colour dots, nesting (collapsible), unread counts, hover ⋮ → colour / show / hide / show if unread / edit / remove · unread counts are bold; totals shown for Drafts. Active item uses `--accent-soft`/`--accent-ink`. Snoozed/Important appear only when their features ship.

### 5.3 List (list-first default)
Toolbar: select-all tri-state checkbox · refresh · ⋮ · right: "1–50 of 1,284" with ‹ › (hover on the count reveals Oldest/Newest). With ≥ 1 selected the toolbar swaps to actions: archive, spam, delete, mark read/unread, labels, move to, more (mute later).

Row anatomy (left→right): checkbox (always visible at 60 % opacity, 100 % on hover/selection — never hover-only) · star · sender(s) (`Aisha, Tom, me (3)`; bold when unread) · subject (weight 600 when unread) – snippet (`--fg-2`) on one line for Compact/Standard, two lines for Comfortable · inline label chips (max 2 + "+1") · paperclip when attachments · date (`10:42 AM` today, `Sep 1` this year, `9/1/25` older; bold when unread). Hover: the date is replaced by archive / delete / mark read / (snooze later). Right-click: context menu with all actions and their kbd hints. Row backgrounds: unread `--surface`, read `--read`, hover `--hover`, focused row `inset 3px 0 0 var(--accent)`, selected `--accent-soft`.

Paging: 50 per page (100 selectable); "load more" sentinel via `hx-trigger="intersect once"` appends the next page in the same list (infinite scroll within a page set) — no client virtualization; `content-visibility: auto; contain-intrinsic-size: auto <row-h>` on rows beyond the first 100.

Rows are `role="row"` inside `role="grid" aria-multiselectable="true"`, with roving `tabindex`, `aria-selected` for selection and `aria-current="true"` for the open conversation.

### 5.4 Empty, loading, error
Skeleton rows (shimmer) only if the response takes > 300 ms. Empty states with specific copy: Inbox → "You're all caught up" + calm illustration (SVG, no photos); other folders → "Nothing here"; search → "No results for …" with operator tips. Errors → toast with Retry; a `JmapError`/`TransportError` mid-request never yields a bare 500: a global exception handler returns an OOB toast fragment for HTMX requests (`HX-Reswap: none`) and a friendly page otherwise; 401 → `HX-Redirect: /login?next=`. Connection lost → thin banner under the top bar ("Reconnecting…"), cleared on reconnect, list refreshed.

## 6. Interaction model

### 6.1 Keyboard (on by default; toggle in settings)
One registry in `static/js/keys.js`: `{id, keys:["g","i"], scope, group, label, run}`; drives dispatch (`keydown` on `window`), the `?` overlay (grouped like Gmail's), hover tooltips (`Archive (e)`), and the palette's shortcut column. Two-key sequences time out after 1000 ms; suppressed in inputs/contenteditable/`isComposing` except `Esc` and modifier chords; rejected keys shake the target 150 ms. Scopes stack: global < list < thread < compose < dialog; palette suppresses all others.

Map (Gmail-compatible): `j/k` older/newer · `o`/`Enter` open · `u` back to list · `x` select · `Shift+J/K` extend selection · `e` archive · `#` delete · `!` spam · `s` star · `Shift+I`/`Shift+U` read/unread · `l` label as… · `v` move to… · `[`/`]` archive & older/newer · `n/p` next/prev message · `;`/`:` expand/collapse all · `r`/`a`/`f` reply/reply-all/forward · `c` compose · `/` search · `z` undo · `.` more actions · `Esc` clear selection / close · `g i` Inbox · `g s` Starred · `g t` Sent · `g d` Drafts · `g a` All mail · `g l` go to label (palette in label mode) · `* a` `* n` `* r` `* u` `* s` `* t` select all/none/read/unread/starred/unstarred · `?` shortcuts · `⌘K` palette · compose: `⌘↵` send, `⌘⇧↵` send & archive, `⌘⇧C/B` Cc/Bcc, `⌘K` link, `⌘B/I/U`, `Esc` close (draft kept).

### 6.2 Command palette (⌘K)
Modal (native `<dialog>`), 600 px, input + grouped results: **Actions** (context-aware for the current selection/open thread), **Go to** (system mailboxes, labels), **Labels/Move** sub-modes with type-to-create, **Settings** entries, **Search** fall-through ("Search mail for …"). Fuzzy matching with Superhuman's `command-score` (MIT, vendored), aliases ("trash" → Delete), recency boost for labels. Each result shows its kbd chips at the right; `↑↓` move, `↵` run, `Esc` close; `Ctrl+J/K` also move (Superhuman muscle memory).

### 6.3 Actions, undo, auto-advance
All mutations are optimistic: Alpine updates the DOM (row leaves with a 160 ms collapse, counts adjust) and fires `hx-post` with `hx-swap="none"`; the server answers 204 or `HX-Trigger` with the canonical delta; failures revert and toast. Undo = **immediate commit + reverse op** kept for 10 s (`z` or the toast's Undo); the toast names what happened ("Archived" / "Moved to Work" / "Deleted"). Only **send** is a delayed commit (default 10 s; 5/10/20/30) with a "Sending… Undo" toast; the pending send is flushed via `fetch(keepalive)` on `pagehide`. Auto-advance after archive/delete/move in the conversation view: **next older** by default (newer / back to list selectable). Bulk actions confirm only when they touch > 100 conversations.

### 6.4 Instant feel
- Prefetch threads with the `preload` extension (`mousedown` + 100 ms hover) and on keyboard focus after 150 ms idle; thread partials are GET, `Cache-Control: private, max-age=60`, `Vary: HX-Request`; a preloaded GET (`HX-Preloaded: true`) never marks read — the read flag is set by the POST fired on actual open.
- Every list/thread swap uses idiomorph (`hx-swap="morph:innerHTML"`) so focus, scroll and Alpine state survive; the compose dock and any open menu are excluded from morphing (`data-morph-ignore`); `hx-history="false"` on the dock.
- One batched JMAP request per view (query → get → thread → get via result references); per-user `JmapClient` kept warm with a cached session and state strings.
- View transitions only on list ⇄ conversation navigation (`transition:true`), never global.

### 6.5 Live updates
Per-user EventSource listener (started on first request after login, stopped after 30 min idle) → per-user SSE hub → each tab's `/events` stream (in-house bridge: `EventSource` → `htmx.trigger(document.body, "mail:changed", detail)`); coalesced 400 ms; the list re-GETs itself at position 0 with the current page size and morphs — new rows appear in place, no "load new mail" pill; unread counts and the document title `(3) Inbox — Mailosh` update; a polite live region announces "3 new messages". Browser `EventSource` reconnects itself; the SSE `id:` carries the JMAP state so the server can replay via `Email/changes`; 120 s polling while disconnected; reconnect banner per §5.4.

> **AS-BUILT amendment (Task 8 review).** "Re-GETs itself at position 0 with the current page size" was wrong on both halves and is superseded by what shipped:
>
> * **Position.** The list re-GETs at its **current `position`**, not literal `0`. Painting page 1 under a URL that says page 2 is a bug, not a refresh.
> * **Limit.** The page size is **`max(page.limit, rows currently rendered)`**. The literal reading truncated an endlessly-scrolled list: after the sentinel had appended to 150 rows, an incoming message re-fetched 50 and morphed the list back to one page, clamping `scrollTop` and re-triggering the sentinel — so every arriving message churned the list under a reader.
> * **Known ceiling.** `MAX_PAGE_SIZE = 100` clamps that re-fetch, so a list scrolled past 100 rows still truncates to 100. Accepted for Phase 1A: re-fetching 300+ rows on every incoming message is the wrong trade. The real fix is to **merge rather than replace** on a live update, tracked for 1E.
>
> Also as-built: an open `/events` stream pins its pooled JMAP client (`pool.streaming`) so the idle sweep cannot close a client a listener is reading, while `drop`/`close_all` still do — session teardown must always kill the client. When a listener does die, `SseHub.upstream_lost()` ends every open subscriber stream so the browser re-dials rather than sitting silently on a dead connection.

## 7. Conversation view

- Route `/t/{threadId}` (full page and HTMX partial); pushes URL; back arrow + `u` hint; the same action bar as the list; "4 of 1,284" with ‹ › for prev/next and auto-advance.
- Subject + label chips; messages as cards, newest at bottom; expanded = unread-when-opened ∪ last ∪ single (snapshot `wasUnread` so marking read does not collapse under the reader); older messages collapse to one line (avatar, name, snippet, date); `;`/`:` expand/collapse all. Mark read on open (delay setting: immediately / 1 s / 3 s / never). Opens scrolled to the first unread.
- Per message: avatar (initial on a deterministic colour), name + address, "to me ▾" details popover (from/to/cc/date/mailed-by/signed-by), timestamp, star, reply, ⋮ (reply all, forward, mark unread from here, show original, print, delete message).
- Quoted text: hidden behind a `•••` pill using ihasmail's selector list (`.gmail_quote`, `blockquote[type=cite]`, `.moz-cite-prefix`, `#divRplyFwdMsg`, `.yahoo_quoted`, `div[id^=appendonsend]`, `.ms-outlook-mobile-reference-message`, `#OLK_SRC_BODY_SECTION`, `.protonmail_quote`, `.mailosh_quote`) plus the "wrote:" / "Original Message" heuristics; plain text uses `findQuoteStart` with depth classes.
- **HTML pipeline** (server): extract `<style>` blocks → sanitize with `tinycss2` (drop `@import`/`@font-face`, allow-list properties, block `url(`/`expression(`/`behavior:`/`position:fixed|sticky`) → `nh3` with the email allow-list (ihasmail's FORBID/ADD lists translated; `url_schemes {http,https,mailto,cid,data}` with `data:` only for `img` + `image/*`; `url_relative="deny"`; `attribute_filter` rewrites `cid:` → `/m/{id}/cid/{cid}` and remote `src`/`href` per the image policy; every link `target=_blank rel="noopener noreferrer nofollow"`; `background`/`srcset`/`poster` never allowed) → wrapper document with `<meta name=color-scheme>`, base CSS (`* {max-width:100%}`, blocked-image placeholder), and one hash-pinned resize script.
- **Delivery:** `GET /m/{id}/html?remote=0|1&theme=light|dark` served with `Content-Security-Policy: sandbox allow-popups allow-popups-to-escape-sandbox; frame-ancestors 'self'; default-src 'none'; img-src data: [https: http: when remote=1]; style-src 'unsafe-inline'; script-src 'sha256-<resize>'` and `Referrer-Policy: no-referrer`; embedded via `<iframe sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer">` (never `allow-same-origin`). Auto-height via the resize script's `postMessage`; the parent accepts only `event.source === frame.contentWindow && event.origin === "null"`, clamps 200–20 000 px.
- **Remote images:** blocked by default; banner "This message has N remote images · Show images · Always show from {sender}"; policy `ask / always / contacts` (contacts = harvested addresses you have written to); allowed images load through `GET /img?u=<signed>` (server fetches with no referrer/cookies, caps size/time, streams) so the sender never sees the reader's IP. Per-sender allow list in Postgres.
- **Dark restyle:** if the mail declares `color-scheme` (meta or CSS) just set `color-scheme: light dark`; else if the body/first-table background is light, apply `html{filter:invert(1) hue-rotate(180deg)} img,[style*="background-image"]{filter:invert(1) hue-rotate(180deg)}`; banner toggle "Show original", remembered per sender. Default follows the app theme; setting to disable.
  - **Amended in implementation (Phase 1B):** the two controls this section names "Show original" — this banner toggle, and the ⋮ menu's raw RFC 5322 source — sit in the same message card, and one name for two different effects is an accessibility failure: a screen-reader user hears it twice and can guess neither. The banner's is **"Original colours"** and the menu's is **"View source"**. Do not fold them back together.
- Attachments: chips (icon by type, name, size) with hover download / open; preview dialog for images, PDF (native viewer in a sandboxed iframe), text; `cid:` parts excluded from the chips list; "Download all" deferred.
- Plain-text bodies: escaped, linkified (http/https/mailto), quote depth colours, `white-space: pre-wrap`. Print: `GET /t/{id}/print` opens a printable page.

## 8. Compose

- **Dock** 560 × 520 fixed bottom-right, header "New message" with minimize (280 × 44 bar), full-screen (inset 24 px), close; up to 3 docks tile leftwards; full-screen automatically under 768 px. Docks are a sibling of the list target (`#compose-dock`), never inside it; excluded from morph and history; the inbox behind stays fully interactive.
- Component: `Alpine.data("compose")` owning a Squire instance in a closure (`init` → `new Squire(root, {sanitizeToDOMFragment: DOMPurify})`, `destroy` → `editor.destroy()`); Alpine auto-initialises swapped content (no `htmx:load` glue).
- Fields: To (chips with avatar, `, ; Enter Tab` commit, Backspace re-edits, paste parses lists, external-domain hint), Cc/Bcc revealed on click or `⌘⇧C/B`, From (identity picker when > 1), Subject. Autocomplete from the harvested contacts table (rank prefix → word → substring, recency/frequency boost, 8 results, 120 ms debounce).
- Bottom toolbar (Gmail order): **Send** + ▾ (Send & archive; Schedule send only when the JMAP session advertises `maxDelayedSend`/FUTURERELEASE), formatting popover (B I U, size, bullets/numbers, quote, link, clear), attach (XHR upload with per-file progress through `POST /attachments` streaming to Stalwart's `uploadUrl`; 25 MB soft warning), link (`⌘K`), plain-text toggle, ⋮ (discard); autosave state ("Saving…"/"Saved") at the right; trash to discard.
- Drafts: autosave 2 s after the last edit (`hx-trigger="input delay:2s"`, `hx-sync="this:replace"`); JMAP bodies are immutable, so each save is `Email/set` create (`$draft`) + destroy previous; drafts reopen from the Drafts folder into a dock.
- Reply / Reply all / Forward: inline composer card at the thread's end (same component) with quoted content in `<div class="mailosh_quote gmail_quote">` under an attribution line ("On Sep 1, 2026 at 8:41 PM, Daniel Okafor wrote:"); pop-out to dock; forward includes attachments. Default reply behaviour setting (reply vs reply all).
- Send: `EmailSubmission/set` with `onSuccessUpdateEmail` (Phase 0 client) after the undo window; "Sending… Undo" toast; success toast "Sent" with "View"; failures reopen the dock with the error. Forgot-attachment check (regex on "attached/attachment") prompts once. Identities and HTML signatures from settings; signature inserted above the quote.

## 9. Auth, sessions, security (amends Phase 0 spec §9)

- **Login** page (username/email + password, "Keep me signed in"). Verification: fetch the JMAP session from Stalwart with basic auth and check `username`/`accounts` in the body (Stalwart returns 200 for anonymous sessions). On success, mint a **per-session Stalwart API key** via the admin client (`x:ApiKey/set` — spike SPK-3) scoped to the user's account, encrypt it with Fernet (key derived from `MAILOSH_SECRET_KEY`), store it in the session row, and discard the password. Logout destroys the API key and the row; "Sign out everywhere" destroys all.
- **Sessions:** Postgres table (`id = secrets.token_urlsafe(32)`, user, created/last-seen, UA, IP, encrypted credential); cookie `__Host-sid` (`Secure` when https, `HttpOnly`, `SameSite=Lax`, `Path=/`); id regenerated at login; idle expiry 14 days sliding (30 with "keep me signed in"), absolute 90 days.
- **CSRF:** per-session token in `<meta name="csrf-token">` sent through inherited `hx-headers` (`X-CSRF-Token`) and as a hidden field in plain forms; compared with `secrets.compare_digest`; unsafe methods also reject `Sec-Fetch-Site: cross-site`. `HX-Request` alone is not trusted.
- **Rate limiting** before Stalwart is contacted (Stalwart auto-bans a source IP after 100 failures/day — the webmail host must never trip it): Postgres-backed counters, 5 failures per account and 20 per IP per 15 min with exponential backoff; generic error copy; audit log rows for login success/failure/logout.
- **Deployment note (Phase 2 owns the wizard, but the compose file changes now):** Stalwart's HTTP port is reachable only from the `mailosh` service network, not the host; `useXForwarded` for real client IPs; dev compose keeps 8080 exposed behind a `dev` profile.
- Per-user runtime: a `JmapClient` per session (LRU, idle-evicted) built from the decrypted API key; per-user EventSource listener and hub keyed by user id (§6.5). Single uvicorn worker in Phase 1 (documented); Postgres `LISTEN/NOTIFY` fan-out is the Phase 2 path to multiple workers.
- Content security for the app itself: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'` (mail frames carry their own stricter header); `X-Content-Type-Options: nosniff`; `Referrer-Policy: same-origin`.

## 10. Organise, find, settings

- **Labels = JMAP mailboxes.** Create / rename / nest (`parentId`) / delete (conversations keep their other labels; confirm) via `Mailbox/set`; colour, visibility (show / hide / show if unread), pinned order in Postgres `label_meta` keyed `(user, account, mailbox_id)`. System mailboxes by role. Label picker (`l`) = popover with search, checkboxes for multi-apply, type-to-create; Move (`v`) = single choice; both also as ⌘K sub-modes; drag conversations onto nav labels (1E).
- **Search.** Pill input; focus shows recent searches and operator hints; typing suggests labels/senders; results page reuses the list component with `SearchSnippet/get` highlights and a chips row (From · To · Any time ▾ · Has attachment · Is unread · Label ▾) that edits the query; advanced panel (From, To, Subject, Has the words, Doesn't have, Size, Date within, Search in, Has attachment). Default scope: everything except Spam/Trash. Grammar (Python parser, no dependency): free text (→ `text`), quoted phrases, `-` negation on any term, `OR`, parentheses, `from: to: cc: bcc: subject: body:`, `has:attachment`, `is:unread|read|starred`, `in:inbox|sent|drafts|archive|spam|trash|anywhere`, `label:`, `before: after:` (`YYYY-MM-DD`, `YYYY/MM/DD`, `M/D/YYYY`), `older_than: newer_than:` (`Nd|Nw|Nm|Ny`), `larger: smaller:` (`k/m/g`); `filename:` approximated via `text` with a hint; unknown operators produce an inline hint, never a 500.
- **Settings** (`/settings/*`): Appearance (theme, density, reading pane, font size), Reading (conversation view on/off, mark-read delay, auto-advance, remote images policy, dark restyle default), Compose (undo-send window, default reply, signatures per identity, default From), Labels (manage), Account (display name, identities), Security (active sessions with sign-out, sign out everywhere). **Quick settings** panel from the gear: theme, density, reading pane, conversation view — applied live with optimistic preview and persisted.

## 11. Responsive, accessibility, performance

- Breakpoints per §4.3; under 768 px: drawer nav, single-pane list ⇄ conversation, full-screen compose, bottom action bar with safe-area padding, 44 px touch targets.
- WCAG 2.2 AA: contrast checked for both themes (4.5:1 text, 3:1 UI/focus); grid semantics and roving tabindex (§5.3); live regions for toasts and new mail; native `<dialog>` for modals (palette, previews, confirmations) with focus return; skip link; every icon button labelled; keyboard-operable everything (checkboxes and hover actions are real buttons, not hover-only).
- Budgets: partial TTFB < 200 ms, swap paint < 100 ms, INP < 200 ms, first-load LCP < 1.5 s on LAN; total JS ≤ 90 KB gz (htmx 16 + idiomorph 3.4 + preload 1.5 + Alpine 19.4 + Squire 18 + DOMPurify 10.6 + command-score 1 + ours ≤ 15), CSS ≤ 30 KB gz, one font file 48 KB. `web-vitals` logged in dev. Static assets content-hashed (`name.<sha256:8>.ext` + manifest) and served `immutable`; HTML `no-cache` + `Vary: HX-Request`; Caddy (`deploy/Caddyfile`) terminates TLS/h2 with `encode zstd gzip` and `file_server precompressed`.

## 12. Architecture and code structure (extends Phase 0 spec §3)

```
mailosh/
  web/            FastAPI routers: auth, shell, mail (list), thread, compose, labels, search, settings, frames (mail frame documents, img proxy, cid parts), events (SSE)
  services/       view-models & orchestration over JmapClient: mailbox_tree, thread_list, conversation, drafts, submission, contacts_harvest
  render/         sanitizer pipeline: html_sanitize (nh3 config), css_sanitize (tinycss2), quote_trim, plain_text, frame_document, image_policy
  search/         grammar parser → JMAP FilterOperator tree; chip/advanced-panel ↔ query round-trip
  security/       sessions, csrf, ratelimit, crypto (Fernet), passwords→api-key exchange
  db/             SQLAlchemy 2 async models + Alembic migrations (app_user, session, label_meta, ui_pref, contact, image_sender_allow, login_attempt, audit_log)
  jmap/           Phase 0 client + additions: mailbox set/update, search snippets, capability probing, per-user client cache
  ui/             Jinja environment, macros (icon, kbd, chip, avatar, relative_time), template filters
  web/templates/  layouts/, auth/, shell/, list/, thread/, compose/, labels/, search/, settings/, fragments/ (toast, status, empty)
  web/static/     js/ (app.js stores, keys.js registry, palette.js, compose.js, frame.js, sse.js), vendor/, icons/, fonts/, styles → app.css
```
Rules: routers stay thin (parse → service → template); services return plain dataclasses/Pydantic view-models; templates contain no logic beyond loops/conditions; JS modules are plain ES modules with no build step; Postgres holds app state only (never mail content); every mutation route is POST + CSRF and returns 204/`HX-Trigger` or a fragment.

Error taxonomy end to end: `JmapError`/`TransportError` → exception handler → toast fragment (HTMX) or error page; auth failures → 401 + `HX-Redirect`; validation → 422 with inline field errors.

## 13. Testing

- Python unit: search grammar (property-based), sanitizer (XSS corpus incl. `javascript:`, `expression(`, `url(`, CSS `@import`, nested `</style`), css sanitizer, quote trimming fixtures (one per client family), relative time, sessions/CSRF/rate-limit, palette action registry serialization, view-models with FakeClient.
- Route tests (TestClient + FakeClient): every route's success, 401, 422 and JMAP-error paths; the HX-Request partial vs full page split; CSRF rejection; optimistic-response headers.
- Integration (live Stalwart, `-m integration`): login → list → open → archive/undo → compose → send → draft autosave → label create/apply → search — one hermetic scenario per plan, self-cleaning with per-run ids (fixes the Phase 0 non-hermetic test).
- Browser QA per plan via the Chrome MCP tooling: keyboard-only triage, dark/light, three densities, compose flows, screenshots attached to the plan's report. `web-vitals` numbers recorded in the findings doc.

## 14. Phase 1 plans (each produces working, reviewable software)

| Plan | Scope | Exit criterion |
|---|---|---|
| **1A Foundation** (~2 wk) | Postgres models + migrations; login/logout/sessions/CSRF/rate limit; per-session API-key exchange; design system (tokens, themes, density, Inter, icons, macros); app shell (top bar, nav with mailboxes + labels + counts, list-first main); list rows with full anatomy; selection toolbar + bulk actions; keyboard registry + core map + `?` overlay; ⌘K v1 (actions, go-to, search fall-through); undo toasts; optimistic archive/star/read/delete; per-user SSE + in-house bridge; global error surface; empty/loading states; NOTICE updated for the bridge | log in, triage 1,000 messages with the keyboard alone, every action reversible, dark/light, three densities, no 500 ever reaches the UI |
| **1B Reading** (~1.5 wk) | conversation view; sanitizer pipeline + frame delivery + resize; remote-image gate + proxy + per-sender allow; dark restyle; quote trimming; attachments + previews; per-message actions; mark-read delay; auto-advance; print | open any real-world HTML mail (Gmail/Outlook/Apple/newsletter corpus) safely and legibly in both themes |
| **1C Compose** (~1.5 wk) | dock + lifecycle; Squire toolbar; recipients + harvested contacts; attachments with progress; drafts autosave; reply/reply-all/forward + inline composer; identities/signatures; undo send; send & archive; schedule send (capability-gated) | write, save, reopen, attach, send, undo within the window; inbox stays live behind the dock |
| **1D Organise & find** (~1.5 wk) | labels CRUD/colour/nesting/visibility; picker + move + ⌘K modes; search bar, chips, advanced panel, grammar → JMAP, snippets; settings pages + Quick settings live preview | Gmail operators return the right results with highlights; labels behave like Gmail's |
| **1E Polish & release** (~1 wk) | responsive/mobile; a11y audit fixes; performance pass (preload, morph audit, hashing, Caddy, budgets measured); first-run coaching (three tips, `?` nudge); drag-and-drop to labels; browser QA matrix; compose hardening (Stalwart HTTP internal-only, `dev` profile exposes it); docs; tag `v0.2.0` | budgets met and recorded; keyboard-only and screen-reader passes; a fresh clone runs `make up` to a working, themed webmail |

Order is fixed (1A → 1E); each plan gets its own implementation plan document and review cycle.

## 15. Risks and verification points

- **Overlay compose lifecycle** (SPK-1 was conditionally closed): 1C's first task is a spike-grade test that a Squire dock survives list morphs, SSE refreshes and a `u` navigation. Fallback: `hx-preserve` on the dock, then a Preact island if still fragile.
- **CSP-sandboxed frame + resize script**: 1B's first task proves the hash-pinned script works in Chrome/Firefox/Safari and that `allow-same-origin` is never needed.
- **Stalwart API keys per session**: 1A verifies create/destroy semantics and that key count per account has no low cap; fallback is one key per user with session-scoped revocation in Postgres.
- **Search snippets**: verify Stalwart implements `SearchSnippet/get`; fallback = server-side highlighting of the preview.
- **FUTURERELEASE**: read `maxDelayedSend` from the session; hide Schedule send when 0.
- **Font subsetting** without Node: use `fonttools` (`pyftsubset`) in the Makefile (dev-only Python dep).
- **Single worker**: fine for Phase 1 scale; documented; NOTIFY fan-out in Phase 2.
- **Design drift**: every plan's browser QA compares against the approved mockup screens, which 1A copies into `docs/design/mockups/` so they are versioned rather than living in untracked scratch.

## 16. Open items (do not block 1A)
Inbox-zero illustration (design it in 1E); exact contact-harvest retention window (default: last 12 months of Sent + From/To of read mail); whether to show unread or total counts on labels (default unread, Gmail-style).
