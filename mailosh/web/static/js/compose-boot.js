// Mailosh — the compose dock, loaded when someone actually composes.
//
// Squire (18 KB gz), DOMPurify (11 KB) and `compose.js` (7 KB minified) are
// 36 KB of the app shell's JavaScript, and none of it does anything until a
// reader opens a dock. Spec §11 budgets 90 KB for the whole shell; carrying
// the editor on every page load spent 40% of that on a feature most page
// views never touch.
//
// This file is what stays: it is small, it is always loaded, and it owns
// the compose entry points until the real module exists. Everything it does
// is a one-shot — once `compose.js` is in, it registers its own surface
// with `keys.js` and installs its own listeners, and this file steps out of
// the way rather than sitting in the middle of every later click.
//
// The alternative was tagging the editor onto only the pages a dock can
// open from. That is every authenticated page (`c` works from anywhere), so
// it would have saved nothing and added a rule someone would eventually get
// wrong.

import { registerCompose } from "./keys.js";

// Every control that opens a compose window: the nav's Compose button, the
// conversation's Reply / Reply all / Forward bar, and a Drafts row. Kept in
// step with `compose.js`'s own delegated handler by the tests below it.
const ENTRY = "[data-compose-open], [data-compose-reply], [data-compose-draft]";

let pending = null;
let loaded = false;

/** A classic `<script>`, as a promise. Squire and DOMPurify are not modules
 *  — they assign `window.Squire` and `window.DOMPurify` — so they cannot be
 *  `import`ed and have to be injected and awaited. */
function loadClassic(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.addEventListener("load", () => resolve());
    el.addEventListener("error", () => reject(new Error("compose: could not load " + src)));
    document.head.append(el);
  });
}

/** Load the editor once. Concurrent callers share the one promise, so three
 *  clicks before it lands cost one download, not three — and each click is
 *  then replayed, so the reader gets the same number of docks they would
 *  have got with the module already in. Measured both ways at 1440px: three
 *  rapid clicks give two docks whether or not this file had to load
 *  anything first, and one click gives one. Matching the loaded path is the
 *  point; the fact that the loaded path answers two rather than three is a
 *  pre-existing race in how fast a dock swap settles, and not this file's
 *  to change.
 *
 *  DOMPurify strictly before Squire: Squire's default `sanitizeToDOMFragment`
 *  calls a *global* `DOMPurify`, so a Squire instance built without it
 *  throws the first time anything is parsed — a paste, or seeding a reply
 *  with its quoted body. That ordering used to be enforced by two script
 *  tags; it is enforced here now, and pinned by a test either way. */
function ensure() {
  if (pending === null) {
    const { composePurify, composeSquire } = document.body.dataset;
    pending = loadClassic(composePurify)
      .then(() => loadClassic(composeSquire))
      .then(() => import("./compose.js"))
      .then((module) => {
        loaded = true;
        return module;
      })
      .catch((error) => {
        // A failed load is not a permanent one. Left set, `pending` would
        // hand the same rejected promise to every later click and `c`,
        // each swallowed by the listener below — compose dead until a
        // reload, and nothing said. Cleared, the next attempt dials again;
        // said, the reader knows why this one did nothing.
        pending = null;
        window.Alpine?.store?.("ui")?.toast?.("Couldn't open compose. Check your connection and try again.");
        throw error;
      });
  }
  return pending;
}

/** The rejection is already reported inside `ensure()`; a caller has
 *  nothing to add, and an unhandled one would only echo into the console. */
const reported = () => {};

// Capture phase, and only until `compose.js` arrives: after that its own
// handler is the one that should run, and a second listener here would open
// two docks for one click.
document.body.addEventListener(
  "click",
  (event) => {
    if (loaded) return;
    const trigger = event.target?.closest?.(ENTRY);
    if (!trigger) return;
    // A modifier click on a Drafts row is still the browser's — the anchor
    // points at a real `/compose/{id}`.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    // Replay the click once the module is in, so the decision about what
    // this control means stays in exactly one place.
    ensure().then(() => trigger.click(), reported);
  },
  true,
);

// `keys.js`'s three-value contract: `true` it ran, `false` live but nothing
// to act on, `undefined` declined. Loading is asynchronous and the answer is
// not, so this answers `true` — the keystroke *was* taken, and the reader
// sees a dock a moment later. Answering `undefined` would hand `c` back to
// the browser and type a "c" into the page.
registerCompose({
  open: (draftId) => {
    ensure().then((module) => module.surface.open(draftId), reported);
    return true;
  },
  reply: (mode) => {
    ensure().then((module) => module.surface.reply(mode), reported);
    return true;
  },
  // Only reachable with a dock already open, which means the module is in.
  // Declining is right: there is nothing to send, reveal or close.
  send: () => undefined,
  reveal: () => undefined,
  close: () => undefined,
});
