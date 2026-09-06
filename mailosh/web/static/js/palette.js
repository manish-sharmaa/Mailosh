// Mailosh — the ⌘K command palette (design spec §6.2).
//
// One `<dialog id="palette">` (`templates/shell/palette.html`, rendered
// once by the app layout and outside every swap target) filled from
// `GET /palette/index` — `mailosh/web/palette.py`, which answers with the
// four groups this file draws: `actions`, `goto`, `labels`, `settings`.
//
// ---------------------------------------------------------------------
// Nothing here decides what a command *is*
//
// Three separate files already own the three kinds of result, and this one
// only finds them and hands over:
//
//   actions   `keys.js`'s registry entry, run through its own `run()` —
//             the same function `e` calls. Its id, its key chip and its
//             availability all come from that one table, so the palette
//             cannot offer a command the keyboard does not have, cannot
//             print a chip for a key that is not bound, and cannot need a
//             second mapping from `mark-read` to `om.act("read")`.
//   goto      `keys.js`'s `navigate()`, which clicks the sidebar's own
//             link — one definition of what a mailbox switch does.
//   settings  the `ui` store's `setPref()` (app.js), which is also what
//             the quick-settings panel calls, so a theme changed from
//             here and a theme changed from the gear are the same write.
//
// What is genuinely this file's own: the fuzzy match, the grouping, the
// cursor, and the 60 s cache.
//
// ---------------------------------------------------------------------
// Which modes exist in 1A, and why `l`/`v` are not among them
//
// Spec §6.2 describes four modes — `command`, `label`, `move`, `goto`.
// Two of them ship here:
//
//   command  (`Mod+K`) everything: Actions, Go to, Settings.
//   goto     (`g l`) the Go to group alone — every system mailbox and
//            every visible label, `Enter` navigates.
//
// `label` (`l`, "label as…") and `move` (`v`, "move to…") do **not**, and
// their registry entries stay `available: false`. There is no route in
// this phase that can apply a label to a conversation or move one to an
// arbitrary mailbox — `mailosh/web/actions.py` has exactly six, and all
// six are already reachable from the `command` mode above. A picker that
// listed every label and did nothing when you picked one is precisely the
// "advertise a broken key" this codebase refuses elsewhere (see keys.js's
// header), so the keys stay reserved and silent until the routes exist.
//
// ---------------------------------------------------------------------
// The CSP, and why this file builds its rows in JavaScript
//
// `script-src 'self'` with no `'unsafe-eval'`. The template ships the
// frame — input, results host, footer, and one `<template>` of design
// system icons to clone — and nothing else; every row is built here with
// `createElement`/`textContent`, so no attacker-controlled label (a
// mailbox name is user data) is ever parsed as markup. `command-score` is
// a real ES module (`vendor/command-score.js`, rewritten to `export
// default` at vendor time — see NOTICE) and is imported here rather than
// loaded by a `<script>` tag nothing referenced.

import commandScore from "../vendor/command-score.js";
import { keyChips, navigate, registerPalette, registry } from "./keys.js";

/** How long a fetched index is reused (the brief's own number). Long
 *  enough that opening the palette twice in a row costs one request;
 *  short enough that a label created in another tab shows up soon. Any
 *  `mail:changed` invalidates it early. */
const INDEX_TTL_MS = 60000;

/** `command-score` returns 0 for "no match at all" and something tiny for
 *  a match made only of character jumps. The brief's threshold. */
const THRESHOLD = 0.001;

/** What running a command is worth to its own next ranking, and how many
 *  are remembered. A boost rather than a separate "Recent" group: the
 *  reader's own habits reorder the list they already know instead of
 *  adding a fourth heading to read past. */
const RECENT_BOOST = 0.2;
const RECENT_MAX = 20;
const RECENTS_KEY = "om:palette:recents";

/** Every mode, and the groups each one draws — in the order it draws
 *  them, top to bottom. Fixed rather than "best match first": the palette
 *  is muscle memory, and a heading that moves depending on what you typed
 *  cannot become that.
 *
 *  This is also the whole list of modes: keys.js's `Mod+K` and `g l`
 *  runners name one of these keys, and `command`'s groups are exactly the
 *  set the three `*Candidates()` builders below tag their results with. A
 *  group listed here that nothing produces renders an empty heading; one
 *  produced and not listed is invisible. */
const MODES = {
  command: {
    groups: ["Actions", "Go to", "Settings"],
    placeholder: "Type a command, label, or search",
  },
  goto: { groups: ["Go to"], placeholder: "Go to a mailbox or label" },
};

/** Spec §6.2's "aliases ("trash" → Delete)". Extra words a result matches
 *  on that its label does not contain — never shown, only searched. */
const ALIASES = {
  archive: "file away done",
  delete: "trash bin remove",
  spam: "junk report block",
  star: "flag favourite favorite important",
  "mark-read": "seen open",
  "mark-unread": "unseen unopen",
};

/** The design system icon each result draws, by id. Actions are
 *  `mailosh/web/palette.py`'s six; the mailbox keys are
 *  `mailosh.services.mailbox_tree`'s own `_SYSTEM_SPEC`/`_MORE_SPEC`
 *  icons, so a "Go to Inbox" row in the palette wears the same glyph as
 *  the sidebar row it takes you to. Labels use a coloured dot instead,
 *  exactly as the sidebar does. */
const ICONS = {
  archive: "archive",
  delete: "trash-2",
  spam: "shield-alert",
  star: "star",
  "mark-read": "mail-open",
  "mark-unread": "mail",
  inbox: "inbox",
  starred: "star",
  sent: "send",
  drafts: "file",
  all: "inbox",
  trash: "trash-2",
  settings: "settings-2",
};

/** `label_color` has already narrowed the server's value to one of spec
 *  §4.1's twelve names, but this is the one place a stored, user-supplied
 *  string would reach a `style` attribute, so it is checked again here
 *  rather than trusted across the wire. */
const COLOR_NAME = /^[a-z]+$/;

const DIALOG = "#palette";

// ---------------------------------------------------------------------
// The index
// ---------------------------------------------------------------------

let index = null;
let loadedAt = 0;
let inflight = null;

/** True if `response` was the login redirect in disguise — same contract
 *  as actions.js's own `handledExpiredSession`, and the same reason the
 *  request below sends `HX-Request`: without it an expired session hands
 *  back 200 and a page full of login HTML, which `.json()` would reject
 *  with a parse error nobody could act on. */
function handledExpiredSession(response) {
  if (response.status !== 401) return false;
  const location = response.headers.get("HX-Redirect");
  if (!location) return false;
  window.location.assign(location);
  return true;
}

function load(force = false) {
  if (!force && index !== null && Date.now() - loadedAt < INDEX_TTL_MS) {
    return Promise.resolve(index);
  }
  if (inflight !== null) return inflight;
  inflight = fetch("/palette/index", {
    credentials: "same-origin",
    headers: { "HX-Request": "true" },
  })
    .then((response) => {
      if (handledExpiredSession(response)) return null;
      if (!response.ok) return null;
      return response.json();
    })
    .then((data) => {
      if (data !== null) {
        index = data;
        loadedAt = Date.now();
      }
      inflight = null;
      return index;
    })
    .catch(() => {
      // A palette that cannot reach the server keeps whatever it already
      // had rather than emptying itself: the six actions in a cached
      // index still work, since running one is a separate POST.
      inflight = null;
      return index;
    });
  return inflight;
}

// New mail moves unread counts and can create labels, so the cached index
// is stale the moment the list changes. Invalidated rather than refetched:
// the cost belongs to the next open, not to every incoming message — with
// one exception, a palette that is open right now and would otherwise go
// on showing the old list.
document.body.addEventListener("mail:changed", () => {
  loadedAt = 0;
  if (!isOpen()) return;
  // Keep the cursor on the *same command* across the re-render. `render()`
  // resets it to the top, so mail landing while a reader arrowed down to
  // "Go to Work" made their Enter run the first action instead. Group
  // and id together: ids are unique within a group, not across them.
  const held = results[state.active] ?? null;
  const key = held === null ? null : held.group + " " + held.id;
  load(true).then(() => {
    // Closed while the load was in flight: nothing to draw into, and
    // repopulating `results` for a dialog that is gone would only leave
    // stale state for the next open.
    if (!isOpen()) return;
    render();
    if (key === null) return;
    const at = results.findIndex((candidate) => candidate.group + " " + candidate.id === key);
    if (at > 0) setActive(at);
  });
});

// ---------------------------------------------------------------------
// Recents
// ---------------------------------------------------------------------

/** `localStorage` throws outright in a browser told to block site data,
 *  and returns junk if something else wrote this key, so every read is
 *  guarded and anything that is not an array of strings is discarded. */
function recents() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RECENTS_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function remember(id) {
  try {
    const next = [id].concat(recents().filter((seen) => seen !== id)).slice(0, RECENT_MAX);
    window.localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Nothing to tell the reader: a command they just ran still ran.
  }
}

// ---------------------------------------------------------------------
// Candidates
// ---------------------------------------------------------------------

const state = { mode: "command", active: 0 };
let results = [];

function entryFor(id) {
  for (const entry of registry) if (entry.id === id) return entry;
  return null;
}

/** What an action would apply to right now — `actions.js`'s own answer,
 *  never a second definition of "the current conversation". */
function targets() {
  const resolve = window.om?.targets;
  return typeof resolve === "function" ? resolve() : [];
}

/** Spec §6.2's "context-aware for the current selection/open thread".
 *
 *  With nothing selected and no conversation open there is nothing any of
 *  the six mutations could act on, so they are left out of the list
 *  entirely rather than listed and inert — the palette's `Enter` has to
 *  mean something every time it lands on a row. An entry the registry
 *  says is not available is dropped for the same reason. */
function actionCandidates() {
  if (index === null || targets().length === 0) return [];
  const found = [];
  for (const action of index.actions) {
    const entry = entryFor(action.id);
    if (entry === null || !entry.available) continue;
    found.push({
      id: action.id,
      group: "Actions",
      label: action.label,
      search: action.label + " " + (ALIASES[action.id] ?? ""),
      binding: entry.keys[0] ?? null,
      icon: ICONS[action.id] ?? null,
      color: null,
      run: entry.run,
    });
  }
  return found;
}

function gotoCandidates() {
  if (index === null) return [];
  const colors = new Map();
  for (const label of index.labels) colors.set(label.id, label.color);
  return index.goto.map((item) => {
    const key = item.id.slice("goto:".length);
    const isLabel = colors.has(key);
    const entry = entryFor("goto-" + key);
    return {
      id: item.id,
      group: "Go to",
      label: item.label,
      search: item.label + (isLabel ? " label" : " mailbox folder"),
      binding: entry !== null && entry.available ? entry.keys[0] : null,
      icon: isLabel ? null : (ICONS[key] ?? "inbox"),
      color: isLabel ? colors.get(key) : null,
      run: () => go(item.href),
    };
  });
}

function settingCandidates() {
  if (index === null) return [];
  return index.settings.map((setting) => ({
    id: setting.id,
    group: "Settings",
    label: setting.label,
    search: setting.label + " settings appearance preferences",
    binding: null,
    icon: ICONS.settings,
    color: null,
    // Two shapes in one group (`mailosh/web/palette.py`'s `PaletteSetting`):
    // a quick toggle writes a preference through the same `ui` store the
    // gear's popover uses, and a page navigates the same way a Go to
    // result does. `href` is what tells them apart.
    run: () => (setting.href ? go(setting.href) : applyPrefs(setting.values)),
  }));
}

function go(href) {
  // The sidebar's own link if it has one — which it does for every entry
  // the server builds, since both lists come from `build_nav` and both
  // drop the same hidden labels. A full load is the honest fallback for
  // the case where they disagree: an `htmx.ajax` here would swap the list
  // without pushing the URL, leaving the address bar lying.
  if (!navigate(href)) window.location.assign(href);
}

function applyPrefs(values) {
  const ui = window.Alpine?.store?.("ui") ?? null;
  if (ui?.setPref === undefined) return;
  for (const name of Object.keys(values)) ui.setPref(name, values[name]);
}

// ---------------------------------------------------------------------
// Matching
// ---------------------------------------------------------------------

/** Every candidate this mode offers, scored against `query` and ordered
 *  best first inside each group.
 *
 *  An empty query scores everything equally, so `sort`'s stability (spec'd
 *  since ES2019) leaves the server's own order intact and the recency
 *  boost is the only thing that reorders it — the palette opens on the
 *  commands this reader actually uses, in a list that is otherwise the
 *  same every time. */
function compute() {
  const mode = MODES[state.mode] ?? MODES.command;
  const query = queryText();
  const recent = recents();
  const scored = [];
  for (const candidate of actionCandidates().concat(gotoCandidates(), settingCandidates())) {
    if (!mode.groups.includes(candidate.group)) continue;
    const base = query === "" ? 1 : commandScore(candidate.search, query);
    if (base < THRESHOLD) continue;
    candidate.score = base + (recent.includes(candidate.id) ? RECENT_BOOST : 0);
    scored.push(candidate);
  }
  scored.sort((a, b) => b.score - a.score);
  const ordered = [];
  for (const group of mode.groups) {
    for (const candidate of scored) if (candidate.group === group) ordered.push(candidate);
  }
  return ordered;
}

// ---------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------

function dialog() {
  return document.querySelector(DIALOG);
}

function isOpen() {
  return dialog()?.open === true;
}

function input() {
  return document.querySelector(DIALOG + ' [data-role="palette-input"]');
}

function queryText() {
  return (input()?.value ?? "").trim();
}

/** A clone of one design system icon, taken from the `<template>` the
 *  palette's own markup carries. Jinja's `icon()` macro is the only thing
 *  in this app that knows how a Lucide glyph is restyled, and it runs on
 *  the server — so the server renders each one it might need, once, and
 *  this copies them rather than hand-writing SVG here. */
function iconNode(name) {
  const source = document.querySelector(DIALOG + ' [data-role="palette-icons"]');
  const found = source?.content?.querySelector('[data-icon="' + name + '"]') ?? null;
  const glyph = found?.firstElementChild ?? null;
  return glyph === null ? null : glyph.cloneNode(true);
}

function dot(color) {
  const el = document.createElement("span");
  el.className = "palette-dot";
  if (COLOR_NAME.test(color)) el.style.background = "var(--label-" + color + ")";
  return el;
}

function groupTitle(group) {
  const heading = document.createElement("div");
  heading.className = "palette-group";
  // Decoration inside a `listbox`: a child that is not an `option` makes
  // that tree invalid, and with `aria-activedescendant` only the active
  // row is ever announced, so the heading loses nothing by stepping out
  // of the accessibility tree and the listbox gains a valid one.
  heading.setAttribute("role", "presentation");
  // "Actions · 3 conversations" — the one thing a reader has to know
  // before pressing Enter on a mutation is how much of their mailbox it
  // is about to touch.
  const chosen = group === "Actions" ? targets().length : 0;
  heading.textContent = chosen > 1 ? group + " · " + chosen + " conversations" : group;
  return heading;
}

function optionNode(candidate, at) {
  const option = document.createElement("div");
  option.className = "palette-option";
  option.id = "palette-option-" + at;
  option.dataset.at = String(at);
  option.setAttribute("role", "option");
  option.setAttribute("aria-selected", "false");

  const glyph = candidate.color === null ? iconNode(candidate.icon) : dot(candidate.color);
  const mark = document.createElement("span");
  mark.className = "palette-mark";
  if (glyph !== null) mark.append(glyph);
  option.append(mark);

  const label = document.createElement("span");
  label.className = "palette-option-label";
  label.textContent = candidate.label;
  option.append(label);

  if (candidate.binding !== null) {
    const keys = document.createElement("span");
    keys.className = "palette-keys";
    keys.append(keyChips(candidate.binding));
    option.append(keys);
  }
  return option;
}

function emptyNode() {
  const empty = document.createElement("div");
  empty.className = "palette-empty";
  if (index === null) {
    empty.textContent = "Loading commands…";
    return empty;
  }
  const said = document.createElement("span");
  said.textContent = "No command matches “" + queryText() + "”.";
  const note = document.createElement("span");
  note.className = "palette-empty-note";
  // Said plainly rather than offering a "Search mail for …" row that
  // would navigate nowhere: spec §6.2's search fall-through needs the
  // search route (1D), and a result that only apologises is not a result.
  note.textContent = "Searching the text of your mail arrives in a later release.";
  empty.append(said, note);
  return empty;
}

function render() {
  const host = document.querySelector(DIALOG + ' [data-role="palette-results"]');
  if (host === null) return;
  results = compute();
  host.replaceChildren();
  if (results.length === 0) {
    host.append(emptyNode());
    setActive(-1);
    return;
  }
  let group = null;
  results.forEach((candidate, at) => {
    if (candidate.group !== group) {
      group = candidate.group;
      host.append(groupTitle(group));
    }
    host.append(optionNode(candidate, at));
  });
  setActive(0);
}

function options() {
  return Array.from(document.querySelectorAll(DIALOG + " .palette-option"));
}

function setActive(at) {
  state.active = at;
  const field = input();
  for (const option of options()) {
    const on = Number(option.dataset.at) === at;
    option.classList.toggle("is-active", on);
    option.setAttribute("aria-selected", on ? "true" : "false");
    if (on) option.scrollIntoView({ block: "nearest" });
  }
  // The combobox pattern: the input keeps DOM focus (so typing never
  // stops working) and points at the row a screen reader should read.
  if (field === null) return;
  if (at < 0) field.removeAttribute("aria-activedescendant");
  else field.setAttribute("aria-activedescendant", "palette-option-" + at);
}

function move(delta) {
  if (results.length === 0) return;
  const at = state.active + delta;
  setActive(at < 0 ? results.length - 1 : at >= results.length ? 0 : at);
}

// ---------------------------------------------------------------------
// Running, opening, closing
// ---------------------------------------------------------------------

function close() {
  const el = dialog();
  // Settled here rather than in a `close` listener: that event does not
  // arrive at all in a tab the browser has marked hidden, which is where
  // the confirm dialog learnt the same lesson (actions.js).
  if (el !== null && el.open) el.close();
  results = [];
  state.active = -1;
}

function runAt(at) {
  const chosen = results[at] ?? null;
  if (chosen === null) return;
  remember(chosen.id);
  // Closed *before* the command runs, and that ordering is load-bearing:
  // a modal dialog makes the rest of the document inert, and every one of
  // these results acts on the page behind it — a row to collapse, a nav
  // link to click, a toast to put focus back near.
  close();
  chosen.run?.();
}

function onKeydown(event) {
  if (event.isComposing || event.keyCode === 229) return;
  const mod = event.ctrlKey && !event.metaKey && !event.altKey;
  if (event.key === "ArrowDown" || (mod && event.key.toLowerCase() === "j")) {
    event.preventDefault();
    move(1);
    return;
  }
  if (event.key === "ArrowUp" || (mod && event.key.toLowerCase() === "k")) {
    event.preventDefault();
    move(-1);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    runAt(state.active);
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  }
}

function bind(el) {
  if (el.dataset.bound === "true") return;
  el.dataset.bound = "true";
  el.addEventListener("keydown", onKeydown);
  el.addEventListener("input", render);
  el.addEventListener("click", (event) => {
    // A backdrop click lands on the dialog element itself — the only
    // close-on-click-out a native `<dialog>` offers.
    if (event.target === el) {
      close();
      return;
    }
    const option = event.target?.closest?.(".palette-option");
    if (option !== null && option !== undefined) runAt(Number(option.dataset.at));
  });
  // Pointer and keyboard share one cursor, so a mouse resting over a row
  // and `Enter` can never mean two different commands.
  el.addEventListener("mousemove", (event) => {
    const option = event.target?.closest?.(".palette-option");
    if (option === null || option === undefined) return;
    const at = Number(option.dataset.at);
    if (at !== state.active) setActive(at);
  });
  el.addEventListener("cancel", () => close());
}

/** Open the palette in `mode`. Called by keys.js's `Mod+K` and `g l`
 *  runners, which is why this module registers itself there rather than
 *  being imported by it. */
export function open(mode = "command") {
  const el = dialog();
  if (el === null) return false;
  bind(el);
  state.mode = Object.hasOwn(MODES, mode) ? mode : "command";
  const field = input();
  if (field !== null) {
    field.value = "";
    field.placeholder = MODES[state.mode].placeholder;
  }
  if (!el.open) el.showModal();
  field?.focus();
  // Twice, deliberately: once now against whatever the cache holds (so a
  // second ⌘K paints instantly) and once when the request lands. A first
  // open draws "Loading commands…" for one round trip rather than an
  // empty panel that looks broken.
  render();
  load().then(() => {
    if (isOpen()) render();
  });
  return true;
}

registerPalette(open);
