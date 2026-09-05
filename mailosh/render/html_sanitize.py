"""HTML allow-listing for message bodies, on top of nh3 (Rust `ammonia`).

This module is the boundary between a message a stranger wrote and a
document a browser will parse. It is deliberately an *allow-list* in every
dimension -- tags, attributes, URL schemes, CSS properties -- because the
only defensible claim about a sanitiser is "nothing I did not name can get
through", and no deny-list has ever been able to make that claim about
HTML.

It is not the only defence. `mailosh/web/frames.py` renders the output into
a sandboxed iframe under a Content-Security-Policy, and `css_sanitize`
handles everything that is CSS. Each of the three assumes the other two may
fail.

Five nh3 0.3.7 behaviours this module is written against
-------------------------------------------------------
Each was verified directly against the installed extension, not read out of
the documentation. Each one, if assumed away, produces a sanitiser that
passes a casual test suite and is wrong.

1.  **`attribute_filter` sits *between* two URL checks, not before them.**
    nh3 drops a URL attribute whose scheme is not in `url_schemes`, and a
    relative URL under `url_relative="deny"`, *before* the filter is
    called -- `href="javascript:alert(1)"` never reaches the filter at all.
    But the value the filter *returns* is re-checked only against
    `url_relative`, never again against `url_schemes`. Two consequences,
    pulling in opposite directions:

    - a rewrite that returns a relative URL is deleted outright
      (`<img src="cid:x">` rewritten to `/m/E1/cid/x` yields `<img>`, src
      gone), so every rewrite here returns an **absolute** URL; and
    - a rewrite that returns `javascript:alert(1)` is emitted **verbatim**,
      so this filter never returns an attacker-supplied string for a URL
      attribute -- it returns either a URL it built itself out of
      `ctx.origin`, or a value whose scheme it re-checked, or `None`.

2.  **The filter sees raw, un-normalised values.** `'  CID:ABC  '` arrives
    exactly like that, and nh3's own scheme check (which folds case and
    trims) has already let it through. `value.startswith("cid:")` would
    both miss that real inline image and mis-handle a padded one, so every
    comparison here is on `value.strip().lower()`.

3.  **The filter is also called for the attributes nh3 injects itself** --
    `("a", "target", "_blank")` from `set_tag_attribute_values` and
    `("a", "rel", "noopener noreferrer nofollow")` from `link_rel`. A
    filter whose default branch returns `None` therefore strips its own
    `rel`, quietly undoing the reverse-tabnabbing and referrer protection
    it asked for. The default branch here returns `value` unchanged.

4.  **`url_schemes` is global, not per tag.** `cid` and `data` are in it so
    that inline images can reach the filter at all -- which also means
    `<a href="data:text/html,...">` and `<a href="cid:...">` reach it. Both
    are gated per tag *inside* the filter; nh3 will not do it.

    nh3's URL-attribute list is also narrower than HTML's: `cite` on
    `<blockquote>`/`<q>`/`<del>`/`<ins>` is **not** scheme-checked at all,
    and `cite="javascript:alert(1)"` survives `nh3.clean` untouched. It is
    inert where it sits today, but it is an unvalidated attacker URL parked
    in the DOM waiting for a feature to make it live, so the filter gates
    it too.

5.  **Three configurations are hard errors, one of them a Rust panic.**
    `rel` in `attributes["a"]` together with `link_rel` raises
    `ValueError`; a tag in both `tags` and `clean_content_tags` raises
    `ValueError`; and `allowed_classes` together with `class` in
    `attributes` **panics the extension module**, raising
    `pyo3_runtime.PanicException` -- which derives from `BaseException`,
    not `Exception`, so an `except Exception` around the call does not
    catch it. `allowed_classes` is never passed. `tests/unit/
    test_html_sanitize.py` asserts all three invariants directly.

What is deliberately *not* here
-------------------------------
`filter_style_properties` is passed as a second line behind
`css_sanitize.sanitize_declarations`, not instead of it: nh3 on its own
happily emits `style="width:expression(alert(1))"`, and its property filter
is case-*sensitive*, so it would keep `POSITION:fixed` if `position` were
ever added to the allow-list. The CSS sanitiser is the real control.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import nh3

from mailosh.render import css_sanitize

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_TAGS",
    "CLEAN_CONTENT_TAGS",
    "DATA_IMAGE_TYPES",
    "URL_SCHEMES",
    "SanitizeContext",
    "SanitizeResult",
    "extract_styles",
    "sanitize_email_html",
]

#: Tags kept, with their contents. Everything not named here is unwrapped:
#: the tag goes, the text inside it stays. Chosen for what real mail (and
#: twenty years of table-layout newsletters) actually uses -- there is no
#: `head`, `body`, `html`, `frame` or any interactive element in the list,
#: because a message is a fragment pasted into a document we own.
ALLOWED_TAGS: frozenset[str] = frozenset(
    """
    a abbr acronym address article aside b bdi bdo big blockquote br caption center cite code
    col colgroup dd del details dfn dir div dl dt em figcaption figure font footer
    h1 h2 h3 h4 h5 h6 header hgroup hr i img ins kbd li main map mark menu nav ol p pre q
    rp rt ruby s samp section small span strike strong sub summary sup
    table tbody td tfoot th thead time tr tt u ul var wbr
    """.split()
)

#: Tags removed *with their contents*, rather than unwrapped. The
#: distinction is the whole defence against mutation XSS: `<noscript>`,
#: `<title>`, `<style>`, `<textarea>`, `<xmp>` and `<plaintext>` all have a
#: raw-text content model, so text that is inert *inside* them becomes live
#: markup the moment the wrapper is peeled off and the remainder is
#: re-parsed. Dropping the subtree means there is never a re-parse step.
#:
#: `svg` and `math` are here for the same reason one level up: inside them
#: the parser switches to foreign-content rules, where `<script>` is not the
#: HTML `<script>` and CDATA sections work.
#:
#: `form` and every control are here because the threat model for mail is
#: phishing at least as much as it is script execution -- and a form
#: stripped to its bare label text still reads as a login box.
#:
#: Must stay disjoint from ALLOWED_TAGS: nh3 raises ValueError on overlap.
CLEAN_CONTENT_TAGS: frozenset[str] = frozenset(
    """
    script style title textarea noscript iframe frame frameset object embed applet
    form input button select option optgroup label fieldset legend
    base link meta template portal dialog canvas audio video source track
    math svg marquee plaintext xmp
    """.split()
)

#: Attributes kept, per tag, plus a `"*"` bucket applied to every tag.
#:
#: Absent by construction, and asserted absent by the tests: every `on*`
#: handler, `srcset`, `poster`, `background`, `formaction`, `ping`,
#: `usemap`, `target` (nh3 sets that itself), `rel` (likewise -- listing it
#: here alongside `link_rel` is one of nh3's hard ValueErrors), `name`,
#: `data-*`, `lowsrc`, `dynsrc`, `loading` and `srcdoc`.
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "*": frozenset(
        {
            "class",
            "id",
            "dir",
            "lang",
            "title",
            "style",
            "align",
            "valign",
            "bgcolor",
            "width",
            "height",
        }
    ),
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "summary"}),
    "td": frozenset({"colspan", "rowspan", "headers", "scope", "nowrap"}),
    "th": frozenset({"colspan", "rowspan", "abbr", "headers", "scope", "nowrap"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "li": frozenset({"value"}),
    "time": frozenset({"datetime"}),
    # `type` is inert markup, but it carries Mozilla's `blockquote[type=cite]`
    # quote marker, which quote_trim (Task 8) keys off.
    "blockquote": frozenset({"cite", "type"}),
    "q": frozenset({"cite"}),
    "del": frozenset({"cite", "datetime"}),
    "ins": frozenset({"cite", "datetime"}),
    "details": frozenset({"open"}),
}

#: Schemes nh3 lets past its own check. This set is *global*, not per tag --
#: `cid` and `data` are in it only so that `<img src>` can reach the
#: attribute filter, which then gates both per tag. Nothing here is trusted
#: on the strength of being in this set.
URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "cid", "data"})

#: `image/<subtype>` values accepted in a `data:` URI on `<img src>`.
#: `svg+xml` is pointedly absent: an SVG document is a scripting context, so
#: a `data:image/svg+xml` image is a script tag with a friendly MIME type.
DATA_IMAGE_TYPES: frozenset[str] = frozenset({"png", "gif", "jpeg", "jpg", "webp", "bmp"})

#: `id`s starting with this belong to the message frame's own chrome (the
#: quote container, the blocked-images banner). A message that could set
#: them could impersonate our UI inside its own body, so they are reserved.
RESERVED_ID_PREFIX = "mailosh-"

#: Schemes a link in a message may navigate to.
_ANCHOR_SCHEMES = frozenset({"http", "https", "mailto"})

#: Schemes a `cite` attribute may reference. Narrower than the anchor set:
#: `cite` is a citation, never a mail composer.
_CITE_SCHEMES = frozenset({"http", "https"})

#: Attributes carrying a citation URL, per HTML. nh3 does not scheme-check
#: any of them (verified against 0.3.7), so this filter does.
_CITE_ATTRIBUTE = "cite"

# A scheme per RFC 3986: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) ":".
# Matched against the *lower-cased, stripped* value, never the raw one.
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:")

# nh3 accepts any set type; these are the exact objects handed to it, kept
# module-level so a message does not pay to rebuild them.
_NH3_TAGS = set(ALLOWED_TAGS)
_NH3_CLEAN_CONTENT_TAGS = set(CLEAN_CONTENT_TAGS)
_NH3_ATTRIBUTES = {tag: set(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()}
_NH3_URL_SCHEMES = set(URL_SCHEMES)


@dataclass(frozen=True, slots=True)
class SanitizeContext:
    """Everything the attribute filter needs to rewrite a URL.

    `origin` is this deployment's own absolute origin (scheme + host, no
    trailing slash needed). Every URL this module emits is built from it,
    because nh3 deletes a filter result that is relative -- see behaviour 1
    in the module docstring.

    `cid_parts` maps a Content-ID (without the angle brackets RFC 2392 puts
    around it in the header) to the JMAP blob id that serves it. The filter
    uses it as a membership test: an inline image whose cid is not one of
    *this message's* parts gets no `src` at all, so a mail cannot address
    another message's attachments by guessing a Content-ID.

    `remote` is the reader's decision about this message ("show remote
    content"), and `sign_image` mints the signed proxy token. Both must be
    present for a remote image to load; either one missing blocks it.

    `sign_cid` mints the capability token for one inline part, given its
    Content-ID. It is required for a `cid:` rewrite for the same reason
    `sign_image` is required for a remote one: the URL both produce is
    fetched by a document with an **opaque origin**, which carries no session
    cookie, so a URL with no token on it is a URL the route will refuse.
    Missing signer, no `src` — the image is dropped here rather than emitted
    as a link that 404s, so the failure is one the sanitiser's own tests can
    see.
    """

    email_id: str
    origin: str
    remote: bool = False
    cid_parts: Mapping[str, str] = field(default_factory=dict)
    sign_image: Callable[[str], str] | None = None
    sign_cid: Callable[[str], str] | None = None


@dataclass(frozen=True, slots=True)
class SanitizeResult:
    """The sanitised message plus what the reader needs told about it.

    `css` is the message's `<style>` blocks, extracted and sanitised. The
    `<style>` *elements* never reach `html` -- they are in
    CLEAN_CONTENT_TAGS -- so the frame inlines this string into a `<style>`
    element of its own instead.

    `blocked_remote` counts images, not hosts: three tracking pixels on one
    host are three blocked loads, and that is the number the banner shows.
    `remote_hosts` is the deduplicated, sorted host list behind it, so the
    reader can see *who* wanted to know they opened the mail.
    """

    html: str
    css: str = ""
    blocked_remote: int = 0
    remote_hosts: tuple[str, ...] = ()


class _StyleCollector(HTMLParser):
    """Collect the text of every `<style>` element, at any nesting depth.

    `HTMLParser` switches to CDATA mode on `<style>` of its own accord, so
    the CSS arrives as raw text rather than being re-tokenised as markup,
    and it accepts the spellings a hand-written mail actually contains --
    `</STYLE>`, `</style >`, an unclosed block at end of input.

    Nothing this class produces is ever re-emitted as markup. The only thing
    that crosses from here to the output is CSS *text*, which
    `css_sanitize.sanitize_stylesheet` then re-parses and re-serialises --
    so a divergence between this parse and html5ever's (the classic way a
    two-parser design gets exploited) cannot inject anything. The worst case
    is that a stray `<` from a mis-parse lands in the text, and
    `sanitize_stylesheet` refuses the whole block for exactly that reason.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "style":
            self._depth += 1
            self.blocks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "style" and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth and self.blocks:
            self.blocks[-1] += data


def extract_styles(html: str) -> list[str]:
    """Return the raw text of every `<style>` block in `html`, in order.

    Raw: no sanitising happens here. Callers pass each block through
    `css_sanitize.sanitize_stylesheet` -- `sanitize_email_html` does.
    """
    parser = _StyleCollector()
    parser.feed(html)
    parser.close()
    return parser.blocks


def _scheme_of(lowered: str) -> str | None:
    """The URL scheme of an already-lower-cased, already-stripped value."""
    match = _SCHEME_RE.match(lowered)
    return match.group(0)[:-1] if match else None


def _cid_src(value: str, ctx: SanitizeContext, origin: str) -> str | None:
    """Rewrite `cid:<content-id>` to this deployment's inline-part URL,
    carrying the capability token that URL is only served against.

    Absolute, and same-origin, for the two independent reasons in the module
    docstring: nh3 deletes a relative rewrite outright, and the frame's CSP
    only permits images from our own origin.

    Two gates before a token is minted at all, and the order matters. The
    Content-ID must be one *this message* actually carries (`cid_parts`), so
    a mail cannot make us sign a capability for a part it merely named; and
    there must be a signer, so a caller that did not supply one emits no
    `src` rather than a URL that cannot be fetched.

    The Content-ID keeps its original case -- only the *scheme* is
    case-insensitive; the id itself is not -- and it is the *bare* id, after
    the angle brackets come off, that is both signed and quoted into the
    path. Signing one spelling and serving another would 404 every inline
    image in the app.

    The token is quoted like the proxy's: it is opaque, may carry `=`
    padding, and an unquoted `&` or `#` in one would truncate the query.
    """
    cid = value[len("cid:") :].strip().strip("<>")
    if not cid or cid not in ctx.cid_parts or ctx.sign_cid is None:
        return None
    path = f"{origin}/m/{quote(ctx.email_id, safe='')}/cid/{quote(cid, safe='')}"
    return f"{path}?u={quote(ctx.sign_cid(cid), safe='')}"


def _data_src(value: str, lowered: str) -> str | None:
    """Keep a `data:` image URI only for a raster type we named.

    The media type is whatever precedes the first `,` and the first `;`, in
    that order -- `data:image/png;base64,AAA` and `data:image/png,AAA` both
    reduce to `image/png`.
    """
    media = lowered[len("data:") :].split(",", 1)[0].split(";", 1)[0].strip()
    if not media.startswith("image/"):
        return None
    subtype = media[len("image/") :]
    return value if subtype in DATA_IMAGE_TYPES else None


def _image_src(
    value: str,
    lowered: str,
    scheme: str | None,
    ctx: SanitizeContext,
    origin: str,
    blocked: list[str],
) -> str | None:
    """The `<img src>` policy: cid rewrite, data allow-list, remote proxy.

    Anything that is not one of those three -- including a scheme nh3 let
    through for another tag's sake -- gets no `src`. Default deny, stated
    once, at the end.
    """
    if scheme == "cid":
        return _cid_src(value, ctx, origin)
    if scheme == "data":
        return _data_src(value, lowered)
    if scheme in ("http", "https"):
        if ctx.remote and ctx.sign_image is not None:
            # The signed token is the *only* thing that reaches the proxy;
            # the raw URL never appears in the document. Quoted because a
            # token is opaque and may legitimately contain "=" padding --
            # an unquoted "&" or "#" in one would truncate the query.
            return f"{origin}/img?u={quote(ctx.sign_image(value), safe='')}"
        blocked.append(urlsplit(value).hostname or "")
        return None
    return None


#: How deeply a message may nest the allow-listed elements below before it
#: is refused. Real mail does not approach this: even a table-in-table
#: newsletter runs to tens of levels. The bound exists because nh3 is
#: quadratic in exactly this dimension -- see the comment at the `nh3.clean`
#: call for the measurements and why the cost lands on every user at once.
MAX_NESTING_DEPTH: int = 100

#: The tags whose nesting drives nh3's quadratic behaviour: html5ever caps
#: its active-formatting-elements list (so `<b>`, `<span>`, `<p>` are fine)
#: but not the open-element stack these push onto.
_STACKING_TAGS: frozenset[str] = frozenset(
    {
        "div",
        "blockquote",
        "section",
        "article",
        "aside",
        "details",
        "figure",
        "footer",
        "header",
        "main",
        "nav",
        "ul",
        "ol",
        "li",
        "dl",
        "dd",
        "dt",
        "pre",
        "center",
        "fieldset",
        "form",
        "table",
        "td",
        "th",
    }
)

_OPEN_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)")


class BodyTooDeep(ValueError):
    """A message nests allow-listed elements past `MAX_NESTING_DEPTH`.

    Raised *before* nh3 runs, because by then the cost has been paid. The
    caller renders a placeholder for that message rather than the body; see
    the comment at the `nh3.clean` call site.
    """


def _nesting_exceeds(html: str, limit: int) -> bool:
    """True when `html` nests `_STACKING_TAGS` deeper than `limit`.

    One linear pass over a regex scan -- deliberately not a parse, because
    the whole point is to answer before anything expensive touches the body.
    It over-counts slightly (an unclosed tag a real parser would
    auto-close still counts) which is the safe direction: the limit is two
    orders of magnitude above what real mail uses, so only a body built to
    be pathological can reach it.
    """
    depth = 0
    for closing, name in _OPEN_TAG_RE.findall(html):
        if name.lower() not in _STACKING_TAGS:
            continue
        if closing:
            depth = max(0, depth - 1)
        else:
            depth += 1
            if depth > limit:
                return True
    return False


def sanitize_email_html(html: str, ctx: SanitizeContext) -> SanitizeResult:
    """Sanitise one message body into something safe to frame.

    Returns the cleaned fragment, the message's own stylesheet (extracted
    and sanitised separately, because the `<style>` element itself is
    dropped with its contents), and the remote-image tally the reader's
    banner is built from.
    """
    origin = ctx.origin.rstrip("/")
    # One entry per *blocked* remote image, holding its host (or "" for a
    # URL with no parseable host). A list, not a counter: the count and the
    # host set are two different questions and the list answers both.
    blocked: list[str] = []

    def attribute_filter(tag: str, attr: str, value: str) -> str | None:
        # Normalise once, for comparisons only. NUL is stripped because a
        # browser ignores it inside a scheme ("\0javascript:" navigates)
        # while a naive startswith does not.
        normalised = value.replace("\x00", "").strip()
        lowered = normalised.lower()
        scheme = _scheme_of(lowered)

        if attr == "style":
            # `or None` matters: a filter that returns "" leaves an empty
            # `style=""` on the element rather than removing the attribute.
            return css_sanitize.sanitize_declarations(value) or None
        if attr == "id" and lowered.startswith(RESERVED_ID_PREFIX):
            return None
        if tag == "img" and attr == "src":
            return _image_src(normalised, lowered, scheme, ctx, origin, blocked)
        if tag == "a" and attr == "href":
            return normalised if scheme in _ANCHOR_SCHEMES else None
        if attr == _CITE_ATTRIBUTE:
            return normalised if scheme in _CITE_SCHEMES else None
        # Everything else, unchanged. This branch is load-bearing: it is
        # what preserves nh3's own injected `rel`/`target` (behaviour 3).
        return value

    # nh3.clean is O(n^2) in the nesting depth of the ~24 allow-listed tags
    # whose open-element stack html5ever does not cap (div, blockquote,
    # section, ul, pre, ...). Measured on this tree: 195 KB of nested <div>
    # takes 2.5 s against 79 ms for the same bytes laid out flat, and the
    # 512 KB a JMAP body may carry takes ~18 s. That is not a slow render --
    # `sanitize_email_html` is called synchronously from an async route, so
    # the whole worker serves nobody for the duration, other users and the
    # background mail poller included. One message, sent by anyone, is a
    # repeatable outage.
    #
    # So the depth is bounded before nh3 sees the body rather than after.
    # A message nested past MAX_NESTING_DEPTH is not rendered: no real mail
    # comes close (deeply table-nested newsletters run to tens, not
    # hundreds), and refusing to render one hostile body is a far better
    # failure than stalling the process for everybody.
    if _nesting_exceeds(html, MAX_NESTING_DEPTH):
        raise BodyTooDeep(f"message nests allow-listed elements more than {MAX_NESTING_DEPTH} deep")

    cleaned = nh3.clean(
        html,
        tags=_NH3_TAGS,
        attributes=_NH3_ATTRIBUTES,
        clean_content_tags=_NH3_CLEAN_CONTENT_TAGS,
        attribute_filter=attribute_filter,
        url_schemes=_NH3_URL_SCHEMES,
        # Relative URLs have no meaning in a message rendered under our own
        # origin: resolving one would point it at *our* routes. Deny, and
        # let the filter's absolute rewrites be the only URLs that survive.
        url_relative="deny",
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        # Second line only; css_sanitize.sanitize_declarations in the filter
        # above is the control that matters. See the module docstring.
        filter_style_properties=set(css_sanitize.ALLOWED_PROPERTIES),
        strip_comments=True,
        # allowed_classes is deliberately NOT passed: with "class" in
        # attributes it panics the Rust extension (behaviour 5).
    )

    sanitised_blocks = (css_sanitize.sanitize_stylesheet(block) for block in extract_styles(html))
    css = "\n".join(block for block in sanitised_blocks if block)

    return SanitizeResult(
        html=cleaned,
        css=css,
        blocked_remote=len(blocked),
        remote_hosts=tuple(sorted({host for host in blocked if host})),
    )
