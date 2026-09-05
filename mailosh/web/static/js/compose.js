// Mailosh — compose: the corner dock, the inline reply card, the recipient
// chips, the Squire editor, attachments, and send.
//
// Design spec §8. `mailosh/web/compose.py` serves the markup; this file is
// everything that happens to it after it lands.
//
// ---------------------------------------------------------------------
// The property everything else is arranged around
//
// **An open dock must leave the inbox behind it completely alive.** It
// scrolls, it takes clicks, it keeps updating over SSE, and `j`/`k`/`e`
// keep working. That is not a nice-to-have; a compose window that freezes
// the mail behind it fails the whole point of a corner dock, which exists
// precisely so you can look something up while you write.
//
// Four things guarantee it, and none of them is an accident:
//
// 1. **Nothing here is a `<dialog>` and nothing paints a backdrop.** A
//    modal dialog hands the platform the keyboard and every click on the
//    page; `keys.js`'s `dispatch` also stops entirely while
//    `dialog[open]` matches anything. The dock is a `<section>`.
// 2. **The dock is a sibling of the list.** It is swapped into
//    `#compose-dock` in `layouts/app.html`, which sits outside `#main`,
//    outside `#list`, and outside the grid — every dock is
//    `position: fixed`, so the mount point holds no height and no hit
//    area at all. No compose request targets `#main`; none pushes a URL.
// 3. **Focus is taken once, on open, and never again.** Nothing below
//    re-focuses a dock on a timer, on a swap, or when a request settles.
//    Click the list and the caret leaves; it does not come back on its
//    own.
// 4. **`compose` is a keyboard scope, not a keyboard mode.** `keys.js`
//    counts the scope live only while focus is actually inside a compose
//    window (`activeScopes`), so a dock open in the corner never takes
//    `⌘K` away from the palette, or `e` away from the list.
//
// ---------------------------------------------------------------------
// CSP
//
// `script-src 'self'`, no `'unsafe-eval'`, and the vendored Alpine is the
// **CSP build** — so the templates carry exactly one directive, a bare
// `x-data="compose"` naming the component registered below with
// `Alpine.data()`. No `x-on`, no `x-show`, no inline expression of any
// kind, and no htmx `hx-on:`/`hx-vals='js:…'` (all three compile with
// `new Function`). Behaviour is delegated listeners keyed off
// `data-role`/`data-fmt`, which is the same model `app.js` and
// `actions.js` already use, bound to `document.body` so no swap can
// detach them.
//
// The component exists for one reason the delegated model cannot cover:
// **a Squire instance has a lifetime.** `Alpine.data`'s `init`/`destroy`
// pair is what creates the editor when a dock is swapped in and destroys
// it when the dock goes away — spec §8's "owning a Squire instance in a
// closure". Alpine initialises swapped content on its own, so there is no
// `htmx:load` glue anywhere in this file.
//
// ---------------------------------------------------------------------
// Transport
//
// Autosave is htmx's, in the markup (`hx-post="/compose/draft"`,
// `hx-trigger="input delay:2s"`, `hx-sync="this:replace"`), and its whole
// round trip — including learning the new draft id — closes with no
// JavaScript at all: the response swaps the state chip and an out-of-band
// hidden input.
//
// Send, discard and close use `fetch` for the reason `actions.js`
// documents for its own six: the responses have no body to swap. Send has
// a second reason, which is the more important one — spec §6.3 makes send
// the *only* delayed commit in this app, so the request must not exist
// until the undo window has run out. Attachments use `XMLHttpRequest`,
// alone in this codebase, because `fetch` cannot report upload progress
// and a 12 MB file with no progress bar is indistinguishable from a hang.

import { registerCompose } from "./keys.js";

const MOUNT = "#compose-dock";
const DOCK = "[data-compose]";

// Spec §4.3's metrics. The dock's own size lives in the stylesheet; these
// are only what the tiling arithmetic needs, and they are the same numbers
// (`.compose` in styles/input.css).
const DOCK_WIDTH = 560;
const DOCK_MIN_WIDTH = 280;
const DOCK_GAP = 12;
const DOCK_EDGE = 16;

// Spec §8: "up to 3 docks tile leftwards".
const MAX_DOCKS = 3;

// Below this the dock is full-screen and the stylesheet owns its geometry
// (spec §4.3/§8), so `reflow()` has nothing to say. Must match the
// `max-width: 767px` block in styles/input.css.
const NARROW = 768;

// Spec §8's soft warning. Deliberately soft: the server's own ceiling
// (`mailosh.web.compose.MAX_ATTACHMENT_BYTES`) is higher, so this is a
// warning the sender can go past, not a refusal wearing the wrong words.
const SOFT_ATTACHMENT_BYTES = 25 * 1024 * 1024;

// Spec §6.3's send window: "immediate commit + reverse op" is undo
// everywhere else in this app, and send is the one exception — a delayed
// commit with a real cancellation window.
const SEND_UNDO_MS = 10000;

// The 12 label colours, in `mailosh.ui.format.LABEL_COLORS`' order. A chip
// built here has to land on the same colour the server would have given
// it, or the same person would change colour between a reopened draft and
// a freshly typed address.
const LABEL_COLORS = [
  "indigo",
  "emerald",
  "rose",
  "amber",
  "sky",
  "violet",
  "teal",
  "orange",
  "pink",
  "lime",
  "slate",
  "red",
];

// What commits a recipient chip (spec §8).
const CHIP_KEYS = [",", ";", "Enter", "Tab"];

/** Per-dock state that is not in the DOM: the Squire instance, and the
 *  send that is counting down.
 *
 *  A `WeakMap` keyed by the dock element rather than a property on the
 *  element itself, so a dock removed from the document takes its entry
 *  with it and nothing here can keep a detached editor alive. */
const live = new WeakMap();

// ---------------------------------------------------------------------
// Small shared helpers
// ---------------------------------------------------------------------

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
}

function uiStore() {
  return window.Alpine?.store?.("ui") ?? null;
}

function toast(message) {
  const store = uiStore();
  if (store) store.toast(message);
}

function mount() {
  return document.querySelector(MOUNT);
}

function docks() {
  return Array.from(document.querySelectorAll(MOUNT + " " + DOCK));
}

/** The compose window a given element belongs to, dock or inline card. */
function composeOf(el) {
  return el?.closest?.(DOCK) ?? null;
}

/** The compose window the reader is actually in, or null. Real focus, not
 *  "the last one opened": with three docks up, `⌘↵` has to send the one
 *  being typed into. */
function focusedCompose() {
  return composeOf(document.activeElement);
}

function formOf(root) {
  return root?.querySelector("form") ?? null;
}

/** Every field in a compose form as `URLSearchParams`.
 *
 *  Not `FormData`: every field in the form is text (the file input lives
 *  *outside* it precisely so an autosave never re-uploads an attachment),
 *  and a urlencoded body is the one shape `fetch(..., {keepalive: true})`
 *  will still send from a `pagehide` handler. That path is what stops a
 *  send counting down in its undo window from being lost when the reader
 *  closes the tab. */
function body(root) {
  const form = formOf(root);
  if (form === null) return new URLSearchParams();
  const params = new URLSearchParams();
  for (const [name, value] of new FormData(form)) {
    if (typeof value === "string") params.append(name, value);
  }
  return params;
}

/** POST `params` to `url` as this session. Mirrors `actions.js`'s `post()`
 *  including the `HX-Request` header, and for the same reason: an expired
 *  session answers a plain request with a 303 to /login that `fetch`
 *  would follow and this would read as success. */
function post(url, params, options = {}) {
  return fetch(url, {
    method: "POST",
    body: params,
    credentials: "same-origin",
    keepalive: options.keepalive === true,
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRF-Token": csrfToken(),
      "HX-Request": "true",
    },
  });
}

/** The `om:error` payload on a response, or null.
 *
 *  Two shapes carry it and both matter here: `mailosh/web/app.py` answers
 *  a JMAP/transport failure with a **`200`** plus this trigger (so
 *  `response.ok` is *true* on a failure — the trap `actions.js`'s
 *  failure-contract header documents), and `POST /compose/send` answers a
 *  `ComposeError` with a `400` plus the same trigger carrying copy the
 *  reader can act on ("ada@ isn't a usable email address"). */
function errorTrigger(response) {
  const raw = response.headers.get("HX-Trigger");
  if (typeof raw !== "string" || !raw.includes("om:error")) return null;
  try {
    return JSON.parse(raw)["om:error"] ?? null;
  } catch {
    return null;
  }
}

function failed(response) {
  return !response.ok || errorTrigger(response) !== null;
}

// ---------------------------------------------------------------------
// Recipients
// ---------------------------------------------------------------------

/** `"Ada Lovelace" <ada@x.test>` -> `{name: "Ada Lovelace", email:
 *  "ada@x.test"}`; a bare address -> `{name: "", email: …}`.
 *
 *  The same shapes `mailosh.web.compose._one_recipient` parses, and — like
 *  it — this **judges nothing**. Anything the reader committed becomes a
 *  chip, valid or not: a chip that is wrong can be seen, corrected and
 *  re-edited with Backspace, while text silently refused would sit in the
 *  entry looking committed and reach nobody. `mailosh.services.compose`
 *  is the one validator, and it names the offender rather than dropping
 *  it. */
function parseAddress(raw) {
  const text = raw.trim().replace(/[,;]+$/, "").trim();
  if (text === "") return null;
  const angled = text.match(/^(.*?)<([^>]*)>\s*$/);
  const email = (angled ? angled[2] : text).trim();
  if (email === "") return null;
  let name = angled ? angled[1].trim() : "";
  if (name.length > 1 && name.startsWith('"') && name.endsWith('"')) {
    name = name.slice(1, -1).trim();
  }
  return { name: name, email: email };
}

/** Split a pasted list into candidate addresses. Commas, semicolons and
 *  newlines all separate; a comma *inside* a quoted display name does
 *  not, which is why this walks the string instead of calling `split`. */
function splitAddresses(raw) {
  const out = [];
  let current = "";
  let quoted = false;
  let angled = false;
  for (const ch of raw) {
    if (ch === '"') quoted = !quoted;
    else if (ch === "<") angled = true;
    else if (ch === ">") angled = false;
    if (!quoted && !angled && (ch === "," || ch === ";" || ch === "\n")) {
      out.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  out.push(current);
  return out;
}

/** The form's `Name <addr>` spelling — the exact text
 *  `mailosh.web.compose._recipients` parses, and the same one
 *  `_person` renders for a chip the server drew. */
function addressValue(person) {
  if (!person.name) return person.email;
  return /^[\w .'-]+$/.test(person.name)
    ? person.name + " <" + person.email + ">"
    : '"' + person.name.replace(/"/g, "") + '" <' + person.email + ">";
}

/** `mailosh.ui.format.initials`, in one line and to the letter: the first
 *  character of the name, else of the address's local part, else `?`. */
function initialOf(person) {
  const source = (person.name || "").trim();
  if (source) return source.slice(0, 1).toUpperCase();
  const local = person.email.split("@", 1)[0].trim();
  return local ? local.slice(0, 1).toUpperCase() : "?";
}

/** Paint `chip`'s avatar with the colour `mailosh.ui.format.avatar_color`
 *  would have chosen — SHA-256 of the lower-cased address, first byte
 *  mod 12.
 *
 *  Asynchronous because `crypto.subtle.digest` is, and that is fine: the
 *  chip is already on screen in the palette's neutral, and the real
 *  colour lands a fraction of a frame later. Where `crypto.subtle` is
 *  missing (a non-secure context — never this app in dev on localhost or
 *  in production over TLS, but not worth throwing over) the neutral
 *  simply stays. */
function paintAvatar(chip, email) {
  const avatar = chip.querySelector(".recipient-avatar");
  if (avatar === null || !window.crypto?.subtle) return;
  const bytes = new TextEncoder().encode(email.trim().toLowerCase());
  window.crypto.subtle
    .digest("SHA-256", bytes)
    .then((digest) => {
      const first = new Uint8Array(digest)[0];
      avatar.style.background = "var(--label-" + LABEL_COLORS[first % 12] + ")";
    })
    .catch(() => {});
}

/** Build one chip, byte-for-byte the element `compose/form.html`'s
 *  `recipient()` macro renders. One shape, two writers — which is why the
 *  macro and this function name the same classes, the same `data-`
 *  attributes and the same hidden input. */
function buildChip(field, person, external) {
  const chip = document.createElement("span");
  // The one judgement made locally, and only in the negative direction:
  // something with no `@` in it cannot be an address, so it is marked now
  // rather than two seconds later. Everything subtler than that is the
  // server's — `InvalidAddress` names the chip and `htmx:afterSwap` below
  // marks it.
  chip.className = "recipient";
  if (external) chip.classList.add("is-external");
  if (!person.email.includes("@")) chip.classList.add("is-invalid");
  chip.dataset.recipient = "";
  chip.dataset.email = person.email;
  chip.title = external ? person.email + " — outside your domain" : person.email;

  const avatar = document.createElement("span");
  avatar.className = "recipient-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = initialOf(person);
  avatar.style.background = "var(--label-slate)";

  const label = document.createElement("span");
  label.className = "recipient-label";
  label.textContent = person.name || person.email;

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "recipient-x";
  remove.dataset.role = "unchip";
  remove.setAttribute("aria-label", "Remove " + (person.name || person.email));
  // The macro inlines the vendored Lucide `x`; a text glyph here would be
  // a second, differently-shaped close affordance on the same control.
  remove.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" ' +
    'stroke-width="1.75" class="size-3" aria-hidden="true">' +
    '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

  const hidden = document.createElement("input");
  hidden.type = "hidden";
  hidden.name = field;
  hidden.value = addressValue(person);

  chip.append(avatar, label, remove, hidden);
  paintAvatar(chip, person.email);
  return chip;
}

/** Whether `email` is outside the sender's own domain — spec §8's
 *  external-domain hint, for a chip built here.
 *
 *  The domain is read off the form's own `data-domain`, which
 *  `mailosh.web.compose._context` writes from the session's user. That
 *  is the same value the server used to decide `is-external` on the chips
 *  it drew, so a typed address and a reopened one cannot disagree — and
 *  it is per-compose rather than per-document, so it is present on an
 *  inline reply card and a dock alike with no page-level plumbing. Absent
 *  means no hint at all, which is the right answer: a hint nobody can
 *  calibrate is noise. */
function isExternal(root, email) {
  const mine = (formOf(root)?.dataset.domain ?? "").trim().toLowerCase();
  if (!mine) return false;
  const at = email.lastIndexOf("@");
  return at === -1 || email.slice(at + 1).toLowerCase() !== mine;
}

/** Tell the form something changed, so htmx's `input delay:2s` autosave
 *  restarts its timer. Committing a chip writes a hidden input, and a
 *  programmatic value change fires no event of its own — without this a
 *  message addressed and then left alone would never be saved. */
function touch(el) {
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

/** Commit whatever is typed in `entry` as chips. Returns true when at
 *  least one landed, which is what tells a `Tab` keypress whether to
 *  claim the keystroke or hand it back to the browser. */
function commitChips(entry) {
  const field = entry.dataset.entry;
  const box = entry.closest("[data-chipbox]");
  if (!field || box === null) return false;
  const existing = new Set(
    Array.from(box.querySelectorAll("[data-recipient]")).map((chip) =>
      (chip.dataset.email ?? "").toLowerCase(),
    ),
  );
  let added = false;
  for (const piece of splitAddresses(entry.value)) {
    const person = parseAddress(piece);
    if (person === null) continue;
    const key = person.email.toLowerCase();
    if (existing.has(key)) continue;
    existing.add(key);
    box.insertBefore(buildChip(field, person, isExternal(composeOf(entry), person.email)), entry);
    added = true;
  }
  if (added) {
    entry.value = "";
    touch(entry);
  }
  return added;
}

/** Backspace on an empty entry: the last chip becomes editable text
 *  again rather than simply vanishing. Spec §8's "Backspace re-edits" —
 *  the difference matters because a chip is usually wrong by one
 *  character, and deleting it outright makes the reader retype an address
 *  they had almost right. */
function reEditLastChip(entry) {
  const box = entry.closest("[data-chipbox]");
  const chips = box === null ? [] : Array.from(box.querySelectorAll("[data-recipient]"));
  if (chips.length === 0) return false;
  const last = chips[chips.length - 1];
  entry.value = last.querySelector('input[type="hidden"]')?.value ?? last.dataset.email ?? "";
  last.remove();
  touch(entry);
  return true;
}

// ---------------------------------------------------------------------
// The editor
// ---------------------------------------------------------------------

/** Sanitise HTML on its way *into* the editor.
 *
 *  A reply quotes a message a stranger wrote. Squire's own default config
 *  routes everything it parses through `DOMPurify` — that is why
 *  `purify.min.js` has to be loaded on any page that builds a Squire
 *  instance, and why `layouts/app.html` loads both — but the seeding call
 *  below goes through this first regardless. Sanitising twice costs
 *  nothing; sanitising zero times, because one library's default config
 *  changed under us, is a stored-XSS bug in a mail client. */
function purify(html) {
  const purifier = window.DOMPurify;
  return purifier?.sanitize ? purifier.sanitize(html) : "";
}

function editorOf(root) {
  return live.get(root)?.editor ?? null;
}

/** Keep the two body fields in step with what is on screen.
 *
 *  In rich mode the editor is the truth: its HTML goes to `[data-html]`
 *  and its rendered text to the `text` textarea, so an outgoing message
 *  always carries a real `text/plain` alternative part rather than one
 *  the server had to guess at from tags.
 *
 *  In plain mode the textarea is the truth and the HTML field is emptied,
 *  which is what makes the toggle mean something: a plain-text message
 *  that still shipped an HTML part would not be one. */
function syncBody(root) {
  const html = root.querySelector("[data-html]");
  const plain = root.querySelector("[data-plain]");
  const editor = editorOf(root);
  if (root.dataset.mode === "plain") {
    if (html) html.value = "";
    return;
  }
  if (editor === null || html === null) return;
  html.value = editor.getHTML();
  if (plain) plain.value = root.querySelector("[data-editor]")?.innerText ?? "";
}

/** Build the Squire instance for one compose window, or leave the
 *  component in plain-text mode if the editor could not be built.
 *
 *  Degrading rather than throwing is deliberate: a vendored script that
 *  failed to load must cost the reader rich text, not the ability to
 *  answer their mail. */
function makeEditor(root) {
  const host = root.querySelector("[data-editor]");
  const html = root.querySelector("[data-html]");
  if (host === null || typeof window.Squire !== "function") {
    root.dataset.mode = "plain";
    return null;
  }
  // Squire's default config already routes parsed content through the
  // global `DOMPurify`; nothing is overridden here, so that default is
  // what runs. `blockTag: "DIV"` is Squire's own default too, named
  // explicitly because it is what `mailosh.web.app.html_to_text` reads
  // when it derives the plain-text part.
  const editor = new window.Squire(host, { blockTag: "DIV" });
  const seed = html?.value ?? "";
  if (seed) editor.setHTML(purify(seed));
  // Both halves: the native `input` a contenteditable fires (which is
  // also what bubbles to the form and drives htmx's autosave) and
  // Squire's own emitter, which reports changes its commands make
  // without a keystroke — a list toggled from the popover, say.
  host.addEventListener("input", () => syncBody(root));
  editor.addEventListener("input", () => syncBody(root));
  return editor;
}

/** Move the editor's content into the textarea, or back. Both directions
 *  are lossy in the way the reader expects — turning formatting off
 *  throws formatting away — so the toggle is `aria-pressed` on a real
 *  button, not a silent mode change. */
function setPlainText(root, on) {
  const editor = editorOf(root);
  const host = root.querySelector("[data-editor]");
  const plain = root.querySelector("[data-plain]");
  const button = root.querySelector('[data-role="plaintext"]');
  if (on) {
    if (plain && host) plain.value = host.innerText;
  } else if (editor !== null && plain) {
    const lines = plain.value.split("\n");
    const escaped = lines.map((line) => {
      const div = document.createElement("div");
      div.textContent = line;
      return "<div>" + (div.innerHTML || "<br>") + "</div>";
    });
    editor.setHTML(escaped.join(""));
  }
  root.dataset.mode = on ? "plain" : "rich";
  button?.setAttribute("aria-pressed", on ? "true" : "false");
  syncBody(root);
  if (on) plain?.focus();
  else editor?.focus();
  touch(root.querySelector("form") ?? root);
}

/** One formatting command from the popover (or from `⌘K`). Squire owns
 *  every one of them; this only maps a `data-fmt` name onto a method, so
 *  the popover's markup names an action rather than an API. */
function format(root, name, size) {
  const editor = editorOf(root);
  if (editor === null) return;
  editor.focus();
  if (name === "bold") editor.bold();
  else if (name === "italic") editor.italic();
  else if (name === "underline") editor.underline();
  else if (name === "ul") editor.makeUnorderedList();
  else if (name === "ol") editor.makeOrderedList();
  else if (name === "quote") editor.increaseQuoteLevel();
  else if (name === "clear") editor.removeAllFormatting();
  else if (name === "unlink") editor.removeLink();
  else if (name === "size" && size) editor.setFontSize(size);
  else if (name === "link") insertLink(root);
  syncBody(root);
  touch(root.querySelector("form") ?? root);
}

/** `⌘K` / the link button. `window.prompt` is the platform's own text
 *  input and needs no markup, no focus trap and no second dialog stacked
 *  over a dock — and unlike a `<dialog>` it does not take the keyboard
 *  away from the page while it is open in a way this app has to unwind. */
function insertLink(root) {
  const editor = editorOf(root);
  if (editor === null) return;
  const url = window.prompt("Link address");
  if (url === null) return;
  const trimmed = url.trim();
  if (trimmed === "") return;
  const href = /^[a-z][a-z0-9+.-]*:/i.test(trimmed) ? trimmed : "https://" + trimmed;
  // Anything but http(s) is refused rather than linked: `javascript:` in
  // a message body is the oldest trick there is, and a compose window is
  // not the place to invent a second sanitiser.
  if (!/^https?:/i.test(href)) {
    toast("Links have to start with http:// or https://");
    return;
  }
  editor.makeLink(href, { target: "_blank", rel: "noopener noreferrer" });
  syncBody(root);
  touch(root.querySelector("form") ?? root);
}

// ---------------------------------------------------------------------
// Dock chrome: minimise, full screen, tiling
// ---------------------------------------------------------------------

/** Lay the docks out along the bottom-right (spec §8: "up to 3 docks tile
 *  leftwards").
 *
 *  Position is written as a custom property rather than as `right`
 *  directly, so the `max-width: 767px` rule in styles/input.css — which
 *  makes a dock full-screen — can still win: an inline `right` would beat
 *  any stylesheet, and a dock would have gone full-screen everywhere
 *  except horizontally.
 *
 *  A dock that would not fit is minimised rather than pushed off screen.
 *  Three 560 px docks need 1740 px, which plenty of laptops do not have;
 *  the alternative to collapsing the oldest one is a compose window with
 *  its Send button past the left edge. */
function reflow() {
  if (window.innerWidth < NARROW) return;
  let x = DOCK_EDGE;
  for (const dock of docks()) {
    if (dock.dataset.state === "full") {
      dock.style.removeProperty("--dock-right");
      continue;
    }
    const wanted = dock.dataset.state === "minimized" ? DOCK_MIN_WIDTH : DOCK_WIDTH;
    if (dock.dataset.state !== "minimized" && x + wanted + DOCK_EDGE > window.innerWidth) {
      dock.dataset.state = "minimized";
    }
    const width = dock.dataset.state === "minimized" ? DOCK_MIN_WIDTH : DOCK_WIDTH;
    dock.style.setProperty("--dock-right", x + "px");
    x += width + DOCK_GAP;
  }
}

function setState(root, state) {
  root.dataset.state = state;
  reflow();
}

function toggleMinimized(root) {
  setState(root, root.dataset.state === "minimized" ? "open" : "minimized");
}

function toggleFull(root) {
  const full = root.dataset.state === "full";
  setState(root, full ? "open" : "full");
  root.querySelector('[data-role="expand"]')?.setAttribute("title", full ? "Full screen" : "Exit full screen");
}

/** Put the caret where the reader would put it: the address field for a
 *  new message, the body for a reply or a forward (the recipients are
 *  already right, and what they came to do is write).
 *
 *  The only place in this file that takes focus, and it runs once per
 *  compose window, at the moment one is opened on purpose. */
function focusFirst(root) {
  const hasRecipients = root.querySelector('[data-field="to"] [data-recipient]') !== null;
  if (!hasRecipients) {
    root.querySelector('[data-entry="to"]')?.focus();
    return;
  }
  if (root.dataset.mode === "plain") root.querySelector("[data-plain]")?.focus();
  else editorOf(root)?.focus();
}

// ---------------------------------------------------------------------
// Attachments
// ---------------------------------------------------------------------

function attachmentRow(root) {
  return root.querySelector("[data-attachments]");
}

/** The chip shown while a file is still going up. Replaced outright by
 *  the server's own `compose/attachment.html` when the upload lands, so
 *  there is exactly one rendering of a finished attachment in this app
 *  and it is the server's. */
function pendingChip(file) {
  const chip = document.createElement("span");
  chip.className = "attach-chip is-uploading";
  chip.style.setProperty("--pct", "0%");
  const name = document.createElement("span");
  name.className = "attach-name";
  name.textContent = file.name;
  const size = document.createElement("span");
  size.className = "attach-size";
  size.textContent = "Uploading…";
  const bar = document.createElement("span");
  bar.className = "attach-bar";
  chip.append(name, size, bar);
  return chip;
}

function upload(root, file) {
  const row = attachmentRow(root);
  if (row === null) return;
  const chip = pendingChip(file);
  row.append(chip);

  const request = new XMLHttpRequest();
  request.open("POST", "/attachments");
  request.setRequestHeader("X-CSRF-Token", csrfToken());
  request.setRequestHeader("HX-Request", "true");
  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    chip.style.setProperty("--pct", Math.round((event.loaded / event.total) * 100) + "%");
  });
  request.addEventListener("load", () => {
    // `mailosh/web/app.py` answers a JMAP failure app-wide with a **200**,
    // an empty body and an `om:error` trigger — the failure contract every
    // `fetch` in this app already checks. Treating that 200 as success
    // swapped the chip for nothing: no file attached, and nothing said.
    const raw = request.getResponseHeader("HX-Trigger") ?? "";
    if (request.status < 200 || request.status >= 300 || raw.includes("om:error")) {
      chip.remove();
      let why = request.status === 413 ? "That file is too large to attach" : "Couldn't attach that file";
      try {
        why = JSON.parse(raw)["om:error"]?.toast ?? why;
      } catch {
        // Not JSON, or not ours: the generic line above stands.
      }
      toast(why);
      return;
    }
    // Our own origin's HTML, rendered by `compose/attachment.html` with
    // Jinja autoescaping on — the same trust an htmx swap places in the
    // same server. It replaces the optimistic chip outright so a finished
    // attachment has exactly one rendering in this app, the server's.
    chip.outerHTML = request.responseText;
    touch(row);
  });
  request.addEventListener("error", () => {
    chip.remove();
    toast("Couldn't attach that file");
  });

  const payload = new FormData();
  payload.append("file", file);
  payload.append("dom_id", root.id);
  request.send(payload);
}

function attach(root, files) {
  let total = 0;
  for (const file of files) total += file.size;
  if (total > SOFT_ATTACHMENT_BYTES) {
    toast("That's over 25 MB — some mail servers will bounce it");
  }
  for (const file of files) upload(root, file);
}

// ---------------------------------------------------------------------
// Saving, sending, discarding
// ---------------------------------------------------------------------

function draftId(root) {
  return root.querySelector('input[name="draft_id"]')?.value ?? "";
}

// Whitespace plus the zero-width characters an editor leaves behind — the
// same set `mailosh.web.compose._BLANK` uses, and for the same reason:
// Squire drops a U+200B into an empty document when a block command runs
// on it, and `String.prototype.trim()` does not remove it. Without this,
// opening a dock, toggling a list on and off again, and closing it left a
// draft in the Drafts folder whose whole body was invisible.
const BLANK = /^[\s\u00a0\u200b\u200c\u200d\ufeff]*$/;

function isEmpty(root) {
  const params = body(root);
  const typed = ["to", "cc", "bcc", "subject", "text"].some((name) =>
    params.getAll(name).some((value) => !BLANK.test(value)),
  );
  return !typed && params.getAll("attachment").length === 0;
}

/** Save now rather than in two seconds — what closing a dock does, so the
 *  answer to "did it keep my draft?" is yes even for a message written
 *  and closed inside the autosave window. */
function saveNow(root) {
  if (isEmpty(root)) return Promise.resolve(null);
  return post("/compose/draft", body(root))
    .then((response) => (failed(response) ? null : response.text()))
    .then((text) => {
      if (text === null) return null;
      // The response is the state fragment plus the out-of-band draft-id
      // input; only the id is wanted here, and it is on the fragment as
      // `data-draft-id` for exactly this reader.
      const found = text.match(/data-draft-id="([^"]*)"/);
      return found ? found[1] : null;
    })
    .catch(() => null);
}

function removeCompose(root) {
  const record = live.get(root);
  if (record?.timer) window.clearTimeout(record.timer);
  root.remove();
  reflow();
}

/** Stop an autosave that is in flight right now. Every path below that
 *  reads `draft_id` and then posts against it — save-and-close, discard,
 *  pop-out — raced that request: both carried the same old id, the server
 *  created a draft for each and destroyed the old one twice, and the
 *  reader found two copies in Drafts (or, after a discard, one that came
 *  back). */
function abortAutosave(root) {
  const form = formOf(root);
  if (form !== null) window.htmx?.trigger?.(form, "htmx:abort");
}

/** Close: the draft is kept. Spec §8's `Esc` "close (draft kept)", and
 *  the header's × says "Save & close" for the same reason — a control
 *  that throws mail away must be a different control, and it is (the
 *  trash, below). */
function close(root) {
  abortAutosave(root);
  const empty = isEmpty(root);
  if (!empty) saveNow(root);
  removeCompose(root);
  if (!empty) toast("Draft saved");
}

function discard(root) {
  abortAutosave(root);
  const params = new URLSearchParams();
  const id = draftId(root);
  if (id) params.append("draft_id", id);
  removeCompose(root);
  post("/compose/discard", params)
    .then((response) => {
      if (failed(response)) toast("Couldn't discard that draft");
      else toast("Draft discarded");
    })
    .catch(() => toast("Couldn't discard that draft"));
}

/** Spec §6.3's send toast: "Sending… Undo", ten seconds, then the request
 *  actually goes.
 *
 *  Its own element rather than `ui.toast()`'s, because that one's Undo
 *  button dispatches `om:undo` and `actions.js` answers it by POSTing
 *  `/a/undo` — the right thing for an archive and completely wrong for a
 *  send that has not happened yet. Same `.toast` component, same
 *  placement, one different button. */
function sendingToast(onUndo) {
  const host = document.getElementById("toasts");
  if (host === null) return null;
  const el = document.createElement("div");
  el.className = "toast pointer-events-auto";
  const text = document.createElement("span");
  text.textContent = "Sending…";
  const undo = document.createElement("button");
  undo.type = "button";
  undo.textContent = "Undo";
  undo.addEventListener("click", () => {
    el.remove();
    onUndo();
  });
  el.append(text, undo);
  host.append(el);
  const status = document.getElementById("status");
  if (status) status.textContent = "Sending";
  return el;
}

/** Everything counting down right now, so `pagehide` can flush it. A
 *  `Set` of dock elements rather than of requests: the body is read at
 *  flush time, which is what makes an undo that fired a millisecond
 *  earlier actually cancel the send rather than race it. */
const sending = new Set();

function flush(root, options = {}) {
  const record = live.get(root);
  if (record?.timer) {
    window.clearTimeout(record.timer);
    record.timer = null;
  }
  if (!sending.has(root)) return;
  sending.delete(root);
  const params = record?.body ?? body(root);
  // The snapshot's `draft_id` is ten seconds old. An autosave armed by the
  // last keystroke (`input delay:2s`) can land in that window, and it
  // *replaces* the draft — a new id is swapped into the still-present form
  // and the old one is destroyed. Sending the old id then destroyed
  // nothing and left the new draft behind as a copy of the sent mail.
  params.set("draft_id", draftId(root));
  if (options.keepalive) {
    post("/compose/send", params, { keepalive: true });
    return;
  }
  post("/compose/send", params)
    .then((response) => {
      const problem = errorTrigger(response);
      if (problem !== null || !response.ok) {
        // Reopened with everything still in it — which is the whole
        // reason the dock is hidden rather than destroyed while a send is
        // in flight (spec §8: "failures reopen the dock with the error").
        // The server's own copy when it had any: "Add a recipient first"
        // is something the reader can act on, "Couldn't send that
        // message" is not.
        setState(root, "open");
        focusFirst(root);
        toast(problem?.toast ?? "Couldn't send that message");
        return;
      }
      removeCompose(root);
      toast("Sent");
    })
    .catch(() => {
      setState(root, "open");
      toast("Couldn't send that message");
    });
}

function send(root) {
  // Whatever is half-typed in a recipient field is committed first. A
  // click on Send does not always take focus out of the entry, and an
  // address left as text in the box is one the message would not go to.
  for (const entry of root.querySelectorAll(".chip-entry")) commitChips(entry);
  const params = body(root);
  const addressed = ["to", "cc", "bcc"].some((name) =>
    params.getAll(name).some((value) => value.trim() !== ""),
  );
  if (!addressed) {
    toast("Add a recipient first");
    root.querySelector('[data-entry="to"]')?.focus();
    return false;
  }
  // Spec §8's forgotten-attachment check: asked once, only when the body
  // says "attached" and nothing is.
  if (
    params.getAll("attachment").length === 0 &&
    /\battach(ed|ment|ments|ing)?\b/i.test(params.get("text") ?? "") &&
    !window.confirm("This message mentions an attachment but has none. Send anyway?")
  ) {
    return false;
  }

  const record = live.get(root) ?? {};
  record.body = params;
  // Hidden, not removed. A send is a *delayed* commit (spec §6.3), so for
  // the next ten seconds this message still exists and Undo has to be
  // able to put it back on screen exactly as it was — editor state
  // included, which a re-render could not restore.
  setState(root, "sending");
  sending.add(root);
  const toastEl = sendingToast(() => {
    sending.delete(root);
    if (record.timer) window.clearTimeout(record.timer);
    record.timer = null;
    setState(root, "open");
    focusFirst(root);
  });
  record.timer = window.setTimeout(() => {
    toastEl?.remove();
    flush(root);
  }, SEND_UNDO_MS);
  live.set(root, record);
  return true;
}

// A send counting down when the tab goes away is still a send the reader
// asked for. `keepalive` is what lets a request outlive the document, and
// a urlencoded body is what `keepalive` will actually carry — see
// `body()`.
window.addEventListener("pagehide", () => {
  for (const root of Array.from(sending)) flush(root, { keepalive: true });
});

// ---------------------------------------------------------------------
// Opening
// ---------------------------------------------------------------------

function ajax(verb, url, target, swap) {
  const htmx = window.htmx;
  if (!htmx?.ajax) return false;
  htmx.ajax(verb, url, { target: target, swap: swap });
  return true;
}

/** A dock: empty, or reopening the saved draft `draftId` names.
 *
 *  Refused rather than silently ignored past three: the fourth would either
 *  tile off screen or stack on top of one of the others, and both are worse
 *  than being told. Reopening a draft goes through here for exactly that
 *  reason -- a Drafts row carrying its own `hx-get` would open a fourth and
 *  never see the toast. */
function open(draftId) {
  if (mount() === null) return undefined;
  if (docks().length >= MAX_DOCKS) {
    toast("Three drafts are already open");
    return false;
  }
  const url = draftId ? `/compose/${encodeURIComponent(draftId)}` : "/compose";
  return ajax("GET", url, MOUNT, "beforeend");
}

/** The conversation the reader is in, and the message they are on — the
 *  two things a reply needs. Read off the DOM `thread/page.html` already
 *  renders (`data-thread-id` on the scroller, `data-email-ids` on each
 *  card), never off a second model kept here that a live update could put
 *  out of step. */
function replyTarget() {
  const scroll = document.querySelector("[data-thread-id]");
  if (scroll === null) return null;
  const cards = Array.from(scroll.querySelectorAll("article[data-email-ids]"));
  if (cards.length === 0) return null;
  const held = document.activeElement?.closest?.("article[data-email-ids]") ?? null;
  const card = held ?? cards[cards.length - 1];
  const id = (card.dataset.emailIds ?? "").split(",")[0];
  return id ? { thread: scroll.dataset.threadId, email: id } : null;
}

/** Where an inline composer card lives: one slot at the end of the
 *  conversation, created on demand.
 *
 *  Created here rather than rendered by `thread/page.html` so the
 *  conversation template owns no compose markup at all — the card is a
 *  compose concern, and a slot that only ever holds one is not worth a
 *  second template's worth of coupling. */
function replySlot() {
  const scroll = document.querySelector("[data-thread-id]");
  if (scroll === null) return null;
  let slot = document.getElementById("compose-inline");
  if (slot === null) {
    slot = document.createElement("div");
    slot.id = "compose-inline";
    scroll.append(slot);
  }
  return slot;
}

function reply(mode) {
  const target = replyTarget();
  if (target === null) return undefined;
  const slot = replySlot();
  if (slot === null) return undefined;
  const url =
    "/compose/reply/" +
    encodeURIComponent(target.email) +
    "?mode=" +
    encodeURIComponent(mode) +
    "&thread=" +
    encodeURIComponent(target.thread);
  return ajax("GET", url, slot, "innerHTML");
}

/** Pop an inline card out into a real dock, keeping what has been typed.
 *
 *  Saves first and reopens the *saved draft* rather than re-deriving the
 *  reply from the thread: re-deriving would quietly throw away everything
 *  written so far, which is the one thing a control called "open in a
 *  compose window" must not do. */
function popOut(root) {
  if (docks().length >= MAX_DOCKS) {
    toast("Three drafts are already open");
    return;
  }
  abortAutosave(root);
  saveNow(root).then((id) => {
    if (!id) {
      toast("Couldn't open that in a window");
      return;
    }
    if (ajax("GET", "/compose/" + encodeURIComponent(id), MOUNT, "beforeend")) {
      removeCompose(root);
    }
  });
}

// ---------------------------------------------------------------------
// The Alpine component (spec §8)
// ---------------------------------------------------------------------

/** `Alpine.data("compose")`. The object carries no state of its own and
 *  no methods a directive could call — the CSP build would reject an
 *  inline expression anyway — only the `init`/`destroy` pair, which is
 *  what a Squire instance needs and what nothing else in this codebase
 *  can provide: htmx will happily swap a dock away without telling
 *  anybody, and an editor left behind keeps its listeners and its
 *  document alive. */
function composeComponent() {
  return {
    init() {
      const root = this.$root ?? this.$el;
      if (root === undefined || root === null || live.has(root)) return;
      live.set(root, { editor: null, timer: null, body: null });
      const record = live.get(root);
      record.editor = makeEditor(root);
      syncBody(root);
      reflow();
      focusFirst(root);
    },
    destroy() {
      const root = this.$root ?? this.$el;
      const record = root === null || root === undefined ? null : live.get(root);
      if (record === null || record === undefined) return;
      if (record.timer) window.clearTimeout(record.timer);
      record.editor?.destroy();
      live.delete(root);
      reflow();
    },
  };
}

function register() {
  window.Alpine.data("compose", composeComponent);
}

if (window.Alpine) register();
else document.addEventListener("alpine:init", register, { once: true });

// ---------------------------------------------------------------------
// Delegated listeners — one of each, on `document.body`
// ---------------------------------------------------------------------

document.body.addEventListener("click", (event) => {
  const control = event.target?.closest?.("[data-role], [data-fmt]");
  const root = composeOf(event.target);
  if (root === null) return;

  if (control === null || control === undefined) {
    // A click anywhere in a chip box puts the caret in that field's
    // entry, which is what makes the whole box read as one control.
    const box = event.target?.closest?.("[data-chipbox]");
    if (box) box.querySelector(".chip-entry")?.focus();
    return;
  }

  if (control.dataset.fmt) {
    event.preventDefault();
    format(root, control.dataset.fmt, control.dataset.size);
    return;
  }

  const role = control.dataset.role;
  if (role === "bar" || role === "title") {
    toggleMinimized(root);
    return;
  }
  event.preventDefault();
  if (role === "minimize") toggleMinimized(root);
  else if (role === "expand") toggleFull(root);
  else if (role === "close") close(root);
  else if (role === "popout") popOut(root);
  else if (role === "discard") discard(root);
  else if (role === "send") send(root);
  else if (role === "attach") root.querySelector("[data-file]")?.click();
  else if (role === "link") insertLink(root);
  else if (role === "plaintext") setPlainText(root, root.dataset.mode !== "plain");
  else if (role === "unchip") {
    const chip = control.closest("[data-recipient]");
    const box = chip?.closest("[data-chipbox]") ?? null;
    chip?.remove();
    if (box) {
      touch(box);
      box.querySelector(".chip-entry")?.focus();
    }
  } else if (role === "unattach") {
    const chip = control.closest("[data-attachment]");
    const row = chip?.closest("[data-attachments]") ?? null;
    chip?.remove();
    if (row) touch(row);
  } else if (role === "reveal") {
    const field = root.querySelector('[data-field="' + control.dataset.target + '"]');
    if (field) {
      field.hidden = false;
      field.querySelector(".chip-entry")?.focus();
    }
  } else if (role === "format") {
    const popover = root.querySelector("[data-popover]");
    if (popover) {
      popover.hidden = !popover.hidden;
      control.setAttribute("aria-expanded", popover.hidden ? "false" : "true");
    }
  }
});

// The header bar is the minimise gesture (Gmail's), so the three controls
// inside it must not also trip it. Handled by ordering rather than by
// `stopPropagation`: the delegated listener above resolves the *nearest*
// `[data-role]`, and a button inside the bar is nearer than the bar.

// Entry points that live OUTSIDE any compose window: the nav's Compose
// button and the conversation's Reply / Reply all / Forward bar. The
// delegated handler above cannot serve them -- it resolves `composeOf(...)`
// first and returns when the click is not inside a dock, which is correct
// for everything it does own.
//
// These route through `open`/`reply` rather than carrying their own
// `hx-get` so that the three-dock limit, its toast, and the choice of
// inline-card-vs-dock stay in exactly one place. A button with its own
// `hx-get="/compose"` would open a fourth dock and never see the toast.
document.body.addEventListener("click", (event) => {
  const trigger = event.target?.closest?.(
    "[data-compose-open], [data-compose-reply], [data-compose-draft]"
  );
  if (!trigger) return;
  // A modifier click on the Drafts row is left to the browser: the anchor
  // still points at `/compose/{id}`, so "open in a new tab" keeps working
  // rather than being swallowed.
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
  event.preventDefault();
  if (trigger.dataset.composeDraft) open(trigger.dataset.composeDraft);
  else if (trigger.hasAttribute("data-compose-open")) open();
  else reply(trigger.dataset.composeReply);
});

document.body.addEventListener("keydown", (event) => {
  const entry = event.target?.closest?.(".chip-entry");
  if (!entry) return;
  if (CHIP_KEYS.includes(event.key)) {
    // Tab with nothing typed is still Tab: the reader is leaving the
    // field, not committing an empty address.
    if (event.key === "Tab" && entry.value.trim() === "") return;
    const committed = commitChips(entry);
    if (event.key !== "Tab" || committed) event.preventDefault();
    return;
  }
  if (event.key === "Backspace" && entry.value === "" && reEditLastChip(entry)) {
    event.preventDefault();
  }
});

// Leaving the field commits what is in it. Without this, typing an
// address and clicking straight into the body silently drops it — the
// single most common way to send a message to nobody.
document.body.addEventListener(
  "focusout",
  (event) => {
    const entry = event.target?.closest?.(".chip-entry");
    if (entry) commitChips(entry);
  },
  true,
);

document.body.addEventListener("paste", (event) => {
  const entry = event.target?.closest?.(".chip-entry");
  if (!entry) return;
  const text = event.clipboardData?.getData("text/plain") ?? "";
  if (!text) return;
  event.preventDefault();
  entry.value = entry.value + text;
  commitChips(entry);
});

document.body.addEventListener("change", (event) => {
  const input = event.target?.closest?.("[data-file]");
  if (!input) return;
  const root = composeOf(input);
  if (root === null) return;
  attach(root, Array.from(input.files ?? []));
  // Cleared so choosing the same file twice in a row still fires.
  input.value = "";
});

// Drag a file onto a compose window and it attaches — the gesture people
// try first, and the one Gmail answers. Bound at document level and
// filtered by `closest` so the rest of the page keeps the browser's own
// "open this file" behaviour.
document.body.addEventListener("dragover", (event) => {
  const root = composeOf(event.target);
  if (root === null || !event.dataTransfer?.types?.includes("Files")) return;
  event.preventDefault();
  root.dataset.drop = "on";
});
document.body.addEventListener("dragleave", (event) => {
  const root = composeOf(event.target);
  if (root !== null && !root.contains(event.relatedTarget)) delete root.dataset.drop;
});
document.body.addEventListener("drop", (event) => {
  const root = composeOf(event.target);
  if (root === null) return;
  const files = Array.from(event.dataTransfer?.files ?? []);
  if (files.length === 0) return;
  event.preventDefault();
  delete root.dataset.drop;
  attach(root, files);
});

// A click outside closes the formatting popover, the way every popover in
// this app behaves. Capture phase so it runs before the delegated click
// listener above can re-open the one that was just toggled.
document.body.addEventListener(
  "click",
  (event) => {
    for (const popover of document.querySelectorAll("[data-popover]:not([hidden])")) {
      const root = composeOf(popover);
      if (event.target?.closest?.("[data-popover]") === popover) continue;
      if (event.target?.closest?.('[data-role="format"]') !== null && composeOf(event.target) === root) {
        continue;
      }
      popover.hidden = true;
      root?.querySelector('[data-role="format"]')?.setAttribute("aria-expanded", "false");
    }
  },
  true,
);

/** Mark the chip a refused autosave named, and clear the marks a
 *  successful one clears.
 *
 *  The only htmx event this file listens for, and it earns it: the state
 *  fragment `POST /compose/draft` swaps in carries `data-error-field` and
 *  `data-error-address` precisely so the reader is pointed at the one
 *  address that is wrong rather than left to compare a sentence against
 *  five chips. Everything else about that round trip — the readout, the
 *  new draft id — needs no JavaScript at all. */
document.body.addEventListener("htmx:afterSwap", (event) => {
  const chip = event.detail?.target ?? event.target;
  if (!chip?.classList?.contains?.("compose-state")) return;
  const root = composeOf(chip);
  if (root === null) return;
  for (const marked of root.querySelectorAll(".recipient.is-invalid")) {
    marked.classList.remove("is-invalid");
  }
  const field = chip.dataset.errorField;
  const address = (chip.dataset.errorAddress ?? "").toLowerCase();
  if (!field || !address) return;
  const scope = root.querySelector('[data-field="' + field + '"]') ?? root;
  for (const candidate of scope.querySelectorAll("[data-recipient]")) {
    if ((candidate.dataset.email ?? "").toLowerCase() === address) {
      candidate.classList.add("is-invalid");
    }
  }
});

window.addEventListener("resize", reflow);

// ---------------------------------------------------------------------
// What `keys.js` calls
// ---------------------------------------------------------------------
//
// Handed over the same way `palette.js` hands over its opener: this file
// imports the registry's registrar, the registry never imports this file.
// Every entry answers the registry's three-value contract — `true` it
// ran, `false` it is live here but had nothing to act on, `undefined` it
// declines and the keystroke goes back to the browser.

// Exported as well as registered: `compose-boot.js` loads this module on
// demand and needs to run the very action that triggered the load, and it
// cannot ask `keys.js` for the surface it just handed over.
export const surface = {
  open: open,
  reply: reply,
  send() {
    const root = focusedCompose();
    return root === null ? undefined : send(root);
  },
  reveal(field) {
    const root = focusedCompose();
    if (root === null) return undefined;
    const target = root.querySelector('[data-field="' + field + '"]');
    if (target === null) return false;
    target.hidden = false;
    target.querySelector(".chip-entry")?.focus();
    return true;
  },
  link() {
    const root = focusedCompose();
    if (root === null || root.dataset.mode === "plain") return undefined;
    insertLink(root);
    return true;
  },
  close() {
    const root = focusedCompose();
    if (root === null) return undefined;
    const popover = root.querySelector("[data-popover]:not([hidden])");
    if (popover !== null) {
      // Esc closes the nearest thing first. Taking the whole dock away
      // because a popover was open would be a keystroke that did far more
      // than the reader asked for.
      popover.hidden = true;
      root.querySelector('[data-role="format"]')?.setAttribute("aria-expanded", "false");
      return true;
    }
    close(root);
    return true;
  },
};

registerCompose(surface);
