// Mailosh — the parent half of the mail frame's resize handshake.
//
// A message body is rendered inside `<iframe class="mail-frame" sandbox>`
// with no `allow-same-origin` (see mailosh/render/frame_document.py), so
// the parent cannot measure it: reading `contentDocument` across an opaque
// origin throws. The frame therefore measures itself and reports the number
// with `postMessage`, and this file is the only thing that listens.
//
// That makes this a message sink open to every window on the internet.
// `window.addEventListener("message")` fires for a `postMessage` from any
// opener, any popup, any other frame, and from a hostile message's own
// `allow-popups` window — none of which may be allowed to resize a frame.
// Three checks, all of them before anything is assigned:
//
//   1. `event.origin === "null"`. A sandboxed document with no
//      `allow-same-origin` has an *opaque* origin, which serialises as the
//      literal string "null". Any real page — including our own — has a
//      real origin, so this alone rejects every window that is not
//      sandboxed. It is not sufficient on its own (another sandboxed frame
//      is also "null"), which is why check 2 exists.
//   2. The sender must BE one of our frames: `frame.contentWindow ===
//      event.source`, identity on the window object itself. Nothing a
//      sender can put in the message body substitutes for this.
//   3. The height is clamped into [MIN, MAX]. An unclamped `height` is a
//      layout denial of service — 10^9 px is a scrollbar the reader can
//      never reach the end of — and a zero would hide the message.
//
// A message failing any one of them returns with no effect. There is
// exactly ONE `message` listener in the whole app, registered here: the
// checks are only as good as the narrowest listener, and a second one
// somewhere else would be a second, unreviewed door.
//
// Deliberately dependency-free, and safe to load on every page: with no
// `.mail-frame` in the document the listener simply never finds a target.

// Both bounds are mirrored from mailosh/render/frame_document.py's
// MIN_FRAME_HEIGHT / MAX_FRAME_HEIGHT. They are the reader-facing contract
// for what a message may do to the page, not a rendering detail.
const MIN_FRAME_HEIGHT = 200;
const MAX_FRAME_HEIGHT = 20000;

const HEIGHT_MESSAGE = "mailosh:frame-height";

// Every element in the app that frames a message body. `.mail-frame` is
// thread/frame.html's (the partial Task 7's banner swaps); `.msg-frame` is
// the conversation view's own inline spelling of the same iframe. Two
// classes is one too many and they should collapse to one — but a frame
// this selector does not match is a frame that never resizes, so the list
// covers both until they do. Widening it is not a security decision: the
// only thing that admits a message is the window-identity check below.
const FRAME_SELECTOR = "iframe.mail-frame, iframe.msg-frame";

window.addEventListener("message", (event) => {
  // 1. Opaque origin only. A named origin is, by construction, not one of
  //    our sandboxed frames.
  if (event.origin !== "null") return;

  const data = event.data;
  if (!data || typeof data !== "object" || data.type !== HEIGHT_MESSAGE) return;

  // 2. Window identity. `querySelectorAll` re-reads the DOM every time on
  //    purpose: frames are swapped in and out by htmx, and a cached list
  //    would go stale in exactly the direction that stops working.
  let frame = null;
  for (const candidate of document.querySelectorAll(FRAME_SELECTOR)) {
    if (candidate.contentWindow === event.source) {
      frame = candidate;
      break;
    }
  }
  if (frame === null) return;

  // 3. Clamp. `Number` (not `parseInt`) so "600px", {} and null become NaN
  //    rather than a plausible-looking number, and NaN is refused outright
  //    instead of clamping to MIN — a frame that cannot say how tall it is
  //    keeps whatever height it already had.
  const reported = Number(data.height);
  if (!Number.isFinite(reported)) return;
  const height = Math.min(MAX_FRAME_HEIGHT, Math.max(MIN_FRAME_HEIGHT, Math.round(reported)));
  frame.style.height = height + "px";
});
