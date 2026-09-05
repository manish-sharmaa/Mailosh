"""Test-only helpers shared across `tests/unit/`.

`parse_attrs` exists because nh3's attribute output order is *not* stable:
`nh3.clean` re-serialises each element from a hash map, so
`<a href=".." target=".." rel="..">` and `<a rel=".." href=".." target="..">`
are both valid outputs of the same call and either may appear on any given
run. Asserting against a raw output string therefore encodes a coincidence,
not a rule -- and the failure mode is the dangerous direction: a substring
check like `'onerror' not in out` also passes when the sanitiser emitted the
attribute with a different quoting, and `'javascript:' not in out` *fails*
on a message that merely mentions the word in a text node.

So every assertion in `tests/unit/test_html_sanitize.py` goes through this:
parse the sanitised HTML back into a `{tag: [{attr: value}, ...]}` map and
assert on the tree.

Kept out of `tests/conftest.py` deliberately -- this is an importable helper,
not a fixture, and pytest's default "prepend" import mode already puts
`tests/` on `sys.path` (there is no `tests/__init__.py`), which is what lets
sibling modules do a plain `from helpers import parse_attrs`, exactly as they
already do with `from conftest import ...`.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _AttrCollector(HTMLParser):
    """Collect every start tag (and self-closing tag) with its attributes.

    `convert_charrefs=True` (the default) is kept on purpose: it decodes
    character references in *text*, which is what a browser does, so a test
    that reads text content sees what the reader would. Attribute values are
    decoded by `HTMLParser` regardless, which is the point -- a payload that
    hides `javascript:` behind `&#106;avascript:` must not slip past an
    assertion just because the escape survived into the output.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, list[dict[str, str]]] = {}

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A valueless attribute (`<input disabled>`) arrives as (name, None).
        # Normalise it to "" so callers can do `value.strip().lower()` on
        # every value without a None check -- an empty value is still a
        # present attribute, which is all the `startswith("on")` assertions
        # care about.
        self.tags.setdefault(tag, []).append({k: ("" if v is None else v) for k, v in attrs})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<br/>` reaches handle_startendtag, not handle_starttag; without
        # this override a self-closed hostile tag would be invisible to the
        # assertions.
        self._record(tag, attrs)


def parse_attrs(html: str) -> dict[str, list[dict[str, str]]]:
    """Parse `html` into `{tag: [{attr: value}, ...]}`, one entry per element.

    Tag and attribute names come back lower-cased (`HTMLParser` folds them),
    so `OnErRoR` and `onerror` are the same key -- which is what makes
    `any(k.startswith("on") for k in attrs)` a complete check rather than a
    check of one spelling.
    """
    parser = _AttrCollector()
    parser.feed(html)
    parser.close()
    return parser.tags
