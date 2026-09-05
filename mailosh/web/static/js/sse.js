// Mailosh — live-update bridge: EventSource -> one coalesced htmx body event.
//
// Design spec §6.5. The server side is mailosh/sse.py (one SseHub +
// one upstream Stalwart listener per user) behind GET /events, which
// streams `event: mail` frames whose data is {"types": [...]} and whose
// `id:` is the JMAP state string. All this file does is turn those into
// `mail:changed` on <body>, which templates listen for with
// hx-trigger="mail:changed from:body" and answer by re-fetching
// themselves. Nothing here renders, parses mail, or knows what a row is.
//
// This is Mailosh's own client, not the vendored htmx-ext-sse extension
// (removed in Task 2 — see NOTICE): sse-swap splices a server-rendered
// fragment straight into the DOM at whatever moment the event arrives,
// which is the wrong shape for a list that must re-query at position 0,
// morph, and update counts/title as one atomic swap. ~90 lines of our own
// beats an extension we would have to fight.
//
// Deliberately dependency-free (no imports): htmx and Alpine are globals
// loaded by layouts/app.html before this module, and both accesses below
// are optional-chained so a page that has not finished booting Alpine's
// stores — or a later task that has not registered the `ui` store yet —
// degrades to "no offline flag" rather than throwing.

const ENDPOINT = "/events";

// Coalesce window: a single delivery can push several state changes in a
// row (Email *and* Mailbox, one per account). Firing the trailing edge
// 400 ms after the first of a burst turns that into one refetch.
const COALESCE_MS = 400;

// How long a dropped stream may stay dropped before the UI admits it.
// EventSource reconnects itself within ~1-3 s on a transient blip, so a
// shorter grace period would flash "offline" on every wifi hiccup. Must
// clear Chrome's own default EventSource reconnect delay (~3000 ms) with
// margin: the server now ends a stream deliberately as part of normal
// recovery (mailosh/sse.py's HubRegistry), so every legitimate reconnect
// races this timer against that browser-side delay too. Do not tidy this
// back down toward 3000 — that turns the race into a coin flip and the
// offline banner flashes on every ordinary recovery.
const OFFLINE_AFTER_MS = 6000;

// Fallback refresh cadence while disconnected (spec §6.5: "120 s polling
// while disconnected").
const POLL_MS = 120000;

// A catch-up/poll refresh cannot know what changed while we were not
// listening, so it claims both mail types rather than an empty list.
const ALL_TYPES = ["Email", "Mailbox"];

let source = null;
let coalesceTimer = null;
let offlineTimer = null;
let pollTimer = null;
let pending = null;

function setOffline(value) {
  const store = window.Alpine?.store?.("ui");
  if (store) store.offline = value;
}

function fire(detail) {
  window.htmx?.trigger(document.body, "mail:changed", detail);
}

function flush() {
  coalesceTimer = null;
  const detail = pending;
  pending = null;
  if (detail) fire(detail);
}

function onMail(event) {
  // A frame we cannot read — malformed JSON, or no `types` key — still
  // means something changed; we just can't tell what. That is the same
  // situation a catch-up refresh is in, so it claims both types rather
  // than an empty list, which a consumer switching on `detail.types`
  // would (correctly) read as "nothing to do" and ignore.
  let types;
  try {
    types = JSON.parse(event.data)?.types ?? ALL_TYPES;
  } catch {
    types = ALL_TYPES;
  }
  pending ??= { types: [], id: null, catchup: false };
  for (const type of types) {
    if (!pending.types.includes(type)) pending.types.push(type);
  }
  // EventSource tracks the last `id:` it saw and replays it as
  // Last-Event-ID on reconnect; carrying it in the detail lets a consumer
  // pass the JMAP state along too.
  pending.id = event.lastEventId || pending.id;
  coalesceTimer ??= setTimeout(flush, COALESCE_MS);
}

function onOpen() {
  clearTimeout(offlineTimer);
  offlineTimer = null;
  clearInterval(pollTimer);
  pollTimer = null;
  setOffline(false);
  // Catch-up: anything delivered while this connection was down was
  // published to a hub nobody was subscribed to, so it is simply gone.
  // One refetch on every open (including the first, right after page
  // load) is the cheap, always-correct answer — it morphs, so a page that
  // was already current does not visibly change.
  fire({ types: [...ALL_TYPES], id: source?.lastEventId || null, catchup: true });
}

// A browser only retries a stream it lost mid-flight. One the server
// refused (a non-2xx response — /events answers 401 once the session has
// expired) is closed for good and nothing would ever reopen it; the same
// goes for one the browser itself tore down while the page sat in the
// bfcache. Both leave a dead EventSource that never fires `error` again,
// so re-dialling has to be driven from outside it.
function redialIfClosed() {
  if (source === null || source.readyState === EventSource.CLOSED) connect();
}

function onOffline() {
  offlineTimer = null;
  setOffline(true);
  pollTimer ??= setInterval(() => {
    fire({ types: [...ALL_TYPES], id: null, catchup: true });
    // Re-dialling on the poll tick means a session that comes back (or a
    // server that comes back up) recovers without a page reload.
    redialIfClosed();
  }, POLL_MS);
}

function onError() {
  // Don't react yet: EventSource is probably already reconnecting, and
  // `open` cancels this timer if it succeeds in time. Never stack a
  // second timer, and never restart the clock once polling has begun.
  if (offlineTimer === null && pollTimer === null) {
    offlineTimer = setTimeout(onOffline, OFFLINE_AFTER_MS);
  }
}

function connect() {
  source?.close();
  source = new EventSource(ENDPOINT);
  source.addEventListener("open", onOpen);
  source.addEventListener("error", onError);
  source.addEventListener("mail", onMail);
}

// Coming back to a page that was never unloaded. Restoring from the
// bfcache (back/forward, or a mobile browser reviving a backgrounded tab)
// can hand back an EventSource the browser already closed on its way out,
// and a closed one fires nothing — no `error`, so no offline timer, no
// poll, no reconnect: the page just sits there silent with `offline`
// false. `pageshow` covers the restore; `visibilitychange` covers a tab
// that was throttled rather than cached. Both are cheap no-ops whenever
// the stream is in fact still open.
window.addEventListener("pageshow", redialIfClosed);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") redialIfClosed();
});

connect();
