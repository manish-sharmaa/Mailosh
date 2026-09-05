// Mailosh — the navigation drawer (design spec §4.3/§11, < 768px).
//
// Above 768px this file does nothing at all: the nav is a column of the
// page grid, always visible, and `styles/responsive.css` is the whole
// story. Below 768px the same `<nav>` becomes a drawer over the list, and
// a drawer is only worth having if it behaves like one — which is four
// things, none of which CSS can do:
//
//   * focus moves into it when it opens,
//   * `Esc` closes it and focus goes back to the control that opened it,
//   * Tab cannot walk out of it into the list behind, and
//   * the list behind cannot be clicked, read by a screen reader, or acted
//     on by a keyboard shortcut while it is up.
//
// ---------------------------------------------------------------------
// Why this is not a `<dialog>`
//
// `showModal()` would hand three of those four to the platform for free,
// and it is the wrong answer twice over:
//
// 1. **`static/js/keys.js` stops dispatching entirely while
//    `dialog[open]` matches** — that is how the ⌘K palette and the `?`
//    overlay take the keyboard. A nav drawer built on `<dialog>` would
//    silently kill every shortcut in the app for as long as it was open.
//    Not "some": `dispatch()` returns before it looks at anything.
// 2. **It is the same element at every width.** A `<dialog>` (or a
//    `role="dialog"`) at 767px and a `<nav>` at 768px is one element
//    claiming two different things about itself depending on how wide the
//    window is, and the 224px sidebar would lose its `navigation`
//    landmark to buy a drawer behaviour it never uses.
//
// So modality is assembled from the piece of the platform that is not
// tangled up with dialogs: `inert`. Marking the header, the offline
// banner, `#main` and the compose mount inert removes all four from hit
// testing, from the tab order and from the accessibility tree in one
// attribute — which is the whole of what `aria-modal` only *claims*. What
// `inert` does not give is the Tab *wrap* at the ends of the drawer and
// `Esc`; those two are below, and they are the only two.
//
// ---------------------------------------------------------------------
// Why the keydown listener swallows everything
//
// While the drawer is open this file takes the keyboard the same way
// `keys.js` hands it to an open dialog — by stopping the event before it
// reaches `keys.js`'s own `window` listener. Without that, `j` and `k`
// would walk the list cursor behind a panel covering it, `e` would archive
// a conversation the reader cannot see, and `u` would navigate out from
// under the drawer. A capture-phase listener on `document` is what beats a
// bubble-phase listener on `window`; nothing here calls `preventDefault()`
// except where it means to cancel a default, so Tab still tabs.

const NAV = "#app-nav";
const TOGGLE = '[data-role="nav-toggle"]';
const CLOSE = '[data-role="nav-close"]';
const SCRIM = '[data-role="nav-scrim"]';

/** Everything the drawer covers. Deliberately not `body > *`: the scrim is
 *  a sibling of the grid precisely so that this list cannot reach it — an
 *  inert scrim is a scrim you cannot click — and `#toasts` stays live so
 *  an undo that is counting down is still there to be taken. */
const BEHIND = [".app-header", ".app-banner", "#main", "#compose-dock"];

/** Focusable, in document order. The `offsetParent === null` filter is
 *  there for one case that really happens: the items inside a *closed*
 *  `<details>` ("More"). The UA hides them with `display: none`, so they
 *  are not tabbable and must not be either end of the wrap — otherwise
 *  Tab at the bottom of the drawer would jump to a link nobody can see.
 *  It is only ever called with the drawer open, so the panel's own
 *  `visibility: hidden` (which `offsetParent` does *not* report) never
 *  reaches it. */
const FOCUSABLE =
  'a[href], button:not([disabled]), summary, input:not([disabled]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Spec §4.3's drawer breakpoint, as a query rather than a number read at
 *  call time: `matches` is live, and `change` fires when a window is
 *  dragged across it. Kept in step with `styles/responsive.css` — the two
 *  are one breakpoint written twice, and `tests/unit/test_responsive.py`
 *  pins them together. */
const PHONE = window.matchMedia("(max-width: 767.98px)");

/** The control that opened the drawer, so `Esc` has somewhere to put focus
 *  back. Not always the toggle: ⌘K could open it later. `<body>` is never
 *  recorded — a drawer opened from a page with nothing focused would
 *  otherwise "restore" focus to the document, which is the same as losing
 *  it, and the toggle is the honest answer in that case. */
let opener = null;

function drawer() {
  return document.querySelector(NAV);
}

function isOpen() {
  return document.documentElement.dataset.drawer === "open";
}

function focusable() {
  const nav = drawer();
  if (nav === null) return [];
  return [...nav.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
}

function open() {
  // Above 768px there is no drawer — only a sidebar already on screen, and
  // "opening" it would mark the whole app inert around a panel that never
  // moved.
  if (isOpen() || !PHONE.matches) return;
  const nav = drawer();
  if (nav === null) return;

  const from = document.activeElement;
  opener = from instanceof HTMLElement && from !== document.body ? from : null;
  document.documentElement.dataset.drawer = "open";
  for (const selector of BEHIND) document.querySelector(selector)?.setAttribute("inert", "");
  document.querySelector(TOGGLE)?.setAttribute("aria-expanded", "true");

  // The close button first, which is what the drawer's own markup puts
  // first; the panel itself (`tabindex="-1"`) only if a swap has somehow
  // left it empty, so focus is never dropped on `<body>`.
  (focusable()[0] ?? nav).focus();
}

function close({ restore = true } = {}) {
  if (!isOpen()) return;
  delete document.documentElement.dataset.drawer;
  // Inertness comes off *before* focus is restored: focus cannot be moved
  // into an inert subtree, and the toggle lives in the header.
  for (const selector of BEHIND) document.querySelector(selector)?.removeAttribute("inert");
  const toggle = document.querySelector(TOGGLE);
  toggle?.setAttribute("aria-expanded", "false");
  if (restore) {
    const back = opener !== null && opener.isConnected ? opener : toggle;
    back?.focus();
  }
  opener = null;
}

/** Tab at either end of the drawer wraps to the other end. `inert` already
 *  stops Tab reaching the list — but not the browser's own chrome, and a
 *  drawer that drops the reader into the address bar and back is a drawer
 *  they have to tab through twice. */
function wrapTab(event) {
  const items = focusable();
  if (items.length === 0) return;
  const first = items[0];
  const last = items[items.length - 1];
  const nav = drawer();
  const active = document.activeElement;
  const outside = nav === null || !nav.contains(active);

  if (event.shiftKey && (outside || active === first)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (outside || active === last)) {
    event.preventDefault();
    first.focus();
  }
}

document.addEventListener(
  "keydown",
  (event) => {
    if (!isOpen()) return;
    // A modal on top of the drawer owns the keyboard: the label menu and
    // the ⌘K palette are real `<dialog>`s, and `Esc` closing one of them is
    // a *default action* on this very keydown. Calling `preventDefault()`
    // below would cancel it and leave the dialog stuck open.
    if (document.querySelector("dialog[open]") !== null) return;

    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key === "Tab") {
      wrapTab(event);
      return;
    }
    // Everything else: the drawer is modal, so nothing behind it acts.
    event.stopPropagation();
  },
  true,
);

// One delegated listener, because `shell/nav.html` renders `#nav` and a
// mailbox switch replaces it out of band — anything bound to a control
// inside it would be bound to an element that no longer exists. The close
// button and the scrim are outside `#nav` for that reason; the links are
// not, and cannot be.
document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (target === null) return;

  if (target.closest(TOGGLE) !== null) {
    if (isOpen()) close();
    else open();
    return;
  }
  if (target.closest(CLOSE) !== null || target.closest(SCRIM) !== null) {
    close();
    return;
  }
  if (!isOpen()) return;

  // A link or Compose: the drawer has done its job, and the screen behind
  // it is about to become the thing the reader asked for. The label menu
  // (⋮), the "new label" `+` and the `<details>` disclosure are all
  // buttons that open something *inside* the drawer, so they are not here.
  if (target.closest(`${NAV} a[href], ${NAV} [data-compose-open]`) !== null) close();
});

// Dragged past the breakpoint with the drawer open: the panel is a grid
// column again and the app behind it must not stay inert. No focus
// restore — the toggle it would go back to is `display: none` at this
// width, and focusing a hidden element quietly drops focus on `<body>`.
PHONE.addEventListener("change", (event) => {
  if (!event.matches) close({ restore: false });
});
