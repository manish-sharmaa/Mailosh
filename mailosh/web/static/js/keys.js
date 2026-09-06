// Mailosh — the keyboard registry (design spec §6.1).
//
// One table drives four things that would otherwise drift apart: `keydown`
// dispatch, the `?` overlay, the `title` hints on every action button
// ("Archive (e)"), and the palette's shortcut column. A key that moves has
// to move in exactly one place.
//
// ---------------------------------------------------------------------
// Why the table is written as JSON
//
// `DEFAULTS` below uses quoted object keys and no trailing comma, which is
// unusual for hand-written JavaScript and deliberate: this repository has
// no Node toolchain and therefore no JS test runner (Global Constraints),
// so the only thing that can hold this table to the Python that has to
// agree with it — `mailosh/web/palette.py`'s action ids and keys, and the
// `title="Archive (e)"` hints in the templates — is a Python test that
// reads this file. A `json.loads` of one slice is exact; a regex over
// JavaScript object literals is a guess. The behaviour (`RUNNERS`) stays
// ordinary JavaScript beneath it.
//
// ---------------------------------------------------------------------
// Binding syntax
//
// `keys` is a list of *bindings*, any of which runs the entry — `["o",
// "Enter"]` is one command with two ways to reach it, not two commands.
// Inside a binding:
//
//   "e"        one chord
//   "Shift+I"  one chord with a held modifier
//   "Mod+K"    ...where Mod is Cmd on a Mac and Ctrl everywhere else
//   "g i"      a two-key *sequence*: `g`, then `i` within 1000 ms
//
// A space is the only thing that separates sequence steps, which is also
// exactly what `mailosh.ui.macros.kbd` splits on, so a binding string is
// already a renderable hint.
//
// Shift is load-bearing only for a *letter*: `Shift+I` and `i` are two
// different chords, but `#`, `!`, `?` and `*` already encode their shift
// in the character the platform reports, and demanding `shiftKey` on top
// of them would break every keyboard layout where they are unshifted.
//
// ---------------------------------------------------------------------
// `available: false`
//
// An entry whose feature has not shipped yet stays in the table (so the
// key is reserved and the id exists for the palette to resolve) but is
// **hidden from the overlay and does nothing at all when pressed** — not
// even the rejection shake. The UI never advertises a broken key, and a
// reader who presses `r` on a conversation gets silence rather than a
// wobble suggesting they nearly did something. Flipping one word here is
// what lands the key with its feature.
//
// The shake is kept for the case it was meant for: a key that IS live in
// this scope but has nothing to act on right now (`e` with no selection
// and no focused row). A runner says so by returning exactly `false`.
//
// ---------------------------------------------------------------------
// Scopes stack
//
// `global` is always live. `list` is live wherever there is a list *or a
// conversation* — `e`, `#` and `s` all act on the open conversation
// through the same `om.act()` that serves the list, so scoping them to the
// list view alone would have made the conversation's own action bar
// unreachable from the keyboard. `thread` adds to `list` rather than
// replacing it, which is what "scopes stack: global < list < thread <
// compose" means. While any `<dialog>` is open the platform owns the
// keyboard and dispatch stops entirely.
//
// `compose` is the one scope that is not decided by what is on screen but
// by **where the caret is** (`activeScopes`). A compose dock is a sibling
// of the list, not a modal over it, so one is routinely open while the
// reader triages the inbox behind it — and a scope keyed on "a dock
// exists" would have taken `⌘K` away from the palette and pointed
// `⌘Enter` at a message nobody was looking at. Focus is the question,
// and it is also what keeps that inbox fully usable.
//
// **Stacking is also a precedence order, not just a union.** One binding
// may be claimed twice as long as the two claims sit at different scopes:
// `lookup` then picks the narrowest live one (`SCOPE_RANK`). `Shift+U` is
// the case that needs it — from the list it marks the whole conversation
// unread, and while reading one it marks it unread *from the card you are
// on down*, which is the same gesture meaning the more precise thing in
// the place that can be precise. Two entries rather than one runner that
// branches, because the `?` overlay describes them differently and a
// single row could only tell the reader one of the two truths.

/** The live registry: `{id, keys, scope, group, label, available, run}`.
 *  `registerDefaults()` fills it; nothing else may push to it. */
export const registry = [];

/** Overlay column order. A group named here with no available entry simply
 *  renders nothing. */
const GROUP_ORDER = ["Navigation", "Selection", "Actions", "Conversation", "Compose", "Application"];

/** Spec §6.1's map. Ids for the six mutations are `mailosh/web/palette.py`'s
 *  — `palette.js` looks an `"action"` result up here by that id and calls
 *  the entry's own `run()`, so a rename here silently breaks it there, and
 *  an entry marked unavailable disappears from the palette as well as from
 *  the overlay. `tests/unit/test_mail_routes.py` pins the two together. */
const DEFAULTS = [
  {"id": "next", "keys": ["j"], "scope": "list", "group": "Navigation", "label": "Next (older) conversation", "available": true},
  {"id": "prev", "keys": ["k"], "scope": "list", "group": "Navigation", "label": "Previous (newer) conversation", "available": true},
  {"id": "open", "keys": ["o", "Enter"], "scope": "list", "group": "Navigation", "label": "Open conversation", "available": true},
  {"id": "back", "keys": ["u"], "scope": "list", "group": "Navigation", "label": "Back to the list", "available": true},
  {"id": "goto-inbox", "keys": ["g i"], "scope": "global", "group": "Navigation", "label": "Go to Inbox", "available": true},
  {"id": "goto-starred", "keys": ["g s"], "scope": "global", "group": "Navigation", "label": "Go to Starred", "available": true},
  {"id": "goto-sent", "keys": ["g t"], "scope": "global", "group": "Navigation", "label": "Go to Sent", "available": true},
  {"id": "goto-drafts", "keys": ["g d"], "scope": "global", "group": "Navigation", "label": "Go to Drafts", "available": true},
  {"id": "goto-all", "keys": ["g a"], "scope": "global", "group": "Navigation", "label": "Go to All mail", "available": true},
  {"id": "goto-label", "keys": ["g l"], "scope": "global", "group": "Navigation", "label": "Go to a mailbox or label", "available": true},

  {"id": "select", "keys": ["x"], "scope": "list", "group": "Selection", "label": "Select conversation", "available": true},
  {"id": "extend-next", "keys": ["Shift+J"], "scope": "list", "group": "Selection", "label": "Extend selection down", "available": true},
  {"id": "extend-prev", "keys": ["Shift+K"], "scope": "list", "group": "Selection", "label": "Extend selection up", "available": true},
  {"id": "select-all", "keys": ["* a"], "scope": "list", "group": "Selection", "label": "Select all", "available": true},
  {"id": "select-none", "keys": ["* n"], "scope": "list", "group": "Selection", "label": "Select none", "available": true},
  {"id": "select-read", "keys": ["* r"], "scope": "list", "group": "Selection", "label": "Select read", "available": true},
  {"id": "select-unread", "keys": ["* u"], "scope": "list", "group": "Selection", "label": "Select unread", "available": true},
  {"id": "select-starred", "keys": ["* s"], "scope": "list", "group": "Selection", "label": "Select starred", "available": true},
  {"id": "select-unstarred", "keys": ["* t"], "scope": "list", "group": "Selection", "label": "Select unstarred", "available": true},

  {"id": "archive", "keys": ["e"], "scope": "list", "group": "Actions", "label": "Archive (in Trash: restore to Inbox)", "available": true},
  {"id": "delete", "keys": ["#"], "scope": "list", "group": "Actions", "label": "Delete (in Trash: delete forever)", "available": true},
  {"id": "spam", "keys": ["!"], "scope": "list", "group": "Actions", "label": "Report spam (in Spam: not spam)", "available": true},
  {"id": "star", "keys": ["s"], "scope": "list", "group": "Actions", "label": "Star", "available": true},
  {"id": "mark-read", "keys": ["Shift+I"], "scope": "list", "group": "Actions", "label": "Mark as read", "available": true},
  {"id": "mark-unread", "keys": ["Shift+U"], "scope": "list", "group": "Actions", "label": "Mark as unread", "available": true},
  {"id": "archive-older", "keys": ["["], "scope": "list", "group": "Actions", "label": "Archive and go older", "available": true},
  {"id": "archive-newer", "keys": ["]"], "scope": "list", "group": "Actions", "label": "Archive and go newer", "available": true},
  {"id": "undo", "keys": ["z"], "scope": "global", "group": "Actions", "label": "Undo the last action", "available": true},
  {"id": "label", "keys": ["l"], "scope": "list", "group": "Actions", "label": "Label as…", "available": true},
  {"id": "move", "keys": ["v"], "scope": "list", "group": "Actions", "label": "Move to…", "available": true},
  {"id": "more", "keys": ["."], "scope": "list", "group": "Actions", "label": "More actions", "available": false},

  {"id": "next-message", "keys": ["n"], "scope": "thread", "group": "Conversation", "label": "Next message", "available": true},
  {"id": "prev-message", "keys": ["p"], "scope": "thread", "group": "Conversation", "label": "Previous message", "available": true},
  {"id": "expand-all", "keys": [";"], "scope": "thread", "group": "Conversation", "label": "Expand all messages", "available": true},
  {"id": "collapse-all", "keys": [":"], "scope": "thread", "group": "Conversation", "label": "Collapse all messages", "available": true},
  {"id": "mark-unread-from-here", "keys": ["Shift+U"], "scope": "thread", "group": "Conversation", "label": "Mark unread from here", "available": true},
  {"id": "reply", "keys": ["r"], "scope": "thread", "group": "Conversation", "label": "Reply", "available": true},
  {"id": "reply-all", "keys": ["a"], "scope": "thread", "group": "Conversation", "label": "Reply all", "available": true},
  {"id": "forward", "keys": ["f"], "scope": "thread", "group": "Conversation", "label": "Forward", "available": true},

  {"id": "send", "keys": ["Mod+Enter"], "scope": "compose", "group": "Compose", "label": "Send", "available": true},
  {"id": "add-cc", "keys": ["Mod+Shift+C"], "scope": "compose", "group": "Compose", "label": "Add Cc", "available": true},
  {"id": "add-bcc", "keys": ["Mod+Shift+B"], "scope": "compose", "group": "Compose", "label": "Add Bcc", "available": true},

  {"id": "shortcuts", "keys": ["?"], "scope": "global", "group": "Application", "label": "Keyboard shortcuts", "available": true},
  {"id": "escape", "keys": ["Escape"], "scope": "global", "group": "Application", "label": "Close compose, or clear the selection", "available": true},
  {"id": "compose", "keys": ["c"], "scope": "global", "group": "Application", "label": "Compose", "available": true},
  {"id": "search", "keys": ["/"], "scope": "global", "group": "Application", "label": "Search mail", "available": true},
  {"id": "palette", "keys": ["Mod+K"], "scope": "global", "group": "Application", "label": "Command palette", "available": true}
];

const LIST = "#list";
const ROW_SELECTOR = "#list [data-id]";

/** One message card in the open conversation (`thread/message.html`).
 *
 *  Matched structurally rather than by class name: `data-email-ids` is the
 *  card's contract with `actions.js` (it is what an action on that one
 *  message posts) and `data-thread-id` is the conversation wrapper's, so
 *  both are pinned by tests elsewhere. A class is styling, and would take
 *  every key below down with a rename nothing else would notice.
 *
 *  The wrapper itself carries `data-email-ids` too — the whole thread — but
 *  is not an `<article>`, so it is not a card and `n`/`p` cannot land on
 *  it. */
const CARD_SELECTOR = "[data-thread-id] article[data-email-ids]";

/** One compose window, dock or inline reply card (`compose/dock.html`,
 *  `compose/inline.html`). Matched on `data-compose` rather than on a
 *  class for the same reason `CARD_SELECTOR` is matched structurally: a
 *  class is styling, and a rename would take the whole compose scope with
 *  it. */
const COMPOSE = "[data-compose]";
const SEQUENCE_MS = 1000;
const SHAKE_MS = 150;

/** Scope specificity: a chord claimed at two scopes belongs to the
 *  narrowest live one. See this file's header — `Shift+U` is the only
 *  binding that uses it, and dispatch would otherwise run whichever entry
 *  happened to come first in `DEFAULTS`. */
const SCOPE_RANK = { global: 0, list: 1, thread: 2, compose: 3 };

/** Where a keystroke is the reader's text, not a command. `Escape` and
 *  Cmd/Ctrl chords are exempt (spec §6.1) — those never mean a character. */
const TYPING = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';

/** Elements the platform already activates on `Enter`/`Space`. A row's own
 *  buttons are in here, which is what keeps `Enter` on a focused checkbox
 *  from *also* opening the conversation behind it. */
const ACTIVATES =
  'a[href], button, summary, input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="menuitem"], [role="tab"]';

const reduced = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

// ---------------------------------------------------------------------
// Chords
// ---------------------------------------------------------------------

const MODIFIERS = {
  mod: "mod",
  cmd: "meta",
  command: "meta",
  meta: "meta",
  ctrl: "ctrl",
  control: "ctrl",
  alt: "alt",
  option: "alt",
  shift: "shift",
};

const parsed = new Map();

/** `"Shift+I"` -> `{base: "I", mod: false, meta: false, ctrl: false, alt:
 *  false, shift: true}`. Split from the right so the base may itself be
 *  `"+"` if a binding ever needs it. */
function parseChord(chord) {
  const cached = parsed.get(chord);
  if (cached !== undefined) return cached;
  const at = chord.lastIndexOf("+");
  const base = at <= 0 ? chord : chord.slice(at + 1);
  const held = at <= 0 ? [] : chord.slice(0, at).split("+");
  const shape = { base: base, mod: false, meta: false, ctrl: false, alt: false, shift: false };
  for (const name of held) {
    const known = MODIFIERS[name.toLowerCase()];
    if (known) shape[known] = true;
  }
  parsed.set(chord, shape);
  return shape;
}

/** True when `base` is a character that already carries its own shift —
 *  `#`, `!`, `?`, `*`, `[`, `]`, `.`, `;`, `:`, `/`. For those the
 *  `shiftKey` flag says nothing portable, so it is not compared. */
function selfShifted(base) {
  return base.length === 1 && !/[a-z0-9]/i.test(base);
}

function matchesChord(chord, event) {
  const want = parseChord(chord);
  const key = event.key;
  const letter = want.base.length === 1 && /[a-z]/i.test(want.base);
  const same = letter
    ? key.length === 1 && key.toLowerCase() === want.base.toLowerCase()
    : key === want.base;
  if (!same) return false;
  if (want.mod) {
    if (!event.metaKey && !event.ctrlKey) return false;
  } else if (want.meta !== event.metaKey || want.ctrl !== event.ctrlKey) {
    return false;
  }
  if (want.alt !== event.altKey) return false;
  if (!selfShifted(want.base) && want.shift !== event.shiftKey) return false;
  return true;
}

// ---------------------------------------------------------------------
// The world the runners act on
// ---------------------------------------------------------------------

let bound = null;

function listStore() {
  return bound ?? window.Alpine?.store?.("list") ?? null;
}

function rows() {
  return Array.from(document.querySelectorAll(ROW_SELECTOR));
}

/** The row the cursor is on. `list.focusId` is the truth; the class is the
 *  fallback for a page whose Alpine never started. */
function focusedRow() {
  const id = listStore()?.focusId ?? null;
  if (id !== null) {
    for (const el of rows()) if (el.dataset.id === id) return el;
  }
  return document.querySelector("#list .row.is-focused");
}

/** What an action would apply to right now — asked of `actions.js` rather
 *  than recomputed here, so "the current conversation" has one definition
 *  in this app and not two that can disagree. */
function targets() {
  const resolve = window.om?.targets;
  return typeof resolve === "function" ? resolve() : [];
}

/** Whether a target is starred all the way through. A list row carries one
 *  `.row-star`; the open conversation carries a `.msg-star` per message,
 *  and checking only the row's class there meant `s` in a conversation
 *  could star but never unstar. */
function starred(el) {
  const stars = el.querySelectorAll(".row-star, .msg-star");
  return stars.length > 0 && Array.from(stars).every((star) => star.classList.contains("is-on"));
}

/** Run one of `actions.js`'s mutations against the current target, or
 *  say there was nothing to run it on. */
function act(kind) {
  if (targets().length === 0) return false;
  window.om.act(kind);
  return true;
}

/** Which mailbox the reader is looking at — `""` off any mailbox.
 *
 *  Read from the toolbar's `data-view-key` (`list/toolbar.html`, and the
 *  conversation's own bar in `thread/page.html`, which carries the
 *  mailbox the reader came from) rather than parsed out of the URL: a
 *  conversation's address names no mailbox, and the toolbar is the one
 *  element that already draws different buttons for Trash and Spam, so it
 *  is the one the keys should agree with. */
function viewKey() {
  return document.querySelector("[data-view-key]")?.dataset.viewKey ?? "";
}

/** `e`, `#` and `!` keep their bindings everywhere and change their
 *  meaning in Trash and Spam, exactly as the buttons they mirror do: in
 *  Trash `e` restores and `#` deletes forever (archiving from Trash would
 *  be a no-op, and there is no second trash to move to), and in Spam `!`
 *  is "not spam". One registry entry each rather than a second entry per
 *  mailbox: the `?` overlay lists one key once, with both readings in its
 *  label, and `lookup` forbids a binding claimed twice at one scope. */
function inView(key, there, elsewhere) {
  return act(viewKey() === key ? there : elsewhere);
}

/** `s` toggles, so its direction comes from what is under the cursor: a
 *  target that is starred all the way through unstars, anything else
 *  stars. (Gmail reads it the same way.) */
function toggleStar() {
  const chosen = targets();
  if (chosen.length === 0) return false;
  window.om.act(chosen.every(starred) ? "unstar" : "star");
  return true;
}

/** Put the cursor on `nextId` once `rowId` has actually left the list.
 *
 *  `[`/`]` archive *and* move, but the row does not leave until the 160 ms
 *  collapse finishes and `ensureFocus` has had its say — so this waits for
 *  the removal rather than racing it, and gives up after a second if the
 *  action failed and the row is still there. */
function focusAfterRemoval(rowId, nextId) {
  const started = Date.now();
  const tick = () => {
    const gone = rows().every((el) => el.dataset.id !== rowId);
    if (!gone) {
      if (Date.now() - started < 1000) requestAnimationFrame(tick);
      return;
    }
    const list = listStore();
    if (list === null || !list.ids().includes(nextId)) return;
    list.focusId = nextId;
    list.render({ focus: true, scroll: true });
  };
  requestAnimationFrame(tick);
}

function archiveThen(delta) {
  const list = listStore();
  const row = focusedRow();
  const chosen = targets();
  // With a selection, `[`/`]` are just Archive: there is no single row the
  // cursor is leaving, so there is nothing to move it relative to.
  // The same reading of `e` the plain key has: in Trash the row leaves by
  // being restored, and the cursor still moves to its neighbour.
  const kind = viewKey() === "trash" ? "restore" : "archive";
  if (list === null || row === null || chosen.length !== 1) return act(kind);
  const ids = list.ids();
  const at = ids.indexOf(row.dataset.id);
  const next = at === -1 ? null : (ids[at + delta] ?? null);
  if (!act(kind)) return false;
  if (next !== null) focusAfterRemoval(row.dataset.id, next);
  return true;
}

/** Navigate by clicking the nav's own link, so the htmx swap, the pushed
 *  URL and the active-item re-render are the ones the sidebar already
 *  does. A mailbox the account does not have has no link, and the caller
 *  is told so rather than a URL being fabricated that would 404.
 *
 *  Exported for `palette.js`, whose "Go to" results name the same
 *  mailboxes and labels the sidebar does — `mailosh/web/palette.py`
 *  builds both from `build_nav` and drops the same `hidden_in_nav` rows —
 *  so routing them through the sidebar's own link keeps one definition of
 *  what a mailbox switch does, instead of a second `htmx.ajax` call free
 *  to swap a different target or forget to push the URL. */
export function navigate(href) {
  for (const link of document.querySelectorAll('#nav a[href^="/mail/"]')) {
    if (link.getAttribute("href") === href) {
      link.click();
      return true;
    }
  }
  return false;
}

function goto(key) {
  return navigate("/mail/" + key);
}

/** The search pill in the top bar (`shell/topbar.html`). Matched on its
 *  `data-role` rather than on a class, for the same reason the thread's
 *  cards are matched structurally: a class is styling, and a rename would
 *  take `/` down with it silently.
 *
 *  It is `null` on the login page, which is the only rendered page in this
 *  app with no top bar — so `/` is declined there rather than throwing. */
const SEARCH_INPUT = '[data-role="search-input"]';

function searchInput() {
  return document.querySelector(SEARCH_INPUT);
}

/** `/`. Selects as well as focuses: pressing it over a search you have
 *  already run means "search for something else" far more often than it
 *  means "append to this", and the existing text is one keystroke away
 *  either way. The focus is also what opens the suggestions popover —
 *  `styles/search.css` reveals it from `:focus-within`, so there is no
 *  second thing here to show. */
function focusSearch() {
  const el = searchInput();
  if (el === null) return false;
  el.focus();
  el.select();
  return true;
}

/** `Esc` while the caret is in the pill: let go of it, which closes the
 *  popover the focus was holding open. Answered before compose and before
 *  the selection (see the `escape` runner) because it is the narrowest
 *  claim on the key — it is true only when the caret is in one specific
 *  input — and `false` everywhere else, so the other two still get their
 *  turn. */
function blurSearch() {
  const el = searchInput();
  if (el === null || document.activeElement !== el) return false;
  el.blur();
  return true;
}

function openFocused() {
  // Off the list there is nothing `Enter` could open, so it is declined
  // rather than rejected — the conversation view has its own uses for the
  // key, and a shake there would be nonsense.
  if (document.querySelector(LIST) === null) return undefined;
  const link = focusedRow()?.querySelector(".row-link") ?? null;
  if (link === null) return false;
  link.click();
  return true;
}

// ---------------------------------------------------------------------
// The conversation (`thread` scope)
//
// Everything below reads the cards `thread/message.html` renders and
// nothing else — there is no second model of "which message am I on" kept
// in JavaScript, because a second model is a thing that can disagree with
// the DOM after a live update morphs a card in or out.
//
// **The cursor is real focus, on the card's `<summary>`.** That is the
// element the platform already made focusable and already activates on
// Enter/Space, so `n` `n` `Enter` expands the third message with nothing
// here handling Enter at all — `suppressed()` hands it straight back to
// the browser. A class-based cursor would have needed its own Enter
// handling, its own focus ring and its own reconciliation after a swap.

function cards() {
  return Array.from(document.querySelectorAll(CARD_SELECTOR));
}

/** The card the reader is on: whatever holds focus, else the one the
 *  conversation opened at (`data-first-unread`, the server's choice), else
 *  the newest message. Never null on a conversation with messages, which is
 *  what lets `n` work as the first key pressed after `o`. */
function currentCard() {
  const held = document.activeElement?.closest?.(CARD_SELECTOR) ?? null;
  if (held !== null) return held;
  const opened = document.querySelector(CARD_SELECTOR + "[data-first-unread]");
  if (opened !== null) return opened;
  const all = cards();
  return all.length === 0 ? null : all[all.length - 1];
}

/** `n`/`p`. Declined (not rejected) off a conversation: on the list `n`
 *  means nothing yet, and shaking would suggest it nearly did something. */
function moveCard(delta) {
  const all = cards();
  if (all.length === 0) return undefined;
  const at = all.indexOf(currentCard());
  const next = all[at + delta] ?? null;
  // The ends are a rejection rather than a wrap: a conversation has a first
  // and a last message, and silently jumping between them would lose the
  // reader's place in a long thread.
  if (at === -1 || next === null) return false;
  // Focused first, scrolled second, and deliberately not by `focus()`
  // alone: the browser's own scroll-on-focus centres or minimally scrolls
  // depending on the engine, while every card in this view opens flush
  // with the top of the scroller.
  (next.querySelector("summary") ?? next).focus({ preventScroll: true });
  next.scrollIntoView({ block: "start" });
  return true;
}

/** `;` and `:`. The fold is the card's *first* `<details>`; the ⋮ menu's is
 *  a later sibling of it and the quoted-text pill's is nested inside its
 *  body, so document order is what tells the three apart without naming a
 *  class that belongs to the stylesheet. */
function foldAll(open) {
  const all = cards();
  if (all.length === 0) return undefined;
  for (const card of all) {
    const fold = card.querySelector("details");
    if (fold !== null) fold.open = open;
  }
  return true;
}

/** `Shift+U` while reading: mark this message and every one after it
 *  unread — the reader is putting the tail of a conversation back on their
 *  pile, not just this one card. The same wording, the same id set and the
 *  same route as the ⋮ menu's own item, so the key and the menu cannot
 *  come to mean two different things. */
function markUnreadFromHere() {
  const all = cards();
  const at = all.indexOf(currentCard());
  if (at === -1) return undefined;
  const ids = [];
  for (const card of all.slice(at)) {
    for (const id of (card.dataset.emailIds ?? "").split(",")) {
      if (id !== "" && !ids.includes(id)) ids.push(id);
    }
  }
  if (ids.length === 0) return false;
  window.om?.act?.("unread", ids);
  return true;
}

/** `u`. Declined on the list, where there is no list to go back to. */
function backToList() {
  const back = document.querySelector('[data-role="back"]');
  if (back === null) return undefined;
  back.click();
  return true;
}

function withList(run) {
  const list = listStore();
  if (list === null || document.querySelector(LIST) === null) return false;
  run(list);
  return true;
}

function clearSelection() {
  const list = listStore();
  if (list === null || list.selected.size === 0) return undefined;
  list.clear();
  return true;
}

// ---------------------------------------------------------------------
// The `?` overlay
// ---------------------------------------------------------------------

/** `Mod` as this platform spells it. The palette draws the same chips
 *  from the same table, so it imports `keyChips` below rather than
 *  deciding for itself what `Mod+K` looks like on a Mac. */
function modLabel() {
  return /mac|iphone|ipad/i.test(window.navigator.platform || "") ? "⌘" : "Ctrl";
}

export function keyChips(binding) {
  const chips = document.createDocumentFragment();
  for (const step of binding.split(" ")) {
    for (const part of step.split("+")) {
      const chip = document.createElement("kbd");
      chip.className = "kbd";
      chip.textContent = part === "Mod" ? modLabel() : part;
      chips.append(chip);
    }
  }
  return chips;
}

/** Build the overlay's body from the registry, once. Only `available`
 *  entries are drawn: the overlay is the app's promise about its own
 *  keyboard, so it may not list a key that does nothing. */
function fillShortcuts(dialog) {
  const body = dialog.querySelector('[data-role="shortcuts-body"]');
  if (body === null || dialog.dataset.filled === "true") return;
  for (const group of GROUP_ORDER) {
    const entries = registry.filter((entry) => entry.available && entry.group === group);
    if (entries.length === 0) continue;
    const section = document.createElement("section");
    section.className = "shortcuts-group";
    const heading = document.createElement("h3");
    heading.className = "shortcuts-group-title";
    heading.textContent = group;
    section.append(heading);
    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = "shortcuts-row";
      const label = document.createElement("span");
      label.className = "shortcuts-label";
      label.textContent = entry.label;
      const keys = document.createElement("span");
      keys.className = "shortcuts-keys";
      entry.keys.forEach((binding, index) => {
        if (index > 0) {
          const or = document.createElement("span");
          or.className = "shortcuts-or";
          or.textContent = "or";
          keys.append(or);
        }
        keys.append(keyChips(binding));
      });
      row.append(label, keys);
      section.append(row);
    }
    body.append(section);
  }
  dialog.dataset.filled = "true";
  // A backdrop click lands on the dialog element itself — the only way a
  // native `<dialog>` offers to close on click-out. Deliberately not via
  // the `close` event, which does not fire at all in a tab the browser has
  // marked hidden.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector('[data-role="shortcuts-close"]')?.addEventListener("click", () => {
    dialog.close();
  });
}

/** Open the `?` overlay. Exported so the top bar's Help control can reach
 *  the same panel the key does. */
export function openShortcuts() {
  const dialog = document.getElementById("shortcuts");
  if (dialog === null) return false;
  fillShortcuts(dialog);
  if (!dialog.open) dialog.showModal();
  return true;
}

// ---------------------------------------------------------------------
// The palette
// ---------------------------------------------------------------------

/** How `static/js/palette.js` hands this file its opener.
 *
 *  The import runs one way only — palette.js imports the registry (for the
 *  chips it draws and for the `run()` that makes an "action" result
 *  actually happen) and registers itself here; this file never imports it
 *  back. An ES-module cycle would work in a browser, but only by accident
 *  of when each side first touches the other's bindings, and a
 *  temporal-dead-zone throw during module evaluation is exactly the kind
 *  of failure this app has no way to see. */
let openPalette = null;

export function registerPalette(open) {
  openPalette = open;
}

/** `Mod+K` and `g l`. Declined rather than rejected when palette.js is
 *  missing: a module that failed to load is an outage, not a key the
 *  reader nearly pressed, and there is nothing sensible to shake. */
function showPalette(mode) {
  return openPalette === null ? undefined : openPalette(mode);
}

// ---------------------------------------------------------------------
// Compose
// ---------------------------------------------------------------------

/** How `static/js/compose.js` hands this file its surface — the same
 *  one-way shape `registerPalette` above uses, and for the same reason:
 *  compose.js imports the registry, the registry never imports it back,
 *  so there is no module cycle whose evaluation order decides whether the
 *  keyboard works.
 *
 *  Every method answers this file's three-value contract — `true` it ran,
 *  `false` it is live here but had nothing to act on, `undefined` it
 *  declines and the keystroke goes back to the browser. */
let compose = null;

export function registerCompose(surface) {
  compose = surface;
}

function composeCall(name, argument) {
  return compose === null ? undefined : compose[name](argument);
}

// ---------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------

/** How `static/js/labels.js` hands this file its surface — the same
 *  one-way shape `registerPalette` and `registerCompose` use, and for the
 *  same reason: labels.js imports the registry, the registry never imports
 *  it back, so no module cycle decides whether the keyboard works.
 *
 *  `null` until that module loads, which is what makes `l` and `v` fall
 *  through to the browser on a page that never loaded it rather than
 *  throwing. */
let labels = null;

export function registerLabels(surface) {
  labels = surface;
}

function labelsCall(name) {
  return labels === null ? undefined : labels[name]();
}

// ---------------------------------------------------------------------
// Runners
// ---------------------------------------------------------------------

/** One per `DEFAULTS` id. A runner returns `false` when the key was live
 *  but had nothing to do; anything else counts as handled. */
const RUNNERS = {
  next: () => withList((list) => list.move(1)),
  prev: () => withList((list) => list.move(-1)),
  open: () => openFocused(),
  back: () => backToList(),
  "goto-inbox": () => goto("inbox"),
  "goto-starred": () => goto("starred"),
  "goto-sent": () => goto("sent"),
  "goto-drafts": () => goto("drafts"),
  "goto-all": () => goto("all"),
  "goto-label": () => showPalette("goto"),

  select: () => withList((list) => list.toggle(list.focusId)),
  "extend-next": () => withList((list) => list.extend(1)),
  "extend-prev": () => withList((list) => list.extend(-1)),
  "select-all": () => withList((list) => list.selectMatching("all")),
  "select-none": () => withList((list) => list.selectMatching("none")),
  "select-read": () => withList((list) => list.selectMatching("read")),
  "select-unread": () => withList((list) => list.selectMatching("unread")),
  "select-starred": () => withList((list) => list.selectMatching("starred")),
  "select-unstarred": () => withList((list) => list.selectMatching("unstarred")),

  archive: () => inView("trash", "restore", "archive"),
  delete: () => inView("trash", "destroy", "delete"),
  spam: () => inView("spam", "unspam", "spam"),
  star: () => toggleStar(),
  "mark-read": () => act("read"),
  "mark-unread": () => act("unread"),
  "archive-older": () => archiveThen(1),
  "archive-newer": () => archiveThen(-1),
  undo: () => {
    window.om?.undoLast?.();
    return true;
  },
  label: () => labelsCall("picker"),
  move: () => labelsCall("move"),
  more: () => undefined,

  "next-message": () => moveCard(1),
  "prev-message": () => moveCard(-1),
  "expand-all": () => foldAll(true),
  "collapse-all": () => foldAll(false),
  "mark-unread-from-here": () => markUnreadFromHere(),
  reply: () => composeCall("reply", "reply"),
  "reply-all": () => composeCall("reply", "reply_all"),
  forward: () => composeCall("reply", "forward"),

  send: () => composeCall("send"),
  "add-cc": () => composeCall("reveal", "cc"),
  "add-bcc": () => composeCall("reveal", "bcc"),

  shortcuts: () => openShortcuts(),
  // Spec §6.1 writes `Esc` as one key with several meanings, so it stays
  // one entry whose runner asks the narrowest question first: the caret in
  // the search pill, then an open compose window, then the selection. A
  // second entry per surface would have claimed the same binding three
  // times and told the `?` overlay three different things about one
  // keystroke.
  escape: () => {
    if (blurSearch()) return true;
    return composeCall("close") ?? clearSelection();
  },
  compose: () => composeCall("open"),
  search: () => focusSearch(),
  // ...and the same shape for `Mod+K`, which spec §6.1 gives to the
  // palette globally and to "link" inside a compose window. `compose.link`
  // declines (returns `undefined`) whenever the caret is not in a rich-text
  // editor, so everywhere else in the app this is still the palette.
  palette: () => composeCall("link") ?? showPalette("command"),
};

/** Fill `registry` from the table above. `store` binds the list store the
 *  runners drive; passing nothing means "look it up on Alpine when the key
 *  is actually pressed", which is what the app does — `app.js` registers
 *  the stores and calls this with the one it just made. */
export function registerDefaults(store = null) {
  bound = store;
  registry.length = 0;
  for (const entry of DEFAULTS) {
    registry.push({
      id: entry.id,
      keys: entry.keys,
      scope: entry.scope,
      group: entry.group,
      label: entry.label,
      available: entry.available,
      run: RUNNERS[entry.id],
    });
  }
  return registry;
}

// ---------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------

function shake(el) {
  const target = el ?? document.activeElement;
  if (target === null || typeof target.animate !== "function") return;
  // Never the whole page. With nothing focused `document.activeElement` is
  // `<body>`, and a rejected key would wobble the entire app — which reads
  // as a bug, not as feedback about one control.
  if (target === document.body || target === document.documentElement) return;
  if (reduced?.matches) return;
  target.animate(
    [
      { transform: "translateX(0)" },
      { transform: "translateX(-3px)" },
      { transform: "translateX(3px)" },
      { transform: "translateX(0)" },
    ],
    { duration: SHAKE_MS, easing: "ease-in-out" },
  );
}

/** Which scopes are live. See this file's header for why a conversation
 *  keeps the `list` bindings.
 *
 *  **`compose` is keyed on focus, not on existence**, and that is the
 *  whole reason an open compose dock leaves the inbox behind it working.
 *  A dock is a sibling of the list, not a modal over it, so it is
 *  routinely on screen while the reader is triaging — and if merely
 *  having one open made this scope live, `Mod+Enter` would try to send a
 *  message the reader was not looking at. The window has to actually hold
 *  the caret. */
function activeScopes() {
  const scopes = new Set(["global"]);
  if (document.querySelector(LIST) !== null) scopes.add("list");
  if (document.querySelector("[data-thread-id]") !== null) {
    scopes.add("list");
    scopes.add("thread");
  }
  if (document.activeElement?.closest?.(COMPOSE) != null) scopes.add("compose");
  return scopes;
}

function suppressed(event) {
  if (event.defaultPrevented) return true;
  // A dead key, or an IME mid-composition: the keystroke is not a command.
  if (event.isComposing || event.keyCode === 229) return true;
  // Spec §6.1's settings toggle. Absent means on.
  //
  // The palette's chord is deliberately exempt. The toggle exists because
  // single-key shortcuts fire while you are reading mail -- `e` archives
  // whatever is focused -- and someone who does not want that needs a way
  // out. A chord cannot go off by accident, and the palette is the only
  // route to the command surface: there is no button for it in the top
  // bar, so honouring the toggle here would leave "turn off shortcuts"
  // silently meaning "lose the command palette until you find the gear".
  if (document.documentElement.dataset.shortcuts === "off") {
    const isPaletteChord = event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey);
    if (!isPaletteChord) return true;
  }
  const target = event.target ?? null;
  const exempt = event.key === "Escape" || event.metaKey || event.ctrlKey;
  if (!exempt && target?.closest?.(TYPING)) return true;
  // A *bare* Enter/Space on a control belongs to the platform. A chord
  // does not: nothing activates a button on Cmd+Enter, and compose's own
  // `Mod+Enter` has to reach dispatch from inside the subject field and
  // the plain-text body, both of which match `ACTIVATES`.
  if (!exempt && (event.key === "Enter" || event.key === " ") && target?.closest?.(ACTIVATES)) {
    return true;
  }
  return false;
}

let pending = null;
let pendingTimer = null;

function clearPending() {
  pending = null;
  if (pendingTimer !== null) window.clearTimeout(pendingTimer);
  pendingTimer = null;
}

/** The entry a keystroke names, preferring one whose scope is live — and
 *  among those, the *narrowest* one (`SCOPE_RANK`).
 *
 *  Table order decides nothing here, deliberately. `Shift+U` is claimed by
 *  `mark-unread` at `list` scope and by `mark-unread-from-here` at `thread`
 *  scope, and both are live while reading a conversation; picking the first
 *  match would have made which one runs a property of where somebody
 *  happened to paste a line into `DEFAULTS`.
 *
 *  A match found only outside the live scopes comes back as `active: false`
 *  so the caller can reject it rather than silently running it. */
function lookup(event, prefix, scopes) {
  let best = null;
  let elsewhere = null;
  for (const entry of registry) {
    for (const binding of entry.keys) {
      const steps = binding.split(" ");
      if (steps.length !== (prefix === null ? 1 : 2)) continue;
      if (prefix !== null && steps[0] !== prefix) continue;
      if (!matchesChord(steps[steps.length - 1], event)) continue;
      if (!scopes.has(entry.scope)) {
        if (elsewhere === null) elsewhere = { entry: entry, active: false };
        continue;
      }
      if (best === null || SCOPE_RANK[entry.scope] > SCOPE_RANK[best.entry.scope]) {
        best = { entry: entry, active: true };
      }
    }
  }
  return best ?? elsewhere;
}

/** The first step of a live two-key sequence this keystroke begins, if any. */
function opensSequence(event, scopes) {
  for (const entry of registry) {
    if (!entry.available || !scopes.has(entry.scope)) continue;
    for (const binding of entry.keys) {
      const steps = binding.split(" ");
      if (steps.length === 2 && matchesChord(steps[0], event)) return steps[0];
    }
  }
  return null;
}

/** Three answers a runner can give, and they are not the same thing:
 *
 *  `true`   it ran — the keystroke is ours, so the platform does not also
 *           get it.
 *  `false`  it is live here and could not run (`e` with nothing to act
 *           on). Ours, and rejected: claim the key and shake.
 *  `undefined`  it declined — this key means nothing in this situation, so
 *           the keystroke is given back untouched. `Esc` with nothing
 *           selected is the case that matters: swallowing it would take
 *           `Esc` away from whatever else on the page might want it.
 */
function fire(found, event) {
  // Reserved, not implemented: silence, so the UI never advertises a key
  // that does nothing.
  if (!found.entry.available) return;
  if (!found.active) {
    event.preventDefault();
    shake();
    return;
  }
  const result = found.entry.run?.();
  if (result === undefined) return;
  event.preventDefault();
  if (result === false) shake();
}

export function dispatch(event) {
  if (suppressed(event)) return;
  // A modal owns the keyboard while it is up: the shortcuts overlay and the
  // bulk-action confirmation both close on `Esc` through the platform, and
  // nothing behind them may act on a keystroke meant for them.
  if (document.querySelector("dialog[open]") !== null) {
    clearPending();
    return;
  }
  if (event.key === "Shift" || event.key === "Control") return;
  if (event.key === "Alt" || event.key === "Meta") return;

  const scopes = activeScopes();
  const prefix = pending;
  clearPending();

  if (prefix !== null) {
    const found = lookup(event, prefix, scopes);
    if (found === null) {
      event.preventDefault();
      shake();
      return;
    }
    fire(found, event);
    return;
  }

  const found = lookup(event, null, scopes);
  if (found !== null && found.active && found.entry.available) {
    fire(found, event);
    return;
  }

  const opener = opensSequence(event, scopes);
  if (opener !== null) {
    event.preventDefault();
    pending = opener;
    pendingTimer = window.setTimeout(clearPending, SEQUENCE_MS);
    return;
  }

  if (found !== null) fire(found, event);
}
