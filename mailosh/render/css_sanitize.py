"""CSS a stranger wrote, re-parsed and re-emitted — never passed through.

Two entry points, one for each place mail CSS can hide:

    sanitize_declarations   one `style=""` attribute value
    sanitize_stylesheet     one `<style>` element's contents

Both parse with tinycss2, throw away everything that is not on an
allow-list, and *rebuild* the CSS from the surviving syntax tree. Nothing
is copied from the input verbatim: the property name that comes out is the
allow-listed constant, not the bytes the sender typed, and the value is
tinycss2's own serialisation of tokens we have already inspected.

The rule that shapes the whole module is: **no `<` may reach a `<style>`
element.** tinycss2's serializer decodes CSS escapes back to literal
characters and does not re-escape them inside string tokens —

    in    p::before { content: "\\3c /style\\3e \\3c img src=x onerror=alert(1)\\3e " }
    out   p::before { content: "</style><img src=x onerror=alert(1)>" }

— so a stylesheet that survives an allow-list can still close the element
it is about to be inlined into and inject markup. Every path that could
emit text therefore checks for `<`, and `_no_angle_bracket` checks the
finished string one last time before it is returned.

Three tinycss2 behaviours the allow-list alone would not have caught, all
of them "the serializer writes attacker bytes back":

- `ast.ParseError` nodes appear *inside* declaration values and rule
  preludes, and `ParseError._serialize_to` re-emits the raw text: a bare
  `}` (which closes the rule it lands in, so everything after it is parsed
  by the browser as top-level CSS that never passed this allow-list), a
  synthetic `url([bad url])`, or an unterminated `"`. Any ParseError in a
  value or a prelude drops what contains it.
- `serialize_identifier` escapes `<` as `\\<`, which is still a literal `<`
  byte in the document. The `<` check catches that too, at the cost of
  dropping rules whose selectors contain escaped angle brackets — no
  legitimate mail CSS has those.
- `tinycss2.serialize` recurses once per nested block and raises
  `RecursionError` at roughly 1000 levels, which 512 KB of `calc(calc(…`
  reaches with room to spare. Depth is bounded *before* anything is
  serialised, so a hostile body is refused rather than raised.
"""

from __future__ import annotations

import tinycss2
from tinycss2 import ast

#: Properties that may survive, spelled out one by one. An allow-list, not a
#: deny-list: a property nobody has thought about yet is dropped, which is the
#: only posture that survives new CSS features being shipped by browsers. Note
#: what is *absent* and therefore always dropped -- `position` (and `top` /
#: `right` / `bottom` / `left` / `z-index` with it, so a message cannot lift
#: itself out of its frame), `background` and `background-image` (silent remote
#: fetches), `behavior` / `-moz-binding` / `filter` / `expression` (legacy
#: script execution), `content` (generated text next to a `<style>` boundary),
#: and the animation/transform family (no motion a reader did not ask for).
#:
#: The `border-{top,right,bottom,left}-{width,style,color}` grid is here in
#: full because leaving it out was an inconsistency, not a policy. Real mail
#: writes a rule both ways -- Word emits `border-bottom: solid #E2E2E2 1.0pt`
#: on one table row and `border-bottom-{width,style,color}` on the next -- and
#: dropping the second spelling of a rule whose first spelling is allowed loses
#: the reader a divider while granting nothing: a longhand is a *component of a
#: shorthand already on this list*, so no declaration becomes expressible that
#: was not expressible before. Their grammars are `<length>` / a keyword /
#: `<color>`; none of the three accepts an image, so none can reach the
#: network even if the value filter let a `url()` through, which it does not.
#:
#: The image-bearing members of those same families are deliberately still
#: absent, and the difference is the point: `border-image-source` and
#: `list-style-image` exist *to* name a remote resource (and neither is
#: settable through `border` or `list-style` either, so allowing them would be
#: a new capability rather than a second spelling of an old one).
ALLOWED_PROPERTIES: frozenset[str] = frozenset(
    {
        "background-color",
        "border",
        "border-bottom",
        "border-bottom-color",
        "border-bottom-style",
        "border-bottom-width",
        "border-collapse",
        "border-color",
        "border-left",
        "border-left-color",
        "border-left-style",
        "border-left-width",
        "border-radius",
        "border-right",
        "border-right-color",
        "border-right-style",
        "border-right-width",
        "border-spacing",
        "border-style",
        "border-top",
        "border-top-color",
        "border-top-style",
        "border-top-width",
        "border-width",
        "caption-side",
        "clear",
        "color",
        "direction",
        "display",
        "empty-cells",
        "float",
        "font",
        "font-family",
        "font-size",
        "font-style",
        "font-variant",
        "font-weight",
        "height",
        "letter-spacing",
        "line-height",
        "list-style",
        "list-style-position",
        "list-style-type",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-height",
        "max-width",
        "min-height",
        "min-width",
        "opacity",
        "overflow-wrap",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "table-layout",
        "text-align",
        "text-decoration",
        "text-indent",
        "text-transform",
        "vertical-align",
        "visibility",
        "white-space",
        "width",
        "word-break",
        "word-spacing",
    }
)

#: Functions a declaration value may contain. Everything else -- `url`,
#: `expression`, `attr`, `image-set`, `element`, `-moz-element`, and whatever
#: is invented next -- drops the whole declaration rather than just the
#: function, because a half-understood value is not worth keeping.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {"rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp", "var"}
)

#: At-rules that may survive. `@media` only: it is what responsive mail needs,
#: and it fetches nothing. `@import` and `@font-face` would both pull a remote
#: resource on render; `@charset` and `@namespace` change how the rest of the
#: sheet is interpreted.
ALLOWED_AT_RULES: frozenset[str] = frozenset({"media"})

#: Refuse a body larger than this outright rather than sanitising it. Half a
#: megabyte of CSS is already far past anything a mail client renders, and the
#: parse cost is attacker-controlled.
MAX_CSS_BYTES: int = 512 * 1024

#: How deeply component values (`calc(`, `(`, `[`, `{`) may nest before the
#: declaration is dropped. Guards `tinycss2.serialize`'s recursion, which
#: raises `RecursionError` around 1000 levels; real CSS never passes 3.
_MAX_VALUE_DEPTH = 16

#: How deeply `@media` may nest before the block is dropped, for the same
#: reason one level up. `@media` inside `@media` is already unusual in mail.
_MAX_RULE_DEPTH = 4


def sanitize_declarations(css: str) -> str:
    """One `style=""` attribute value, allow-listed and re-serialised.

    Returns `""` -- never whitespace, never a stray `;` -- when nothing
    survives, so a caller can write `sanitize_declarations(value) or None`
    and have the attribute dropped entirely.
    """
    if _too_large(css):
        return ""
    return _no_angle_bracket(_sanitize_declaration_list(css))


def sanitize_stylesheet(css: str) -> str:
    """One `<style>` element's contents, allow-listed and rebuilt.

    Rules whose selector cannot be re-emitted safely are dropped whole, as
    are rules and `@media` blocks left with no declarations.
    """
    if _too_large(css):
        return ""
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    return _no_angle_bracket("\n".join(_sanitize_rules(rules, depth=1)))


def _too_large(css: str) -> bool:
    """True when `css` blows the byte budget.

    The character count is checked first because UTF-8 never encodes a
    character in fewer than one byte: a hostile 100 MB body is refused
    without paying to encode it. `surrogatepass` is deliberate -- a body
    decoded with `errors="surrogateescape"` upstream can carry lone
    surrogates, and this is a length measurement, not a re-encoding.
    """
    return len(css) > MAX_CSS_BYTES or len(css.encode("utf-8", "surrogatepass")) > MAX_CSS_BYTES


def _no_angle_bracket(result: str) -> str:
    """Belt and braces: nothing carrying a `<` is ever returned.

    Every rejection above this is a rule about a payload someone thought of.
    This is the rule about the payloads nobody thought of: whatever survived,
    if it contains a `<` it can close the `<style>` element it is inlined
    into -- `"</style><img src=x onerror=alert(1)>"` reached here as `\\3c`
    escapes and came back out of tinycss2's serializer as literal markup --
    so the whole result is discarded instead.
    """
    return "" if "<" in result else result


def serialize_bounded(nodes: list[ast.Node]) -> str | None:
    """`tinycss2.serialize`, or `None` when the input nests too deeply to
    serialise without blowing the interpreter's stack.

    `tinycss2.serialize` recurses once per nested block, so a value like
    `calc(` x 600 raises `RecursionError` from about 1 KB of input. Inside
    this module that never escapes, because `_unsafe_values` rejects
    anything past `_MAX_VALUE_DEPTH` *before* serialising.

    This exists because `mailosh.render.dark` needs the same protection and
    was reading the raw, unsanitised `<style>` blocks with its own copy of
    the walk -- so a ~1.2 KB message crashed the reading route and made
    itself permanently unopenable. Two copies of a walk, one bound: the
    bound belongs in one place, and this is it. Any module that serialises
    attacker-supplied component values calls this, never `tinycss2.serialize`.
    """
    if _too_deep(nodes):
        return None
    return tinycss2.serialize(nodes)


def _too_deep(nodes: list[ast.Node]) -> bool:
    """True when nesting exceeds `_MAX_VALUE_DEPTH`.

    Iterative for the same reason `_unsafe_values` is: the input chooses the
    depth, and a recursive check would hit Python's own limit before this
    one could answer.
    """
    stack: list[tuple[ast.Node, int]] = [(node, 1) for node in nodes]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_VALUE_DEPTH:
            return True
        children = getattr(node, "content", None) or getattr(node, "arguments", None)
        if isinstance(children, list):
            stack.extend((child, depth + 1) for child in children)
    return False


def _unsafe_values(nodes: list[ast.Node], *, allow_listed_functions: bool) -> bool:
    """True when this component-value list must not be re-emitted.

    Walked iteratively with an explicit stack: the input controls the nesting
    depth, and a recursive walker would hit Python's own recursion limit
    before `_MAX_VALUE_DEPTH` could reject anything.

    `allow_listed_functions` is off for selectors, whose functions are
    structural (`:not()`, `:nth-child()`, `:is()`) rather than value
    functions -- none of them fetches or evaluates anything, and applying the
    value allow-list there would drop ordinary responsive-mail selectors.
    """
    stack: list[tuple[ast.Node, int]] = [(node, 1) for node in nodes]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_VALUE_DEPTH:
            return True
        # ParseError._serialize_to writes the raw delimiter back -- a "}" here
        # would close the rule this value sits in, handing the rest of the
        # sheet to the browser as CSS this allow-list never saw.
        if isinstance(node, ast.ParseError):
            return True
        # url() without quotes is its own token type, not a function.
        if isinstance(node, ast.URLToken):
            return True
        if isinstance(node, ast.FunctionBlock):
            if allow_listed_functions and node.lower_name not in ALLOWED_FUNCTIONS:
                return True
            stack.extend((child, depth + 1) for child in node.arguments)
        elif isinstance(
            node, (ast.ParenthesesBlock, ast.SquareBracketsBlock, ast.CurlyBracketsBlock)
        ):
            stack.extend((child, depth + 1) for child in node.content)
    return False


def _sanitize_declaration_list(nodes: str | list[ast.Node]) -> str:
    """The one declaration walker, shared by the attribute and stylesheet
    paths so the two cannot drift apart.

    Takes either the raw text of a `style=""` attribute or the already-parsed
    contents of a rule's block.
    """
    kept: list[str] = []
    for node in tinycss2.parse_blocks_contents(nodes, skip_comments=True, skip_whitespace=True):
        if not isinstance(node, ast.Declaration):
            continue
        if node.lower_name not in ALLOWED_PROPERTIES:
            continue
        if _unsafe_values(node.value, allow_listed_functions=True):
            continue
        value = tinycss2.serialize(node.value).strip()
        if not value or "<" in value:
            continue
        # `lower_name`, not `node.name`: tinycss2 decodes escapes in property
        # names, so this is the ASCII allow-list entry that just matched
        # rather than any bytes the sender chose (`\70 osition` is `position`
        # and never got here; `CoLoR` comes out as `color`).
        kept.append(f"{node.lower_name}:{value}" + (" !important" if node.important else ""))
    return ";".join(kept)


def _safe_prelude(nodes: list[ast.Node]) -> str | None:
    """A selector or media query re-serialised, or `None` if it cannot be."""
    if _unsafe_values(nodes, allow_listed_functions=False):
        return None
    prelude = tinycss2.serialize(nodes).strip()
    if not prelude or "<" in prelude:
        return None
    return prelude


def _sanitize_rules(nodes: list[ast.Node], depth: int) -> list[str]:
    """Qualified rules and at-rules, rebuilt one by one.

    A rule is emitted only if both its selector and at least one of its
    declarations survive, so nothing empty reaches the document.
    """
    if depth > _MAX_RULE_DEPTH:
        return []
    kept: list[str] = []
    for rule in nodes:
        if isinstance(rule, ast.QualifiedRule):
            prelude = _safe_prelude(rule.prelude)
            if prelude is None:
                continue
            body = _sanitize_declaration_list(rule.content)
            if body:
                kept.append(f"{prelude}{{{body}}}")
        elif isinstance(rule, ast.AtRule):
            # `lower_at_keyword` is ASCII-lowercased by tinycss2 and has to be
            # in ALLOWED_AT_RULES to get past here, so what gets written below
            # is the constant "media", not the sender's bytes.
            if rule.lower_at_keyword not in ALLOWED_AT_RULES or rule.content is None:
                continue
            prelude = _safe_prelude(rule.prelude)
            if prelude is None:
                continue
            inner = _sanitize_rules(
                tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True),
                depth=depth + 1,
            )
            if inner:
                body = "\n".join(inner)
                kept.append(f"@{rule.lower_at_keyword} {prelude}{{{body}}}")
        # Anything else -- a ParseError, a stray declaration at the top level
        # of a sheet -- is dropped without comment.
    return kept
