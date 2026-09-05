"""The half of Task 9 that runs in the browser, pinned from Python.

There is no Node toolchain in this project (Global Constraints) and so no
JS test runner, which leaves three kinds of breakage that no other test in
this suite would notice:

1. **The route/JS split.** `static/js/actions.js` posts to paths that live
   in `mailosh/web/actions.py`. Renaming one and not the other is a
   silent 404 in the browser and a green suite here.
2. **The markup/JS split.** The templates carry `data-action` hooks; one
   delegated listener in `actions.js` interprets them. A hook nobody
   handles is a button that looks alive and does nothing.
3. **The CSP.** `script-src 'self'` with no `'unsafe-eval'` fails at
   *runtime*, silently, the first time a directive is evaluated — never at
   build time. The three htmx attributes that compile their value with
   `new Function` are checked for here so that a template adding one fails
   in CI rather than in someone's browser.
4. **The response/JS split.** `mailosh/web/app.py` answers a failed HTMX
   request with `200` + `HX-Reswap: none` + an `om:error` trigger, so
   `response.ok` is *true* on a failure and a swap changes nothing on
   screen. Both halves shipped correct and never met: nothing read the
   event, so a failed archive kept its optimistic paint and — worse — ran
   `leaveConversation()`, bouncing the reader out of a conversation whose
   mail had not moved, exactly as if it had worked.
5. **Alpine's silent failures.** A directive on an element with no
   `x-data`/`x-init` root is never evaluated; `x-show` cannot beat the
   `hidden` attribute; and Alpine loading before `app.js` leaves every
   directive subscribed to nothing. None of the three logs anything.

Plus the stylesheet: the app serves the compiled `static/app.css`, so a
rule added to `styles/input.css` and never built changes nothing at all.

There is still no JS runtime here, so the tests for (4) read `actions.js`'s
*control flow* — which guard a check sits in, what a path does before it
returns, how many call sites something has — via the brace-counting helpers
below, rather than asserting that some string appears somewhere in the file.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from mailosh.web.actions import router as actions_router

ROOT = pathlib.Path(__file__).resolve().parents[2]
ACTIONS_JS = ROOT / "mailosh/web/static/js/actions.js"
APP_JS = ROOT / "mailosh/web/static/js/app.js"
SSE_JS = ROOT / "mailosh/web/static/js/sse.js"
ROW_HTML = ROOT / "mailosh/web/templates/list/row.html"
ACTIONS_PY = ROOT / "mailosh/web/actions.py"
APP_PY = ROOT / "mailosh/web/app.py"
INPUT_CSS = ROOT / "styles/input.css"
APP_CSS = ROOT / "mailosh/web/static/app.css"

TEMPLATES_DIR = ROOT / "mailosh/web/templates"
THREAD_HTML = TEMPLATES_DIR / "thread/page.html"
APP_LAYOUT = TEMPLATES_DIR / "layouts/app.html"
FRAGMENT_LAYOUT = TEMPLATES_DIR / "layouts/fragment.html"
OFFLINE_HTML = TEMPLATES_DIR / "fragments/offline.html"
TOPBAR_HTML = TEMPLATES_DIR / "shell/topbar.html"

TEMPLATES = sorted(TEMPLATES_DIR.rglob("*.html"))


def _without_comments(markup: str) -> str:
    """`markup` minus its `{# … #}` blocks — these templates explain
    themselves at length, and a rule about what the markup does must not be
    satisfied (or broken) by prose describing it."""
    return re.sub(r"\{#.*?#\}", "", markup, flags=re.S)


def _actions_js() -> str:
    return ACTIONS_JS.read_text()


def _code(source: str) -> str:
    """`source` minus its comments — same reason `_without_comments` strips
    `{# … #}` out of a template: this file explains itself at length, and a
    rule about what the code *does* must not be satisfied (or broken) by
    prose describing it. Whole-line `//` and `/* … */` blocks only, so a
    `//` inside a string literal survives."""
    without = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in without.splitlines() if not line.lstrip().startswith("//"))


# ---------------------------------------------------------------------------
# Reading control flow out of the JS
#
# There is no JS runtime here to *run* `run()` in, so the next best thing is
# to read its structure rather than grep its text: which guard a statement
# sits inside, whether a path returns before reaching another, how many call
# sites something has. All three helpers below count braces, because a regex
# for "the body of this function" stops at the first `}` it meets — which in
# `run()` is the end of its first `if`, three guards above the interesting
# part.
# ---------------------------------------------------------------------------


def _function_body(source: str, name: str, required: bool = True) -> str | None:
    """The whole body of `function name(...)`/`async function name(...)`."""
    opening = re.search(rf"\bfunction {re.escape(name)}\([^)]*\)\s*\{{", source)
    if opening is None:
        assert not required, f"actions.js no longer declares {name}()"
        return None
    return _block_at(source, opening.start())[2]


def _block_at(text: str, index: int) -> tuple[int, int, str]:
    """The `{ … }` block opening at or after `index`, as
    `(open_index, close_index, inner_text)`."""
    start = text.index("{", index)
    depth = 0
    for at in range(start, len(text)):
        if text[at] == "{":
            depth += 1
        elif text[at] == "}":
            depth -= 1
            if depth == 0:
                return start, at, text[start + 1 : at]
    raise AssertionError(f"unbalanced braces from offset {index}")


def _nesting_at(body: str, index: int) -> int:
    """How many blocks deep `index` is inside `body`. `0` means "runs on
    every pass through this function", which is the whole question for a
    guard that has to see responses another guard would have skipped."""
    return body.count("{", 0, index) - body.count("}", 0, index)


def _effects(source: str, block: str) -> str:
    """`block`, plus the body of every function reachable from it that this
    same file declares. A path that reverts and toasts through two named
    helpers is the same path as one that does both inline, and a test about
    *what happens* must not be a test about how it was factored."""
    seen: set[str] = set()
    collected = [block]
    frontier = [block]
    while frontier:
        for name in sorted(set(re.findall(r"\b([A-Za-z_]\w*)\(", frontier.pop()))):
            if name in seen:
                continue
            seen.add(name)
            body = _function_body(source, name, required=False)
            if body is None:
                continue
            collected.append(body)
            frontier.append(body)
    return "\n".join(collected)


def _kinds() -> set[str]:
    """The action kinds `actions.js` knows how to run, read off its own
    `ROUTES` table rather than restated here — a table that drifts from its
    own consumers is exactly what these tests exist to catch."""
    block = re.search(r"const ROUTES = \{(.*?)\n\};", _actions_js(), re.S)
    assert block is not None, "actions.js no longer declares a ROUTES table"
    return set(re.findall(r"^\s+(\w+):", block.group(1), re.M))


def test_actions_js_posts_to_the_action_routes_and_to_nothing_else():
    served = {route.path for route in actions_router.routes}
    posted = set(re.findall(r'"(/a/[a-z]+)"', _actions_js()))

    assert posted == served


def test_every_data_action_hook_in_the_templates_has_a_handler():
    # `select` is the one hook that runs no request: it toggles the
    # selection the other six act on, so it is handled beside them rather
    # than by a listener of its own.
    handled = _kinds() | {"select"}
    hooks = {
        hook
        for template in TEMPLATES
        for hook in re.findall(r'data-action="([^"]+)"', _without_comments(template.read_text()))
    }

    assert hooks, "no template carries a data-action hook any more"
    assert hooks <= handled


def test_the_two_way_hooks_say_which_way_they_mean_in_the_dom():
    row = _without_comments(ROW_HTML.read_text())
    # The star is one control for two routes, and `aria-pressed` is what
    # turns a click into `on=0`. Without it the client would have to guess
    # from a class name, or the template would need two buttons.
    assert re.search(r'aria-pressed="\{\{[^}]*starred[^}]*\}\}"[^>]*data-action="star"', row, re.S)
    # Read/unread is two controls and `.row.is-unread` picks one, which is
    # what makes the optimistic toggle a class flip rather than a rebuild.
    assert 'class="btn-icon size-7 act-read" data-action="read"' in row
    assert 'class="btn-icon size-7 act-unread" data-action="unread"' in row
    # ...and `row.unread` is read exactly once, into that class. Branching
    # on it a second time — to render one of the two buttons — is what
    # would put the read state in two places and turn the optimistic
    # toggle back into a DOM rebuild.
    assert row.count("row.unread") == 1


#: The one action surface that acts on the *selection* instead of on
#: something it sits inside. It lives outside every row, so
#: `actions.js`'s `targets()` falls through to `defaultTargets()` and reads
#: the ids off the selected rows — which is exactly why it must not carry
#: ids of its own: copied ids would go stale the moment the selection
#: changed, and `defaultTargets()` would never be consulted.
SELECTION_BARS = {"toolbar_selected.html"}


def test_every_template_with_an_action_bar_carries_the_ids_it_would_post():
    """A `data-action` control needs something to act on, and the routes
    take *message* ids. A row and the open conversation say so the same
    way — `data-email-ids` — which is what lets one delegated listener read
    both. `thread/page.html` shipped an action bar carrying only
    `data-thread-id`, which no route can take, so its Archive, Delete and
    Mark-as-unread buttons resolved to nothing and silently did nothing.
    """
    with_a_bar = []
    on_the_selection = []
    for template in TEMPLATES:
        markup = _without_comments(template.read_text())
        # `select` is the one hook that posts nothing: it toggles the
        # selection the other six act on.
        if not set(re.findall(r'data-action="([^"]+)"', markup)) - {"select"}:
            continue
        if template.name in SELECTION_BARS:
            on_the_selection.append(template.name)
            assert "data-email-ids" not in markup, template.name
            assert "data-id=" not in markup, template.name
            continue
        with_a_bar.append(template.name)
        assert "data-email-ids" in markup, template.name

    # Every action surface is covered by one branch or the other, so
    # neither can pass by finding nothing to check. `message.html` is the
    # conversation's per-message card: its star is a `data-action` control
    # like the row's, and it names its own message in `data-email-ids`.
    assert sorted(with_a_bar) == ["message.html", "page.html", "row.html"]
    assert sorted(on_the_selection) == sorted(SELECTION_BARS)


def test_the_dispatcher_reaches_the_open_conversation_only_off_the_list():
    """`defaultTargets` falls back to `[data-email-ids]` when there is no
    selection and no focused row. Unguarded, that selector matches the
    *first row* of the list page, so an action with nothing selected would
    quietly hit whichever conversation happened to be at the top."""
    source = _actions_js()
    block = re.search(r"function openConversation\(\) \{(.*?)\n\}", source, re.S)
    assert block is not None, "actions.js no longer resolves the open conversation"
    assert "LIST" in block.group(1)
    assert '"[data-email-ids]"' in block.group(1)
    # ...and that guarded lookup is the only one in the file.
    assert source.count('querySelector("[data-email-ids]")') == 1


def test_a_removed_conversation_leaves_the_thread_view_it_was_read_in():
    """Archive and Delete have no row to collapse on the thread page, so
    the page has to leave — otherwise it sits there showing mail that is no
    longer in this mailbox, with an Undo whose refresh nothing listens for.
    It leaves through `thread/page.html`'s own back control, so there is one
    idea of where back goes rather than two."""
    source = _actions_js()
    assert re.search(r"REMOVES_ROWS\.has\(kind\) && openConversation\(\) !== null", source)
    hook = re.search(r"""querySelector\('\[data-role="back"\]'\)""", source)
    assert hook is not None, "actions.js no longer clicks the thread's back control"
    assert 'data-role="back"' in _without_comments(THREAD_HTML.read_text())


# ---------------------------------------------------------------------------
# The failure contract: `om:error` on a `200`
#
# `mailosh/web/app.py` answers a `JmapError`/`TransportError` on an HX
# request with `200` + `HX-Reswap: none` + an `om:error` trigger, because
# htmx reads a 4xx/5xx as a load failure and gives a listener nothing to
# work with. The cost of that — and the regression these four tests pin —
# is that `response.ok` is **true** on a failed request, so a client that
# checks only the status reads a dead mail server as a success.
# ---------------------------------------------------------------------------

#: How `run()`/`undo()` ask a response whether it failed.
ERROR_CHECK = 'trigger(response, "om:error")'


def test_a_failed_action_reverts_the_optimistic_paint_and_stays_in_the_conversation():
    """Two things go wrong when a failure is read as a success, and the
    second is the worse one.

    The row was already collapsed optimistically, so without a revert it
    stays gone while the mail never moved. And `leaveConversation()` runs
    on the success path: an archive taken from an open conversation bounces
    the reader back to the mailbox *exactly as if it had worked*. Silently
    pretending mail moved is the worst failure shape a mail client has.
    """
    source = _actions_js()
    body = _function_body(source, "run")
    _, closes, guard = _block_at(body, body.index(ERROR_CHECK))
    effects = _effects(source, guard)

    # The revert: `applyOptimistic` has no "unapply" (only the server knows
    # what a partly applied `Email/set` left behind), so re-fetching the
    # list is how a collapsed row comes back.
    assert "refreshList()" in effects
    # ...and the reader is told, rather than left to notice.
    assert re.search(r"\btoast\(", effects)
    # Control stops here. Everything below reads the response as a success.
    assert guard.strip().endswith("return;")

    # The reader stays put: the one call site sits past that return.
    assert source.count("leaveConversation();") == 1
    assert body.index("leaveConversation();") > closes
    # ...as does the success path it belongs to.
    assert body.index('trigger(response, "om:done")') > closes


def test_a_failure_is_recognised_on_a_200_not_only_on_a_bad_status():
    """The check has to run on *every* response. Nested inside a
    `!response.ok` branch it would never execute at all, which is precisely
    what shipped: `HX-Reswap: none` meant the page did not even flicker, so
    a failed archive looked exactly like a successful one.
    """
    body = _function_body(_actions_js(), "run")

    assert _nesting_at(body, body.index(ERROR_CHECK)) == 0


def test_undo_reads_the_same_failure_shape_as_the_action_it_reverses():
    """`/a/undo` fails the same way for the same reason, and reading only
    its status would hand `applyDone` a response with no `om:done` in it —
    which resolves to nothing and says nothing. A failed undo has to be as
    loud as a failed archive, and it re-fetches nothing because nothing
    moved.
    """
    source = _actions_js()
    body = _function_body(source, "undo")
    _, closes, guard = _block_at(body, body.index(ERROR_CHECK))

    assert _nesting_at(body, body.index(ERROR_CHECK)) == 0
    assert re.search(r"\btoast\(", _effects(source, guard))
    assert guard.strip().endswith("return;")
    assert body.index('trigger(response, "om:done")') > closes


def test_an_htmx_request_that_fails_is_not_silent_either():
    """The other half of the same contract. A list refetch, a nav swap or a
    thread open is an *htmx* request, not one of this file's `fetch`es, so
    its `om:error` arrives as a bubbling DOM event on the requesting
    element instead of as a response object. With nobody listening — and
    with `HX-Reswap: none` swapping nothing — clicking Inbox against a dead
    mail server did nothing at all, silently. One listener, on the element
    every other delegated listener in this app is bound to.
    """
    source = _actions_js()
    assert source.count('addEventListener("om:error"') == 1

    at = source.index('addEventListener("om:error"')
    assert "document.body" in source[at - 40 : at]
    listener = _block_at(source, at)[2]
    assert re.search(r"\btoast\(", _effects(source, listener))


def test_every_error_event_the_server_sends_reaches_a_consumer():
    """The route/JS split, for the error surface: `app.py` names its events
    in one helper, and every name it sends has to be one this file reads.
    An event nobody consumes is a failure nobody sees — which is exactly
    how the two individually-correct halves of this contract shipped
    without ever meeting.
    """
    sent = set(re.findall(r'_hx_trigger\(\s*\n?\s*"([^"]+)"', APP_PY.read_text()))
    assert sent == {"om:error"}

    source = _actions_js()
    consumed = set(re.findall(r'trigger\(response, "([^"]+)"\)', source))
    consumed |= set(re.findall(r'addEventListener\("([^"]+)"', source))

    assert sent <= consumed


def test_the_optimistic_paint_hands_back_its_own_inverse():
    """Re-fetching the list is not a revert when the mail server is the
    thing that is down — the re-fetch fails too, and reverts nothing. A row
    left carrying `.is-leaving` is `height: 0; opacity: 0`: still in the
    document, invisible, indistinguishable from one that really was
    archived. So every branch of the paint returns the function that undoes
    it, and the failure path runs that before asking the server for the
    truth it may never get.
    """
    source = _actions_js()
    paint = _function_body(source, "applyOptimistic")

    # Every exit hands back an inverse; not one of them returns bare.
    assert re.search(r"\breturn;", paint) is None
    assert len(re.findall(r"\breturn\b", paint)) == len(re.findall(r"\breturn \(\) =>", paint))
    # ...and the collapse's inverse actually un-paints, rather than only
    # abandoning the pending removal.
    assert re.search(r'classList\.remove\("is-leaving"\)', source)

    # Held across the request, and run on every path that failed.
    body = _function_body(source, "run")
    assert re.search(r"const revert = confirmed \? null : applyOptimistic\(", body)
    for guard in ("response.status === 409", ERROR_CHECK):
        assert "revert" in _block_at(body, body.index(guard))[2], guard
    assert "reportFailure(kind, revert)" in body


def test_the_optimistic_paint_never_touches_anything_but_what_it_can_render():
    """Two of its three branches are row rendering (`.is-leaving`,
    `.is-unread`); handed the thread body they would collapse the whole
    conversation out of the page before the server had answered. The star is
    the one control a *message card* renders too, so it is painted through a
    selector of its own — and the point of this test is that the raw
    argument is never reached past either of them.
    """
    source = _actions_js()
    block = re.search(r"function applyOptimistic\(kind, elements\) \{(.*?)\n\}\n", source, re.S)
    assert block is not None, "actions.js no longer declares applyOptimistic"
    body = block.group(1)

    # Every use of the argument is a `.filter(...)` against a named selector
    # constant — nothing iterates, collapses or paints `elements` itself.
    uses = re.findall(r"\belements\b(\.\w+)?", body)
    filtered = re.findall(r"elements\.filter\(\(el\) => el\.matches\((\w+)\)\)", body)
    assert uses == [".filter"] * len(filtered)
    assert sorted(filtered) == ["ROW_SELECTOR", "STAR_SELECTOR"]

    # ...and each of those constants is anchored somewhere real, rather than
    # being a name that quietly widened to match the whole document.
    for name, expected in (("ROW_SELECTOR", "#list [data-id]"), ("STAR_SELECTOR", "article.msg")):
        declared = re.search(rf'const {name} = "([^"]+)"', source)
        assert declared is not None, name
        assert expected in declared.group(1), name


def test_the_range_readout_is_declared_once_and_rendered_by_both_paths():
    """One span, two renderers: `list/toolbar.html` in place and
    `list/rows.html` out of band. The readout describes `#list` but lives
    outside it, and `#list` is all a `mail:changed` swap replaces — so a
    second, hand-copied span would drift the moment either changed, and the
    id is what htmx matches the out-of-band swap on."""
    declaring = [
        template.name
        for template in TEMPLATES
        if 'id="list-range"' in _without_comments(template.read_text())
    ]
    assert declaring == ["range.html"]

    for name in ("list/toolbar.html", "list/rows.html"):
        markup = _without_comments((TEMPLATES_DIR / name).read_text())
        assert 'include "list/range.html"' in markup, name

    rows = _without_comments((TEMPLATES_DIR / "list/rows.html").read_text())
    assert re.search(r'\{% with oob = true %\}\{% include "list/range\.html" %\}', rows)


def test_only_too_many_is_ever_explained_to_the_reader():
    """`undo_unavailable`'s two codes, and the rule for surfacing them.

    `"no_change"` arrives *alongside* an ordinary success toast — starring
    an already-starred message answers `toast: "Starred"` with
    `undo_unavailable: "no_change"` — so explaining it would tell the
    reader that something they did successfully had failed. Only
    `"too_many"` describes something they can see is missing.
    """
    emitted = set(re.findall(r'\["undo_unavailable"\] = "(\w+)"', ACTIONS_PY.read_text()))
    assert emitted == {"no_change", "too_many"}

    note = re.search(r"const UNDO_UNAVAILABLE_NOTE = new Map\(\[(.*?)\]\);", _actions_js(), re.S)
    assert note is not None, "actions.js no longer declares the note map"
    explained = re.findall(r'\["(\w+)",', note.group(1))

    assert explained == ["too_many"]


def test_undo_unavailable_is_read_by_presence_never_compared_with_null():
    # It is absent, not null, whenever `undo` is present — the asymmetry
    # `test_shape_undo_kept_leaves_out_undo_unavailable_entirely` pins on
    # the server side. An equality test against null would never fire.
    source = _actions_js()
    assert "undo_unavailable ===" not in source
    assert "undo_unavailable !==" not in source


def test_the_undo_codes_are_never_echoed_into_the_ui():
    # They are stable identifiers, not copy. The wording lives in the note
    # map above precisely so a copy edit or an i18n pass is a client
    # change; a code reaching `textContent` would undo that.
    source = _actions_js()
    assert not re.search(r"textContent = [^\n;]*undo_unavailable", source)


# ---------------------------------------------------------------------------
# The shell's two client-driven controls: the offline banner and Help
# ---------------------------------------------------------------------------


def test_the_offline_banner_is_declared_once_and_bound_to_the_flag_sse_sets():
    """Spec §5.4's connection-lost banner. `sse.js` has set
    `$store.ui.offline` since live updates landed and its 120 s fallback
    polling has worked all along — but no template rendered the flag, so
    the banner three places described did not exist.

    Two Alpine traps, both silent in the console as well as in a test that
    only grepped for the id:

    1. Alpine initializes only trees rooted at `[x-data]`/`[x-init]`. A
       standalone directive on an element with neither is never evaluated.
    2. `x-show` works by clearing inline `display`, which loses outright to
       `[hidden]`'s `display: none !important` in Tailwind's preflight — an
       `x-show`n element still carrying `hidden` never appears. Binding the
       attribute is the only way to have both the served-HTML `hidden` and
       a reactive one.
    """
    declaring = [
        template.name
        for template in TEMPLATES
        if 'id="offline"' in _without_comments(template.read_text())
    ]
    assert declaring == ["offline.html"]

    banner = _without_comments(OFFLINE_HTML.read_text())
    tag = re.search(r'<div[^>]*id="offline"[^>]*>', banner)
    assert tag is not None, "fragments/offline.html no longer renders the banner"
    assert re.search(r"\bx-data\b", tag.group(0))
    assert "x-show" not in tag.group(0)
    assert re.search(r'x-bind:hidden="[^"]*\$store\.ui\.offline', tag.group(0))
    assert re.search(r"\shidden[\s>]", tag.group(0))
    assert "Reconnecting to your mailbox" in banner

    # The flag it binds to is the one sse.js actually writes, and the store
    # that declares it is app.js's.
    assert re.search(r"store\.offline = value", SSE_JS.read_text())
    assert re.search(r"^  offline: false,$", APP_JS.read_text(), re.M)

    # Rendered by the full layout only: a fragment swap carrying a second
    # `id="offline"` would put two in the document, and Alpine would drive
    # whichever came first.
    assert 'include "fragments/offline.html"' in _without_comments(APP_LAYOUT.read_text())
    assert "offline" not in _without_comments(FRAGMENT_LAYOUT.read_text())


def test_alpine_loads_after_every_module_that_registers_a_store():
    """Alpine's bundle queues `start()` in a microtask that drains the
    moment its own script finishes — before the next script in the list
    runs. Ahead of `app.js` it walks the DOM while `$store` does not exist:
    every directive reading it throws once, invisibly (Alpine rethrows
    asynchronously), and an effect that throws before touching a reactive
    property never subscribes to one. The store appears a moment later with
    nobody listening, and the toolbar's `x-show` and this banner's
    `x-bind` simply never run again. No console error, correct-looking
    markup, a control that can never appear.
    """
    markup = _without_comments(APP_LAYOUT.read_text())
    order = re.findall(r"static\('((?:js|vendor)/[\w.-]+)'\)", markup)

    assert "js/app.js" in order
    assert order[-1] == "vendor/alpine.min.js"


def test_the_help_button_is_live_and_opens_the_shortcuts_overlay():
    """It shipped `aria-disabled` and titled "arrives with the key
    registry". The registry has landed, so that title was a false claim on
    the only control a mouse user has for the `?` overlay — the overlay was
    reachable by keyboard alone.
    """
    marked = [
        template.name
        for template in TEMPLATES
        if 'data-role="help"' in _without_comments(template.read_text())
    ]
    assert marked == ["topbar.html"]

    button = re.search(
        r'<button[^>]*data-role="help"[^>]*>', _without_comments(TOPBAR_HTML.read_text())
    )
    assert button is not None
    assert "aria-disabled" not in button.group(0)
    assert "registry" not in button.group(0)

    # `data-role`, not `data-action`: that hook is reserved for the six
    # routes, and this control posts nothing.
    assert "data-action" not in button.group(0)

    # One overlay, one registry: the button opens the same one `?` does
    # rather than a second implementation that could list other keys.
    source = _actions_js()
    assert 'import { openShortcuts } from "./keys.js";' in source
    handler = _block_at(source, source.rindex("addEventListener(", 0, source.index('"help"')))[2]
    assert "openShortcuts()" in handler


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_no_template_uses_an_htmx_attribute_the_csp_forbids(template: pathlib.Path):
    """`hx-on:`, `hx-vals='js:…'` and `hx-trigger="…[expr]"` all compile
    their attribute value with `new Function`, which `script-src 'self'`
    without `'unsafe-eval'` blocks. None of the three fails at build time;
    they fail silently in the browser."""
    markup = _without_comments(template.read_text())

    assert "hx-on" not in markup
    assert "hx-vals" not in markup
    assert not re.search(r'hx-trigger="[^"]*\[', markup)


def test_the_action_layer_binds_its_own_listeners_rather_than_any_markup():
    # Behaviour never rides on an attribute: everything is bound with
    # `addEventListener`, so nothing in this file depends on an evaluator.
    source = _actions_js()
    assert "addEventListener" in source
    assert not re.search(r"\bnew Function\s*\(", source)
    assert not re.search(r"(?<![.\w])eval\s*\(", source)


def test_the_toast_takes_its_note_and_its_window_from_the_action_layer():
    # One number for the undo window: `actions.js` decides how long the
    # offer stands (and how long `z` keeps working), and passes it in.
    assert re.search(r"toast\(message, undoToken = null, note = null, timeout", APP_JS.read_text())
    assert re.search(r"ui\.toast\(message, undoToken, note, UNDO_WINDOW_MS\)", _actions_js())


#: The classes this task's markup and JS depend on. The app serves the
#: *compiled* stylesheet, so each one has to survive `make css` as well as
#: exist in the source.
OWNED_CLASSES = [
    ".row.is-leaving",
    ".act-read",
    ".act-unread",
    ".toast-note",
    ".confirm",
    ".confirm-text",
    ".confirm-actions",
    ".confirm-cancel",
    ".confirm-go",
    ".offline",
]


@pytest.mark.parametrize("selector", OWNED_CLASSES)
def test_the_action_styles_exist_in_the_source_stylesheet(selector: str):
    assert selector in INPUT_CSS.read_text()


@pytest.mark.skipif(not APP_CSS.exists(), reason="`make css` has not run in this tree")
@pytest.mark.parametrize("selector", OWNED_CLASSES)
def test_the_action_styles_survived_the_build(selector: str):
    """An edit to `styles/input.css` that was never compiled changes nothing
    in the browser — `layouts/app.html` links `static/app.css`."""
    assert selector in APP_CSS.read_text()


# ---------------------------------------------------------------------------
# The conversation's own interactions
#
# Three of the four below are things `run()`'s `fetch` never sees: an action
# posted by htmx, a timer, and a navigation. All of them are `actions.js`'s,
# and none of them has a runtime here — so, as above, what is read is the
# control flow rather than the presence of a string.
# ---------------------------------------------------------------------------

MENU_HTML = TEMPLATES_DIR / "thread/menu.html"


def test_a_control_acts_on_the_nearest_thing_that_names_its_own_messages():
    """The per-message star was conversation-scoped: `targets()` sent every
    `data-action` outside `#list` to `defaultTargets()`, which off the list
    resolves to the `.thread-scroll` wrapper — the whole thread. Starring
    one message starred all of them.

    A conversation nests two `[data-email-ids]`, so the fix is which one
    wins: `closest` stops at the card, while the action bar above the cards
    is inside neither and still reaches the whole conversation.
    """
    source = _actions_js()
    body = _function_body(source, "targets")

    row = body.index("closest(ROW_SELECTOR)")
    scoped = body.index('closest("[data-email-ids]")')
    fallback = body.index("defaultTargets()")
    # Row first (a hover action is aimed at what is under the pointer), then
    # the nearest thing naming its own ids, and only then the page-level
    # fallback — which is the one that means "the whole conversation".
    assert row < scoped < fallback

    # `closest`, never a document-wide `querySelector`: the guarded lookup
    # in `openConversation()` is still the only one of those in the file.
    assert source.count('querySelector("[data-email-ids]")') == 1
    assert "querySelectorAll" not in body


def test_the_menus_two_mutating_items_still_carry_their_own_ids():
    """Deliberately *not* riding on the fix above. "Delete message" resolving
    to the whole conversation is data loss, so those two post real forms
    with the exact ids in hidden fields — what the button says is what the
    server is asked for, whatever `targets()` does.
    """
    menu = _without_comments(MENU_HTML.read_text())
    forms = re.findall(r"<form\b[^>]*>.*?</form>", menu, re.S)
    assert len(forms) == 2

    for form in forms:
        assert re.search(r'hx-post="/a/(read|delete)"', form), form
        assert 'name="ids"' in form
        assert "data-action" not in form


def test_an_action_htmx_posted_gets_the_same_toast_and_undo_as_a_fetched_one():
    """`run()` reads `om:done` off its own response; the ⋮ menu's forms are
    htmx requests, so theirs arrives as a bubbling DOM event instead. With
    nobody listening, "Mark unread from here" moved the mail and then said
    nothing at all — no toast, and no Undo for a reader who meant the card
    below. One listener, on the element every other delegated listener in
    this file is bound to, feeding the same consumer.
    """
    source = _actions_js()
    assert source.count('addEventListener("om:done"') == 1

    at = source.index('addEventListener("om:done"')
    assert "document.body" in source[at - 40 : at]
    listener = _block_at(source, at)[2]
    assert "applyDone(" in listener

    # Both halves reach one consumer, so a toast cannot exist on one path
    # and not the other: the declaration, `run()`, `undo()`, and this.
    assert _code(source).count("applyDone(") == 4
    # ...and `run()`'s own posts cannot also arrive here: a `fetch`
    # dispatches no DOM event, which is what keeps this from double-applying.
    assert "fetch(" in _function_body(source, "post")


def test_marking_read_on_open_is_armed_by_a_settle_and_dropped_on_leaving():
    """A `preload`ed GET fetches the conversation on `mousedown` and may
    never be opened, so the timer cannot be armed by the response arriving —
    only by the page actually settling into the DOM. And leaving within the
    delay has to cancel it, or a conversation the reader bailed out of is
    marked read behind them.
    """
    source = _actions_js()
    settle = _block_at(source, source.index('addEventListener("htmx:afterSettle"'))[2]
    assert "conversationSettled()" in settle
    # One settle handler owns both concerns; a second could disagree with it
    # about which conversation is on screen.
    assert source.count('addEventListener("htmx:afterSettle"') == 1

    body = _function_body(source, "conversationSettled")
    # Cancelled before anything is re-armed, and before the "no conversation
    # here" exit — so leaving for the list drops the pending timer too.
    cancel = body.index("cancelMarkRead()")
    assert _nesting_at(body, cancel) == 0
    assert cancel < body.index("setTimeout")
    assert cancel < body.rindex("page === null")

    # The timer posts through the same action layer everything else does,
    # with the ids the page already carries for it.
    armed = _block_at(body, body.index("setTimeout"))[2]
    # Silent: the page reading itself is not the reader acting, so no toast
    # and no claim on the undo slot `z` reads.
    assert 'om.act("read", ids, { silent: true })' in armed
    assert "dataset.unreadIds" in body


def test_a_conversation_the_reader_asked_never_to_mark_read_arms_nothing():
    """`-1` is "never" and `0` is "immediately", so the guard is a sign test
    on a number — not a truthiness test, which would make `0` mean never and
    silently drop the setting most readers are on by default.
    """
    body = _function_body(_actions_js(), "conversationSettled")
    guard = re.search(r"if \((.*delay.*)\) return;", body)
    assert guard is not None, "conversationSettled no longer guards on the delay"
    assert "delay < 0" in guard.group(1)
    # ...and it returns before arming anything.
    assert body.index(guard.group(0)) < body.index("setTimeout")

    # Read as a number, with "never" as the default when the attribute is
    # missing entirely — an unparsed attribute must not arm a timer.
    assert re.search(r'Number\.parseInt\(page\.dataset\.markReadDelay \?\? "-1", 10\)', body)


def test_a_removed_conversation_advances_where_the_reader_asked_to_go():
    """Spec §10's `auto_advance`. The server resolves it — only the route
    knows this conversation's position and the pref behind it — so this end
    reads one finished URL. Back is the fallback, not the first choice, and
    the order matters: reading `data-role="back"` first would have made the
    setting unreachable.
    """
    source = _actions_js()
    body = _function_body(source, "leaveConversation")

    advance = body.index("[data-advance-url]")
    fallback = body.index('[data-role="back"]')
    assert advance < fallback
    # The advance path returns, so back never also runs.
    assert body[advance:fallback].count("return;") == 1
    # Same in-place swap the back control does, rather than a full reload.
    assert 'target: "#main"' in body
    assert 'swap: "morph:innerHTML"' in body
    # No `pushState`: both targets answer with `HX-Push-Url`, and pushing
    # here as well would put one address in the history twice.
    assert "pushState" not in _code(source)

    # Still only reachable past the failure guard in `run()` — a failed
    # archive leaves the reader exactly where they were.
    assert source.count("leaveConversation();") == 1


def test_the_bulk_confirm_dialog_is_named_by_its_own_question():
    """A `<dialog>` with no accessible name announces as just "dialog".

    The reader then hears the buttons — "Archive", "Cancel" — and is asked
    to confirm something nobody told them. This dialog only ever appears for
    a *bulk* action, so the sentence it withholds is the one saying how many
    conversations are about to change.

    `aria-labelledby` pointing at the question, rather than a static
    `aria-label`, so the name is the server's actual message and cannot
    drift from what is on screen.
    """
    source = _actions_js()
    assert 'dialog.setAttribute("aria-labelledby", text.id)' in source
    assert 'text.id = "confirm-question"' in source
