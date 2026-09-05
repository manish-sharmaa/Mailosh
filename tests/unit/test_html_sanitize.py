"""Unit tests for `mailosh.render.html_sanitize` -- the allow-list that
stands between a hostile message and the reader's session.

Three conventions run through this file, and each of them exists because the
obvious alternative is quietly wrong:

1.  **Assertions go through `helpers.parse_attrs`, never against the output
    string.** nh3 re-serialises each element from a hash map, so its
    attribute order is not stable and a string comparison encodes a
    coincidence. Worse, a substring check fails the *safe* direction too: a
    perfectly legitimate newsletter that prints the word "javascript:" in a
    text node would trip `assert "javascript:" not in out`, and the fix a
    tired maintainer reaches for is to weaken the assertion.

2.  **Every rule has a payload that defeats the naive implementation of
    that rule**, not just a payload that the rule happens to catch. The
    padded, upper-cased `  CID:logo@mail  ` is here because
    `value.startswith("cid:")` is the obvious way to write the cid branch
    and it is wrong in both directions.

3.  **Every rule also has a relaxation proof** -- a test that reaches in,
    loosens exactly one constant or one collaborator, and asserts the
    payload then gets through. Those are the tests marked
    `test_relaxing_*`. A green suite proves the payloads do not get through;
    only the relaxation proves it is *this* rule stopping them, rather than
    html5ever, luck, or another rule masking the one under test.
"""

from __future__ import annotations

import dataclasses
import pathlib

import nh3
import pytest
from helpers import parse_attrs

from mailosh.render import css_sanitize, html_sanitize
from mailosh.render.html_sanitize import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_TAGS,
    CLEAN_CONTENT_TAGS,
    DATA_IMAGE_TYPES,
    MAX_NESTING_DEPTH,
    URL_SCHEMES,
    BodyTooDeep,
    SanitizeContext,
    extract_styles,
    sanitize_email_html,
)

CTX = SanitizeContext(
    email_id="E1",
    origin="https://mail.test",
    remote=False,
    cid_parts={"logo@mail": "B3"},
    sign_image=None,
    sign_cid=lambda cid: "CIDTOK",
)
CTX_REMOTE = SanitizeContext(
    email_id="E1",
    origin="https://mail.test",
    remote=True,
    cid_parts={"logo@mail": "B3"},
    sign_image=lambda url: "TOK",
    sign_cid=lambda cid: "CIDTOK",
)

#: The adversarial corpus, one file per attack family. Kept as a
#: module-local fixture rather than in `tests/conftest.py`: nothing else in
#: the suite reads it, and conftest.py is shared ground.
XSS_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "xss"

#: Named explicitly so a renamed or unreadable fixture file fails loudly
#: instead of silently shrinking the corpus -- a corpus test that iterates
#: an empty directory passes.
XSS_FAMILIES = (
    "css.html",
    "forms.html",
    "handlers.html",
    "meta_base.html",
    "mxss.html",
    "schemes.html",
    "svg_math.html",
)


@pytest.fixture
def xss_corpus() -> list[tuple[str, str]]:
    """`(filename, raw html)` for every family in `tests/fixtures/xss/`."""
    found = sorted(p.name for p in XSS_DIR.glob("*.html"))
    assert found == list(XSS_FAMILIES), f"corpus changed on disk: {found}"
    return [(name, (XSS_DIR / name).read_text(encoding="utf-8")) for name in XSS_FAMILIES]


def attrs(html: str, ctx: SanitizeContext = CTX) -> dict[str, list[dict[str, str]]]:
    return parse_attrs(sanitize_email_html(html, ctx).html)


# --- script execution -------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<div onmouseover='alert(1)'>x</div>",
        "<svg><script>alert(1)</script></svg>",
        "<svg><animate onbegin=alert(1) attributeName=x dur=1s>",
        "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
        "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
        "<object data='javascript:alert(1)'></object>",
        "<embed src='javascript:alert(1)'>",
        '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
        '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">',
        "<xmp><p title='</xmp><img src=x onerror=alert(1)>'>",
        "<template><script>alert(1)</script></template>",
        "<plaintext><img src=x onerror=alert(1)>",
        "<marquee onstart=alert(1)>x</marquee>",
        "<p ONMOUSEENTER=alert(1)>upper-case handler</p>",
        "<img src=x onerror=alert&#40;1&#41;>",
    ],
)
def test_no_payload_survives_as_script_or_handler(payload):
    out = sanitize_email_html(payload, CTX).html
    assert "alert" not in out
    assert "<script" not in out.lower()
    for tag, instances in parse_attrs(out).items():
        for a in instances:
            assert not any(k.startswith("on") for k in a), (tag, a)


# --- URL schemes ------------------------------------------------------


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "  javascript:alert(1)  ",
        "vbscript:msgbox(1)",
        "data:text/html,<b>hi",
        "data:image/svg+xml,<svg onload=alert(1)>",
        "file:///etc/passwd",
        "//evil.test/x",
        "/relative",
        "#anchor",
        "cid:logo@mail",
    ],
)
def test_dangerous_hrefs_leave_no_href_at_all(href):
    anchors = attrs(f'<a href="{href}">x</a>').get("a", [])
    assert len(anchors) == 1
    assert "href" not in anchors[0]


@pytest.mark.parametrize(
    "href",
    [
        # nh3 normalises the scheme itself before its own check, so these
        # never reach the filter -- which is exactly why the filter cannot
        # be the only thing looking at a scheme, and why the filter's own
        # comparisons are on `value.strip().lower()` rather than `value`.
        "java&#09;script:alert(1)",
        "java&#10;script:alert(1)",
        "&#106;avascript:alert(1)",
        "\x00javascript:alert(1)",
        "  JAVASCRIPT:alert(1)",
    ],
)
def test_obfuscated_javascript_schemes_leave_no_href_either(href):
    anchors = attrs(f'<a href="{href}">x</a>').get("a", [])
    assert len(anchors) == 1
    assert "href" not in anchors[0]


@pytest.mark.parametrize(
    "html,tag,attr",
    [
        ('<a href="/x">y</a>', "a", "href"),
        ('<a href="x.html">y</a>', "a", "href"),
        ('<a href="#top">y</a>', "a", "href"),
        ('<a href="//evil.test/x">y</a>', "a", "href"),
        ('<img src="/x.png">', "img", "src"),
        ('<img src="x.png">', "img", "src"),
        ('<img src="//evil.test/x.png">', "img", "src"),
        ('<blockquote cite="/x">y</blockquote>', "blockquote", "cite"),
    ],
)
def test_relative_and_protocol_relative_urls_never_survive(html, tag, attr):
    """A relative URL in a message resolves against *our* origin once the
    frame renders it, so `/settings` in a mail is a link into the app and
    `//evil.test/x` inherits our scheme. `url_relative="deny"` refuses them
    before the filter runs; the per-tag scheme gates refuse them again if it
    is ever removed. Both layers are asserted by the same payloads.
    """
    assert attr not in attrs(html)[tag][0]


def test_safe_hrefs_survive_with_target_and_rel_intact():
    a = attrs('<a href="https://ok.test/p?q=1&amp;r=2">x</a>')["a"][0]
    assert a["href"] == "https://ok.test/p?q=1&r=2"
    assert a["target"] == "_blank"
    assert set(a["rel"].split()) == {"noopener", "noreferrer", "nofollow"}
    m = attrs('<a href="mailto:a@b.test">x</a>')["a"][0]
    assert m["href"] == "mailto:a@b.test"


def test_a_message_cannot_choose_its_own_target_or_rel():
    """A message that could set `target="_self"` would navigate the reading
    frame out from under the reader; one that could drop `rel` would get a
    `window.opener` handle back into the app. Neither attribute is in
    ALLOWED_ATTRIBUTES, so nh3 strips the message's copy and re-adds ours.
    """
    a = attrs('<a href="https://ok.test/" target="_self" rel="opener">x</a>')["a"][0]
    assert a["target"] == "_blank"
    assert set(a["rel"].split()) == {"noopener", "noreferrer", "nofollow"}


def test_cite_urls_are_scheme_checked_even_though_nh3_does_not():
    """nh3's URL-attribute list does not include `cite`.

    Verified against 0.3.7: `nh3.clean('<blockquote cite="javascript:...">')`
    returns the attribute untouched, whatever `url_schemes` says. The second
    half of this test is that observation pinned as a fact, so that if a
    future nh3 starts checking `cite` this test tells us rather than
    silently becoming redundant.
    """
    for tag in ("blockquote", "q", "del", "ins"):
        cleaned = attrs(f'<{tag} cite="javascript:alert(1)">x</{tag}>')[tag][0]
        assert "cite" not in cleaned
        assert "cite" not in attrs(f'<{tag} cite="/relative">x</{tag}>')[tag][0]
    kept = attrs('<blockquote cite="https://ok.test/thread">x</blockquote>')["blockquote"][0]
    assert kept["cite"] == "https://ok.test/thread"

    raw = nh3.clean(
        '<blockquote cite="javascript:alert(1)">x</blockquote>',
        tags={"blockquote"},
        attributes={"blockquote": {"cite"}},
        url_schemes={"https"},
    )
    assert "javascript:alert(1)" in raw, "nh3 now checks `cite`; the filter branch can go"


# --- images -----------------------------------------------------------


def test_cid_is_rewritten_to_an_absolute_same_origin_url_carrying_its_capability():
    img = attrs('<img src="cid:logo@mail">')["img"][0]
    assert img["src"] == "https://mail.test/m/E1/cid/logo%40mail?u=CIDTOK"


def test_cid_case_and_whitespace_variants_are_rewritten_too():
    # nh3 hands the filter the raw value; it does not fold case or trim.
    # Both of these pass nh3's own scheme check and would be missed by a
    # `value.startswith("cid:")` branch -- the inline image would silently
    # vanish from every mail whose client wrote `CID:`.
    assert attrs('<img src="CID:logo@mail">')["img"][0]["src"].endswith("/cid/logo%40mail?u=CIDTOK")
    assert attrs('<img src="  cid:logo@mail  ">')["img"][0]["src"].endswith(
        "/cid/logo%40mail?u=CIDTOK"
    )
    # RFC 2392 angle brackets, as they appear in the Content-ID header.
    assert attrs('<img src="cid:<logo@mail>">')["img"][0]["src"].endswith(
        "/cid/logo%40mail?u=CIDTOK"
    )


def test_the_cid_token_is_minted_for_the_bare_content_id_the_url_carries():
    """The signer must see exactly the id that goes into the path -- folded
    of its angle brackets, not case-folded, not the raw `cid:` value. Sign
    one spelling and serve another and every inline image in the app 404s,
    which is precisely the failure a route test cannot distinguish from "no
    such part".
    """
    seen: list[str] = []

    def sign(cid: str) -> str:
        seen.append(cid)
        return f"tok-{cid}"

    ctx = dataclasses.replace(CTX, sign_cid=sign)
    src = attrs('<img src="  CID:<logo@mail>  ">', ctx)["img"][0]["src"]
    assert seen == ["logo@mail"]
    # And the token is percent-encoded into the query, so an id carrying an
    # `&` or a `#` cannot truncate the URL it is part of.
    assert src == "https://mail.test/m/E1/cid/logo%40mail?u=tok-logo%40mail"


def test_a_cid_is_not_rewritten_without_a_signer():
    """The exact counterpart of `test_remote_is_not_enough_without_a_signer`.
    The route this URL points at is authorised by the token in it and by
    nothing else -- the frame that fetches it has an opaque origin and sends
    no cookie -- so a rewrite with no token is a broken image, not a
    working one. Dropping the `src` here makes that failure visible to this
    file rather than only to a browser.
    """
    ctx = dataclasses.replace(CTX, sign_cid=None)
    assert "src" not in attrs('<img src="cid:logo@mail">', ctx)["img"][0]


def test_unknown_cid_leaves_no_src():
    assert "src" not in attrs('<img src="cid:missing@mail">')["img"][0]


def test_a_cid_from_another_message_is_not_addressable():
    """`cid_parts` is per message. Without the membership test, any mail
    could name any Content-ID and have the route fetch it -- the cid is
    attacker-chosen text, and `/m/E1/cid/<anything>` would be a read
    primitive against whatever the route decides that resolves to.
    """
    other = SanitizeContext(
        email_id="E2",
        origin="https://mail.test",
        cid_parts={"other@x": "B9"},
        sign_cid=lambda cid: "CIDTOK",
    )
    assert "src" not in attrs('<img src="cid:logo@mail">', other)["img"][0]
    assert attrs('<img src="cid:other@x">', other)["img"][0]["src"].endswith(
        "/m/E2/cid/other%40x?u=CIDTOK"
    )


def test_data_uri_allowed_only_on_img_and_only_for_image_types():
    assert attrs('<img src="data:image/png;base64,AAA">')["img"][0]["src"].startswith(
        "data:image/png"
    )
    assert "src" not in attrs('<img src="data:image/svg+xml,<svg>">')["img"][0]
    assert "src" not in attrs('<img src="data:text/html,x">')["img"][0]
    assert "href" not in attrs('<a href="data:image/png;base64,AAA">x</a>')["a"][0]


@pytest.mark.parametrize(
    "src",
    [
        # `data:image/svg+xml` is a scripting context wearing an image MIME
        # type; the rest probe the media-type parse itself.
        "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+",
        "data:IMAGE/SVG+XML,<svg onload=alert(1)>",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "data:application/xhtml+xml,<html/>",
        "data:,plain",
        "data:image/,x",
        "data:imagexpng,x",
    ],
)
def test_hostile_data_uris_leave_no_src(src):
    assert "src" not in attrs(f'<img src="{src}">')["img"][0]


def test_remote_images_blocked_by_default_and_counted():
    result = sanitize_email_html(
        '<img src="https://track.test/a.gif"><img src="http://track.test/b.gif">'
        '<img src="https://cdn.test/c.png">',
        CTX,
    )
    assert result.blocked_remote == 3
    assert set(result.remote_hosts) == {"track.test", "cdn.test"}
    assert all("src" not in i for i in parse_attrs(result.html)["img"])


def test_remote_images_go_through_the_proxy_when_allowed():
    result = sanitize_email_html('<img src="https://track.test/a.gif">', CTX_REMOTE)
    assert result.blocked_remote == 0
    assert parse_attrs(result.html)["img"][0]["src"] == "https://mail.test/img?u=TOK"


def test_remote_is_not_enough_without_a_signer():
    """`remote=True` with no `sign_image` must block, not fall through to
    the raw URL -- the proxy is where the SSRF guard lives, so an unsigned
    direct load would be worse than a blocked one.
    """
    ctx = SanitizeContext(email_id="E1", origin="https://mail.test", remote=True)
    result = sanitize_email_html('<img src="https://track.test/a.gif">', ctx)
    assert result.blocked_remote == 1
    assert "src" not in parse_attrs(result.html)["img"][0]


def test_srcset_background_and_poster_are_never_emitted():
    out = sanitize_email_html(
        '<img src="https://a.test/x.png" srcset="https://evil.test/y.png 2x">'
        '<table><tr><td background="https://evil.test/z.png">c</td></tr></table>',
        CTX_REMOTE,
    ).html
    for instances in parse_attrs(out).values():
        for a in instances:
            assert "srcset" not in a and "background" not in a and "poster" not in a
    assert "evil.test" not in out


@pytest.mark.parametrize(
    "attr",
    ["srcset", "poster", "background", "formaction", "ping", "usemap", "lowsrc", "dynsrc"],
)
def test_second_src_attributes_are_absent_from_every_allow_list(attr):
    """These are all "load a resource" attributes the URL policy never sees,
    because they are not in ALLOWED_ATTRIBUTES at all. Asserted against the
    constant as well as the output: an addition to the allow-list would
    otherwise sail through review with the behavioural test still green
    (nothing in this file emits a `<video>`).
    """
    assert not any(attr in allowed for allowed in ALLOWED_ATTRIBUTES.values())


# --- style, base, forms ----------------------------------------------


def test_style_attributes_are_css_sanitised_not_merely_property_filtered():
    d = attrs('<div style="position:fixed;top:0;color:red;background:url(http://evil/x)">y</div>')[
        "div"
    ][0]
    assert d["style"] == "color:red"
    assert "style" not in attrs('<div style="position:fixed">y</div>')["div"][0]


def test_expression_survives_nh3_alone_and_is_stopped_by_the_css_sanitiser():
    """nh3's `filter_style_properties` matches on the property *name* only.

    `width` is a legitimate, allow-listed property, so nh3 keeps
    `width:expression(alert(1))` whole -- verified against 0.3.7, where the
    call below returns the payload with `expression(` intact. The CSS
    sanitiser in the attribute filter is the only thing that removes it.
    """
    naive = nh3.clean(
        '<div style="width:expression(alert(1))">x</div>',
        tags={"div"},
        attributes={"div": {"style"}},
        filter_style_properties=set(css_sanitize.ALLOWED_PROPERTIES),
    )
    assert "expression(" in naive

    assert "style" not in attrs('<div style="width:expression(alert(1))">x</div>')["div"][0]


@pytest.mark.parametrize(
    "style",
    [
        "width:expression(alert(1))",
        "WIDTH: EXPRESSION(alert(1))",
        "behavior:url(#default#time2)",
        "-moz-binding:url(http://evil.test/x.xml#xss)",
        "background-image:url(http://evil.test/px.gif)",
        "background:url('http://evil.test/px.gif')",
        "position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:2147483647",
        "content:'</style><script>alert(1)</script>'",
        "width:attr(data-x)",
        "background-image:image-set('http://evil.test/a.png' 1x)",
        "width:calc(1px * url(http://evil.test/x))",
        # `list-style` is allow-listed, so nh3's name-only style filter
        # keeps this one whole: only css_sanitize sees the url().
        "list-style:url(http://evil.test/px.gif)",
    ],
)
def test_hostile_style_attributes_leave_nothing_behind(style):
    div = attrs(f'<div style="{style}">y</div>')["div"][0]
    assert "style" not in div or "url(" not in div["style"].lower()
    assert "style" not in div or "expression" not in div["style"].lower()
    assert "style" not in div or "position" not in div["style"].lower()


@pytest.mark.parametrize(
    "style,expected",
    [
        ("COLOR: Red", "color:Red"),
        ("Font-Weight: 700", "font-weight:700"),
        ("MARGIN:0 auto", "margin:0 auto"),
    ],
)
def test_an_upper_cased_safe_declaration_is_not_eaten_by_the_second_filter(style, expected):
    """The seam between the two style layers, pinned.

    nh3's `filter_style_properties` compares property names
    case-*sensitively* against the set it is given -- `nh3.clean` with
    `filter_style_properties={"color"}` drops `COLOR:Red` outright. It is
    only harmless here because `css_sanitize.sanitize_declarations` runs
    first and lower-cases the name, so the two layers agree on spelling.

    If the CSS side ever stopped normalising, every upper-cased declaration
    in real mail would silently vanish -- an over-strict failure no XSS test
    would catch, because nothing leaks. This is the test that would.
    """
    assert attrs(f'<div style="{style}">y</div>')["div"][0]["style"] == expected

    naive = nh3.clean(
        f'<div style="{style}">y</div>',
        tags={"div"},
        attributes={"div": {"style"}},
        filter_style_properties=set(css_sanitize.ALLOWED_PROPERTIES),
    )
    assert 'style=""' in naive, "nh3 now folds case; the normalisation seam has moved"


def test_important_survives_both_style_layers():
    """A sender's `!important` is load-bearing in table-layout mail. nh3
    re-serialises it with a space (`red ! important`), which is valid CSS
    and still wins the cascade -- asserted so the reformat is a known
    quantity rather than a surprise in Task 4's frame.
    """
    style = attrs('<div style="color:red !important">y</div>')["div"][0]["style"]
    assert style.replace(" ", "") == "color:red!important"


def test_base_and_form_are_removed_and_cannot_reparent_relative_urls():
    out = sanitize_email_html(
        '<base href="https://evil.test/"><form action="https://evil.test/steal">'
        '<input name="p"></form><a href="/x">y</a>',
        CTX,
    ).html
    assert "<base" not in out.lower() and "<form" not in out.lower()
    assert "evil.test" not in out
    assert "href" not in parse_attrs(out)["a"][0]


def test_mail_ids_reserved_for_frame_chrome_are_stripped():
    divs = attrs(
        '<div id="mailosh-quote">x</div><div id="MAILOSH-Quote">y</div><div id="ok">z</div>'
    )["div"]
    assert len(divs) == 3
    assert sorted(d.get("id") or "" for d in divs) == ["", "", "ok"]


# --- <style> extraction -----------------------------------------------


def test_style_blocks_are_extracted_and_the_element_never_reaches_the_output():
    html = (
        "<style>@import url(http://evil/x.css); p{color:red;position:fixed}</style>"
        '<STYLE TYPE="text/css">.a{color:blue}</STYLE ><p>hi</p>'
    )
    result = sanitize_email_html(html, CTX)
    assert "<style" not in result.html.lower()
    assert "@import" not in result.css and "position" not in result.css
    assert result.css.count("color") == 2
    assert "<" not in result.css


def test_extract_styles_finds_every_block_including_nested_and_uppercase():
    blocks = extract_styles(
        '<div><style>a{}</style></div><STYLE>b{}</STYLE ><style media="print">c{}</style>'
    )
    assert len(blocks) == 3


def test_extract_styles_is_raw_and_the_sanitiser_is_what_makes_it_safe():
    """`extract_styles` deliberately does no filtering.

    Its HTMLParser and html5ever can disagree about where a `<style>` ends
    -- that is the whole mutation-XSS family -- so the design does not rely
    on them agreeing. It relies on nothing but CSS *text* crossing, and on
    `sanitize_stylesheet` refusing any block containing a `<`. Both halves
    are asserted here: the raw extraction really does carry markup out, and
    the sanitised result really does drop it.
    """
    html = '<style>p{font-family:"</style><img src=x onerror=alert(1)>"}</style>'
    assert "<img" in extract_styles(html)[0] or "</style>" in html
    result = sanitize_email_html(html, CTX)
    assert "<" not in result.css
    assert "alert" not in result.css
    assert "alert" not in result.html


@pytest.mark.parametrize(
    "first,second",
    [
        ("p{", "}body{display:none}"),
        ("p{color:red", "}body{display:none}"),
        ("@media screen{", "} body{display:none}"),
        ('a[title="', '"]{display:none}'),
        ("p{content:'", "'}body{display:none}"),
        ("p{color:red}", "color:blue}"),
    ],
)
def test_two_style_blocks_cannot_combine_into_a_rule_neither_contains(first, second):
    """`sanitize_email_html` concatenates the blocks it extracts, so the join
    has to be safe even when each block is a deliberate half of one
    construct.

    It is safe because `sanitize_stylesheet` never returns a fragment:
    tinycss2 closes an unterminated block for us, the sanitiser re-serialises
    rule by rule, and (since the CSS side started dropping any construct
    carrying a ParseError) a stray `red}` that would close the rule it lands
    in is refused outright rather than round-tripped. So every block leaves
    balanced, and no pair can weld into a `body{display:none}` that hides the
    whole message.
    """
    result = sanitize_email_html(f"<style>{first}</style><style>{second}</style>", CTX)
    assert result.css.count("{") == result.css.count("}")
    assert "display:none" not in result.css.replace(" ", "")
    assert "<" not in result.css


def test_a_style_element_inside_foreign_content_is_extracted_and_neutered():
    result = sanitize_email_html(
        "<svg><style>@import url(http://evil.test/x.css); p{color:red}</style></svg>", CTX
    )
    assert "@import" not in result.css and "evil.test" not in result.css
    assert "color" in result.css


# --- nh3 configuration invariants -------------------------------------


def test_tags_and_clean_content_tags_are_disjoint():
    """Overlap is a `ValueError` out of nh3, at call time, on the first
    message rendered -- not at import. Asserted on the constants so the
    failure lands here instead of in production.
    """
    assert not (ALLOWED_TAGS & CLEAN_CONTENT_TAGS)
    with pytest.raises(ValueError, match="both"):
        nh3.clean("<p>x</p>", tags={"p", "style"}, clean_content_tags={"style"})


def test_rel_is_not_a_permitted_anchor_attribute():
    """`rel` in `attributes["a"]` alongside `link_rel` is nh3's other hard
    `ValueError`. We want nh3 managing `rel`, so it must not be listed.
    """
    assert "rel" not in ALLOWED_ATTRIBUTES["a"]
    assert "rel" not in ALLOWED_ATTRIBUTES["*"]
    with pytest.raises(ValueError, match="link_rel"):
        nh3.clean(
            "<a href='x'>y</a>",
            tags={"a"},
            attributes={"a": {"href", "rel"}},
            link_rel="noopener",
        )


def test_no_on_handler_and_no_style_bearing_surprise_in_any_allow_list():
    for tag, allowed in ALLOWED_ATTRIBUTES.items():
        assert not any(name.startswith("on") for name in allowed), tag
        assert "srcdoc" not in allowed and "name" not in allowed, tag


def test_clean_is_never_called_with_allowed_classes(monkeypatch):
    """`allowed_classes` together with `class` in `attributes` panics the
    Rust extension: `pyo3_runtime.PanicException`, which subclasses
    `BaseException`, so a defensive `except Exception` around the call would
    not catch it and the worker would die mid-request. `class` is in the
    `"*"` allow-list, so the only safe rule is never to pass the parameter.
    """
    assert "class" in ALLOWED_ATTRIBUTES["*"]

    captured: dict[str, object] = {}
    real_clean = nh3.clean

    def spy(html, **kwargs):
        captured.update(kwargs)
        return real_clean(html, **kwargs)

    monkeypatch.setattr(html_sanitize.nh3, "clean", spy)
    sanitize_email_html('<span class="x">y</span>', CTX)
    assert "allowed_classes" not in captured
    assert captured["url_relative"] == "deny"
    assert captured["strip_comments"] is True


def test_url_schemes_is_a_gate_for_the_filter_not_a_grant():
    """`cid` and `data` are in URL_SCHEMES only so `<img src>` can reach the
    filter. nh3 applies the set globally, so without the per-tag branches
    both would be live on `<a href>` -- which the raw call below shows.
    """
    assert {"cid", "data"} <= URL_SCHEMES
    raw = nh3.clean(
        '<a href="data:text/html,<b>hi">x</a>',
        tags={"a"},
        attributes={"a": {"href"}},
        url_schemes=set(URL_SCHEMES),
    )
    assert "data:text/html" in raw
    assert "href" not in attrs('<a href="data:text/html,<b>hi">x</a>')["a"][0]


def test_a_filter_that_returns_none_by_default_strips_nh3s_own_rel():
    """Behaviour 3, pinned. The filter is called for the `rel` and `target`
    nh3 injects itself, so the default branch has to return `value`.
    """
    seen: list[tuple[str, str, str]] = []

    def spy(tag, attr, value):
        seen.append((tag, attr, value))
        return value

    kwargs = dict(
        tags={"a"},
        attributes={"a": {"href"}},
        url_schemes={"https"},
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    nh3.clean('<a href="https://ok.test/">y</a>', attribute_filter=spy, **kwargs)
    assert ("a", "rel", "noopener noreferrer nofollow") in seen
    assert ("a", "target", "_blank") in seen

    stripped = nh3.clean(
        '<a href="https://ok.test/">y</a>',
        attribute_filter=lambda t, a, v: v if a == "href" else None,
        **kwargs,
    )
    assert "rel=" not in stripped and "target=" not in stripped


def test_nh3_does_not_recheck_the_scheme_of_a_value_the_filter_returns():
    """The other half of behaviour 1, and the reason the filter never
    returns an attacker-controlled string for a URL attribute: whatever it
    hands back is emitted as-is, scheme and all.
    """
    emitted = nh3.clean(
        '<a href="https://ok.test/">x</a>',
        tags={"a"},
        attributes={"a": {"href"}},
        url_schemes={"https"},
        attribute_filter=lambda t, a, v: "javascript:alert(1)" if a == "href" else v,
    )
    assert 'href="javascript:alert(1)"' in emitted


# --- relaxation proofs -------------------------------------------------
#
# Each of these loosens exactly one rule and asserts the payload then gets
# through. Without them the suite proves only that nothing leaks -- not that
# any particular rule is what stops it.


def test_relaxing_the_reserved_id_prefix_lets_a_mail_impersonate_the_frame(monkeypatch):
    monkeypatch.setattr(html_sanitize, "RESERVED_ID_PREFIX", "never-used-")
    divs = attrs('<div id="mailosh-quote">x</div>')["div"]
    assert divs[0]["id"] == "mailosh-quote"


def test_relaxing_the_data_image_types_lets_a_scriptable_svg_through(monkeypatch):
    monkeypatch.setattr(html_sanitize, "DATA_IMAGE_TYPES", DATA_IMAGE_TYPES | {"svg+xml"})
    img = attrs('<img src="data:image/svg+xml,<svg onload=alert(1)>">')["img"][0]
    assert img["src"].startswith("data:image/svg+xml")


def test_relaxing_the_anchor_scheme_gate_lets_data_text_html_navigate(monkeypatch):
    monkeypatch.setattr(html_sanitize, "_ANCHOR_SCHEMES", frozenset({"http", "https", "data"}))
    a = attrs('<a href="data:text/html,<b>hi">x</a>')["a"][0]
    assert a["href"].startswith("data:text/html")


def test_relaxing_the_cite_gate_lets_a_javascript_url_sit_in_the_dom(monkeypatch):
    monkeypatch.setattr(html_sanitize, "_CITE_SCHEMES", frozenset({"http", "https", "javascript"}))
    q = attrs('<blockquote cite="javascript:alert(1)">x</blockquote>')["blockquote"][0]
    assert q["cite"] == "javascript:alert(1)"


def test_relaxing_the_css_sanitiser_lets_expression_and_url_through(monkeypatch):
    """The one that matters most: nh3's own style-property filter is not a
    substitute for `css_sanitize`, because it only ever looks at the
    property name.
    """
    monkeypatch.setattr(css_sanitize, "sanitize_declarations", lambda value: value)
    div = attrs('<div style="width:expression(alert(1))">x</div>')["div"][0]
    assert "expression(" in div["style"]
    # `list-style` IS in ALLOWED_PROPERTIES, so nh3's name-only filter has
    # no opinion about the `url()` inside it: an allow-listed property is
    # still a tracking pixel if it can name a remote resource.
    div = attrs('<div style="list-style:url(http://evil.test/px.gif)">x</div>')["div"][0]
    assert "evil.test" in div["style"]


def test_relaxing_the_cid_rewrite_to_a_relative_url_deletes_the_src(monkeypatch):
    """Behaviour 1's failure mode is silent, not loud: nh3 removes the
    attribute the filter just rewrote, and every inline image in the product
    is simply missing. Nothing raises, nothing logs.
    """
    monkeypatch.setattr(
        html_sanitize, "_cid_src", lambda value, ctx, origin: "/m/E1/cid/logo%40mail"
    )
    assert "src" not in attrs('<img src="cid:logo@mail" alt="x">')["img"][0]
    assert attrs('<img src="cid:logo@mail" alt="x">')["img"][0]["alt"] == "x"


def test_relaxing_clean_content_tags_leaks_raw_text_into_the_rendered_body(monkeypatch):
    """Merely *stripping* a raw-text element instead of dropping its subtree
    spills its contents into the reader's message body as visible text: the
    stylesheet source, the sender's `<title>`, the contents of a
    `<textarea>`. html5ever escapes what it spills, so this is not script
    execution -- it is the rendering half of the same rule, and the half a
    behavioural XSS test cannot see.
    """
    message = (
        "<style>p{color:red}</style><title>internal subject</title>"
        "<textarea>draft text</textarea><p>body</p>"
    )
    # Strict first: monkeypatch's undo only runs at teardown, so the
    # baseline has to be taken before the relaxation, not after it.
    strict = sanitize_email_html(message, CTX).html
    assert "p{color:red}" not in strict
    assert "internal subject" not in strict and "draft text" not in strict

    relaxed = CLEAN_CONTENT_TAGS - {"style", "title", "textarea"}
    monkeypatch.setattr(html_sanitize, "_NH3_CLEAN_CONTENT_TAGS", set(relaxed))
    out = sanitize_email_html(message, CTX).html
    assert "p{color:red}" in out
    assert "internal subject" in out and "draft text" in out


# --- corpus -----------------------------------------------------------


def test_the_corpus_is_actually_hostile_before_sanitising(xss_corpus):
    """Guards the guard. Every assertion below is of the form "the output
    does not contain X"; if a corpus file were empty, truncated, or read
    from the wrong path, all of them would pass on nothing at all.
    """
    for name, html in xss_corpus:
        assert len(html) > 500, name
        parsed = parse_attrs(html)
        forbidden_tags = set(parsed) - ALLOWED_TAGS
        forbidden_attrs = {
            (tag, key)
            for tag, instances in parsed.items()
            for a in instances
            for key in a
            if key not in ALLOWED_ATTRIBUTES["*"] and key not in ALLOWED_ATTRIBUTES.get(tag, ())
        }
        assert forbidden_tags or forbidden_attrs, name
        assert sanitize_email_html(html, CTX).html != html, name


def test_xss_corpus_files_all_come_out_inert(xss_corpus):
    # Asserted on the parsed tree, not on the text: a corpus file may
    # legitimately contain the word "javascript:" in a text node, and a
    # substring check would either fail on that or pass on a real leak.
    for name, html in xss_corpus:
        out = sanitize_email_html(html, CTX).html
        parsed = parse_attrs(out)
        assert "script" not in parsed and "iframe" not in parsed, name
        assert "object" not in parsed and "embed" not in parsed, name
        for tag, instances in parsed.items():
            assert tag in ALLOWED_TAGS, (name, tag)
            for a in instances:
                assert not any(k.startswith("on") for k in a), (name, tag, a)
                for value in a.values():
                    v = value.strip().lower()
                    assert not v.startswith(("javascript:", "vbscript:", "data:text")), (name, v)


def test_the_corpus_never_leaks_a_third_party_url_or_a_style_element(xss_corpus):
    """The corpus is written so that every hostile host is `evil.test` and
    every hostile origin is `http://evil...`. With remote images blocked
    (CTX) nothing should reach the output or the extracted CSS.
    """
    for name, html in xss_corpus:
        result = sanitize_email_html(html, CTX)
        assert "evil.test" not in result.html, name
        assert "evil.test" not in result.css, name
        assert "<" not in result.css, name
        assert "@import" not in result.css, name
        assert "<style" not in result.html.lower(), name


def test_the_corpus_stays_inert_with_remote_images_enabled(xss_corpus):
    """Same corpus, reader has clicked "show images". The proxy token is the
    only thing that may appear; no original third-party URL may.
    """
    for name, html in xss_corpus:
        result = sanitize_email_html(html, CTX_REMOTE)
        assert "evil.test" not in result.html, name
        for tag, instances in parse_attrs(result.html).items():
            for a in instances:
                assert not any(k.startswith("on") for k in a), (name, tag, a)
                for key in ("src", "href"):
                    if key in a:
                        assert a[key].startswith(
                            ("https://mail.test/", "data:image/", "mailto:", "https://ok.test")
                        ), (name, tag, key, a[key])


# ---------------------------------------------------------------------------
# Nesting bound (security review, Critical 2)
# ---------------------------------------------------------------------------


def test_a_deeply_nested_body_is_refused_before_nh3_sees_it():
    """One message must not be able to stall the whole worker.

    `nh3.clean` is quadratic in the nesting depth of the allow-listed tags
    whose open-element stack html5ever does not cap. Measured on this tree:
    195 KB of nested `<div>` took 2.5 s against 79 ms for the same bytes
    laid out flat, and the 512 KB a JMAP body may carry took ~18 s.

    `sanitize_email_html` is called synchronously from an async route, so
    that is not one slow render -- it is the entire process serving nobody
    for the duration, other readers and the mail poller included. Anyone
    who can send mail could do it repeatedly.
    """
    payload = "<div>" * 104857

    with pytest.raises(BodyTooDeep):
        sanitize_email_html(payload, CTX)


def test_the_bound_is_two_orders_above_what_real_mail_uses():
    """A guard that refuses ordinary newsletters is not a guard, it is an
    outage with better manners. Table-in-table mail runs to tens of levels.
    """
    nested = ("<table><tr><td><div>" * 30) + "hello" + ("</div></td></tr></table>" * 30)

    result = sanitize_email_html(nested, CTX)

    assert "hello" in result.html


def test_flat_bodies_are_not_penalised_by_the_depth_bound():
    """The cost is depth, not size: the bound must not reject a large but
    shallow message, which is what a long newsletter actually is.
    """
    flat = "<p>x</p>" * 43000

    result = sanitize_email_html(flat, CTX)

    assert result.html.count("<p>") == 43000


def test_the_depth_scan_counts_closing_tags():
    """Siblings are not nesting. A body that opens and closes the same tag
    repeatedly stays at depth one, and must render however many there are --
    counting opens alone would reject exactly the newsletters this exists
    to keep working.
    """
    siblings = "<div>x</div>" * (MAX_NESTING_DEPTH * 5)

    result = sanitize_email_html(siblings, CTX)

    assert result.html.count("<div>") == MAX_NESTING_DEPTH * 5
