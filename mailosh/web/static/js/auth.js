// Mailosh — sign-in behaviour (auth/login.html).
//
// Three small things, each of which exists because it removes a real
// moment of doubt rather than because it moves:
//
//   1. A reveal toggle on the password field. Typing a long password
//      blind, on a page that answers wrong-password and unknown-account
//      identically (design spec §9, deliberately), is the single most
//      likely way to get stuck here.
//   2. A Caps Lock warning. Same reason, and the browser will not tell
//      you.
//   3. A submit state. Signing in is not instant — it verifies against
//      Stalwart over HTTP and then mints or reuses an API key — so
//      without feedback the button looks ignored and gets pressed twice.
//
// Progressive enhancement throughout: the toggle button ships `hidden`
// in the markup and is revealed here, so with JS off there is no dead
// control, and the form still posts and still works.
//
// No inline handlers anywhere: the app serves `script-src 'self'` with no
// `unsafe-eval`, so behaviour lives in this module, not in attributes.

const form = document.querySelector("[data-auth-form]");
const password = document.getElementById("password");

/** Swap the password field between hidden and visible, keeping the
 *  caret where the reader left it — `type` changes reset the selection
 *  in some browsers, which is jarring mid-word. */
function armReveal() {
  const toggle = document.querySelector("[data-auth-reveal]");
  if (toggle === null || password === null) return;

  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    const shown = password.type === "text";
    const { selectionStart, selectionEnd } = password;
    password.type = shown ? "password" : "text";
    toggle.setAttribute("aria-pressed", String(!shown));
    toggle.setAttribute("aria-label", shown ? "Show password" : "Hide password");
    try {
      password.setSelectionRange(selectionStart, selectionEnd);
    } catch {
      // Firefox throws on setSelectionRange for some input types; the
      // caret lands at the end, which is survivable.
    }
    password.focus();
  });
}

/** Show a hint while Caps Lock is on. `getModifierState` reports the
 *  lock's state on every key event, so this catches the case where it
 *  was already on before the field was ever focused — checking only on
 *  keypress of a letter would not. */
function armCapsLock() {
  const hint = document.querySelector("[data-auth-caps]");
  if (hint === null || password === null) return;

  const update = (event) => {
    if (typeof event.getModifierState !== "function") return;
    hint.hidden = !event.getModifierState("CapsLock");
  };

  password.addEventListener("keydown", update);
  password.addEventListener("keyup", update);
  password.addEventListener("blur", () => {
    hint.hidden = true;
  });
}

/** Mark the form busy on submit, and refuse a second one.
 *
 *  The button is deliberately NOT disabled: a disabled control is
 *  removed from the tab order the instant it is pressed, which throws a
 *  keyboard user's focus to the top of the document. `aria-busy` plus a
 *  guard flag says the same thing without moving anyone's focus. */
function armSubmit() {
  if (form === null) return;
  let submitting = false;

  form.addEventListener("submit", (event) => {
    if (submitting) {
      event.preventDefault();
      return;
    }
    submitting = true;
    form.classList.add("is-busy");
    form.setAttribute("aria-busy", "true");
  });
}

armReveal();
armCapsLock();
armSubmit();
