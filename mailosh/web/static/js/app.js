// Mailosh — app bootstrap: the two Alpine stores every later task reads,
// and the DOM bookkeeping that makes the list behave like an ARIA grid
// (roving tabindex, arrow-key navigation, a focus ring that survives a
// swap).
//
// What this file deliberately does NOT do: decide what any *shortcut*
// means (keys.js owns the registry and dispatch; this file only hands it
// the store and binds the listener), the command palette (palette.js,
// Task 11), and any action request at all (actions.js, Task 9 — which owns
// `[data-action]` dispatch, the POSTs, and undo). The stores below are the
// shared state those three bind to; nothing here mutates mail.
//
// One request does live here, and only one: `POST /prefs`. Quick settings
// (spec §10) is a property of the *shell* rather than of the mailbox — it
// writes `<html>`'s dataset, which is what `[data-theme]`/`[data-density]`
// in styles/input.css select on — and both surfaces that change a
// preference (the gear's panel and the palette's "Theme: dark" result) go
// through the same `ui.setPref()` below, so there is one write and one
// optimistic paint rather than two that could disagree.
//
// ---------------------------------------------------------------------
// The grid, and why every row control is `tabindex="-1"`
//
// `#list` is `role="grid"`, each row is `role="row"`, and the rows share
// ONE tab stop between them (the ARIA "roving tabindex" pattern): the
// focused row carries `tabindex="0"` and every other row — and every
// control inside every row — carries `-1`. Tab therefore enters the list
// once and leaves it once; arrows move inside it.
//
// The alternative, which is what shipped before this task, is worth
// spelling out because it looked correct: rows were reachable, but the
// checkbox, the star and the three hover actions were all natively
// focusable, and `.row:focus-within` *revealed* the hover actions
// mid-traversal. A measured tab-walk stopped 5 times per row — about 251
// stops on a 50-row page — and the row it finally landed on did nothing,
// because nothing bound `Enter`. Reachable and inert is worse than
// unreachable. Both halves are fixed here: `onGridKeydown` below moves the
// cursor and `Enter`/`o` (keys.js's `open` entry) activates the row.
//
// The app's CSP is `script-src 'self'` with no `'unsafe-eval'`, and what
// is vendored is Alpine's **CSP build** (`@alpinejs/csp`, see NOTICE and
// the Makefile) — the standard build evaluates every inline expression
// through `new AsyncFunction`, which fails at runtime the first time a
// directive is evaluated, not at build time. This is a mail client, the
// app category where XSS is the highest-consequence bug and where we
// deliberately render attacker-controlled HTML, so the CSP stays and the
// evaluator gives way.
//
// What that costs any task adding markup is narrower than it sounds. The
// CSP build replaces `new AsyncFunction` with its own mini-JS parser —
// tokenizer, recursive-descent parser, tree-walking evaluator, and no
// `Function` constructor anywhere in the bundle — and that parser does
// evaluate method calls WITH arguments. `list.select(id)`, `toggle(id)`,
// `move(delta)` and `ui.toast(msg, token)` are all callable from a
// directive, as are property paths, computed access (`row[key]`),
// ternaries, arithmetic, comparison, `&&`/`||`, assignment, and
// object/array literals in `x-data`. (Checked against the vendored
// 3.17.1 bytes, not taken on trust: an earlier version of this comment
// asserted the opposite and was wrong.)
//
// What it does reject: template literals; anything not in the Alpine
// scope, `window`, `document`, `Math` and `JSON` included ("Undefined
// variable"); more than one statement per expression (`a(); b()`);
// spread and destructuring; shorthand object keys (`{ open }` — write
// `open: open`); optional chaining; `constructor`/`__proto__`/
// `setAttribute` and friends, which are an explicit blocklist; and every
// form of inline function — arrow, `function`, method shorthand — so an
// `x-data` literal carries data and its methods come from
// `Alpine.data()`.
//
// htmx's `hx-on:`, `hx-vals='js:…'` and `hx-trigger="…[expr]"` are
// unavailable too, and that part genuinely is a `new Function` problem:
// htmx compiles those attribute values that way, which is what
// `script-src 'self'` without `'unsafe-eval'` forbids. Registering
// stores and reading them from plain JS (as here, and as sse.js does)
// needs no evaluator at all.
//
// One directive does ship with this task — the toolbar's `x-show`, which
// swaps the list toolbar for the selection toolbar. It reads
// `$store.list.count`, a plain number `render()` below writes, rather than
// `selected.size`: whether a Set behind Alpine's reactive proxy tracks
// `size` is a question about `@vue/reactivity`'s collection handlers, and
// a number is not a question at all.

import { dispatch, registerDefaults } from "./keys.js";

const LIST = "#list";
const ROW_SELECTOR = "#list [data-id]";
// Every control that re-GETs the whole list into `#list` and therefore has
// to be told how many rows are actually on screen — see `syncListLimit`.
const LIST_REFETCHERS = [LIST, '[data-role="refresh"]'];

function rows() {
  return Array.from(document.querySelectorAll(ROW_SELECTOR));
}

// One delegated `click` listener is the whole click model for the list.
// Three things about it are load-bearing for the tasks that extend it:
//
// 1. CAPTURE phase. htmx binds its own click listener to the row's
//    stretched anchor (`.row-link`), which is deeper in the tree than
//    anything we can delegate from, so a capture-phase listener above it
//    is the only kind that gets to decide anything first.
// 2. `document.body`, not `#list`. A nav or pager swap replaces `#main`'s
//    innerHTML and `#list` goes with it; body never does.
// 3. Row controls need NOTHING here to keep their clicks off the
//    conversation. `.row-check`, `.row-star` and `.row-actions` are
//    *siblings* of `.row-link` (the anchor is `position: absolute;
//    inset: 0; z-index: 0`, the controls sit at `z-index: 1` above it),
//    so a click on a control never passes through the anchor and htmx
//    never sees it. This file used to call `stopPropagation()` on each
//    control for that — correct back when the row itself carried the
//    `hx-get`, obsolete once the anchor landed, and by then actively
//    harmful: the elements it silenced are exactly the ones carrying
//    `data-action`, so no delegated listener could ever have seen them.
//
// This listener's whole job is the modifier branch below. `[data-action]`
// dispatch — every triage action, the selection toggle, and the undo
// toast's POST — is actions.js's sibling listener on this same element,
// in the ordinary bubble phase: nothing stops a control's click, so it
// has no one to beat and needs no capture. That is one delegated model in
// two files, not two models; shift-click ranges live in actions.js's
// `select` case rather than in a third listener here, which is also why
// the row keeps every native modifier click the branch below restores.
// Both phases can claim a gesture with `preventDefault()` — which is why
// the branch below calls neither `preventDefault()` nor
// `stopImmediatePropagation()`, only `stopPropagation()`.
function onClick(event) {
  const link = event.target?.closest?.(".row-link");
  if (!link) return;
  // Modifier clicks on a link belong to the browser. htmx's boosted-anchor
  // path hands back only `ctrlKey || metaKey` and unconditionally
  // `preventDefault()`s everything else, so Shift-click ("open in a new
  // window") and Alt-click ("save link as") both collapsed into an
  // in-place swap. Middle-click was never affected — that is `auxclick`,
  // which htmx does not bind. Keeping the event away from htmx's listener
  // does not touch the default action, so the browser does whatever the
  // platform says these gestures mean.
  if (event.shiftKey || event.altKey) event.stopPropagation();
}
document.body.addEventListener("click", onClick, true);

function store(name) {
  return window.Alpine?.store?.(name) ?? null;
}

// Endless scroll appends pages into `#list`, but `#list`'s own refresh is
// baked into the markup as `?position=<page>&limit=<page size>`. Once the
// sentinel has grown the list to 150 rows, one `mail:changed` re-GET asks
// for 50 again and morphs the reader's list back down to a single page —
// clamping scrollTop and re-arming the sentinel, on every incoming message
// (review finding B3). The position stays the page's own (a `position=0`
// refetch would paint page 1 under a URL that says page 2); the limit grows
// to cover what is actually rendered.
//
// Rewriting the attribute is not enough on its own: htmx snapshots
// `hx-get` when it first processes a node, so `htmx.process()` has to
// re-read it. `hx-vals='js:…'` — the obvious alternative — is unavailable
// under this app's `script-src 'self'` CSP, which is also why the value is
// not computed in the markup.
function syncListLimit() {
  const listEl = document.querySelector(LIST);
  if (!listEl) return;
  const rendered = listEl.querySelectorAll("[data-id]").length;
  for (const selector of LIST_REFETCHERS) {
    const el = document.querySelector(selector);
    const raw = el?.getAttribute("hx-get");
    if (!raw) continue;
    const url = new URL(raw, document.baseURI);
    if (rendered <= Number(url.searchParams.get("limit") ?? 0)) continue;
    url.searchParams.set("limit", String(rendered));
    el.setAttribute("hx-get", url.pathname + url.search);
    window.htmx?.process?.(el);
  }
}

/** The three quick-settings fields `mailosh/web/prefs.py` accepts, and
 *  the only names `setPref` will write or post. A `data-pref` naming
 *  anything else is markup asking for a preference this app does not
 *  have, and is dropped rather than posted for a 422. */
const PREF_FIELDS = ["theme", "density", "shortcuts"];

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
}

/** POST one or more preferences and hand back what the server says it
 *  actually stored (its `om:prefs` trigger), or `null` if it did not.
 *
 *  `HX-Request` for the same reason actions.js sends it: an expired
 *  session answers a plain request with a 303 to /login, which `fetch`
 *  would follow and this would read as a successful save. */
function savePrefs(values) {
  const body = new URLSearchParams();
  for (const name of Object.keys(values)) {
    const value = values[name];
    body.append(name, typeof value === "boolean" ? String(value) : value);
  }
  return fetch("/prefs", {
    method: "POST",
    body: body,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRF-Token": csrfToken(),
      "HX-Request": "true",
    },
  }).then((response) => {
    if (response.status === 401) {
      const location = response.headers.get("HX-Redirect");
      if (location) window.location.assign(location);
      return null;
    }
    if (!response.ok) return null;
    const raw = response.headers.get("HX-Trigger");
    if (!raw) return null;
    try {
      return JSON.parse(raw)["om:prefs"] ?? null;
    } catch {
      return null;
    }
  });
}

const ui = {
  // Rendered server-side onto <html> (spec §4.1: no flash), so the DOM is
  // the source of truth here, not a default guessed in JS.
  theme: document.documentElement.dataset.theme || "system",
  density: document.documentElement.dataset.density || "comfortable",
  // Absent means on, matching keys.js's own reading of the attribute.
  shortcuts: document.documentElement.dataset.shortcuts !== "off",
  offline: false,

  /** Apply one preference to the page, without persisting it.
   *
   *  `<html>`'s dataset is the whole mechanism: `[data-theme=dark]` and
   *  `[data-density=compact]` in styles/input.css swap the token blocks
   *  and `--row-h`, and keys.js reads `[data-shortcuts=off]`. So a theme
   *  change is one attribute write, and it lands before the request that
   *  records it has left. */
  applyPref(name, value) {
    if (!PREF_FIELDS.includes(name)) return;
    this[name] = value;
    const root = document.documentElement;
    if (name === "shortcuts") root.dataset.shortcuts = value ? "on" : "off";
    else root.dataset[name] = value;
    this.syncPrefControls();
  },

  /** Put every `[data-pref]` control in agreement with the state above.
   *
   *  The quick-settings panel is served once, with the preferences the
   *  page was rendered for; a theme changed from the palette afterwards
   *  would leave its radio behind. This is the one place that state
   *  becomes control state — the same job `list.render()` does for
   *  selection — so the panel is right whenever it is opened, whichever
   *  surface last changed something. */
  syncPrefControls() {
    for (const control of document.querySelectorAll("[data-pref]")) {
      const name = control.dataset.pref;
      if (!PREF_FIELDS.includes(name)) continue;
      if (control.type === "checkbox") control.checked = this[name] === true;
      else control.checked = control.value === this[name];
    }
  },

  /** Apply a preference and persist it (spec §10: "applied live with
   *  optimistic preview and persisted").
   *
   *  Optimistic, and reverted on failure the same way an archive is: the
   *  reader sees the new theme immediately, and if the write did not land
   *  the page goes back to what is actually stored rather than showing a
   *  setting that will be gone on the next load. */
  setPref(name, value) {
    if (!PREF_FIELDS.includes(name)) return Promise.resolve(null);
    const before = this[name];
    if (before === value) return Promise.resolve(null);
    this.applyPref(name, value);
    const values = {};
    values[name] = value;
    const settle = (stored) => {
      // Superseded: the reader changed this preference again while the
      // request was in flight, so neither the echo nor the revert may
      // touch it — the later click is the one that is true, and its own
      // response is still to come.
      if (this[name] !== value) return stored;
      if (stored === null) {
        this.applyPref(name, before);
        this.toast("Couldn't save that setting");
        return null;
      }
      // The server is the authority on what it kept; anything it names is
      // applied over the guess, which for a single-field POST is normally
      // the same value arriving twice.
      for (const field of Object.keys(stored)) this.applyPref(field, stored[field]);
      return stored;
    };
    // One settle for both failures — a refused write and a request that
    // never arrived look the same to the reader, and a revert that ran on
    // one path and not the other would leave the page lying.
    return savePrefs(values).then(settle, () => settle(null));
  },

  // Spec §6.3's undo toast. This renders it and reports the click; the
  // POST to /a/undo belongs to actions.js (Task 9), which listens for
  // `om:undo`. Auto-dismisses after the 10s undo window.
  //
  // Undo here is a *reverse operation on an already committed action*, not
  // a delayed commit: the mail moved the moment the row collapsed, and
  // "Undo" puts it back. So this is not a cancellation window, and no
  // copy that reaches it may suggest one — the toast text is past tense
  // ("Archived") and the affordance is a verb, not "Cancel".
  //
  // `note` is a second, quieter line for the one thing a toast sometimes
  // has to explain: an action too large to be undoable at all
  // (`undo_unavailable: "too_many"`). actions.js owns that wording and
  // decides when it applies — in particular `"no_change"` never reaches
  // here, because the action it accompanies succeeded and the reader has
  // nothing to be told.
  toast(message, undoToken = null, note = null, timeout = 10000) {
    const host = document.getElementById("toasts");
    if (!host) return null;
    const el = document.createElement("div");
    el.className = "toast pointer-events-auto";
    const text = document.createElement("span");
    text.textContent = message;
    el.append(text);
    if (note) {
      const explanation = document.createElement("span");
      explanation.className = "toast-note";
      explanation.textContent = note;
      el.append(explanation);
    }
    // The 150ms exit half of the toast's motion (design polish v2 §6): the
    // node is kept alive long enough for `.is-leaving` to play, and the
    // reduced-motion block zeroes that transition so it is instant there.
    const dismiss = () => {
      el.classList.add("is-leaving");
      setTimeout(() => el.remove(), 150);
    };
    if (undoToken) {
      const undo = document.createElement("button");
      undo.type = "button";
      // No `text-accent`: the toast is an inverted surface, so its
      // affordance takes the *other* theme's accent — `.toast button` in
      // styles/input.css owns that (`--toast-link`, polish v2 §5.7).
      // `--accent` on `--toast` would have been 1.6:1 in light.
      undo.textContent = "Undo";
      undo.addEventListener("click", () => {
        dismiss();
        document.body.dispatchEvent(
          new CustomEvent("om:undo", { bubbles: true, detail: { token: undoToken } }),
        );
      });
      el.append(undo);
    }
    host.append(el);
    // The toast stack is not a live region (a queue of them would each
    // interrupt the last); `#status` is, so the same words go there once.
    const status = document.getElementById("status");
    if (status) status.textContent = note ? message + ". " + note : message;
    setTimeout(dismiss, timeout);
    return el;
  },
};

// ---------------------------------------------------------------------
// Quick settings (spec §10)
// ---------------------------------------------------------------------
//
// A native `<dialog>` (`shell/quick_settings.html`) opened with
// `showModal()`: the focus trap, `Esc`, and the backdrop whose click is a
// `<dialog>`'s only close-on-click-out all come from the platform. Closed
// from the handlers rather than from a `close` listener — that event does
// not arrive at all in a tab the browser has marked hidden, which is the
// same trap actions.js's confirmation documents.

function quickSettings() {
  return document.getElementById("quick-settings");
}

function openQuickSettings() {
  const dialog = quickSettings();
  if (dialog === null) return false;
  // Whatever last changed a preference — this panel, the palette, another
  // tab's reload — the controls agree with the page before it is shown.
  (store("ui") ?? ui).syncPrefControls();
  if (!dialog.open) dialog.showModal();
  return true;
}

function closeQuickSettings() {
  const dialog = quickSettings();
  if (dialog !== null && dialog.open) dialog.close();
}

// The gear, the panel's own close button, and a click on the backdrop.
// One delegated listener on `document.body` rather than listeners bound to
// the dialog: the same model as every other click in this file, and the
// gear lives in the top bar, which no swap replaces either.
document.body.addEventListener("click", (event) => {
  const dialog = quickSettings();
  if (dialog !== null && event.target === dialog) {
    closeQuickSettings();
    return;
  }
  const control = event.target?.closest?.('[data-role="settings"], [data-role="quick-close"]');
  if (control === null || control === undefined) return;
  event.preventDefault();
  if (control.dataset.role === "settings") openQuickSettings();
  else closeQuickSettings();
});

// `change`, not `click`: these are real radios and a real checkbox, so the
// event fires for a click, for Space, and for the arrow keys that move
// through a radio group — one listener covers every way a reader can
// reach them.
document.body.addEventListener("change", (event) => {
  const control = event.target?.closest?.("[data-pref]");
  if (control === null || control === undefined) return;
  const value = control.type === "checkbox" ? control.checked : control.value;
  (store("ui") ?? ui).setPref(control.dataset.pref, value);
});

// `mailosh/web/prefs.py` answers 204 with `HX-Trigger: {"om:prefs": {…}}`.
// `savePrefs` reads that header itself, so this listener is for anything
// *else* that ever persists a preference through htmx — it applies what
// the server says was stored, and applying a value that is already set is
// a no-op.
document.body.addEventListener("om:prefs", (event) => {
  const changed = event.detail ?? null;
  if (changed === null || typeof changed !== "object") return;
  const target = store("ui") ?? ui;
  for (const name of Object.keys(changed)) target.applyPref(name, changed[name]);
});

/** Which rows `* r`/`* u`/`* s`/`* t` mean, read off the row's own
 *  rendering rather than off a parallel model — `.is-unread` and
 *  `.row-star.is-on` are already what the optimistic paint in actions.js
 *  flips, so a selection made here can never disagree with what is on
 *  screen. */
function matchesKind(el, kind) {
  if (kind === "all") return true;
  if (kind === "none") return false;
  const unread = el.classList.contains("is-unread");
  if (kind === "read") return !unread;
  if (kind === "unread") return unread;
  const starred = el.querySelector(".row-star")?.classList.contains("is-on") === true;
  if (kind === "starred") return starred;
  if (kind === "unstarred") return !starred;
  return false;
}

const list = {
  focusId: null,
  selected: new Set(),
  // The two reactive readouts the toolbar binds to. `render()` is the only
  // writer; see the CSP note in this file's header for why they are plain
  // values rather than `selected.size` read through a directive.
  //: Whether the last `ensureFocus` ran with no `#list` in the document —
  //: i.e. the reader is inside a conversation. Read once on the way back to
  //: decide whether to restore their scroll position.
  away: false,
  //: The list's scroll offset as it was before the most recent swap.
  scrollTop: 0,
  count: 0,
  countLabel: "0 selected",
  // Where a range starts: the last row selected by an unmodified click or
  // by `x`. Shift-clicking a checkbox fills from here to there.
  anchorId: null,

  ids() {
    return rows().map((el) => el.dataset.id);
  },

  select(id, on = true) {
    if (on) {
      this.selected.add(id);
      this.anchorId = id;
    } else {
      this.selected.delete(id);
    }
    this.render();
  },

  toggle(id) {
    if (id === null || id === undefined) return;
    this.select(id, !this.selected.has(id));
  },

  clear() {
    this.selected.clear();
    this.anchorId = null;
    this.render();
  },

  /** Everything from `fromId` to `toId` inclusive, in list order — the
   *  shift-click range. Additive, never subtractive: a range is a widening
   *  gesture, and dropping rows the reader picked one at a time before
   *  shift-clicking would be a surprise. */
  range(fromId, toId) {
    const ids = this.ids();
    const from = ids.indexOf(fromId);
    const to = ids.indexOf(toId);
    if (from === -1 || to === -1) {
      this.toggle(toId);
      return;
    }
    for (let at = Math.min(from, to); at <= Math.max(from, to); at += 1) {
      this.selected.add(ids[at]);
    }
    this.focusId = toId;
    this.render();
  },

  /** `* a`/`* n`/`* r`/`* u`/`* s`/`* t`. Each one *replaces* the
   *  selection: "select unread" names a set, it does not add to one. */
  selectMatching(kind) {
    this.selected.clear();
    this.anchorId = null;
    for (const el of rows()) {
      if (matchesKind(el, kind)) this.selected.add(el.dataset.id);
    }
    this.render();
  },

  /** The toolbar's tri-state box: nothing selected -> everything, anything
   *  selected -> nothing. */
  selectAll(on = true) {
    this.selectMatching(on ? "all" : "none");
  },

  /** `Shift+J`/`Shift+K`. Takes the row it starts on, then adds the next
   *  one — or, walking back over a row it has already taken, gives that
   *  one back. That is what makes the pair a reversible walk rather than a
   *  selection that only ever grows (Gmail behaves the same way). */
  extend(delta) {
    const ids = this.ids();
    if (ids.length === 0) return;
    const at = ids.indexOf(this.focusId);
    const from = at === -1 ? 0 : at;
    this.selected.add(ids[from]);
    const to = from + delta;
    if (to < 0 || to >= ids.length) {
      this.render({ focus: true, scroll: true });
      return;
    }
    if (this.selected.has(ids[to])) this.selected.delete(ids[from]);
    else this.selected.add(ids[to]);
    this.focusId = ids[to];
    this.render({ focus: true, scroll: true });
  },

  // j/k and ArrowUp/ArrowDown. Clamped at both ends rather than wrapping —
  // Gmail does not wrap either.
  move(delta) {
    const ids = this.ids();
    if (ids.length === 0) return;
    const at = ids.indexOf(this.focusId);
    const next = at === -1 ? 0 : Math.min(ids.length - 1, Math.max(0, at + delta));
    this.focusId = ids[next];
    this.render({ focus: true, scroll: true });
  },

  // Home/End. A negative index counts from the end, so `moveTo(-1)` is the
  // last row without the caller having to know how many there are.
  moveTo(index) {
    const ids = this.ids();
    if (ids.length === 0) return;
    const at = index < 0 ? ids.length - 1 : Math.min(index, ids.length - 1);
    this.focusId = ids[at];
    this.render({ focus: true, scroll: true });
  },

  // Focus arrived at a row some other way — a click on its checkbox, a Tab
  // into the list. The cursor follows, so the next `x` or `e` means the row
  // the reader is actually looking at. Guarded against re-entry: `render()`
  // calls `.focus()`, which would otherwise come straight back here.
  setFocus(id) {
    if (id === this.focusId) return;
    this.focusId = id;
    this.render();
  },

  // Called after every swap. If the focused row is gone (archived, or
  // simply not on the page any more) focus lands on whatever now occupies
  // its position — the row that visually took its place — which is what
  // makes "archive, archive, archive" work without touching the mouse.
  ensureFocus(previousIds = null) {
    // The list being *absent* is not the list showing different rows, and
    // conflating the two is what lost a reader's work: opening a
    // conversation swaps `#list` out of the document entirely, so every
    // selected id looked "gone" and was dropped on the way *in* — pressing
    // `u` then came back to nothing selected and the scroll at the top.
    // Reproduced at 1200px as readily as at 390px, so this was never about
    // the single-pane layout that found it.
    //
    // Holding the state costs nothing while away: `render()` walks the rows
    // that exist, and there are none. The rows come back with the same ids
    // (`u` re-renders the same page of the same mailbox), and the clause
    // below then finds them all present and drops nothing.
    if (document.querySelector(LIST) === null) {
      this.away = true;
      this.render();
      return;
    }
    // True exactly once: on the swap that brings the list back. An ordinary
    // `mail:changed` refresh never sets `away`, which is what keeps this
    // from yanking the viewport under someone reading a list that just
    // refreshed itself.
    const returning = this.away === true;
    this.away = false;
    if (returning && this.scrollTop > 0) {
      const el = document.querySelector(LIST);
      // Clamped by the browser anyway, but do it explicitly: coming back to
      // a shorter list (a message archived while away) would otherwise ask
      // for an offset past the end and land at the bottom rather than where
      // the reader was.
      if (el !== null) el.scrollTop = Math.min(this.scrollTop, el.scrollHeight - el.clientHeight);
    }
    const ids = this.ids();
    // A selection only means anything about rows that are on screen. After
    // a swap that replaced them — a mailbox switch, a pager step — ids that
    // are gone are dropped, so the toolbar can never claim "3 selected"
    // over three rows nobody can see. A `mail:changed` refresh brings the
    // same ids back and so drops nothing.
    for (const id of Array.from(this.selected)) {
      if (!ids.includes(id)) this.selected.delete(id);
    }
    if (this.anchorId !== null && !ids.includes(this.anchorId)) this.anchorId = null;
    if (ids.length === 0) {
      this.focusId = null;
      this.render();
      return;
    }
    if (this.focusId !== null && ids.includes(this.focusId)) {
      this.render();
      return;
    }
    let index = 0;
    if (previousIds !== null && this.focusId !== null) {
      const was = previousIds.indexOf(this.focusId);
      if (was !== -1) index = Math.min(was, ids.length - 1);
    }
    this.focusId = ids[index];
    // Only take real DOM focus if the list already had it — a background
    // refresh must never steal the caret out of an input.
    //
    // `scroll` is deliberately the wider condition. Coming back from a
    // conversation, focus is on `<body>`, so `inList` is false and the
    // reader was returned to a list scrolled to the top with their row far
    // below. Scrolling the focused row into view is not stealing anything:
    // it moves a scrollport, not the caret. It is still refused when the
    // caret is somewhere that scrolling would disturb — a compose dock, the
    // search pill — which is the case `inList` was really guarding.
    const inList = document.activeElement?.closest?.(LIST) != null;
    this.render({ focus: inList, scroll: inList });
  },

  // The one place selection/focus state becomes DOM state.
  render({ focus = false, scroll = false } = {}) {
    const all = rows();
    for (const el of all) {
      const id = el.dataset.id;
      const selected = this.selected.has(id);
      const focused = id === this.focusId;
      el.classList.toggle("is-selected", selected);
      el.classList.toggle("is-focused", focused);
      el.setAttribute("aria-selected", selected ? "true" : "false");
      el.tabIndex = focused ? 0 : -1;
      const check = el.querySelector(".row-check");
      if (check) check.setAttribute("aria-pressed", selected ? "true" : "false");
      if (focused) {
        if (focus) el.focus({ preventScroll: true });
        if (scroll) el.scrollIntoView({ block: "nearest" });
      }
    }
    const chosen = this.selected.size;
    this.count = chosen;
    this.countLabel = chosen + " selected";
    // `querySelectorAll`, not `querySelector`: the select-all box exists in
    // both toolbars — the one the reader sees and the one `x-show` has
    // hidden — and the hidden one has to be right the moment it appears.
    const state = chosen === 0 ? "false" : chosen >= all.length ? "true" : "mixed";
    for (const box of document.querySelectorAll('[data-role="select-all"]')) {
      box.setAttribute("aria-checked", state);
    }
  },
};

// ---------------------------------------------------------------------
// The grid's own keyboard (WAI-ARIA "grid" pattern)
//
// Arrows move *within* the grid, Tab moves past it. Deliberately bound
// here rather than added to keys.js's registry: these are not shortcuts a
// reader learns or would look for in the `?` overlay, they are what a
// `role="grid"` is required to do, and they must only fire when focus is
// actually inside the list — a global ArrowDown would steal scrolling from
// the rest of the page.
//
// Bound on `document.body` (which no swap detaches) and filtered by
// `closest()`, the same delegated model as the click listeners above.
// ---------------------------------------------------------------------

/** The controls ArrowLeft/ArrowRight walk through inside one row, in DOM
 *  order and skipping whatever is not currently drawn — the hover actions
 *  are `display: none` until the row is hovered or holds focus, and the
 *  read/unread pair always has exactly one of the two showing. */
function rowControls(row) {
  const controls = row.querySelectorAll(".row-check, .row-star, .row-actions button");
  return Array.from(controls).filter((el) => el.offsetParent !== null);
}

function onGridKeydown(event) {
  if (event.defaultPrevented || event.isComposing) return;
  const row = event.target?.closest?.(ROW_SELECTOR);
  if (!row) return;
  const store_ = store("list") ?? list;

  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    store_.move(event.key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    store_.moveTo(event.key === "Home" ? 0 : -1);
    return;
  }
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    event.preventDefault();
    const controls = rowControls(row);
    const at = controls.indexOf(document.activeElement);
    if (event.key === "ArrowLeft") {
      if (at <= 0) row.focus({ preventScroll: true });
      else controls[at - 1].focus();
      return;
    }
    const next = at + 1;
    if (next < controls.length) controls[next].focus();
    return;
  }
  // Space is the platform's selection key in a multi-selectable grid, next
  // to Gmail's `x`. Only when the row itself has focus: on a control it
  // belongs to that control.
  if (event.key === " " && event.target === row) {
    event.preventDefault();
    store_.toggle(row.dataset.id);
  }
}
document.body.addEventListener("keydown", onGridKeydown);

// Focus that arrives at a row by any other route — Tab into the list, a
// click on a checkbox — moves the cursor with it.
document.body.addEventListener("focusin", (event) => {
  const row = event.target?.closest?.(ROW_SELECTOR);
  if (row) (store("list") ?? list).setFocus(row.dataset.id);
});

// The toolbar's tri-state box. `data-role`, not `data-action`: every
// `data-action` in this app names one of `mailosh/web/actions.py`'s six
// routes, and this control posts nothing at all.
document.body.addEventListener("click", (event) => {
  const box = event.target?.closest?.('[data-role="select-all"]');
  if (!box) return;
  event.preventDefault();
  const store_ = store("list") ?? list;
  store_.selectAll(store_.selected.size === 0);
});

function registerStores() {
  window.Alpine.store("ui", ui);
  window.Alpine.store("list", list);
  // The registry's runners drive this exact store instance rather than
  // looking it up per keystroke.
  registerDefaults(window.Alpine.store("list"));
}

// The CSP build's CDN bundle calls `Alpine.start()` in a microtask right
// after it executes (same as the standard one), which is before this
// deferred module runs — so `alpine:init`
// has usually already fired by now. Handle both orders; `Alpine.store()`
// is valid before and after start.
if (window.Alpine) registerStores();
else document.addEventListener("alpine:init", registerStores, { once: true });

// Spec §6.1's `keydown` on `window`, bound once and never re-bound: no swap
// can detach it, and keys.js reads the live DOM on every keystroke rather
// than holding a reference to anything a swap could replace. Filled with
// the unbound store first so the keyboard works even on a page whose Alpine
// failed to load — `registerStores` calls it again with the real store.
registerDefaults();
window.addEventListener("keydown", dispatch);

// After every swap the row set may be entirely different objects (or the
// same ones, morphed). Re-read it and put focus back where it belongs.
// Nothing needs re-binding: the click model is one delegated listener on
// `document.body`, which no swap can detach.
let idsBeforeSwap = null;
document.body.addEventListener("htmx:beforeSwap", () => {
  const state = store("list") ?? list;
  idsBeforeSwap = state.ids();
  // Remember where the reader actually was, not which row happens to be
  // focused. Those are different things and conflating them was the first
  // attempt at this: selecting rows with the checkbox never moves focus, so
  // "scroll the focused row into view" scrolled to whichever row the list
  // defaulted to — the top — which looks exactly like not restoring at all.
  const el = document.querySelector(LIST);
  if (el !== null) state.scrollTop = el.scrollTop;
});
document.body.addEventListener("htmx:afterSettle", () => {
  syncListLimit();
  const previous = idsBeforeSwap;
  idsBeforeSwap = null;
  (store("list") ?? list).ensureFocus(previous);
});

// …and once for the page as loaded. `htmx:afterSettle` does not fire on a
// first full page load, so everything above used to run only after the
// first swap: the served page carried no `tabindex="0"` anywhere, which
// left the whole list unreachable by Tab (review finding B1). `row.html`
// now renders the 0 on its first row so the list is reachable with no JS
// at all; this call is what puts `list.focusId` in agreement with it, so
// the first selection or `j` keypress does not move the cursor somewhere
// the reader was not looking.
syncListLimit();
(store("list") ?? list).ensureFocus();

// ---------------------------------------------------------------------
// Alpine's `x-show` vs idiomorph
// ---------------------------------------------------------------------

// `x-show` hides an element by writing `display: none` straight onto it.
// The server markup it is morphed against carries no inline style, so
// idiomorph — correctly, by its own rules — removes the attribute it sees
// as surplus. Alpine does not put it back, because its expression
// (`$store.list.count !== 0`) never changed: the DOM was edited underneath
// a reactive effect that had no reason to re-run. The visible symptom is
// the selection toolbar reappearing as "0 selected" after any navigation
// into `#main`, on top of the toolbar that should be there.
//
// The fix has to be narrow, and the first attempt was not. Refusing every
// `style` update on any node that *currently* has `x-show` also refused it
// when the incoming node was a different element entirely: navigating from
// the list to a conversation morphs a hidden `.list-toolbar` onto
// `.thread-scroll`, which has no `x-show` of its own, and the preserved
// `display: none` then hid every message. Reading mail broke to keep a
// toolbar honest.
//
// So the guard asks about **both** sides. `beforeNodeMorphed` sees the
// incoming node as well as the existing one, and the inline style is
// carried across only when both are `x-show` elements — the one case where
// it really is the same Alpine-managed element and Alpine really will not
// re-run. Everything else morphs untouched.
if (window.Idiomorph?.defaults?.callbacks) {
  const callbacks = window.Idiomorph.defaults.callbacks;
  const previous = callbacks.beforeNodeMorphed;
  callbacks.beforeNodeMorphed = (oldNode, newNode) => {
    const bothManaged =
      oldNode?.nodeType === 1 &&
      newNode?.nodeType === 1 &&
      oldNode.hasAttribute("x-show") &&
      newNode.hasAttribute("x-show");
    if (bothManaged) {
      const inline = oldNode.getAttribute("style");
      // Written onto the *incoming* node, so the morph sees the two as
      // already equal and leaves it alone. Nothing is refused, which is
      // why this cannot strand an unrelated element the way refusing did.
      if (inline) newNode.setAttribute("style", inline);
    }
    return previous ? previous(oldNode, newNode) : true;
  };
}
