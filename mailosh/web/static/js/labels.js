// Mailosh — the label picker, the Move chooser, and the label menu.
//
// Design spec §10. Three popovers, one `<dialog>`, and every one of them
// filled by a fragment the server rendered (`mailosh/web/labels.py` ->
// `templates/labels/*.html`) rather than built here row by row. That is the
// opposite of `palette.js`'s choice and it is deliberate: the palette's
// rows are context-dependent in a way only the client knows (which commands
// apply to the current selection), while these three are pure functions of
// server state — which labels exist, which the selection already carries,
// what colour each one has. Rendering them in Jinja means one definition of
// a label row, `label_color` narrowing every colour exactly as it does for
// a sidebar dot and a list chip, and no mailbox name ever assembled into
// markup on this side.
//
// ---------------------------------------------------------------------
// One dialog, created here, never in a template
//
// `actions.js` already builds its bulk-confirmation `<dialog>` with
// `createElement`, and this follows it: the element is made once, appended
// to `document.body`, and reused. A `<dialog>` in a template would have to
// live in `layouts/app.html` (outside every swap target) to survive a nav
// or list swap, and this task does not own that file. Creating it here has
// the same effect with none of that coupling — and `showModal()` puts it in
// the top layer regardless of where in the document it sits.
//
// While it is open the platform owns the keyboard: `keys.js`'s dispatch
// returns early whenever `dialog[open]` matches, so `j`/`k`/`e` cannot fire
// underneath a picker and the arrow keys below are unambiguous.
//
// ---------------------------------------------------------------------
// The CSP
//
// `script-src 'self'`, no `'unsafe-eval'`. Nothing here is an Alpine
// directive, an `hx-on:`, an `hx-vals='js:…'` or an `hx-trigger` filter —
// all four of which compile attribute text with `new Function` — and no
// fragment this file swaps in carries behaviour. Every control is found by
// a `data-role` hook and bound by one delegated listener, the same model
// `actions.js` and `app.js` use.
//
// The one place attacker-influenced text meets the DOM is a label's name in
// the create row and the delete confirmation, and both go through
// `textContent`. Search filtering compares `data-name` against typed text
// in JavaScript and never builds a selector out of either.
//
// ---------------------------------------------------------------------
// Entry points, and the two that are not wired yet
//
// Live today: the sidebar's `+` (`[data-label-new]`), a label's hover `⋮`
// (`[data-label-menu]`), and `[data-role="label-picker"]` /
// `[data-role="label-move"]` for any toolbar button that wants to open the
// two selection popovers.
//
// Spec §6.1 gives those last two the keys `l` and `v`. Their registry
// entries in `static/js/keys.js` exist and are `available: false`, and that
// file belongs to another change right now — so this module *exports*
// `openPicker`/`openMove` and registers nothing. Flipping the two entries
// to `available: true` and pointing their runners here is the whole of what
// is left; until then the keys stay silent, which is that file's own rule
// (a UI never advertises a broken key).

import { registerLabels } from "./keys.js";
const DIALOG_ID = "label-popover";

/** Rows the picker will render without a search field. Below this, typing
 *  to filter is slower than reading the list. */
const SEARCH_FROM = 7;

/** Where a selection's message ids come from. `actions.js` owns the one
 *  definition of "the conversations this would apply to" — the selection,
 *  or the focused row when nothing is selected — and asking it is what
 *  keeps `l` and the toolbar's Labels button aimed at the same mail as `e`
 *  and Archive. */
function targets() {
  const resolve = window.om?.targets;
  return typeof resolve === "function" ? resolve() : [];
}

/** Every message id behind those rows. Same `data-email-ids` contract every
 *  row and the open conversation already carry; deduplicated because two
 *  selected rows of one thread would otherwise name a message twice. */
function emailIds(elements) {
  const ids = [];
  for (const el of elements) {
    for (const id of (el.dataset.emailIds ?? "").split(",")) {
      if (id !== "" && !ids.includes(id)) ids.push(id);
    }
  }
  return ids;
}

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
}

/** POST a form body, exactly as `actions.js` posts one.
 *
 *  `HX-Request: true` is not a CSRF signal (`security/csrf.py` never trusts
 *  it): it is what makes an expired session answer `401` + `HX-Redirect`
 *  instead of a `303` to the login page that `fetch` would follow and hand
 *  back as a perfectly successful-looking 200 full of login HTML. */
function post(url, values) {
  const body = new URLSearchParams();
  for (const name of Object.keys(values)) {
    const value = values[name];
    if (Array.isArray(value)) for (const item of value) body.append(name, item);
    else if (value !== null && value !== undefined) body.append(name, value);
  }
  return fetch(url, {
    method: "POST",
    body: body,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRF-Token": csrfToken(),
      "HX-Request": "true",
    },
  });
}

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

function handledExpiredSession(response) {
  if (response.status !== 401) return false;
  const location = response.headers.get("HX-Redirect");
  if (!location) return false;
  window.location.assign(location);
  return true;
}

function toast(message) {
  const ui = window.Alpine?.store?.("ui") ?? null;
  if (ui?.toast) {
    ui.toast(message);
    return;
  }
  const status = document.getElementById("status");
  if (status !== null) status.textContent = message;
}

/** Ask the list to re-read itself. The nav rides along: every mailbox view
 *  re-renders `#nav` out of band, so one refresh brings back the label
 *  tree, the row chips and the unread counts together — which is why no
 *  route in `web/labels.py` tries to describe a label change as a delta. */
function refresh() {
  window.htmx?.trigger?.(document.body, "mail:changed", {
    types: ["Email", "Mailbox"],
    id: null,
    catchup: true,
  });
}

// ---------------------------------------------------------------------
// The dialog
// ---------------------------------------------------------------------

let dialog = null;
/** What the open popover would act on, captured at open time.
 *
 *  Captured rather than re-read on Apply, and that matters: the dialog is
 *  modal, so the list behind it is inert and the selection cannot change —
 *  but `om.targets()` falls back to *the focused row* when nothing is
 *  selected, and focus does move (into the dialog). Re-reading at commit
 *  time would silently retarget an apply from the row the reader opened the
 *  picker on to whatever `defaultTargets()` resolved to afterwards. */
let pending = [];

function ensureDialog() {
  if (dialog !== null && dialog.isConnected) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = DIALOG_ID;
  dialog.className = "lp-dialog";
  dialog.addEventListener("click", (event) => {
    // A backdrop click lands on the dialog element itself — the only
    // close-on-click-out a native `<dialog>` offers.
    if (event.target === dialog) close();
  });
  dialog.addEventListener("cancel", () => close());
  dialog.addEventListener("keydown", onKeydown);
  dialog.addEventListener("input", onInput);
  dialog.addEventListener("change", onChange);
  dialog.addEventListener("submit", onSubmit);
  dialog.addEventListener("click", onClick);
  document.body.append(dialog);
  return dialog;
}

function close() {
  if (dialog !== null && dialog.open) dialog.close();
  pending = [];
}

/** Fetch a fragment and show it. Every popover in this file opens the same
 *  way, so a failure looks the same way too: a toast, and no half-drawn
 *  panel left on screen. */
async function open(url, values) {
  const el = ensureDialog();
  let response = null;
  try {
    response = await post(url, values);
  } catch {
    toast("Couldn't open that");
    return;
  }
  if (handledExpiredSession(response)) return;
  const failure = trigger(response, "om:error");
  if (failure !== null || !response.ok) {
    toast(failure?.toast ?? "Couldn't open that");
    return;
  }
  // The response is a server-rendered fragment, so it is set as HTML —
  // the one place in this file that happens, and the reason every dynamic
  // string below uses `textContent` instead.
  el.innerHTML = await response.text();
  if (!el.open) el.showModal();
  afterRender();
}

/** Put the fragment into the state HTML cannot express, and focus the one
 *  control the reader is most likely to want. */
function afterRender() {
  // `indeterminate` is a DOM property with no attribute, so a "mixed"
  // checkbox has to be set here. `data-state` is also what `commit()` reads
  // back to tell a box the reader actually changed from one that merely
  // started out partly applied — clicking Apply without touching anything
  // must send no adds and no removes at all.
  for (const box of dialog.querySelectorAll(".lp-check")) {
    box.indeterminate = box.dataset.state === "mixed";
  }
  const panel = dialog.querySelector(".lp");
  if (panel !== null) {
    const options = dialog.querySelectorAll(".lp-option:not(.lp-create)").length;
    // Move mode only. In label mode the field is also where type-to-create
    // lives (`filter()` offers "Create …" for a name with no match), and
    // hiding it under SEARCH_FROM meant a reader with fewer than seven
    // labels — every new account — had no way to make one from here.
    panel.querySelector(".lp-field").hidden = options < SEARCH_FROM && panel.dataset.mode === "move";
    setHint();
    filter();
  }
  const field = dialog.querySelector(
    '[data-role="lp-search"]:not([hidden]), .lm-input, [data-role="lm-close"]',
  );
  field?.focus();
  if (typeof field?.select === "function") field.select();
}

/** "3 conversations", under the buttons — the one thing a reader has to
 *  know before pressing Apply is how much of their mailbox it touches. */
function setHint() {
  const hint = dialog.querySelector('[data-role="lp-hint"]');
  if (hint === null) return;
  const count = pending.length;
  hint.textContent = count > 1 ? count + " conversations" : "";
}

// ---------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------

function searchText() {
  return (dialog?.querySelector('[data-role="lp-search"]')?.value ?? "").trim();
}

/** Filter the rendered rows to what was typed, and decide what the tail of
 *  the list says when nothing matches.
 *
 *  Local, over rows the server already sent — so typing costs no round trip
 *  and the list cannot flicker between two server answers. A plain
 *  case-insensitive substring match rather than the palette's fuzzy scorer:
 *  a picker is a list of names the reader chose themselves, and `wk`
 *  matching "Work" is a nicety, while `re` *not* obviously matching
 *  "Receipts" would be a bug. Superhuman's own label picker is a prefix
 *  match for the same reason.
 *
 *  A checked box is never filtered away, whatever is typed: hiding what the
 *  reader has already ticked is how an Apply silently drops a label. */
function filter() {
  const panel = dialog?.querySelector(".lp");
  if (!panel) return;
  const query = searchText().toLowerCase();
  let shown = 0;
  let exact = false;
  for (const option of panel.querySelectorAll(".lp-option:not(.lp-create)")) {
    const name = option.dataset.name ?? "";
    const input = option.querySelector("input");
    const ticked = input?.checked === true || input?.indeterminate === true;
    const hit = query === "" || name.toLowerCase().includes(query);
    option.hidden = !hit && !ticked;
    if (!option.hidden) shown += 1;
    if (name.toLowerCase() === query) exact = true;
  }
  const create = panel.querySelector('[data-role="lp-create"]');
  const empty = panel.querySelector('[data-role="lp-empty"]');
  // Type-to-create belongs to the picker only. "Move into a folder that
  // does not exist yet" is two gestures wearing one coat, so Move offers
  // nothing here and says so.
  const offerCreate = panel.dataset.mode !== "move" && query !== "" && !exact;
  if (create !== null) {
    create.hidden = !offerCreate;
    const name = create.querySelector('[data-role="lp-create-name"]');
    if (name !== null) name.textContent = query;
  }
  if (empty !== null) empty.hidden = shown !== 0 || offerCreate;
  setActive(0);
}

// ---------------------------------------------------------------------
// The cursor
// ---------------------------------------------------------------------

function options() {
  if (dialog === null) return [];
  return Array.from(dialog.querySelectorAll(".lp-option")).filter((el) => !el.hidden);
}

let active = 0;

function setActive(at) {
  const rows = options();
  if (rows.length === 0) {
    active = -1;
    return;
  }
  active = at < 0 ? rows.length - 1 : at >= rows.length ? 0 : at;
  rows.forEach((row, index) => {
    const on = index === active;
    row.classList.toggle("is-active", on);
    if (on) row.scrollIntoView({ block: "nearest" });
  });
}

function move(delta) {
  setActive(active + delta);
}

/** Activate the row under the cursor: tick a checkbox, choose a radio, or
 *  run the create row. The search field keeps DOM focus throughout, so
 *  typing never stops working — the same combobox shape the palette uses. */
function activate() {
  const row = options()[active] ?? null;
  if (row === null) return;
  if (row.dataset.role === "lp-create" || row.classList.contains("lp-create")) {
    createFromSearch();
    return;
  }
  const input = row.querySelector("input");
  if (input === null) return;
  if (input.type === "radio") {
    input.checked = true;
    commit();
    return;
  }
  input.indeterminate = false;
  input.checked = !input.checked;
  // A box the reader has just touched is no longer "as the server found
  // it", so its `data-state` moves with it — `commit()` diffs the two.
  input.dataset.touched = "true";
}

// ---------------------------------------------------------------------
// Committing
// ---------------------------------------------------------------------

/** Everything the reader changed, as one apply.
 *
 *  The diff is against `data-state` — the state the server reported when
 *  the picker opened — so pressing Apply after ticking one box sends one
 *  `add` and nothing else, and pressing it after touching nothing sends
 *  nothing at all and closes. That is what makes two labels across twenty
 *  conversations *one* request: the whole diff goes in a single POST, and
 *  `mailosh/services/labels.py` turns it into one `Email/set`. */
async function commit() {
  const panel = dialog?.querySelector(".lp");
  if (!panel) return;
  if (panel.dataset.mode === "move") {
    const chosen = panel.querySelector('input[name="to"]:checked');
    if (chosen === null) {
      toast("Pick somewhere to move to");
      return;
    }
    await send("/labels/move", { ids: pending, to: chosen.value });
    return;
  }
  const add = [];
  const remove = [];
  for (const box of panel.querySelectorAll(".lp-check")) {
    const was = box.dataset.state;
    const now = box.indeterminate ? "mixed" : box.checked ? "on" : "off";
    if (now === was) continue;
    if (now === "on") add.push(box.value);
    else if (now === "off") remove.push(box.value);
  }
  if (add.length === 0 && remove.length === 0) {
    close();
    return;
  }
  await send("/labels/apply", { ids: pending, add: add, remove: remove });
}

/** POST, then say what happened.
 *
 *  The success path deliberately hands `om:done` to `actions.js` by
 *  re-dispatching it on `document.body`: that listener already draws the
 *  toast, wires its Undo button to `/a/undo`, removes the rows the server
 *  named and moves the nav badges. A second copy of any of that here would
 *  be a second answer to "what does a completed mutation look like".
 *
 *  The failure path never claims success and never guesses: a partial
 *  apply's own message ("Only 12 of 20 messages could be updated") arrives
 *  in the trigger with `refresh: true`, so the list is re-read from the
 *  server rather than patched from an optimistic guess that is now wrong
 *  for an unknown subset of rows. */
async function send(url, values) {
  close();
  let response = null;
  try {
    response = await post(url, values);
  } catch {
    toast("Couldn't reach the mail server. Try again.");
    refresh();
    return;
  }
  if (handledExpiredSession(response)) return;
  const failure = trigger(response, "om:error");
  if (failure !== null || !response.ok) {
    toast(failure?.toast ?? "Something went wrong");
    refresh();
    return;
  }
  const done = trigger(response, "om:done");
  if (done !== null) window.htmx?.trigger?.(document.body, "om:done", done);
  // One refresh. `web/labels.py`'s `_changed()` sets both `refresh: true`
  // and `om:labels` on the same reply, and answering each separately sent
  // two `#list` GETs and two nav swaps for every rename or colour change.
  if (done?.refresh === true || trigger(response, "om:labels") !== null) refresh();
}

// ---------------------------------------------------------------------
// Delete, and its confirmation
// ---------------------------------------------------------------------

/** Confirm, then delete. The question is the server's — `labels/menu.html`
 *  renders it from this label's real message count — so the button here
 *  only has to name what is about to happen.
 *
 *  `window.confirm` is deliberately not used: it is blocked in a sandboxed
 *  frame, unstyled, and cannot carry the two-line explanation this needs.
 *  A nested `<dialog>` on top of an open modal is legal and stacks in the
 *  top layer, which is what keeps the menu behind it visible. */
function confirmDelete(name, note) {
  return new Promise((resolve) => {
    const ask = document.createElement("dialog");
    ask.className = "confirm";
    const title = document.createElement("p");
    title.className = "confirm-title";
    title.textContent = "Delete “" + name + "”?";
    const body = document.createElement("p");
    body.className = "confirm-text";
    body.textContent = note;
    const row = document.createElement("div");
    row.className = "confirm-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "confirm-cancel";
    cancel.textContent = "Cancel";
    const go = document.createElement("button");
    go.type = "button";
    go.className = "confirm-go is-danger";
    go.textContent = "Delete label";
    row.append(cancel, go);
    ask.append(title, body, row);
    document.body.append(ask);

    // Every way out goes through here and the first one wins: the answer is
    // given once, the dialog is closed before it is detached (removing an
    // open modal would leave the top layer holding a node that is no longer
    // in the document), and the node never outlives the question.
    let settled = false;
    const settle = (answer) => {
      if (settled) return;
      settled = true;
      if (ask.open) ask.close();
      ask.remove();
      resolve(answer);
    };
    cancel.addEventListener("click", () => settle(false));
    go.addEventListener("click", () => settle(true));
    ask.addEventListener("cancel", (event) => {
      event.preventDefault();
      settle(false);
    });
    ask.addEventListener("click", (event) => {
      if (event.target === ask) settle(false);
    });
    ask.showModal();
    cancel.focus();
  });
}

// ---------------------------------------------------------------------
// Listeners
// ---------------------------------------------------------------------

function onKeydown(event) {
  if (event.isComposing || event.keyCode === 229) return;
  const inPicker = dialog?.querySelector(".lp") !== null;
  const mod = event.ctrlKey && !event.metaKey && !event.altKey;
  if (inPicker && (event.key === "ArrowDown" || (mod && event.key.toLowerCase() === "j"))) {
    event.preventDefault();
    move(1);
    return;
  }
  if (inPicker && (event.key === "ArrowUp" || (mod && event.key.toLowerCase() === "k"))) {
    event.preventDefault();
    move(-1);
    return;
  }
  if (inPicker && event.key === "Enter") {
    event.preventDefault();
    // Enter on a row picks it; Cmd/Ctrl+Enter applies everything at once,
    // so multi-apply never needs the mouse.
    if (event.metaKey || event.ctrlKey) commit();
    else activate();
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  }
}

function onInput(event) {
  if (event.target?.dataset?.role === "lp-search") filter();
}

function onChange(event) {
  const target = event.target;
  if (target?.classList?.contains("lp-check")) {
    target.indeterminate = false;
    return;
  }
  if (target?.dataset?.role === "lm-visibility") {
    saveMeta({ visibility: target.value });
    return;
  }
  if (target?.dataset?.role === "lm-nest") {
    const menu = dialog?.querySelector(".lm");
    send("/labels/" + encodeURIComponent(menu?.dataset.label ?? "") + "/nest", {
      parent_id: target.value,
    });
  }
}

function saveMeta(values) {
  const menu = dialog?.querySelector(".lm");
  if (!menu) return;
  send("/labels/" + encodeURIComponent(menu.dataset.label) + "/meta", values);
}

function onSubmit(event) {
  const form = event.target;
  event.preventDefault();
  if (form?.dataset?.role === "lm-rename") {
    const menu = dialog?.querySelector(".lm");
    send("/labels/" + encodeURIComponent(menu?.dataset.label ?? "") + "/rename", {
      name: form.querySelector('[name="name"]').value,
    });
    return;
  }
  if (form?.dataset?.role === "lm-create") {
    // `ids` is the selection the picker was opened on, still held in
    // `pending` because `open()` does not clear it — only `close()` does,
    // and `send()` calls that *after* these values are read. Type-to-create
    // used to post the name alone: the label appeared, the five selected
    // threads it was typed for stayed unlabelled, and the toast said
    // "Created" as if that were the whole job.
    send("/labels", {
      name: form.querySelector('[name="name"]').value,
      parent_id: form.querySelector('[name="parent_id"]')?.value ?? "",
      ids: pending,
    });
  }
}

function createFromSearch() {
  const typed = searchText();
  if (typed === "") return;
  open("/labels/new", { name: typed });
}

async function onClick(event) {
  const control = event.target?.closest?.("[data-role]");
  if (control === null || control === undefined) return;
  const role = control.dataset.role;

  if (role === "lp-cancel" || role === "lm-close") {
    event.preventDefault();
    close();
    return;
  }
  if (role === "lp-commit") {
    event.preventDefault();
    commit();
    return;
  }
  if (role === "lp-create") {
    event.preventDefault();
    createFromSearch();
    return;
  }
  if (role === "lm-color") {
    event.preventDefault();
    saveMeta({ color: control.dataset.color });
    return;
  }
  if (role === "lm-color-clear") {
    event.preventDefault();
    const menu = dialog?.querySelector(".lm");
    send("/labels/" + encodeURIComponent(menu?.dataset.label ?? "") + "/color/clear", {});
    return;
  }
  if (role === "lm-delete") {
    event.preventDefault();
    const menu = dialog?.querySelector(".lm");
    if (!menu) return;
    const name = menu.dataset.name ?? "";
    const note = menu.querySelector(".lm-note")?.textContent?.trim() ?? "";
    if (!(await confirmDelete(name, note))) return;
    send("/labels/" + encodeURIComponent(menu.dataset.label) + "/delete", {});
  }
}

/** Open the label picker for whatever `l` (or a toolbar button) would act
 *  on. Exported so `keys.js` can point its `label` runner here once that
 *  file's entry is flipped to `available: true`. */
export function openPicker() {
  pending = emailIds(targets());
  if (pending.length === 0) {
    // The same three-value contract keys.js's runners use: `false` means
    // "this key is live here but has nothing to act on", which shakes the
    // target rather than posting an empty request.
    return false;
  }
  open("/labels/picker", { ids: pending, mode: "label" });
  return true;
}

/** The same, for Move (`v`) — a single choice rather than checkboxes. */
export function openMove() {
  pending = emailIds(targets());
  if (pending.length === 0) return false;
  open("/labels/picker", { ids: pending, mode: "move" });
  return true;
}

// One delegated listener on `document.body` for the entry points, in the
// ordinary bubble phase — the same model `actions.js` uses, and `body`
// rather than `#nav` because a mailbox switch replaces the nav out of band
// and would take any listener bound to it along.
document.body.addEventListener("click", (event) => {
  if (event.defaultPrevented) return;
  const control = event.target?.closest?.(
    "[data-label-menu], [data-label-new], [data-role='label-picker'], [data-role='label-move']",
  );
  if (control === null || control === undefined) return;
  if (control.getAttribute("aria-disabled") === "true") return;
  event.preventDefault();
  if (control.dataset.role === "label-picker") {
    if (openPicker() === false) toast("Select a conversation first");
    return;
  }
  if (control.dataset.role === "label-move") {
    if (openMove() === false) toast("Select a conversation first");
    return;
  }
  if (control.hasAttribute("data-label-new")) {
    pending = [];
    open("/labels/new", {});
    return;
  }
  pending = [];
  open("/labels/menu/" + encodeURIComponent(control.dataset.labelMenu), {});
});


// Hand the keyboard registry this module's surface, so `l` and `v` reach
// the same two popovers the toolbar buttons open. One-way: this file
// imports keys.js, never the reverse.
registerLabels({ picker: openPicker, move: openMove });
