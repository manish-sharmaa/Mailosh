// Mailosh — the settings pages' client half (design spec §10).
//
// Deliberately small. Every control on `/settings/*` is a real form field
// inside a real `<form hx-post>`, so saving, the toast and the theme change
// are all handled by code that already exists: htmx makes the request,
// `actions.js`'s body-level `om:done` listener draws the toast, and
// `app.js`'s `ui` store applies `theme`/`density`/`shortcuts` from the
// `om:prefs` trigger to `<html>`. What is left for this file is the one
// appearance preference that store does not own — `font_size`, which
// `styles/settings.css` reads off `<html data-font-size>` — and a
// sign-out confirmation the platform can give us for free.
//
// Loaded on the shell (`layouts/app.html`) rather than by the settings page:
// that page arrives as a `#main` swap, and a module tag inside a morphed
// fragment is not reliably executed. It imports nothing — in particular
// not `keys.js`, whose importer set is pinned by a test — and binds one
// listener per concern on `document.body`, so nothing here is lost when
// `#main` is replaced.
//
// The CSP is `script-src 'self'` with no `'unsafe-eval'`: no `hx-on:`, no
// Alpine expression, no inline handler anywhere in `templates/settings/`.
// Controls are found by `data-role` and nothing here builds markup.

/** The legal `font_size` values — `mailosh/web/prefs.py`'s `FontSize`.
 *  Anything else is left alone rather than written to `<html>`, where an
 *  unknown token would match no stylesheet rule and silently mean "md". */
const FONT_SIZES = ["sm", "md", "lg"];

function applyFontSize(value) {
  if (!FONT_SIZES.includes(value)) return;
  document.documentElement.dataset.fontSize = value;
}

// `mailosh/web/settings.py` answers a saved appearance form with
// `HX-Trigger: {"om:prefs": {...}}`; htmx dispatches that on the form and
// it bubbles here. `app.js` applies the fields it knows and ignores this
// one, so there is exactly one writer per attribute.
document.body.addEventListener("om:prefs", (event) => {
  const changed = event.detail ?? null;
  if (changed === null || typeof changed !== "object") return;
  if ("font_size" in changed) applyFontSize(changed.font_size);
  syncQuickSettings(changed);
});

/** Re-check the quick-settings popover's *reading* radios from a save
 *  made on the full page. The popover is rendered once with the values
 *  the shell loaded with; `app.js` re-syncs the three `data-pref` controls
 *  it owns and leaves the rest, so a mark-as-read delay changed on
 *  `/settings/reading` would otherwise still show the old choice behind
 *  the gear until the next full load. Values are compared as strings —
 *  `mark_read_delay` arrives as a number, a boolean as `true`/`false` —
 *  which is exactly how the radios spell them. */
function syncQuickSettings(changed) {
  const panel = document.getElementById("quick-settings");
  if (panel === null) return;
  for (const name of Object.keys(changed)) {
    const value = String(changed[name]);
    for (const radio of panel.querySelectorAll('input[type="radio"][name="' + name + '"]')) {
      if (radio.dataset.pref !== undefined) continue;
      radio.checked = radio.value === value;
    }
  }
}

// A per-row "Sign out" on the Security page and "Sign out everywhere" both
// ask first. `hx-confirm` would do this, but it evaluates nothing and is
// fine under the CSP — what it cannot do is *skip* the question for the
// keyboard-only path a `<form>` submit takes, so the question is asked
// here, once, on the form's own `submit`, whichever way it was reached.
document.body.addEventListener("submit", (event) => {
  const form = event.target;
  const question = form?.dataset?.confirm;
  if (!question) return;
  if (!window.confirm(question)) event.preventDefault();
});
