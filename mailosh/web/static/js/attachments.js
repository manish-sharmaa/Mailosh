// Mailosh — the attachment preview dialog (spec §7).
//
// A chip is a real link to `/m/{id}/att/{blob}?inline=1` and stays one:
// this module intercepts the click and shows the file in
// `thread/preview.html`'s `<dialog>` instead of a new tab. Everything it
// does is an enhancement of markup that already works — no module, or a
// modifier click, and the browser opens the attachment exactly as it did
// before.
//
// The dialog is a `<dialog>` opened with `showModal()`, so Escape, the
// inert backdrop, the focus trap and the top layer are the platform's
// rather than three hundred lines of ours. The one thing the element does
// not do is put focus back where it came from — a modal that returns the
// reader to the top of the page has lost their place in a conversation —
// so the chip that opened it is remembered and focused on close.
//
// Nothing is cached between opens. The `src`/`href` of every surface is
// written on open and cleared on close, which is what stops a closed
// dialog holding a decoded image, or a frame still holding a document,
// from a message the reader has moved on from.

/** The chip that opened the dialog, so focus can go back to it. Held for
 *  exactly as long as the dialog is open. */
let opener = null;

const dialog = () => document.getElementById("att-preview");

const part = (root, role) => root.querySelector(`[data-role="${role}"]`);

/** Empty every surface. Called on close, and again on open before anything
 *  is written, so the image of one attachment can never be on screen under
 *  the name of the next. */
function clear(root) {
  const image = part(root, "attachment-image");
  const frame = part(root, "attachment-frame");
  image.removeAttribute("src");
  image.alt = "";
  image.hidden = true;
  // `removeAttribute`, not `src = ""`: an empty string resolves against the
  // page's own URL, which would point the frame at the conversation.
  frame.removeAttribute("src");
  frame.hidden = true;
}

function open(trigger) {
  const root = dialog();
  if (root === null) return false;

  const { previewKind, previewName, downloadUrl } = trigger.dataset;
  clear(root);
  part(root, "attachment-name").textContent = previewName ?? "";

  const download = part(root, "attachment-download");
  download.href = downloadUrl ?? trigger.href;
  download.setAttribute("download", previewName ?? "");
  part(root, "attachment-open").href = trigger.href;

  // `image` is the only kind that is a picture; `pdf` and `text` are
  // documents, and a document a stranger sent is shown the way every other
  // one in this app is — inside the sandboxed frame. An unknown kind opens
  // nothing: the chip's own link is then the honest answer, so the click is
  // handed back to the browser.
  if (previewKind === "image") {
    const image = part(root, "attachment-image");
    image.src = trigger.href;
    image.alt = previewName ?? "";
    image.hidden = false;
  } else if (previewKind === "pdf" || previewKind === "text") {
    const frame = part(root, "attachment-frame");
    frame.src = trigger.href;
    frame.hidden = false;
  } else {
    return false;
  }

  opener = trigger;
  root.showModal();
  return true;
}

document.body.addEventListener("click", (event) => {
  const root = dialog();

  // Inside the dialog first: its own close button, and a click on the
  // backdrop — which is a click whose target is the `<dialog>` element
  // itself, since every visible part of it is a child. The two links are
  // deliberately not handled at all; they are links, and following one is
  // what they are for.
  if (root !== null && root.open) {
    if (event.target?.closest?.('[data-role="attachment-close"]') || event.target === root) {
      root.close();
      return;
    }
  }

  const trigger = event.target?.closest?.('[data-role="attachment-preview"]');
  if (!trigger) return;
  // A modifier or middle click is the browser's: the chip is a real link to
  // a URL that works, and "open in a new tab" must keep meaning that.
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
    return;
  }
  if (open(trigger)) event.preventDefault();
});

// `close` does not bubble, so this listens in the capture phase — which
// reaches an ancestor for every event, bubbling or not. One listener on the
// body rather than one bound per open: `#main` is swapped by htmx, and a
// listener bound to a dialog that has been swapped away is a leak with a
// stale element on the end of it.
document.body.addEventListener(
  "close",
  (event) => {
    const root = event.target;
    if (root?.id !== "att-preview") return;
    clear(root);
    // Back to the chip, not to the top of the conversation. `preventScroll`
    // because the chip is where the reader already was — scrolling to it
    // would be the page moving under someone who has not asked it to.
    opener?.focus?.({ preventScroll: true });
    opener = null;
  },
  true,
);
