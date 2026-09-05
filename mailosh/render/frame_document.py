"""The sandboxed document a message body is rendered into, and the CSP
that contains it.

The sanitisers (`html_sanitize`, `css_sanitize`) decide what markup may
exist. This module decides what that markup is *allowed to do* once a
browser has it, which is a different question with a different answer: an
allow-list can be wrong, and when it is, this is what is still standing.

Three properties, in the order they matter.

**`allow-same-origin` appears in neither sandbox.** Not in the CSP
`sandbox` directive, not in the `<iframe sandbox>` attribute, nowhere. It
is the single property that keeps a hostile message out of the reader's
session: without it the framed document has an *opaque* origin, so it
cannot read the app's cookies, cannot reach `localStorage`, cannot touch
`parent.document`, and cannot make a same-origin request carrying the
reader's session. Every other rule here is a defence; this one is the
wall.

**The CSP `sandbox` directive carries `allow-scripts`, and the spec's own
header did not.** A document under both a CSP sandbox and an iframe
sandbox gets the **intersection** of the two permission sets, not the
union. Spec §7 writes `sandbox allow-popups allow-popups-to-escape-sandbox`
in the header while the iframe attribute carries `allow-scripts` — which
would have stripped script execution from the intersection and silently
prevented the hash-pinned resize script from ever running, making the
`script-src 'sha256-…'` the header goes to the trouble of computing
meaningless. `docs/plans/2026-09-03-phase1b-reading.md` records the
departure; this module implements it. `allow-same-origin` is still absent
from both, which is the property that actually matters.

**`img-src` is `'self' data:` and never gains `http:`/`https:`.** The
header is byte-identical whether the reader has turned remote images on or
off, because "on" does not mean "let the browser fetch the sender's host" —
`html_sanitize` rewrites every permitted remote image to `/img?u=<signed>`
on this app's own origin, and `fetch_guard` fetches it server-side. So a
sanitiser miss cannot leak the reader's IP to a tracking pixel: the CSP
refuses the request whether or not the `src` survived. A header that
widened with `?remote=1` would give that back.

**The two restyle fragments are imported, not restated.** `restyle` is a
mode name decided by `mailosh.render.dark.restyle_mode`; the CSS each mode
means (`COLOR_SCHEME_CSS`, `INVERT_CSS`) is defined once, there, and
inlined verbatim here. A second copy in this module would be a spec §7
rule that two files could disagree about — and the disagreement would show
up as a mail that looks right in one test and wrong in a browser.

Why the document is Python string concatenation and not a Jinja template:
the CSP names a `sha256` of the script's exact bytes. A template's
whitespace handling (trim_blocks, keep_trailing_newline, an editor
stripping trailing space on save) is not a contract, and every one of those
would turn `script-src` into a hash of something the browser never sees —
which fails *closed*, as a refused inline script and a frame that never
resizes. `FRAME_SCRIPT_HASH` is computed from `FRAME_SCRIPT` at import
time for the same reason: there is no second copy of the script to drift
from.

`FRAME_SCRIPT` and the base stylesheet are both assembled from
line-per-string tuples rather than written as one triple-quoted literal.
That is not a style preference: several of these lines are longer than this
project's 100-column limit, and a `# noqa` cannot live inside a string
literal without becoming part of the script.
"""

from __future__ import annotations

import base64
import hashlib

from mailosh.render.dark import COLOR_SCHEME_CSS, INVERT_CSS

__all__ = [
    "FRAME_SCRIPT",
    "FRAME_SCRIPT_HASH",
    "MAX_FRAME_HEIGHT",
    "MIN_FRAME_HEIGHT",
    "csp_header",
    "render_frame",
]

#: The parent clamps a frame to this range before assigning a height
#: (`static/js/frame.js`). Named here, next to the script that reports the
#: height, so the two halves of the handshake cannot drift apart.
MIN_FRAME_HEIGHT = 200
MAX_FRAME_HEIGHT = 20_000

#: The only script the frame ever runs, inlined verbatim and pinned by its
#: own sha256 in the CSP. It reports its document height to the parent and
#: drives the quote toggle — the toggle lives *inside* the frame because
#: expanding a quote changes the height, and the parent has no way to
#: measure a document it is forbidden to read.
#:
#: `postMessage(..., "*")` is correct here rather than lax: the frame has an
#: opaque origin and so cannot name the parent's, and there is nothing
#: secret in `{type, height}`. The trust runs the other way — the parent
#: verifies the sender, which is `frame.js`'s job.
FRAME_SCRIPT = "\n".join(
    (
        "(function () {",
        "  var LAST = 0;",
        "  function post() {",
        "    var d = document.documentElement, b = document.body;",
        "    var h = Math.max(d.scrollHeight, d.offsetHeight,"
        " b ? b.scrollHeight : 0, b ? b.offsetHeight : 0);",
        "    if (h === LAST) return;",
        "    LAST = h;",
        '    parent.postMessage({ type: "mailosh:frame-height", height: h }, "*");',
        "  }",
        '  var t = document.querySelector("[data-mailosh-quote-toggle]");',
        '  var q = document.querySelector("[data-mailosh-quote]");',
        "  if (t && q) {",
        '    t.addEventListener("click", function () {',
        '      var opening = q.hasAttribute("hidden");',
        '      if (opening) { q.removeAttribute("hidden"); }'
        ' else { q.setAttribute("hidden", ""); }',
        '      t.setAttribute("aria-expanded", opening ? "true" : "false");',
        "      post();",
        "    });",
        "  }",
        '  window.addEventListener("load", post);',
        '  window.addEventListener("resize", post);',
        "  if (window.ResizeObserver)"
        " { new ResizeObserver(post).observe(document.documentElement); }",
        "  setTimeout(post, 0); setTimeout(post, 300); setTimeout(post, 1200);",
        "})();",
    )
)

#: `sha256-<base64>`, computed from `FRAME_SCRIPT`'s exact bytes at import
#: time. Never hardcode this: a stale literal and a live script disagree
#: silently until a browser refuses the inline script.
FRAME_SCRIPT_HASH = (
    "sha256-" + base64.b64encode(hashlib.sha256(FRAME_SCRIPT.encode("utf-8")).digest()).decode()
)

#: The CSP directives, in the order `csp_header` emits them. `default-src
#: 'none'` is the floor every unnamed fetch falls through to — no
#: `connect-src`, so a message cannot phone home; no `frame-src`, so it
#: cannot nest another document; no `font-src`, no `media-src`.
#: `frame-ancestors 'self'` means only this app may frame the document, so
#: `/m/{id}/html` pasted into a hostile page renders nothing.
_CSP_DIRECTIVES = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox",
    "frame-ancestors 'self'",
    "default-src 'none'",
    # 'self' covers both the cid: rewrite (/m/{id}/cid/{cid}) and the remote
    # proxy (/img?u=…). Deliberately identical for remote=0 and remote=1 —
    # see the module docstring.
    "img-src 'self' data:",
    # No 'unsafe-eval', no nonce, no host: the only stylesheets are the two
    # <style> elements this module writes itself.
    "style-src 'unsafe-inline'",
)

#: The document head, byte for byte. `color-scheme: light dark` on the meta
#: tag tells the browser both are possible; the resolved choice is the
#: generated `html{color-scheme:…}` rule below, which is what actually
#: decides the UA-default colours for form controls and scrollbars inside
#: the frame.
_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="color-scheme" content="light dark">\n'
)

#: The base stylesheet, emitted *before* the message's own so that a mail's
#: rules win every specificity tie. `img:not([src])` is the blocked-image
#: placeholder and needs no marker attribute of its own: a blocked image is
#: precisely one whose `src` the sanitiser removed.
_BASE_CSS_RULES = (
    "body{margin:0;padding:12px 14px;"
    'font:14px/1.55 Inter,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;'
    "word-break:break-word}",
    "*{max-width:100%}",
    "img{max-width:100%;height:auto;border:0}",
    "img:not([src]){display:inline-block;min-width:64px;min-height:24px;"
    "border:1px dashed currentColor;border-radius:4px;opacity:.45}",
    "table{max-width:100%}",
    ".mailosh-quote-toggle{display:inline-flex;align-items:center;height:18px;"
    "margin:8px 0;padding:0 9px;border:0;border-radius:999px;"
    "background:rgba(127,127,127,.22);color:inherit;font:inherit;"
    "font-weight:700;letter-spacing:.15em;cursor:pointer}",
)

#: The quote toggle and the container it hides. `data-mailosh-quote-toggle`
#: /`data-mailosh-quote` are what `FRAME_SCRIPT` binds to; `html_sanitize`
#: reserves the `mailosh-` id prefix so a message cannot forge either.
_QUOTE_TOGGLE = (
    '<button type="button" class="mailosh-quote-toggle" data-mailosh-quote-toggle'
    ' aria-expanded="false">&#8226;&#8226;&#8226;</button>'
)

#: `theme` -> the `color-scheme` value for a message we are *not*
#: restyling. "system" hands the browser both and lets
#: `prefers-color-scheme` decide.
_THEME_SCHEMES = {"light": "light", "dark": "dark", "system": "light dark"}


def csp_header() -> str:
    """The `Content-Security-Policy` for `GET /m/{id}/html`.

    One string, one order, no parameters: nothing about a message, a
    reader's preferences or a remote-image decision may widen it. That is
    the point — see the module docstring on why `img-src` is identical for
    `?remote=0` and `?remote=1`.
    """
    return "; ".join((*_CSP_DIRECTIVES, f"script-src '{FRAME_SCRIPT_HASH}'"))


def _scheme_rule(theme: str, restyle: str) -> str:
    """The `html{color-scheme:…}` rule.

    `restyle == "color-scheme"` means the mail declared its own scheme
    support (spec §7), so it is handed both and left alone rather than
    being pinned to the reader's theme — the mail already knows how to be
    dark. That case emits `dark.COLOR_SCHEME_CSS` verbatim rather than a
    second spelling of the same rule: `mailosh.render.dark` is where spec
    §7's two restyle fragments live, and the module that *decides* a mode
    and the module that *emits* it must not each carry their own copy.

    An unrecognised `theme` falls back to "system" rather than raising:
    this is a query parameter, and a typo must not 500.
    """
    if restyle == "color-scheme":
        return COLOR_SCHEME_CSS
    if restyle == "invert":
        # Light, always -- the inversion is what produces the dark result.
        # Pinning this to the reader's theme instead cancels the two out: at
        # `dark` the UA paints default text white on a canvas the mail never
        # gave a background to, `INVERT_CSS` turns that white text black, and
        # black text lands on the app's own dark card with nothing behind it.
        # The message renders as an empty box. Starting light means the
        # inversion has a white canvas and black text to actually invert.
        return "html{color-scheme:light}"
    return f"html{{color-scheme:{_THEME_SCHEMES.get(theme, _THEME_SCHEMES['system'])}}}"


def _base_css(theme: str, restyle: str) -> str:
    """The base stylesheet for this theme/restyle pair.

    `dark.INVERT_CSS` (spec §7's pair: `html` inverted, then `img` and
    inline `background-image` elements counter-inverted so a photograph is
    not a negative) is appended only for `restyle == "invert"`, and is
    wrapped in `@media (prefers-color-scheme: dark)` when the reader's
    theme is "system" — under "system" the server cannot know which way the
    OS will resolve, so the media query is what makes the inversion apply
    in dark and not in light.
    """
    rules = [_scheme_rule(theme, restyle), *_BASE_CSS_RULES]
    if restyle == "invert":
        # An explicit white ground, painted before the inversion runs.
        #
        # Spec §7 inverts a mail "if the body/first-table background is
        # light" -- but most mail declares no background at all, and
        # `background_is_light` treats that absence as light (mail defaults
        # to white paper). For those, `filter` on the root had nothing to
        # inverta root filter does not repaint the canvas, so the canvas
        # stayed white from `color-scheme: light` while the black text
        # inverted to white. White on white: the message body rendered
        # blank, which is what this looked like in the browser.
        #
        # Giving the root a real background means the inversion has a
        # surface: white becomes near-black, the black text becomes white,
        # and a mail that *did* declare its own background still overrides
        # this rule and inverts on its own terms.
        rules.append("html{background:#ffffff}")
        rules.append(
            f"@media (prefers-color-scheme: dark){{{INVERT_CSS}}}"
            if theme == "system"
            else INVERT_CSS
        )
    return "\n".join(rules)


def _quote_markup(quoted_html: str, *, expand: bool) -> str:
    """The trimmed-quote half of the body.

    Three shapes, and the toggle exists in exactly one of them: no quote at
    all (nothing), a quote behind the toggle (button + hidden container),
    or `expand=True` (the quote bare, no button and no `hidden`) — the
    print page and "show original", where a control nobody can click would
    only hide half the message.
    """
    if not quoted_html:
        return ""
    if expand:
        return quoted_html
    return _QUOTE_TOGGLE + "<div data-mailosh-quote hidden>" + quoted_html + "</div>"


def render_frame(
    *,
    visible_html: str,
    quoted_html: str,
    mail_css: str,
    theme: str,
    restyle: str,
    expand: bool = False,
) -> str:
    """Assemble the whole framed document.

    `visible_html`/`quoted_html` are the two halves of an **already
    sanitised** body (`html_sanitize.sanitize_email_html` then
    `quote_trim.split_html`), and `mail_css` is the message's own
    stylesheet after `css_sanitize.sanitize_stylesheet`. Nothing is escaped
    here — this function concatenates, and every caller must hand it output
    the sanitisers approved.

    The one thing it does re-check is `mail_css` for a literal `<`.
    `css_sanitize` already guarantees there is none (its serializer decodes
    CSS escapes, so `content:"\\3c /style\\3e "` would otherwise close the
    element it is inlined into), but this function is the last code between
    that string and a `<style>` element, and a second check here costs one
    comparison per message.
    """
    if "<" in mail_css:
        mail_css = ""
    return "".join(
        (
            _HEAD,
            "<style>",
            _base_css(theme, restyle),
            "</style>",
            "<style>",
            mail_css,
            "</style>",
            "</head><body>",
            visible_html,
            _quote_markup(quoted_html, expand=expand),
            "<script>",
            FRAME_SCRIPT,
            "</script></body></html>",
        )
    )
