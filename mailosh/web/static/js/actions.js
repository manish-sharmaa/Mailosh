// Mailosh — triage actions, the undo toast, and the bulk confirmation.
//
// Design spec §6.3. The six routes in `mailosh/web/actions.py` answer
// `204 No Content` (or `409`) with their whole result in an `HX-Trigger`
// response header and no body at all, so everything a reader sees after
// clicking Archive is built here: the row leaving, the nav badge moving,
// the toast, and the Undo that reverses it.
//
// **Undo is an immediate commit plus a reverse operation, not a delayed
// one.** By the time the toast appears the mail has already moved; "Undo"
// POSTs a signed token to `/a/undo`, which puts every message back in the
// mailboxes it came from. So the toast is never a cancellation window and
// its copy never promises one — every toast string the server sends is
// past tense ("Archived", "Deleted", "Marked as read"), and the wording
// this file owns follows suit. Only *send* uses a delayed commit, and send
// is not in this phase.
//
// ---------------------------------------------------------------------
// The click model
//
// One delegated `click` listener on `document.body`, keyed off
// `data-action`. That is the same delegated model app.js uses, in the same
// place, and it works now for a reason worth writing down: nothing stops a
// control's click any more. The row's stretched anchor (`.row-link`) is a
// *sibling* of the controls and paints beneath them, so a click on a
// button never reaches it, and the `stopPropagation()` calls that used to
// stand in for that are gone. app.js's own listener is capture-phase
// purely so it can beat htmx's listener *on the anchor*; nothing here
// needs to beat anyone, so this one is an ordinary bubble-phase listener.
// `document.body` rather than `#list` because a nav or pager swap replaces
// `#main`, and `#list` goes with it.
//
// ---------------------------------------------------------------------
// Transport: `fetch`, not `htmx.ajax`
//
// The responses have no body — there is nothing to swap, so htmx's entire
// contribution would be its request lifecycle. Against that, the 409
// confirmation path needs the *status and headers of one specific
// request*: the server deliberately does not echo the selected ids back
// (a few thousand of them in a response header is exactly what the byte
// budget exists to prevent), so the client re-posts from the selection it
// still holds. Through htmx that correlation is a global event and a
// module-level "last request" variable, which two overlapping actions
// would race. Here the response is simply the value the `await` returns.
//
// CSRF rides on `X-CSRF-Token` read from the `<meta>` the layout renders —
// the same token htmx sends via `hx-headers` — and `HX-Request: true` is
// sent for one reason only, documented at `post()`.
//
// ---------------------------------------------------------------------
// CSP
//
// `script-src 'self'` with no `'unsafe-eval'`. That constrains *Alpine
// directives* (app.js's header documents the grammar its CSP build
// accepts) and it rules out htmx's `hx-on:`, `hx-vals='js:…'` and
// `hx-trigger="…[expr]"`, all three of which compile attribute text with
// `new Function`. It does not constrain this file: a module served from
// our own origin is ordinary JavaScript. Everything below is bound with
// `addEventListener`, and no markup here carries behaviour.

// `openShortcuts` is imported rather than re-implemented so the top bar's
// Help button and the `?` key open the *same* overlay, filled from the
// same registry. The listener that calls it is at the bottom of this
// file, beside the other delegated ones — this module owns the ordinary
// bubble-phase `click` listener on `document.body`, and a second file
// binding a second one to reach one button would be a third click model.
import { openShortcuts } from "./keys.js";

// ---------------------------------------------------------------------
// The wire contract (see `mailosh/web/actions.py`)
//
// `HX-Trigger` is JSON. Because the whole header is budgeted against
// nginx's 4 KB `proxy_buffer_size`, an oversized payload sheds fields, and
// the KEYS differ between the resulting shapes, not just the values:
//
//   undo kept        counts, removed, toast, undo
//   rows shed        counts, refresh, removed, toast, undo
//                    (removed = [], refresh = true)
//   undo shed        counts, refresh, removed, toast, undo, undo_unavailable
//                    (undo = null, undo_unavailable = "too_many")
//   nothing changed  counts, removed, toast, undo, undo_unavailable
//                    (undo = null, undo_unavailable = "no_change")
//
// and `/a/undo`'s own reply is narrower still — `{toast, refresh}`, with
// no `undo`, `removed` or `counts` key at all. So every read below is
// defaulted (`?? []`, `?? {}`, `?? null`) and none of them assumes a key
// exists.
//
// Two traps, both of which review flagged as easy to get wrong:
//
// 1. `undo_unavailable` is **absent**, not null, whenever undo is present.
//    It is asymmetric with `undo`, which is always present (sometimes as
//    null). Test the key, never `=== null`.
// 2. `"no_change"` **contradicts the toast beside it**: starring an
//    already-starred message answers `toast: "Starred"` together with
//    `undo_unavailable: "no_change"`. The action did what was asked; there
//    is simply nothing to reverse. So only `"too_many"` is ever explained
//    (see `UNDO_UNAVAILABLE_NOTE`) — telling someone why a button they
//    were not looking for is missing is noise, not helpfulness.

// ---------------------------------------------------------------------
// The failure contract (`om:error`, see `mailosh/web/app.py`)
//
// **A failed request answers `200`.** Design spec §9 says a `JmapError`/
// `TransportError` must never reach a caller as a bare 500, so the app's
// exception handlers answer an HX request — which every request this file
// makes is, see `post()` — with `200`, `HX-Reswap: none`, no body, and
//
//   HX-Trigger: {"om:error": {"toast": "…", "retry": true}}
//
// `RequestValidationError` sends the same event on its own 422.
//
// That inverts the check this file used to make. `response.ok` is **true**
// on a JMAP failure, so `!response.ok` is not what a failure looks like
// any more; the presence of `om:error` is. Reading only `response.ok` left
// an archive that never happened painted as though it had — and, worse,
// ran `leaveConversation()` on the way out, bouncing the reader out of a
// conversation whose mail never moved, exactly as if it had worked. For a
// mail client that is the worst available failure shape. So `om:error` and
// `!response.ok` are checked together, in one guard, before anything reads
// a response as a success.
//
// `retry` is deliberately **not** acted on. It says the failure is
// transient (a dead mail server, not a malformed request), and nothing in
// this phase re-issues a *write*: the six routes are not idempotent from
// the client's side — it holds no request id the server could de-duplicate
// on — so a silent second attempt could move mail the reader has just been
// told did not move. The retrying this app really does is on the read path
// (`refreshList()` below; sse.js's 120 s polling while the stream is down)
// and needs no flag to decide to do it. The honest consumer for `retry` is
// an explicit "Try again" affordance on the toast, which is not in this
// phase.

/** Route per action kind. */
const ROUTES = {
  archive: "/a/archive",
  delete: "/a/delete",
  spam: "/a/spam",
  star: "/a/star",
  unstar: "/a/star",
  read: "/a/read",
  unread: "/a/read",
};

// The `on` form field for the two routes that take a direction. Folded
// into the kind rather than passed as a separate flag so that one name
// means one operation everywhere it is written down — the `data-action`
// hook, `om.act()`, Task 10's key binding and Task 11's palette entry all
// say "unread", never `("read", {on: 0})`.
const ON = { star: "1", unstar: "0", read: "1", unread: "0" };

/** Kinds whose rows leave the list, and which therefore collapse. */
const REMOVES_ROWS = new Set(["archive", "delete", "spam"]);

// The client owns every word below. `undo_unavailable` is a stable code,
// never display copy (that is what keeps a copy edit or an i18n pass from
// being a server change), so this map is the only place its wording
// exists — and it deliberately holds one entry. A `Map`, not an object
// literal, so a code that happened to name an `Object.prototype` member
// could never resolve to a function.
const UNDO_UNAVAILABLE_NOTE = new Map([["too_many", "Too many messages to undo"]]);

/** What the toast says when the request itself failed. */
const FAILED = {
  archive: "Couldn't archive",
  delete: "Couldn't delete",
  spam: "Couldn't report spam",
  star: "Couldn't star",
  unstar: "Couldn't unstar",
  read: "Couldn't mark as read",
  unread: "Couldn't mark as unread",
  undo: "Couldn't undo",
};

/** Last resort for an `om:error` that arrived with no `toast` of its own.
 *  The server always sends one; a proxy rewriting the header, or a future
 *  sender that forgets, must still not produce a silent failure. */
const UNEXPLAINED_FAILURE = "Something went wrong";

/** How long an action's own failure toast speaks for the whole page.
 *
 *  `run()`'s failure path calls `refreshList()`, and on a dead mail server
 *  that refetch fails too — with the same `om:error`, this time through
 *  htmx, which the body listener at the bottom of this file would toast a
 *  second time. One click, one toast: the generic listener stays quiet
 *  while an action is already reporting the same outage. Longer than a
 *  refetch's round trip, far shorter than the toast's own 10 s life. */
const ERROR_QUIET_MS = 3000;

/** The confirm dialog's action button. The server sends the *question*; the
 *  button's verb is the client's, and has to name what is about to happen. */
const CONFIRM_LABEL = {
  archive: "Archive",
  delete: "Delete",
  spam: "Report spam",
  star: "Star",
  unstar: "Unstar",
  read: "Mark as read",
  unread: "Mark as unread",
};

// Spec §6.3's undo window. The signed token itself lives 60 s
// (`services/undo.py`), deliberately longer, to cover a slow round trip —
// but the *offer* is what the reader can see, so the toast's lifetime and
// `undoLast()`'s cutoff are the same number, declared once, here.
const UNDO_WINDOW_MS = 10000;

/** Row collapse before removal — spec §6.3's 160 ms, `--dur-2`. */
const COLLAPSE_MS = 160;

const LIST = "#list";
const ROW_SELECTOR = "#list [data-id]";

/** What the optimistic star may paint: a list row, or one conversation card
 *  (`thread/message.html`'s `<article class="msg">`). Both render the same
 *  `aria-pressed` toggle, and both now resolve to *themselves* — see
 *  `targets()` — so both can be painted before the server answers. */
const STAR_SELECTOR = "#list [data-id], article.msg";

const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)") ?? null;

/** `ms`, or 0 when the reader asked for no motion — a collapse nobody can
 *  see must not still cost the delay it was buying. */
function motionMs(ms) {
  return reduced?.matches ? 0 : ms;
}

// Read at call time, never cached: this module is evaluated before app.js
// registers the stores (layouts/app.html loads them in that order), and a
// page whose Alpine failed to load must degrade rather than throw.
// Duplicated from app.js rather than imported, so neither file's load
// order depends on the other's.
function store(name) {
  return window.Alpine?.store?.(name) ?? null;
}

// ---------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
}

/** POST `values` to `url` as a form body. Array values repeat their field,
 *  which is how the routes take a selection (`ids=a&ids=b`). */
function post(url, values) {
  const body = new URLSearchParams();
  for (const name of Object.keys(values)) {
    const value = values[name];
    if (Array.isArray(value)) for (const item of value) body.append(name, item);
    else body.append(name, value);
  }
  return fetch(url, {
    method: "POST",
    body: body,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRF-Token": csrfToken(),
      // Not a CSRF signal — `security/csrf.py` never trusts this header,
      // by design. It is here because an expired session raises
      // `SessionRequired`, whose handler answers a *303 to /login* for an
      // ordinary request and `401` + `HX-Redirect` for an HX one. `fetch`
      // follows redirects, so without this an expired session would hand
      // back a perfectly successful-looking 200 full of login HTML and
      // this file would read it as "the archive worked".
      "HX-Request": "true",
    },
  });
}

/** The `name` entry of a response's `HX-Trigger` header, or null. */
function trigger(response, name) {
  const raw = response.headers.get("HX-Trigger");
  if (!raw) return null;
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const value = parsed?.[name];
  return value !== null && typeof value === "object" ? value : null;
}

/** True if `response` was the login redirect in disguise, in which case the
 *  navigation to the login page has already started.
 *
 *  A 401 without the header is not claimed: it is a failure nobody has
 *  explained, and falling through to the ordinary error path says so
 *  rather than leaving the reader with a click that did nothing. */
function handledExpiredSession(response) {
  if (response.status !== 401) return false;
  const location = response.headers.get("HX-Redirect");
  if (!location) return false;
  window.location.assign(location);
  return true;
}

// ---------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------

function rows() {
  return Array.from(document.querySelectorAll(ROW_SELECTOR));
}

/** The open conversation, on a page that is showing one instead of a list.
 *
 *  `thread/page.html` carries the same `data-email-ids` a row does, on its
 *  `.thread-scroll`. Keyed off the *absence* of `#list` rather than off a
 *  class name: the two views never coexist, and matching a bare
 *  `[data-email-ids]` on the list page would silently resolve to its first
 *  row — an action aimed at a conversation nobody chose. */
function openConversation() {
  if (document.querySelector(LIST) !== null) return null;
  return document.querySelector("[data-email-ids]");
}

/** The rows an action with no explicit target applies to: the selection,
 *  or — with nothing selected — the row under the cursor. That is what a
 *  keyboard shortcut or a palette entry means by "this conversation", and
 *  it is why `e` works before you have selected anything.
 *
 *  Off the list, "this conversation" is the one being read, which is how
 *  the thread view's own action bar reaches the same six routes without a
 *  second dispatcher. */
function defaultTargets() {
  const selected = rows().filter((el) => el.getAttribute("aria-selected") === "true");
  if (selected.length > 0) return selected;
  const focused = rows().filter((el) => el.classList.contains("is-focused"));
  if (focused.length > 0) return focused;
  const conversation = openConversation();
  return conversation === null ? [] : [conversation];
}

/** Every message id the given rows cover, de-duplicated in order.
 *
 *  `data-email-ids` is the whole conversation, not just the messages
 *  visible in this mailbox (`data-count`) — archiving a row archives the
 *  thread, which is what the row represents. */
function emailIds(elements) {
  const ids = [];
  for (const el of elements) {
    for (const id of (el.dataset.emailIds ?? "").split(",")) {
      if (id !== "" && !ids.includes(id)) ids.push(id);
    }
  }
  return ids;
}

// Bumped by anything that re-renders the list from the server. A pending
// collapse captures the value and abandons itself if it changed:
// idiomorph reuses a row node whose `id` survived the swap, so a timeout
// firing after a refetch would delete a row the server had just said is
// still there.
let listGeneration = 0;
document.body.addEventListener("htmx:afterSettle", () => {
  listGeneration += 1;
  conversationSettled();
});

/** Re-fetch the list from the server — the answer to `refresh: true`, and
 *  the one revert this file needs. `#list` listens for `mail:changed from:
 *  body` and re-GETs itself, and the `/rows` fragment carries the nav and
 *  the document title out of band, so one event puts the rows, the badges
 *  and the title all back in agreement with the server. */
function refreshList() {
  listGeneration += 1;
  window.htmx?.trigger?.(document.body, "mail:changed", {
    types: ["Email", "Mailbox"],
    id: null,
    catchup: true,
  });
}

/** Collapse `elements` out of the list, then reconcile selection and focus.
 *
 *  Focus deliberately lands on whatever now occupies the removed row's
 *  position — app.js's `ensureFocus(previous)` — which is what makes
 *  "archive, archive, archive" work without the mouse. It only takes real
 *  DOM focus if the list already had it, and by the time this runs the
 *  clicked button is gone, so a mouse-driven archive does not steal it. */
function collapseRows(elements) {
  const live = elements.filter((el) => el.isConnected);
  if (live.length === 0) return;
  const list = store("list");
  const previous = list?.ids() ?? null;
  const generation = listGeneration;
  const gone = live.map((el) => el.dataset.id);
  for (const el of live) el.classList.add("is-leaving");
  setTimeout(() => {
    if (generation !== listGeneration) return;
    for (const el of live) el.remove();
    if (list) {
      for (const id of gone) list.selected.delete(id);
      list.ensureFocus(previous);
    }
    // The empty state, and how many rows a page should hold, are the
    // server's to decide: once nothing is left, ask for the list rather
    // than leaving a blank grid where "You're all caught up" belongs.
    if (document.querySelector(LIST) !== null && rows().length === 0) refreshList();
  }, motionMs(COLLAPSE_MS));
}

/** Apply the response's `removed` (thread ids). Rows an optimistic pass
 *  already took out are simply not there, which is why this matches on the
 *  live set rather than removing by id. */
function removeThreads(threadIds) {
  if (threadIds.length === 0) return;
  const wanted = new Set(threadIds);
  collapseRows(rows().filter((el) => wanted.has(el.dataset.id)));
}

/** The nav link for a mailbox key, matched by href rather than built into
 *  a selector so a key never has to be escaped into one. */
function navLink(key) {
  const href = "/mail/" + key;
  for (const link of document.querySelectorAll('#nav a[href^="/mail/"]')) {
    if (link.getAttribute("href") === href) return link;
  }
  return null;
}

/** Apply `counts` — per-mailbox badge deltas, e.g. `{"inbox": -3}` — to the
 *  nav.
 *
 *  Only two badges are ever keyed here (`services/actions.py`): Inbox
 *  counts unread messages, Drafts counts all of them, and a delta that
 *  would not move a badge is omitted server-side rather than sent as 0.
 *  `shell/nav.html` renders no `<b>` at all at zero, so this both removes
 *  the badge on the way down and creates it on the way back up. */
function applyCounts(counts) {
  for (const key of Object.keys(counts)) {
    const delta = counts[key];
    if (!delta) continue;
    const link = navLink(key);
    if (link === null) continue;
    let badge = link.querySelector(".nav-count");
    const next = Math.max(0, (Number.parseInt(badge?.textContent ?? "0", 10) || 0) + delta);
    if (next === 0) {
      badge?.remove();
      continue;
    }
    if (badge === null) {
      badge = document.createElement("b");
      badge.className = "nav-count";
      link.append(badge);
    }
    badge.textContent = String(next);
  }
}

// ---------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------

/** Show a toast through the `ui` store, degrading to the live region if
 *  Alpine is not there — a failed action must still be announced. */
function toast(message, undoToken = null, note = null) {
  const ui = store("ui");
  if (ui?.toast) return ui.toast(message, undoToken, note, UNDO_WINDOW_MS);
  const status = document.getElementById("status");
  if (status !== null) status.textContent = note ? message + ". " + note : message;
  return null;
}

// When something last told the reader a request had failed. Both failure
// paths write it and both read it, so one outage is one toast.
let lastFailureAt = 0;

/** The whole answer to a failed action: put the rows back, say so, and ask
 *  the server what is actually true.
 *
 *  Two reverts, in that order and both needed. `revert` is
 *  `applyOptimistic`'s own inverse and lands immediately — it is the only
 *  one that works when the mail server is the thing that is down, because
 *  the re-fetch is then failing too. `refreshList()` is the authoritative
 *  one: only the server knows what a partly applied `Email/set` left
 *  behind, and its answer overwrites the local guess whenever it arrives.
 *
 *  The wording is this file's `FAILED[kind]`, deliberately not the
 *  server's `om:error` copy: "Couldn't archive" names what the reader just
 *  did and says plainly that it did not happen, while the server's
 *  page-level sentence ends "— retrying", which after a click on Archive
 *  would promise a second attempt nothing here makes. It is also what the
 *  sibling `catch` in `run()` — no response at all — says for the same
 *  visible outcome, and one outcome must not have two vocabularies. */
function reportFailure(kind, revert = null) {
  revert?.();
  announceFailure(FAILED[kind]);
  refreshList();
}

/** Tell the reader something failed, and start the quiet window. Split
 *  out of `reportFailure` for `/a/undo`, which fails without changing
 *  anything and therefore has nothing to re-fetch. */
function announceFailure(message) {
  lastFailureAt = Date.now();
  toast(message);
}

// ---------------------------------------------------------------------
// Bulk confirmation (409)
// ---------------------------------------------------------------------

/** Ask before a selection over 100 messages (`BULK_CONFIRM_OVER`), and
 *  resolve to whether the reader agreed.
 *
 *  A native `<dialog>` opened with `showModal()`: the focus trap, the
 *  backdrop, the inert background behind it and the return of focus on
 *  close are all the platform's, and none of it needs markup in a shared
 *  template. The question is the server's (`message`); the action button's
 *  verb is the client's, because "Archive" tells the reader what is about
 *  to happen and "OK" does not. */
function confirmBulk(kind, ask) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm";

    // Every way out goes through here, and the first one wins: the answer
    // is given once, the dialog is closed before it is detached (removing
    // an open modal would leave the top layer holding a node that is no
    // longer in the document), and the node never outlives the question.
    //
    // The buttons settle directly rather than by closing and listening for
    // `close`. That indirection is one event deep and it is the *only*
    // path a bulk delete has, so it is not a place to be clever — and it
    // is genuinely unreliable: in a page Chrome has marked hidden (an
    // automated tab, a fully occluded window) `close` does not arrive, and
    // the dialog stays on screen with both buttons dead. `close`/`cancel`
    // stay bound for `Esc` and for any programmatic close, with a keydown
    // of last resort behind them for the same reason.
    let settled = false;
    const finish = (agreed) => {
      if (settled) return;
      settled = true;
      if (dialog.open) dialog.close();
      dialog.remove();
      resolve(agreed);
    };

    const text = document.createElement("p");
    text.className = "confirm-text";
    text.id = "confirm-question";
    text.textContent = ask.message ?? "Apply this to the whole selection?";
    // The question *is* the dialog's name. Without this a screen reader
    // announces "dialog" and then reads the buttons, so the reader is asked
    // to confirm something they were never told -- and this dialog only ever
    // appears for a bulk action, where the thing not being said is how many
    // conversations it is about to touch. One dialog exists at a time (every
    // exit path closes and detaches it before another can be made), so the
    // fixed id cannot collide.
    dialog.setAttribute("aria-labelledby", text.id);

    const actions = document.createElement("div");
    actions.className = "confirm-actions";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "confirm-cancel";
    cancel.textContent = "Cancel";
    // The safe answer holds focus: this dialog only ever appears in front
    // of an action about to touch more than a hundred messages.
    cancel.autofocus = true;
    cancel.addEventListener("click", () => finish(false));

    const go = document.createElement("button");
    go.type = "button";
    go.className = "confirm-go";
    // No red variant, deliberately: the question already reads "Delete 101
    // messages?", so the danger is carried by the words rather than by a
    // colour that would need a `--danger-hover` token added to all three
    // theme blocks for one button.
    go.textContent = Object.hasOwn(CONFIRM_LABEL, kind) ? CONFIRM_LABEL[kind] : "Continue";
    go.addEventListener("click", () => finish(true));

    actions.append(cancel, go);
    dialog.append(text, actions);
    dialog.addEventListener("cancel", () => finish(false));
    dialog.addEventListener("close", () => finish(dialog.returnValue === "confirm"));
    dialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") finish(false);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

// ---------------------------------------------------------------------
// Running an action
// ---------------------------------------------------------------------

let lastUndo = null;
let lastUndoAt = 0;

/** Paint the result of `kind` on `elements` before the server has answered,
 *  and hand back the function that puts that paint back.
 *
 *  **Every branch returns an inverse, and the failure path calls it.** This
 *  used to return nothing, on the reasoning that a re-fetch is the only
 *  honest revert — the server is the one that knows what a partly applied
 *  `Email/set` left behind. That reasoning is still why the failure path
 *  re-fetches *as well*; what it missed is that the failure this most
 *  matters for is the mail server being unreachable, in which case the
 *  re-fetch fails too and reverts nothing. A row left carrying
 *  `.is-leaving` is `height: 0; opacity: 0` — gone from the reader's screen
 *  though its mail never moved, which is the exact lie the whole failure
 *  path exists to prevent. So the paint is undone locally at once, and the
 *  re-fetch overwrites that guess with the truth if it arrives.
 *
 *  One case the inverse genuinely cannot cover: a row whose collapse
 *  already finished (the response took longer than `COLLAPSE_MS`) is off
 *  the DOM, and this file does not keep its markup. That row comes back
 *  with the next successful re-fetch and not before. */
function applyOptimistic(kind, elements) {
  // Rows only. The collapse and `.is-unread` are *row* rendering, and the
  // thread view has neither: collapsing the conversation you are reading
  // would blank the page instead of leaving it, which is
  // `leaveConversation`'s job, after the server has confirmed.
  const painted = elements.filter((el) => el.matches(ROW_SELECTOR));
  const starrable = elements.filter((el) => el.matches(STAR_SELECTOR));
  if (REMOVES_ROWS.has(kind)) {
    collapseRows(painted);
    return () => restoreCollapse(painted);
  }
  if (kind === "star" || kind === "unstar") {
    const on = kind === "star";
    const before = [];
    // The one exception to "rows only": a message card renders the same
    // toggle (`.msg-star`, `aria-pressed`) and now resolves to itself
    // rather than to the whole conversation, so it can be painted the same
    // way. Without this the card's star posts correctly and then sits
    // there looking unchanged until something re-renders the page — a
    // control that works but cannot be seen to.
    for (const el of starrable) {
      const starButton = el.querySelector(".row-star, .msg-star");
      if (starButton === null) continue;
      // Recorded, not inferred: "unstar" does not imply the row was
      // starred (a toolbar or palette entry always stars), so flipping
      // `on` back would invent a state the row never had.
      before.push([starButton, starButton.classList.contains("is-on")]);
      starButton.classList.toggle("is-on", on);
      starButton.setAttribute("aria-pressed", on ? "true" : "false");
    }
    return () => {
      for (const [starButton, was] of before) {
        starButton.classList.toggle("is-on", was);
        starButton.setAttribute("aria-pressed", was ? "true" : "false");
      }
    };
  }
  // `.is-unread` carries the whole read/unread rendering — the weights,
  // the date, and which of the row's two mail buttons is displayed — so
  // one class flip is the entire optimistic update.
  const before = painted.map((el) => [el, el.classList.contains("is-unread")]);
  for (const el of painted) el.classList.toggle("is-unread", kind === "unread");
  return () => {
    for (const [el, was] of before) el.classList.toggle("is-unread", was);
  };
}

/** Abandon a collapse that has not finished, and un-paint it.
 *
 *  Both halves are needed. Bumping the generation is what stops the pending
 *  `setTimeout` from removing rows the server never took (the same guard a
 *  re-fetch uses), and clearing `.is-leaving` is what makes them visible
 *  again — without it the nodes survive at zero height, which looks
 *  identical to having been archived. */
function restoreCollapse(elements) {
  listGeneration += 1;
  for (const el of elements) el.classList.remove("is-leaving");
}

/** Leave the conversation being read: auto-advance where the reader asked
 *  to be taken, otherwise back to the list.
 *
 *  A thread that has just been archived or deleted must not stay on screen
 *  (and there is no row to collapse instead). Where it goes is spec §10's
 *  `auto_advance`, and the *server* resolved it: the page carries one
 *  finished `data-advance-url`, already the next or previous conversation's
 *  own address or the list's, because only the route knows this
 *  conversation's neighbours and the pref behind it. Reading one attribute
 *  keeps that decision in one place instead of re-deriving it from a
 *  readout here.
 *
 *  That address names the conversation (`/t/{id}?key=&pos=`), not the place
 *  it sits in, and the difference matters precisely here: the action this
 *  function runs after is the one that removes the current conversation
 *  from the mailbox, so every position after it shifts by one. The id was
 *  captured before that happened.
 *
 *  The fallback is `data-role="back"`, clicked rather than assigned to
 *  `location`: that keeps htmx's in-place `#main` swap and its history
 *  entry, and re-uses the page's own idea of where back goes — the mailbox
 *  the reader came from — instead of inventing a second one here. The
 *  advance takes the same swap for the same reason, and pushes no URL of
 *  its own: both targets answer with `HX-Push-Url`, so a `pushState` here
 *  would put the same address in the history twice.
 *
 *  Undo still works from the toast either way, because the token is in the
 *  toast rather than in the page that just went away. */
function leaveConversation() {
  const url = document.querySelector("[data-advance-url]")?.dataset.advanceUrl ?? "";
  if (url !== "" && window.htmx?.ajax) {
    window.htmx.ajax("GET", url, { target: "#main", swap: "morph:innerHTML" });
    return;
  }
  document.querySelector('[data-role="back"]')?.click();
}

// ---------------------------------------------------------------------
// Mark read on open, and where the conversation opens
//
// Both are "the conversation just landed in the DOM" concerns, so **one**
// settle handler owns them. Split in two they would get two chances to
// disagree about which conversation is on screen — and the failure that
// causes is silent: a timer armed for the thread you just left, firing
// against the one you are now reading.
//
// Armed from `htmx:afterSettle`, never from parse. A `preload`ed GET
// (spec §6.4) fetches this page on `mousedown` and may never be opened at
// all; only a settle means the reader is actually looking at it. The
// server renders the attribute either way.
//
// The delay itself is the reader's (`prefs.mark_read_delay`, seconds):
// `0` marks on arrival, `-1` never marks at all. The ids are the page's
// own `data-unread-ids` — the subset with work to do, which
// `thread/page.html` already carries and which `build_conversation`
// snapshotted *before* anything was marked, so a card cannot collapse
// under the reader the instant it is read. Deliberately *not* a second
// `data-mark-read-ids` naming the same set: two attributes with one
// meaning is how they come to disagree.
//
// Both attributes are `thread/page.html`'s, on `.thread-scroll` beside
// `data-thread-id`:
//
//   data-mark-read-delay="{{ mark_read_delay }}"
//   data-advance-url="{{ advance_url }}"
//
// One element carrying all three is the point. This handler keys its timer
// off *which conversation* is on screen, so a delay read from one element
// and a thread id read from another could disagree — and the failure that
// causes is silent: a timer armed for the conversation you just left,
// firing against the one you are now reading.

/** The pending `POST /a/read`, and the conversation it belongs to. */
let markReadTimer = null;
let readingThread = null;

function cancelMarkRead() {
  if (markReadTimer !== null) window.clearTimeout(markReadTimer);
  markReadTimer = null;
}

/** Re-arm for whatever conversation is on screen now.
 *
 *  Keyed off which thread that is, not off the settle itself: an
 *  out-of-band nav swap or a background list refetch settles too, and
 *  restarting the delay on each one would postpone marking read for as
 *  long as anything else on the page kept moving. Leaving early is the
 *  case that has to work — the timer is dropped the moment a different
 *  conversation (or no conversation) is what settled. */
function conversationSettled() {
  const page = document.querySelector("[data-mark-read-delay]");
  const thread = page === null ? null : (page.dataset.threadId ?? "");
  if (thread === readingThread) return;
  cancelMarkRead();
  readingThread = thread;
  if (page === null) return;

  // Spec §7: a conversation opens at its first unread message, or at the
  // last one when it has all been read. `thread/message.html` marks that
  // card itself, so there is one reader for one attribute.
  document.querySelector("[data-first-unread]")?.scrollIntoView({ block: "start" });

  const ids = (page.dataset.unreadIds ?? "").split(",").filter((id) => id !== "");
  const delay = Number.parseInt(page.dataset.markReadDelay ?? "-1", 10);
  if (ids.length === 0 || !Number.isFinite(delay) || delay < 0) return;
  markReadTimer = window.setTimeout(() => {
    markReadTimer = null;
    om.act("read", ids);
  }, delay * 1000);
}

// A conversation reached by its own URL rather than by a swap — a pasted
// link, a bookmark, a reload — is the live document from the start and
// gets no settle. It is still a reader looking at mail, so it arms here.
// Not the prefetch case: a preloaded response is text in htmx's cache and
// never becomes a document at all.
document.addEventListener("DOMContentLoaded", conversationSettled);

/** Consume an `om:done` payload: the rows, the badges, the undo token and
 *  the toast. Every field is optional — see the shape table at the top. */
function applyDone(done) {
  if (done === null) return;
  removeThreads(done.removed ?? []);
  applyCounts(done.counts ?? {});
  if (done.refresh) refreshList();

  const token = done.undo ?? null;
  lastUndo = token;
  lastUndoAt = Date.now();
  // Key presence, not `=== null`: `undo_unavailable` is absent whenever
  // undo is present. `no_change` resolves to nothing on purpose.
  const note = UNDO_UNAVAILABLE_NOTE.get(done.undo_unavailable) ?? null;
  if (done.toast) toast(done.toast, token, note);
}

/** Run one action. `elements` are the rows to paint optimistically (empty
 *  is fine — the response still reconciles everything). */
async function run(kind, elements, ids, confirmed = false) {
  if (!Object.hasOwn(ROUTES, kind) || ids.length === 0) return;

  const values = { ids: ids };
  if (Object.hasOwn(ON, kind)) values.on = ON[kind];
  if (confirmed) values.confirm = "1";

  // Not on the confirmed re-post: the first attempt already painted it,
  // and the 409 that came back put it straight again. `revert` is that
  // paint's inverse, held for every path below that has to undo it.
  const revert = confirmed ? null : applyOptimistic(kind, elements);

  let response = null;
  try {
    response = await post(ROUTES[kind], values);
  } catch {
    reportFailure(kind, revert);
    return;
  }

  if (handledExpiredSession(response)) return;

  if (response.status === 409) {
    // Nothing happened server-side — not even a read — so the optimistic
    // paint is now a lie and has to go before the question is asked.
    revert?.();
    refreshList();
    const ask = trigger(response, "om:confirm");
    if (ask === null) return;
    if (await confirmBulk(kind, ask)) await run(kind, [], ids, true);
    return;
  }

  // The two shapes a failure arrives in, in one guard, because they mean
  // the same thing to the reader and because separating them is how the
  // regression above happened. `om:error` on a `200` is a JMAP/transport
  // failure or a 422 (see the failure contract at the top of this file);
  // `!response.ok` is everything the app's own handlers never saw — a
  // proxy's 502, a CSRF 403, an unhandled 500. Neither moved any mail, so
  // both revert the optimistic paint, say so, and stop here.
  if (trigger(response, "om:error") !== null || !response.ok) {
    reportFailure(kind, revert);
    return;
  }

  applyDone(trigger(response, "om:done"));
  // Reading a conversation that is no longer in this mailbox: leave, now
  // that the toast (and its Undo) is up. Reachable only past the guard
  // above, which is the whole point of where it sits: a failed archive
  // leaves the reader exactly where they were, looking at mail that never
  // moved. That guard has to test `om:error` and not just `response.ok`,
  // because the server answers a dead mail server with a 200 — checking
  // only `ok` bounced the reader out of the conversation on failure.
  if (REMOVES_ROWS.has(kind) && openConversation() !== null) leaveConversation();
}

/** Reverse a committed action. Its reply is the narrow shape —
 *  `{toast: "Undone", refresh: true}` — so `applyDone` refetches rather
 *  than trying to work out where each restored row belongs. */
async function undo(token) {
  if (!token) return;
  if (token === lastUndo) lastUndo = null;

  let response = null;
  try {
    response = await post("/a/undo", { token: token });
  } catch {
    announceFailure(FAILED.undo);
    return;
  }

  if (handledExpiredSession(response)) return;

  // Same inversion as `run()`: a JMAP failure here answers `200` with
  // `om:error`, so `!response.ok` alone would read a failed undo as a
  // success and `applyDone(null)` would then say nothing at all.
  if (trigger(response, "om:error") !== null || !response.ok) {
    // 400 is the documented failure and means one thing: the window
    // closed (or the token was never this account's — the route answers
    // both identically, on purpose). Nothing changed, so nothing is
    // refetched.
    announceFailure(response.status === 400 ? "That undo has expired" : FAILED.undo);
    return;
  }

  applyDone(trigger(response, "om:done"));
}

// ---------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------

// app.js's toast renders the Undo button and reports the click here rather
// than posting itself — the store draws, this module acts.
document.body.addEventListener("om:undo", (event) => {
  undo(event.detail?.token ?? null);
});

// Every `om:error` that did NOT come back to `run()`'s own `fetch`: a
// failure on an *htmx* request — a list refetch, a nav or thread swap, a
// prefs POST — which htmx dispatches as a bubbling DOM event on the
// element that made it. Nothing else in this app listens for it, so
// without this a dead mail server turned every navigation into a click
// that silently did nothing at all: `HX-Reswap: none` means the page does
// not even flicker.
//
// The copy is the server's here (unlike `reportFailure`): this listener
// cannot know what the reader was trying to do, and the server's sentence
// can.
document.body.addEventListener("om:error", (event) => {
  if (Date.now() - lastFailureAt < ERROR_QUIET_MS) return;
  lastFailureAt = Date.now();
  const message = event.detail?.toast;
  toast(typeof message === "string" && message !== "" ? message : UNEXPLAINED_FAILURE);
});

// The success half of that same split, and it was missing. Not every
// mutation in this app goes through `run()`'s `fetch`: the per-message ⋮
// menu posts real `<form>`s (`thread/menu.html`), on purpose — its two
// mutating items name their exact message ids in hidden fields, because
// "Delete message" resolving to the whole conversation would be data loss.
// htmx makes those requests, so their `om:done` arrives as a bubbling DOM
// event rather than as a response object, and `applyDone` never saw it:
// "Mark unread from here" moved the mail and then said nothing at all — no
// toast, and no Undo for a reader who meant the card below.
//
// Same payload, same consumer. `run()`'s own posts cannot reach here (a
// `fetch` dispatches nothing), so nothing is applied twice.
document.body.addEventListener("om:done", (event) => {
  applyDone(event.detail ?? null);
});

// The top bar's Help button. `data-role`, not `data-action`: every
// `data-action` in this app names one of `mailosh/web/actions.py`'s six
// routes, and this control posts nothing — it opens the same `?` overlay
// keys.js's `shortcuts` entry opens, from the same registry, so the mouse
// and the keyboard cannot end up with two different lists of shortcuts.
// Bound here because this file already owns the bubble-phase delegated
// `click` on `document.body`.
document.body.addEventListener("click", (event) => {
  const control = event.target?.closest?.('[data-role="help"]');
  if (control === null || control === undefined) return;
  event.preventDefault();
  openShortcuts();
});

/** The rows a `[data-action]` control applies to.
 *
 *  A control inside a row acts on that row, even when other rows are
 *  selected — a hover action is aimed at the thing under the pointer, and
 *  Gmail reads it the same way. A control outside the list (the toolbar,
 *  the palette) acts on the selection.
 *
 *  **Nearest wins, and off the list that is the message, not the thread.**
 *  A conversation nests two `[data-email-ids]`: the `.thread-scroll`
 *  wrapper carries the whole thread and each `<article>` carries its own
 *  one message. Falling straight through to `defaultTargets()` here — which
 *  resolves to the wrapper — made the per-message star conversation-scoped:
 *  starring one message starred every message in the thread. `closest`
 *  stops at the card, so the toolbar above the cards (outside both) still
 *  reaches the whole conversation, and the star reaches exactly the card it
 *  is painted on. */
function targets(control) {
  const row = control.closest(ROW_SELECTOR);
  if (row !== null) return [row];
  const scoped = control.closest("[data-email-ids]");
  return scoped === null ? defaultTargets() : [scoped];
}

function onClick(event) {
  if (event.defaultPrevented) return;
  const control = event.target?.closest?.("[data-action]");
  if (control === null || control === undefined) return;
  if (control.getAttribute("aria-disabled") === "true") return;

  const kind = control.dataset.action;
  const elements = targets(control);
  event.preventDefault();

  if (kind === "select") {
    // The mouse half of the selection model, extended here rather than in
    // a listener of its own.
    //
    // **Shift-click ranges live on the checkbox, not on the row.** The two
    // gestures collided: the row's stretched anchor deliberately restored
    // the browser's native modifier clicks (⌘/Ctrl new tab, middle-click,
    // Shift new window, Alt save-as), and Gmail's range selection wants
    // Shift-click too. Taking Shift back from the anchor would have undone
    // a fix on purpose-built behaviour to buy a gesture that already has a
    // better home: a checkbox has no native modifier meaning to lose, it
    // is what "selection" means on this row, and Shift-clicking one
    // checkbox after another is Gmail's own gesture. So the row keeps
    // every native modifier click, and the range is anchored where the
    // reader started selecting. `Shift+J`/`Shift+K` are the keyboard's
    // equivalent.
    //
    // Nothing has to be un-prevented for that to hold: the checkbox is a
    // sibling painted *above* the anchor, so a click on it never reaches
    // the anchor at all, and `event.preventDefault()` above already
    // covers this control.
    const list = store("list");
    for (const el of elements) {
      const id = el.dataset.id;
      if (event.shiftKey && list?.anchorId) list.range(list.anchorId, id);
      else list?.toggle(id);
    }
    return;
  }

  // The row star is a toggle, so which route "star" means depends on the
  // control's own state. A `[data-action="star"]` without `aria-pressed`
  // — a toolbar or palette entry — always stars.
  const resolved =
    kind === "star" && control.getAttribute("aria-pressed") === "true" ? "unstar" : kind;
  run(resolved, elements, emailIds(elements));
}
document.body.addEventListener("click", onClick);

/** The action surface Task 10 (keys) and Task 11 (palette) call into.
 *
 *  `act(kind)` with no ids means "the current conversation" — the
 *  selection, or the focused row when nothing is selected. Passing ids
 *  explicitly skips the optimistic paint (there are no rows to paint), and
 *  the response reconciles the list on its own. */
const om = {
  act(kind, ids = null) {
    if (Array.isArray(ids)) return run(kind, [], ids);
    const elements = defaultTargets();
    return run(kind, elements, emailIds(elements));
  },

  /** What `act(kind)` would apply to right now, without running anything.
   *
   *  keys.js needs this twice, and both times a second implementation
   *  would be a second definition of "the current conversation": to reject
   *  a key that has nothing to act on (`e` with an empty list) instead of
   *  posting an empty request, and to decide which way `s` toggles. */
  targets() {
    return defaultTargets();
  },

  /** Spec §6.3's `z`. Offered only while the toast that carried it would
   *  still be on screen, even though the token itself outlives that. */
  undoLast() {
    if (!lastUndo || Date.now() - lastUndoAt > UNDO_WINDOW_MS) return Promise.resolve();
    return undo(lastUndo);
  },
};

window.om = om;
