// Mailosh — the dedicated print page's one behaviour: print itself.
//
// `GET /t/{id}/print` (thread/print.html) is opened by the ⋮ menu's Print
// item in a new tab, and what the reader asked for is a print dialog, not a
// second page to press Cmd+P on. The CSP is `script-src 'self'` with no
// `unsafe-eval` and no inline script, so `onload="window.print()"` is not
// available — this module is where that call lives instead.
//
// **`load`, not `DOMContentLoaded`.** Every HTML message body on that page
// is an `<iframe src="/m/{id}/html?…">` with no `loading="lazy"`, and a
// frame that has not loaded yet prints blank. `load` is the one event that
// waits for subframes; the DOM being ready says nothing about them.
//
// The two frames after it are for the height handshake. A framed message
// measures itself and posts its height (mailosh/render/frame_document.py),
// and `frame.js` — loaded just before this file — writes that number onto
// the frame. The post is dispatched as the frame's own document finishes,
// so it is normally delivered before the parent's `load`; the two
// `requestAnimationFrame`s give the task queue a turn either way, so a
// long message is laid out at its full height before the dialog freezes
// the page. A frame that never reports keeps its CSS height and prints
// what fits — the same outcome as printing the reading view.
//
// Once, and only from this page: the module is loaded by no other
// template. `print()` is deliberately not re-armed on `afterprint` — a
// reader who cancels the dialog is left with the page, which is a
// perfectly good thing to be left with, and re-opening it would be an app
// arguing with someone who just said no.

function printWhenPainted() {
  requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
}

if (document.readyState === "complete") {
  // Already loaded: a module is deferred, so on a warm cache the `load`
  // event can have fired before this file ran, and a listener registered
  // then would never fire at all.
  printWhenPainted();
} else {
  window.addEventListener("load", printWhenPainted, { once: true });
}
