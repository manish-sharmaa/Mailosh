import pytest
import tinycss2
from tinycss2 import ast

from mailosh.render import css_sanitize as css


def parse(out):
    """Parse sanitiser output back into {property: value} so tests assert on
    declarations, not on serialisation whitespace."""
    return {
        d.lower_name: tinycss2.serialize(d.value).strip()
        for d in tinycss2.parse_blocks_contents(out, skip_comments=True, skip_whitespace=True)
        if getattr(d, "lower_name", None)
    }


def _walk(nodes, prefix, into):
    for node in nodes:
        if isinstance(node, ast.QualifiedRule):
            selector = tinycss2.serialize(node.prelude).strip()
            for name, value in parse(node.content).items():
                into.append(((*prefix, selector), name, value))
        elif isinstance(node, ast.AtRule):
            at = f"@{node.lower_at_keyword} {tinycss2.serialize(node.prelude).strip()}".strip()
            inner = tinycss2.parse_rule_list(
                node.content or [], skip_comments=True, skip_whitespace=True
            )
            _walk(inner, (*prefix, at), into)
        else:
            # Anything that is neither a rule nor an at-rule means the sanitiser
            # emitted text that does not re-parse as CSS. Surface it as a row so
            # a structural assertion below fails loudly instead of ignoring it.
            into.append((prefix, f"!{type(node).__name__}", tinycss2.serialize([node])))
    return into


def sheet(out):
    """Sanitiser output re-parsed into (context, property, value) triples, where
    context is the @media chain plus the selector the declaration landed in."""
    return _walk(tinycss2.parse_stylesheet(out, skip_comments=True, skip_whitespace=True), (), [])


# Payloads that must never produce output containing "<". Anything added here is
# checked by test_no_payload_can_put_an_angle_bracket_in_a_style_element against
# both entry points, so a new vector only has to be written down once.
BREAKOUT_CORPUS = (
    r'p::before { content: "\3c /style\3e \3c img src=x onerror=alert(1)\3e " }',
    r'p { font-family: "\3c /style\3e \3c script\3e alert(1)\3c /script\3e " }',
    'p { font-family: "</style><script>alert(1)</script>" }',
    r'p { font-family: "a\3c /style>b" }',
    r'a[title="</style><img src=x onerror=alert(1)>"] { color: red }',
    r'a[title="\3c /style\3e "] { color: red }',
    r"\3c /style\3e p { color: red }",
    r"@media \3c /style\3e screen { p { color: red } }",
    r"@media screen and (max-width: \3c /style\3e ) { p { color: red } }",
    "p{color:red} </style><script>alert(1)</script> div{color:blue}",
    "color: red; font-family: '</style><svg onload=alert(1)>'",
    r"font-family: '\3c /style\3e '",
    "color: <!-- red",
    "font-family: a<!--b",
    r"font-family: \3c",
)


@pytest.mark.parametrize(
    "payload",
    [
        "position: fixed",
        "position: sticky",
        "width: expression(alert(1))",
        "behavior: url(#default#time2)",
        "-moz-binding: url(http://evil/x.xml)",
        "background-image: url(http://evil/px.gif)",
        "background: url('http://evil/px.gif')",
        "color: rgb(0,0,0); background-image: URL(http://evil/px.gif)",
        "content: '</style><script>alert(1)</script>'",
        "width: attr(data-x)",
        "background-image: image-set('http://evil/a.png' 1x)",
        "top: 0; left: 0; z-index: 99999",
    ],
)
def test_declaration_payloads_are_dropped(payload):
    out = css.sanitize_declarations(payload)
    decls = parse(out)
    assert "position" not in decls and "background-image" not in decls
    assert "behavior" not in decls and "-moz-binding" not in decls
    assert "content" not in decls and "top" not in decls and "z-index" not in decls
    assert "url(" not in out.lower()
    assert "expression" not in out.lower()
    assert "<" not in out


def test_safe_declarations_survive_with_important_and_case_folding():
    out = css.sanitize_declarations("COLOR: Red !important; Font-Weight: 700; margin:0 auto")
    decls = parse(out)
    assert decls["color"].lower() == "red"
    assert decls["font-weight"] == "700"
    assert decls["margin"] == "0 auto"
    assert len(decls) == 3


def test_empty_result_is_empty_string_not_whitespace():
    assert css.sanitize_declarations("position:fixed") == ""
    assert css.sanitize_declarations("") == ""
    assert css.sanitize_declarations("}}}garbage{{{") == ""


def test_stylesheet_drops_import_fontface_and_keeps_media():
    out = css.sanitize_stylesheet(
        '@charset "utf-8";'
        '@import url("http://evil/x.css");'
        "@font-face { font-family: E; src: url(http://evil/f.woff) }"
        "@namespace svg url(http://www.w3.org/2000/svg);"
        "@media screen and (max-width: 600px) { .a { color: red; position: fixed } }"
        "p { color: blue }"
    )
    assert "@import" not in out and "@font-face" not in out
    assert "@charset" not in out and "@namespace" not in out
    assert "@media screen and (max-width: 600px)" in out
    assert "position" not in out
    assert out.count("color") == 2


def test_stylesheet_cannot_break_out_of_the_style_element():
    # tinycss2's serializer decodes \3c back to a literal "<" and never
    # re-escapes it, so a string literal is a real </style> breakout vector.
    for payload in [
        'p { font-family: "</style><script>alert(1)</script>" }',
        'p { font-family: "a\\3c /style>b" }',
        'a[title="</style><img src=x onerror=alert(1)>"] { color: red }',
    ]:
        out = css.sanitize_stylesheet(payload)
        assert "<" not in out
        assert "script" not in out.lower()


def test_nested_media_is_recursed_not_passed_through():
    out = css.sanitize_stylesheet(
        "@media print { @import url(http://evil/x.css); p { color: red } }"
    )
    assert "@import" not in out and "evil" not in out
    assert "color" in out


def test_oversized_input_is_refused_whole():
    assert css.sanitize_stylesheet("p{color:red}" + "/*" + "x" * css.MAX_CSS_BYTES + "*/") == ""
    assert css.sanitize_declarations("color:red;" + "a" * css.MAX_CSS_BYTES) == ""


def test_a_breakout_swallows_the_rule_it_lands_in_and_nothing_else_leaks():
    # tinycss2 parses everything after `p{...}` as ONE qualified rule whose
    # prelude is `</style><script>alert(1)</script> div`. The `<` in that
    # prelude drops the whole rule -- including the `div` selector welded to
    # it -- which is the correct trade: losing one rule beats emitting a `<`.
    out = css.sanitize_stylesheet("p{color:red} </style><script>alert(1)</script> div{color:blue}")
    assert "<" not in out and "script" not in out.lower()
    assert out.count("color") == 1
    assert "div" not in out


# --- the escape-decoding breakout, from every position it can be written ---


@pytest.mark.parametrize("payload", BREAKOUT_CORPUS)
def test_no_payload_can_put_an_angle_bracket_in_a_style_element(payload):
    assert "<" not in css.sanitize_stylesheet(payload)
    assert "<" not in css.sanitize_declarations(payload)


def test_escape_decoding_is_the_reason_the_angle_bracket_rule_exists():
    # Guard on the tinycss2 behaviour the module is written against: if a
    # future release starts re-escaping "<" inside string tokens, this fails
    # and the "<" rule can be revisited rather than silently kept as folklore.
    src = r'p::before { content: "\3c /style\3e " }'
    assert "</style>" in tinycss2.serialize(
        tinycss2.parse_stylesheet(src, skip_comments=True, skip_whitespace=True)
    )
    assert css.sanitize_stylesheet(src) == ""


def test_the_last_resort_guard_discards_the_whole_result():
    # The guard the module ends with, on its own. It is deliberately redundant
    # with the per-declaration and per-prelude checks -- it exists for the
    # payload nobody thought of -- so it needs a test of its own rather than
    # borrowing coverage from the layers in front of it.
    assert css._no_angle_bracket("p{color:red}") == "p{color:red}"
    assert css._no_angle_bracket("p{color:red}</style><script>alert(1)</script>") == ""


@pytest.mark.parametrize("payload", BREAKOUT_CORPUS)
def test_the_angle_bracket_checks_hold_with_the_last_resort_disabled(payload, monkeypatch):
    # The other half of that redundancy: with the last resort turned into a
    # pass-through, the per-declaration and per-prelude checks must still keep
    # every corpus payload from putting a "<" in the document.
    monkeypatch.setattr(css, "_no_angle_bracket", lambda result: result)
    assert "<" not in css.sanitize_stylesheet(payload)
    assert "<" not in css.sanitize_declarations(payload)


def test_the_emitted_property_name_is_the_allow_list_entry_not_the_senders_bytes():
    # Nothing is copied out of the input: the name written is the ASCII
    # allow-list entry that just matched, and `!important` is a constant.
    assert css.sanitize_declarations("CoLoR: red") == "color:red"
    assert css.sanitize_declarations("MARGIN-Top: 0 !IMPORTANT") == "margin-top:0 !important"
    assert css.sanitize_stylesheet("P.Foo { CoLoR: red }") == "P.Foo{color:red}"


def test_an_escaped_identifier_still_serialises_a_literal_angle_bracket():
    # serialize_identifier() escapes "<" as "\<" -- still a literal "<" byte in
    # the <style> element, so the rule has to reject it too.
    assert css.sanitize_stylesheet(r"a\3c b { color: red }") == ""
    assert css.sanitize_declarations(r"font-family: a\3c b") == ""


def test_a_media_query_cannot_smuggle_a_breakout_through_its_prelude():
    out = css.sanitize_stylesheet(
        r"@media screen and (min-width: 1px), \3c /style\3e { p { color: red } }"
    )
    assert out == ""


# --- ParseError nodes re-serialise attacker bytes verbatim ---


@pytest.mark.parametrize(
    ("payload", "leak"),
    [
        ("color: red}", "}"),  # closes the rule this lands in
        ("color: red)", ")"),
        ("width: 1px]", "]"),
        ('color: url(a"b)', "url("),  # ParseError(bad-url) serialises as "url([bad url])"
        ('font-family: "unterminated', '"'),  # ParseError(eof-in-string): unterminated string
    ],
)
def test_parse_errors_inside_a_value_drop_the_declaration(payload, leak):
    # A naive `serialize(d.value)` re-emits these: ParseError._serialize_to
    # writes the raw delimiter back ("}"), or a synthetic "url([bad url])".
    out = css.sanitize_declarations(payload)
    assert out == ""
    assert leak not in out


def test_a_parse_error_only_drops_its_own_declaration():
    out = css.sanitize_declarations("color: red; width: 1px}")
    assert parse(out) == {"color": "red"}


def test_a_parse_error_in_a_selector_drops_the_rule():
    for payload in ["p} { color: red }", "p) { color: red }", "p] { color: red }"]:
        assert css.sanitize_stylesheet(payload) == ""


def test_a_parse_error_in_a_media_prelude_drops_the_at_rule():
    assert css.sanitize_stylesheet("@media screen) { p { color: red } }") == ""


# --- the property allow-list ---


def test_allow_list_is_an_allow_list_of_exactly_the_agreed_shape():
    assert len(css.ALLOWED_PROPERTIES) == 70
    assert css.ALLOWED_FUNCTIONS == frozenset(
        {"rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp", "var"}
    )
    assert css.ALLOWED_AT_RULES == frozenset({"media"})
    assert css.MAX_CSS_BYTES == 512 * 1024
    assert all(p == p.lower() and p.isascii() for p in css.ALLOWED_PROPERTIES)


@pytest.mark.parametrize(
    "prop",
    [
        "position",
        "top",
        "right",
        "bottom",
        "left",
        "z-index",
        "background",
        "background-image",
        "behavior",
        "-moz-binding",
        "filter",
        "content",
        "transform",
        "animation",
        "transition",
        "cursor",
        "pointer-events",
        "src",
        "all",
        # Properties whose whole purpose is to name a resource. Widening the
        # border longhands does not reach these, and nothing should: unlike a
        # longhand, none of them is a component of an allow-listed shorthand
        # (`border` cannot set `border-image-source`, `list-style` cannot set
        # `list-style-image`), so each would be a new capability rather than a
        # second spelling of one already granted.
        "border-image",
        "border-image-source",
        "border-image-slice",
        "list-style-image",
        "mask",
        "mask-image",
        "shape-outside",
    ],
)
def test_properties_that_must_not_be_on_the_allow_list(prop):
    assert prop not in css.ALLOWED_PROPERTIES
    assert css.sanitize_declarations(f"{prop}: inherit") == ""


@pytest.mark.parametrize("prop", sorted(css.ALLOWED_PROPERTIES))
def test_every_allowed_property_actually_survives(prop):
    assert parse(css.sanitize_declarations(f"{prop}: inherit")) == {prop: "inherit"}


def test_property_names_are_matched_after_case_and_escape_folding():
    # tinycss2 decodes escapes in property names, so the allow-list sees the
    # decoded name -- both of these are `position` and both must be dropped.
    assert css.sanitize_declarations("POSITION: fixed") == ""
    assert css.sanitize_declarations("posit\\69 on: fixed") == ""
    assert css.sanitize_declarations("\\70 osition: fixed") == ""
    assert parse(css.sanitize_declarations("CoLoR: red")) == {"color": "red"}


# --- the border longhand grid, and why widening to it granted nothing ---
#
# `border-bottom: solid #E2E2E2 1.0pt` was always allowed and
# `border-bottom-{width,style,color}` was not, so real mail lost a rule
# depending only on which spelling its client chose -- twelve declarations
# across four of the six messages in `tests/fixtures/mail/corpus/`, including
# two rows of one Word table where the first row keeps its underline and the
# second does not. The tests below are the safety half of that change: not
# "the longhands are allowed now" but "allowing them moved nothing".

#: The full grid, in both spellings. Parametrised from the same comprehension
#: the allow-list is written out from, so a missing corner fails rather than
#: going untested.
BORDER_LONGHANDS = [
    f"border-{side}-{prop}"
    for side in ("top", "right", "bottom", "left")
    for prop in ("width", "style", "color")
]

#: Every way a value can name something outside the document or ask for
#: evaluation, written the ways that have historically slipped past a naive
#: filter: unquoted (a `URLToken`, not a function), quoted (a `FunctionBlock`),
#: upper-cased, escape-folded, and buried inside a function that *is*
#: allow-listed.
RESOURCE_VALUES = (
    "url(https://track.example/p.gif)",
    'url("https://track.example/p.gif")',
    "url('https://track.example/p.gif')",
    "URL(https://track.example/p.gif)",
    "Url( https://track.example/p.gif )",
    "\\75 rl(https://track.example/p.gif)",
    "url(javascript:alert(1))",
    "url(#default#VML)",
    "expression(alert(1))",
    "attr(data-x)",
    "image-set('https://track.example/p.gif' 1x)",
    "-moz-element(#x)",
    "element(#x)",
    "calc(url(https://track.example/p.gif))",
    "var(--x, url(https://track.example/p.gif))",
    "rgb(1, expression(alert(1)), 2)",
)


def test_the_border_longhand_grid_is_complete_and_every_corner_is_allowed():
    """All twelve, not the six the corpus happened to use.

    The gap was `border-{top,bottom}-*`; `border-{left,right}-*` are the same
    property with a different side keyword, and leaving them out would have
    left the same inconsistency in place for the next message to hit.
    """
    assert len(BORDER_LONGHANDS) == 12
    for longhand in BORDER_LONGHANDS:
        assert longhand in css.ALLOWED_PROPERTIES
        # ...and the shorthand it is a component of was already allowed, which
        # is the whole argument for allowing it.
        assert longhand.rsplit("-", 1)[0] in css.ALLOWED_PROPERTIES
        assert longhand.rsplit("-", 2)[0] in css.ALLOWED_PROPERTIES


def test_a_rule_survives_the_same_whichever_spelling_the_client_chose():
    """The defect, stated as the property it violated.

    Word writes one table row's underline as the shorthand and the next as
    three longhands. Both are the same 1pt line on screen and both must
    survive, or the reader sees one row ruled and the row under it bare.
    """
    shorthand = parse(css.sanitize_declarations("border-bottom:solid #E2E2E2 1.0pt"))
    longhand = parse(
        css.sanitize_declarations(
            "border-bottom-width:1.0pt;border-bottom-style:solid;border-bottom-color:#F2F2F2"
        )
    )
    assert shorthand == {"border-bottom": "solid #E2E2E2 1.0pt"}
    assert longhand == {
        "border-bottom-width": "1.0pt",
        "border-bottom-style": "solid",
        "border-bottom-color": "#F2F2F2",
    }


@pytest.mark.parametrize("longhand", BORDER_LONGHANDS)
@pytest.mark.parametrize("value", RESOURCE_VALUES)
def test_no_border_longhand_can_carry_a_fetch_or_an_evaluation(longhand, value):
    """The claim the widening rests on, tested rather than asserted.

    A property name on the allow-list buys a declaration nothing except the
    right to be *considered*: `_unsafe_values` still has to pass its value, and
    it rejects every `URLToken`, every `FunctionBlock` outside
    `ALLOWED_FUNCTIONS` at any nesting depth, and every `ParseError` -- without
    ever looking at which property it belongs to.
    """
    out = css.sanitize_declarations(f"{longhand}:{value}")
    assert out == ""
    assert "track.example" not in out and "url" not in out.lower()


@pytest.mark.parametrize("prop", sorted(css.ALLOWED_PROPERTIES))
def test_the_value_filter_does_not_care_which_property_it_is_filtering(prop):
    """The mechanism behind the test above, checked across the whole list.

    This is what makes "a longhand grants no capability its shorthand did not"
    a structural fact rather than a per-property audit: `_unsafe_values` takes
    a component-value list and nothing else, so there is no property for which
    a `url()` is treated differently. If a future change ever made the value
    rules property-dependent, this fails for the property it exempted.
    """
    assert css.sanitize_declarations(f"{prop}: url(https://track.example/p.gif)") == ""
    assert css.sanitize_declarations(f"{prop}: expression(alert(1))") == ""
    assert css.sanitize_declarations(f"{prop}: attr(data-x)") == ""


@pytest.mark.parametrize("longhand", BORDER_LONGHANDS)
def test_the_border_longhands_are_filtered_identically_inside_a_stylesheet(longhand):
    """Both entry points, because a `<style>` block is where the table
    newsletter writes its masthead rule and the attribute path is where Word
    writes the same rule again.
    """
    assert css.sanitize_stylesheet(f"td {{ {longhand}: url(https://track.example/p.gif) }}") == ""
    assert css.sanitize_stylesheet(f"td {{ {longhand}: inherit }}") == f"td{{{longhand}:inherit}}"


def test_a_border_longhand_cannot_reach_a_custom_property_the_sender_defined():
    """`var()` is allow-listed, so a longhand may carry one -- and that is
    still not a way out.

    A sender cannot define the custom property it would read: `--x` is not on
    the allow-list (`test_custom_properties_are_dropped`), and the frame
    document declares none of its own, so `var(--x)` resolves to nothing and
    the browser drops the declaration at computed-value time. What it can
    never do is fetch: none of `width`/`style`/`color` accepts an image, so
    even a resolved value has no network reachability.
    """
    assert css.sanitize_stylesheet("td { --x: red; border-top-color: var(--x) }") == (
        "td{border-top-color:var(--x)}"
    )
    assert css.sanitize_declarations("--x: url(https://track.example/p.gif)") == ""


def test_custom_properties_are_dropped():
    assert css.sanitize_declarations("--x: red") == ""
    assert css.sanitize_declarations("--x: </style><script>alert(1)</script>") == ""
    assert css.sanitize_stylesheet("p { --x: red; color: blue }") == "p{color:blue}"


# --- functions and url() ---


@pytest.mark.parametrize(
    "payload",
    [
        "width: calc(10px + url(http://evil/x))",
        "color: var(--x, url(http://evil/x))",
        "width: min(1px, expression(alert(1)))",
        "width: clamp(1px, attr(data-x), 3px)",
        "width: calc(calc(calc(url(http://evil/x))))",
        "font-family: rgb(1, image-set('http://evil/a.png' 1x), 2)",
    ],
)
def test_a_disallowed_function_nested_in_an_allowed_one_is_caught(payload):
    out = css.sanitize_declarations(payload)
    assert out == ""
    assert "evil" not in out and "url(" not in out.lower()


def test_url_is_rejected_quoted_unquoted_and_upper_cased():
    for payload in [
        "background-color: url(http://evil/x)",
        "background-color: url('http://evil/x')",
        "background-color: URL(http://evil/x)",
        "background-color: Url( http://evil/x )",
        "background-color: \\75 rl(http://evil/x)",
    ]:
        assert css.sanitize_declarations(payload) == ""


def test_allowed_functions_survive_with_their_arguments_intact():
    decls = parse(
        css.sanitize_declarations(
            "color: rgba(1, 2, 3, .5); width: calc(100% - 2px); max-width: min(600px, 100%)"
        )
    )
    assert decls == {
        "color": "rgba(1, 2, 3, .5)",
        "width": "calc(100% - 2px)",
        "max-width": "min(600px, 100%)",
    }


# --- stylesheet structure ---


def test_media_block_structure_is_rebuilt_not_passed_through():
    out = css.sanitize_stylesheet(
        "@media screen and (max-width: 600px) { .a { color: red; position: fixed } }"
        " p { color: blue }"
    )
    assert sheet(out) == [
        (("@media screen and (max-width: 600px)", ".a"), "color", "red"),
        (("p",), "color", "blue"),
    ]


def test_media_inside_media_is_recursed_all_the_way_down():
    out = css.sanitize_stylesheet(
        "@media print { @media screen { p { color: red; position: fixed } } }"
    )
    assert sheet(out) == [(("@media print", "@media screen", "p"), "color", "red")]


def test_rules_and_media_blocks_that_end_up_empty_are_not_emitted():
    assert css.sanitize_stylesheet("p { position: fixed }") == ""
    assert css.sanitize_stylesheet("@media screen { p { position: fixed } }") == ""
    assert css.sanitize_stylesheet("@media screen { }") == ""
    assert css.sanitize_stylesheet("@media screen;") == ""
    assert css.sanitize_stylesheet("{ color: red }") == ""


@pytest.mark.parametrize(
    "at_rule",
    [
        '@import url("http://evil/x.css");',
        "@font-face { font-family: E; src: url(http://evil/f.woff) }",
        '@charset "utf-8";',
        "@namespace svg url(http://www.w3.org/2000/svg);",
        "@supports (display: grid) { p { color: red } }",
        "@page { margin: 0 }",
        "@keyframes spin { from { color: red } }",
        "@document url(http://evil/) { p { color: red } }",
    ],
)
def test_only_media_survives_at_the_top_level_and_inside_media(at_rule):
    assert css.sanitize_stylesheet(at_rule) == ""
    assert css.sanitize_stylesheet(f"@media screen {{ {at_rule} }}") == ""


def test_declarations_stranded_at_the_top_level_of_a_stylesheet_are_dropped():
    # `color: red` with no rule around it parses as a qualified rule with no
    # block; emitting it unwrapped would put a bare declaration into <style>.
    assert css.sanitize_stylesheet("color: red") == ""


# --- resource limits ---


def test_deeply_nested_values_are_refused_rather_than_crashing():
    # tinycss2.serialize() recurses per block: ~1000 levels raises
    # RecursionError, and 512 KB of input buys far more than that.
    assert css.sanitize_declarations("width: calc(" * 2000 + "1px" + ")" * 2000) == ""
    assert css.sanitize_stylesheet("p { width: " + "calc(" * 2000 + "1px" + ")" * 2000 + " }") == ""
    assert css.sanitize_stylesheet("a" + "[x=y]" * 2000 + " { color: red }") is not None
    assert css.sanitize_stylesheet("a" + "[" * 2000 + " { color: red }") == ""


def test_deeply_nested_media_blocks_are_refused_rather_than_crashing():
    assert css.sanitize_stylesheet("@media a{" * 2000 + "p{color:red}" + "}" * 2000) == ""


def test_the_size_bail_is_measured_in_bytes_not_characters():
    # A body of 3-byte characters is over the byte budget well before it is
    # over MAX_CSS_BYTES characters.
    body = "中" * (css.MAX_CSS_BYTES // 2)
    assert len(body) < css.MAX_CSS_BYTES
    assert css.sanitize_stylesheet(f'p {{ font-family: "{body}" }}') == ""
    assert css.sanitize_declarations(f'font-family: "{body}"') == ""


def test_a_body_just_under_the_budget_is_still_sanitised():
    filler = "p{color:red}" * 100
    assert len(filler.encode()) < css.MAX_CSS_BYTES
    assert css.sanitize_stylesheet(filler).count("color") == 100


# --- output is stable and re-parseable ---


@pytest.mark.parametrize("payload", BREAKOUT_CORPUS)
def test_sanitising_twice_changes_nothing(payload):
    once = css.sanitize_stylesheet(payload)
    assert css.sanitize_stylesheet(once) == once
    once = css.sanitize_declarations(payload)
    assert css.sanitize_declarations(once) == once


def test_output_re_parses_without_a_single_parse_error():
    out = css.sanitize_stylesheet(
        "@media screen and (max-width: 600px) { .a > .b:not(.c) { color: red } }"
        "p, a[href] { margin: 0 auto; font-family: 'Helvetica Neue', Arial }"
    )
    assert [row for row in sheet(out) if row[1].startswith("!")] == []
    assert sheet(out) == [
        (("@media screen and (max-width: 600px)", ".a > .b:not(.c)"), "color", "red"),
        (("p, a[href]",), "margin", "0 auto"),
        # tinycss2 re-quotes string tokens with double quotes and escapes any
        # `"` or `\\` inside them, so quoting is normalised on the way out.
        (("p, a[href]",), "font-family", '"Helvetica Neue", Arial'),
    ]


def test_important_survives_on_both_paths():
    assert parse(css.sanitize_declarations("color: red !IMPORTANT")) == {"color": "red"}
    out = css.sanitize_stylesheet("p { color: red !important }")
    decls = tinycss2.parse_blocks_contents(
        tinycss2.parse_stylesheet(out, skip_comments=True, skip_whitespace=True)[0].content,
        skip_comments=True,
        skip_whitespace=True,
    )
    assert [(d.lower_name, d.important) for d in decls] == [("color", True)]
