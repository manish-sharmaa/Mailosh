// Mailosh — the search pill's browser half (design spec §10).
//
// Almost all of search is server-rendered and needs nothing here: the pill
// is a real `<form>` with a real `action`, every chip is a link whose `q`
// the server computed while it drew the chip, both dropdown kinds are
// native `<details>`, and the focus popover is revealed by a CSS
// `:focus-within` rule. Turn JavaScript off and search still works, chips
// included.
//
// Four things genuinely cannot be done that way, and they are all this
// file does:
//
// 1. **Recent searches.** Mailosh stores no per-user search history on the
//    server — there is no table for it and adding one to record what
//    people look for is not a trade this app makes — so recents live in
//    `localStorage`, per browser, and never leave it.
// 2. **Keeping the pill in step.** A chip, the pager and the advanced
//    panel all change the query, and all of them swap `#main`. The top bar
//    is outside `#main`, so the input would still be showing the query
//    from before the click.
// 3. **Closing the advanced panel** after it has searched. Its `<details>`
//    lives in the top bar, so nothing replaces it, and a disclosure cannot
//    close itself.
// 4. **Letting go of the pill** once a search has run, so the popover
//    stops covering the results the reader just asked for.
//
// Everything is built with `createElement`/`textContent` — a recent search
// is the reader's own text coming back out of storage, and this app never
// turns a string into markup.
//
// The `/` key is not here: it belongs to the single registry in
// `static/js/keys.js`, like every other shortcut in this app.

const INPUT = '[data-role="search-input"]';
const RECENT_BOX = '[data-role="search-recent"]';
const RECENT_LIST = '[data-role="search-recent-list"]';
const RECENT_CLEAR = '[data-role="search-recent-clear"]';
const ADVANCED = "details.search-adv";

/** Where the results page writes the query it is showing. Read from the
 *  DOM rather than from `location.search` because the two disagree for
 *  exactly as long as it takes htmx to push the URL, and this runs on
 *  `htmx:afterSettle` — inside that window. */
const QUERY_MARK = "[data-search-query]";

const STORE_KEY = "mailosh.recent-searches";
const MAX_RECENT = 6;

/** Every `localStorage` access in a `try`. It throws outright in a Safari
 *  private window and in any browser set to block site data, and a search
 *  box that cannot remember is not a search box that should stop
 *  working. */
function readRecent() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    const parsed = raw === null ? [] : JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((q) => typeof q === "string" && q !== "") : [];
  } catch {
    return [];
  }
}

function writeRecent(queries) {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(queries));
  } catch {
    /* Not remembering is the failure mode, and it is an acceptable one. */
  }
}

/** Most recent first, no duplicates, capped. A repeat of a query already
 *  in the list moves it to the front rather than appearing twice — paging
 *  through one result set records the same query several times, and a
 *  history of the same search six times over is no history at all. */
function remember(query) {
  const trimmed = query.trim();
  if (trimmed === "") return;
  const kept = readRecent().filter((q) => q !== trimmed);
  kept.unshift(trimmed);
  writeRecent(kept.slice(0, MAX_RECENT));
  renderRecent();
}

function searchUrl(query) {
  return "/search?q=" + encodeURIComponent(query);
}

function renderRecent() {
  const box = document.querySelector(RECENT_BOX);
  const list = document.querySelector(RECENT_LIST);
  if (box === null || list === null) return;
  const queries = readRecent();
  box.hidden = queries.length === 0;
  list.replaceChildren();
  for (const query of queries) {
    const link = document.createElement("a");
    link.className = "search-pop-item";
    link.href = searchUrl(query);
    // The same four attributes every other in-app link carries, so a
    // recent search is the same swap a chip is rather than a full page
    // load. `htmx.process` is what makes htmx read them on an element it
    // did not see at load time; without it the `href` alone still works,
    // which is the fallback if htmx never arrived.
    link.setAttribute("hx-get", link.href);
    link.setAttribute("hx-target", "#main");
    link.setAttribute("hx-swap", "morph:innerHTML show:none");
    link.setAttribute("hx-push-url", "true");
    link.textContent = query;
    list.append(link);
  }
  window.htmx?.process?.(list);
}

/** Put the query the page is showing back into the pill — unless the
 *  reader is typing in it, in which case what they are typing wins. */
function syncInput(query) {
  const input = document.querySelector(INPUT);
  if (input === null || document.activeElement === input) return;
  input.value = query;
}

/** After every settled swap: record what was searched and re-sync the
 *  pill. `[data-search-query]` exists only on the results page, so every
 *  other swap in the app falls straight through. */
function afterSwap() {
  const mark = document.querySelector(QUERY_MARK);
  if (mark === null) return;
  const query = mark.getAttribute("data-search-query") ?? "";
  syncInput(query);
  remember(query);
}

document.body.addEventListener("htmx:afterSettle", afterSwap);

// One delegated listener on `body`, not on the panel: `#main` is replaced
// on every navigation, and a listener bound to anything inside it is a
// listener that dies on the first swap.
document.body.addEventListener("click", (event) => {
  if (event.target?.closest?.(RECENT_CLEAR) == null) return;
  writeRecent([]);
  renderRecent();
});

// A search from the advanced panel closes it, and a search from the pill
// lets go of the pill — in both cases so that what the reader asked for is
// not covered by what they asked with. `htmx:afterRequest` rather than
// `afterSettle`: this is about the surface the request came *from*, which
// `detail.elt` names, and that element may well have been swapped away by
// the time the new content settles.
//
// **The test is the panel's own form, not the `<details>` around it**, and
// the difference is not pedantry: the panel is *lazily loaded* by an
// `hx-get` on the `<details>`'s own toggle, so a request "from inside the
// advanced panel" is, the first time, the request that fetches the panel.
// Closing on that closed the disclosure the instant it was opened, every
// time — the button lit up and nothing appeared. Only a submit of
// `.search-adv-form` has actually searched and is therefore finished with
// the panel.
document.body.addEventListener("htmx:afterRequest", (event) => {
  const from = event.detail?.elt ?? null;
  if (from === null) return;
  const form = from.closest?.("form.search-adv-form") ?? null;
  if (form !== null) {
    const panel = form.closest(ADVANCED);
    if (panel !== null) panel.open = false;
    return;
  }
  if (from.matches?.("form.search-pill")) document.querySelector(INPUT)?.blur();
});

renderRecent();
afterSwap();
