"""The framed document and the CSP that contains it
(`mailosh.render.frame_document`).

Four of these tests are the only automated proof of a security property,
and each is written so that relaxing exactly one rule in the module turns
exactly one of them red:

- `allow-same-origin` is in neither sandbox. That omission is what gives
  the framed document an opaque origin and so no reach into the reader's
  session; adding it would defeat every other control in this file at once.
- the CSP `sandbox` directive *does* carry `allow-scripts`. Two sandboxes
  intersect rather than union, so dropping it here would silently stop the
  hash-pinned script from running while leaving the iframe attribute
  looking correct.
- `img-src` is `'self' data:` and does not widen with `?remote=1`. Asserted
  as an exact token list, and again as an equality between the two headers
  in `test_frame_routes.py`.
- the script hash is recomputed from the served bytes, never compared to a
  literal — a hardcoded hash is a lie the moment the script changes, and it
  fails as a refused inline script rather than as a test.

Nothing here asserts "this substring is present". A substring check passes
on a coincidence and, worse, an absence check (`"allow-same-origin" not in
doc`) also passes when the attribute is spelled differently — so every
assertion below goes through a real parse of the document, or through an
exact equality on a parsed structure.
"""

from __future__ import annotations

import base64
import hashlib
import re

from helpers import parse_attrs

from mailosh.render import frame_document as fd

STYLE_RE = re.compile(r"<style>(.*?)</style>", re.DOTALL)


def _styles(doc: str) -> list[str]:
    """The text of every `<style>` element, in document order."""
    return STYLE_RE.findall(doc)


def _directives(csp: str) -> dict[str, list[str]]:
    """`{name: [source, ...]}` for a Content-Security-Policy string."""
    out: dict[str, list[str]] = {}
    for chunk in csp.split(";"):
        parts = chunk.split()
        if parts:
            out[parts[0]] = parts[1:]
    return out


def _render(**overrides: object) -> str:
    defaults: dict[str, object] = {
        "visible_html": "<p>a</p>",
        "quoted_html": "",
        "mail_css": "",
        "theme": "light",
        "restyle": "none",
    }
    defaults.update(overrides)
    return fd.render_frame(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The script, and the hash that pins it
# ---------------------------------------------------------------------------


def test_hash_matches_the_script_bytes_exactly():
    digest = hashlib.sha256(fd.FRAME_SCRIPT.encode("utf-8")).digest()
    assert fd.FRAME_SCRIPT_HASH == "sha256-" + base64.b64encode(digest).decode()


def test_document_embeds_the_script_verbatim_so_the_hash_holds():
    """The hash in the header is only worth anything if the bytes it names
    are the bytes the browser parses. Any interpolation, indentation or
    trailing newline between the constant and the `<script>` element would
    fail *closed* — a refused inline script and a frame that never resizes.
    """
    doc = _render(mail_css="p{color:red}")
    assert doc.count("<script>") == 1
    assert doc.count("</script>") == 1
    start = doc.index("<script>") + len("<script>")
    embedded = doc[start : doc.index("</script>", start)]
    assert embedded == fd.FRAME_SCRIPT
    digest = hashlib.sha256(embedded.encode("utf-8")).digest()
    assert f"script-src 'sha256-{base64.b64encode(digest).decode()}'" in fd.csp_header()


def test_the_script_is_the_only_one_the_document_ever_carries():
    """Two `<script>` elements would mean one of them is unhashed, and a
    CSP that names one hash refuses the other — so the count is the check,
    not the presence.
    """
    for kwargs in (
        {},
        {"quoted_html": "<p>b</p>"},
        {"quoted_html": "<p>b</p>", "expand": True},
        {"mail_css": ".m{color:red}", "theme": "dark", "restyle": "invert"},
    ):
        assert _render(**kwargs).count("<script>") == 1


def test_the_clamp_bounds_are_the_documented_pair():
    assert (fd.MIN_FRAME_HEIGHT, fd.MAX_FRAME_HEIGHT) == (200, 20_000)


# ---------------------------------------------------------------------------
# The CSP
# ---------------------------------------------------------------------------


def test_csp_is_exactly_the_agreed_policy():
    assert fd.csp_header() == (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
        "frame-ancestors 'self'; default-src 'none'; img-src 'self' data:; "
        "style-src 'unsafe-inline'; script-src '" + fd.FRAME_SCRIPT_HASH + "'"
    )


def test_the_csp_sandbox_never_grants_same_origin():
    """The one property that keeps a hostile message out of the reader's
    session. Asserted on the parsed token list, not on the raw string: a
    `not in` over the whole header would also pass if the directive were
    misspelled into inertness.
    """
    sandbox = _directives(fd.csp_header())["sandbox"]
    assert "allow-same-origin" not in sandbox
    assert sorted(sandbox) == sorted(
        ["allow-scripts", "allow-popups", "allow-popups-to-escape-sandbox"]
    )


def test_the_csp_sandbox_grants_scripts_so_the_hashed_script_can_run():
    """A document under both a CSP sandbox and an iframe sandbox gets the
    INTERSECTION of the two. The spec's own header omitted `allow-scripts`,
    which would have made the `script-src 'sha256-…'` beside it dead
    letter — the script could never run, and the frame could never report
    its height. The departure is recorded in the module docstring.
    """
    assert "allow-scripts" in _directives(fd.csp_header())["sandbox"]


def test_img_src_is_self_and_data_and_names_no_remote_host():
    """Every permitted remote image is rewritten to the same-origin proxy,
    so this header never needs a host — which means a sanitiser miss still
    cannot leak the reader's IP to a tracking pixel, because the browser is
    refused the request whether or not the `src` survived.
    """
    directives = _directives(fd.csp_header())
    assert directives["img-src"] == ["'self'", "data:"]
    for name, sources in directives.items():
        for source in sources:
            assert not source.startswith(("http:", "https:", "//")), (name, source)


def test_the_csp_denies_by_default_and_refuses_a_foreign_framer():
    directives = _directives(fd.csp_header())
    assert directives["default-src"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'self'"]
    # No connect-src/frame-src/font-src of their own: each falls through to
    # default-src 'none', which is stricter than any list we could write.
    assert set(directives) == {
        "sandbox",
        "frame-ancestors",
        "default-src",
        "img-src",
        "style-src",
        "script-src",
    }


def test_the_csp_is_the_same_string_every_time_it_is_asked_for():
    """`csp_header()` takes no arguments on purpose: nothing about a
    message, a theme or a remote-image decision may widen the policy.
    """
    assert fd.csp_header() == fd.csp_header()


# ---------------------------------------------------------------------------
# The quoted half
# ---------------------------------------------------------------------------


def test_quoted_half_renders_hidden_behind_a_toggle_only_when_present():
    with_quote = _render(quoted_html="<p>b</p>")
    tags = parse_attrs(with_quote)
    buttons = [a for a in tags.get("button", []) if "data-mailosh-quote-toggle" in a]
    containers = [a for a in tags.get("div", []) if "data-mailosh-quote" in a]
    assert len(buttons) == 1
    assert buttons[0]["aria-expanded"] == "false"
    assert len(containers) == 1
    assert "hidden" in containers[0]

    without = parse_attrs(_render(quoted_html=""))
    assert [a for a in without.get("button", []) if "data-mailosh-quote-toggle" in a] == []
    assert [a for a in without.get("div", []) if "data-mailosh-quote" in a] == []


def test_expand_renders_the_quote_open_with_no_toggle():
    doc = _render(quoted_html="<p>b</p>", expand=True)
    tags = parse_attrs(doc)
    assert [a for a in tags.get("button", []) if "data-mailosh-quote-toggle" in a] == []
    # Nothing in the whole document carries `hidden` — the quote is open,
    # and there is no control that could close it.
    assert [attrs for group in tags.values() for attrs in group if "hidden" in attrs] == []
    assert doc.index("<p>b</p>") > doc.index("<p>a</p>")


# ---------------------------------------------------------------------------
# The two stylesheets
# ---------------------------------------------------------------------------


def test_mail_css_is_placed_in_its_own_style_element_after_the_base_css():
    doc = _render(mail_css=".mail{color:red}", theme="dark")
    blocks = _styles(doc)
    assert len(blocks) == 2
    assert "max-width" in blocks[0] and ".mail{color:red}" not in blocks[0]
    assert blocks[1] == ".mail{color:red}"
    assert parse_attrs(doc)["meta"][2]["name"] == "color-scheme"


def test_mail_css_that_could_close_its_style_element_is_dropped():
    """`css_sanitize` already guarantees no `<` reaches here (its serializer
    decodes CSS escapes, so `content:"\\3c /style\\3e "` would otherwise
    close the element it is inlined into). This function is the last code
    between that string and a `<style>`, and drops the whole block rather
    than emitting a document whose script count it can no longer vouch for.
    """
    doc = _render(mail_css='p::before{content:"</style><script>alert(1)</script>"}')
    assert _styles(doc)[1] == ""
    assert doc.count("<script>") == 1


def test_the_scheme_follows_the_readers_theme():
    for theme, scheme in (("light", "light"), ("dark", "dark"), ("system", "light dark")):
        assert f"html{{color-scheme:{scheme}}}" in _styles(_render(theme=theme))[0]
    # An unrecognised theme is a query parameter typo, not a 500.
    assert "html{color-scheme:light dark}" in _styles(_render(theme="sepia"))[0]


def test_a_mail_that_declares_its_own_scheme_is_handed_both_and_left_alone():
    """Spec §7: a message with its own `color-scheme` support already knows
    how to be dark, so it is not pinned to the reader's theme.
    """
    for theme in ("light", "dark", "system"):
        base = _styles(_render(theme=theme, restyle="color-scheme"))[0]
        assert "html{color-scheme:light dark}" in base


def test_invert_rules_appear_only_for_that_restyle_and_only_under_dark():
    """The filter pair is Task 12's inversion. Under `theme="system"` the
    server cannot know how the OS will resolve, so the rules are wrapped in
    a `prefers-color-scheme: dark` media query rather than applied
    unconditionally — otherwise a light-mode reader would be shown a
    negative.
    """
    assert "filter:invert(1)" not in _styles(_render(theme="dark", restyle="none"))[0]

    dark = _styles(_render(theme="dark", restyle="invert"))[0]
    assert dark.count("filter:invert(1) hue-rotate(180deg)") == 2
    assert "@media" not in dark

    system = _styles(_render(theme="system", restyle="invert"))[0]
    assert system.count("@media (prefers-color-scheme: dark){") == 1
    assert system.count("filter:invert(1) hue-rotate(180deg)") == 2
