"""Rendering a `text/plain` body: escape, linkify, colour by depth.

A plain-text body is the one kind of mail content this app renders **inside
its own origin** — the conversation page prints these lines directly, with
no sandboxed frame and no CSP standing between a sender's text and the
reader's session. Everything here follows from that.

**Escape first, scan second.** `linkify` hands the raw text to
`markupsafe.escape` and only *then* looks for URLs, in the escaped string.
The order is the whole safety argument: by the time the regex runs, a `"`
the sender typed is already an entity and cannot close the `href=""` it
lands in, so no URL — however it is crafted — can add an attribute to the
anchor it becomes. Linkifying first and escaping second would either
destroy the anchors just built or, worse, leave the sender's quote live
inside the attribute. `tests/unit/test_plain_text.py` writes the reverse
order out in full and asserts the injection it allows, so the rule is
pinned by a demonstration rather than by this paragraph.

Only `http:`, `https:` and `mailto:` become links (spec §7). A bare
`www.example.test` stays text: guessing a scheme for it means guessing
`http://`, and a link the reader did not ask for is not worth downgrading
one for. Every anchor carries the same `target="_blank"` and
`rel="noopener noreferrer nofollow"` the HTML sanitiser puts on links in
sanitised mail, so a link means the same thing whichever body type carried
it.

**Depth rides on the line, not on a wrapper.** `render_plain` returns
`TextLine`s, each with its own `>`-nesting depth, because a quoted block
routinely mixes levels ("> > they said" under "> I asked") and one wrapper
element cannot colour both. `styles/input.css` carries `.q1`-`.q4` for
those depths.

`split_quoted` — the `>`-run grouper the Phase 0 thread view uses — lives
here too, moved out of `mailosh.web.app`: it is the same question about the
same text, and keeping it in the web layer meant `mailosh.web.mail` had to
reach back into the app module through a deferred import to dodge a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markupsafe import Markup, escape

from mailosh.render.quote_trim import quote_depth, split_plain

__all__ = ["TextLine", "linkify", "render_plain", "split_quoted"]

#: The URL scanner, run over the **escaped** string. `<`, `>` and `"` are
#: excluded from the URL body as belt and braces: after escaping none of the
#: three can occur literally, so the class is really "up to the next
#: whitespace", and the exclusions only matter if this regex is ever
#: (wrongly) pointed at raw text.
_URL_RE = re.compile(r"\b(?:https?://|mailto:)[^\s<>\"]+")

#: The same `rel` `mailosh.render.html_sanitize` puts on every link in
#: sanitised HTML mail — `noopener`/`noreferrer` so a mail link cannot reach
#: back through `window.opener` or leak the reading URL, `nofollow` because
#: nothing a stranger sends is an endorsement.
_LINK_REL = "noopener noreferrer nofollow"


@dataclass(frozen=True)
class TextLine:
    """One rendered line: the markup to print, and its quote depth (0-4).

    `html` is already escaped and linkified — a `Markup`, so a template
    prints it without escaping it a second time and turning the anchors back
    into visible text. `depth` becomes the line's `.q1`-`.q4` class; depth 0
    gets no class at all.
    """

    html: Markup
    depth: int


def _anchor(match: re.Match[str]) -> str:
    """Wrap one already-escaped URL in an anchor.

    Used as `re.sub`'s replacement *function*, not a template string, so
    backslashes and `\\g` sequences in a sender's URL stay literal rather
    than being read as group references.
    """
    url = match.group(0)
    return f'<a href="{url}" target="_blank" rel="{_LINK_REL}">{url}</a>'


def linkify(text: str) -> Markup:
    """Escape `text`, then turn every http/https/mailto URL in the *escaped*
    string into an anchor.

    The ordering is load-bearing; see this module's docstring.
    """
    return Markup(_URL_RE.sub(_anchor, str(escape(text))))


def _lines(chunk: str) -> list[TextLine]:
    """One `TextLine` per line of `chunk`, or none at all if it is empty.

    A single trailing newline terminates the last real line rather than
    starting an empty one — the same rule `split_quoted` applies below, and
    for the same reason: without it a body ending in `"\\n"` would render a
    blank line the sender did not write.
    """
    if not chunk:
        return []
    return [
        TextLine(html=linkify(line), depth=quote_depth(line))
        for line in chunk.removesuffix("\n").split("\n")
    ]


def render_plain(text: str | None) -> tuple[list[TextLine], list[TextLine]]:
    """Render a plain-text body as `(visible_lines, quoted_lines)`.

    The cut is `mailosh.render.quote_trim.split_plain`'s, so the two halves
    together are the whole body: `quoted_lines` is what a `•••` toggle
    hides, not what is thrown away. Both lists are empty for an absent or
    empty body — a caller rendering a bodyless message needs no special
    case, it just prints nothing.
    """
    visible, quoted = split_plain(text or "")
    return _lines(visible), _lines(quoted)


def split_quoted(text: str | None) -> list[tuple[str, bool]]:
    """Split a plain-text message body into consecutive-line runs, each
    tagged quoted or not, so a thread view can hide the quoted runs behind
    one collapse toggle per message instead of trying to do this grouping
    itself (kept a plain function specifically so it has its own unit tests
    in ``tests/unit/test_web_thread.py`` — see that module for the grouping
    cases: an all-quoted body, an all-unquoted one, and one that interleaves
    both).

    A line counts as quoted when it starts with ``>``, possibly after
    leading whitespace (``"> ..."``, ``">> ..."``, ``"  > ..."``) — the
    standard plain-text reply-quoting marker every mail client produces.
    Consecutive lines sharing the same quoted-ness collapse into one segment
    (rejoined with ``"\\n"``, so a multi-line quoted block still renders as
    one block inside a single ``<pre>``). ``None`` and ``""`` both yield
    ``[("", False)]`` rather than ``[]`` — always at least one segment — so
    a caller (a thread route, for a bodyless message) never needs a special
    case.

    A single trailing ``"\\n"`` terminates the body's last real line; it is
    not itself a further, empty line, so it is stripped before splitting.
    Without this, e.g. ``"hi\\n> quoted\\n"``'s naive ``.split("\\n")`` would
    emit a spurious final ``""`` element — unquoted, differing from the
    preceding quoted line — which would then flush as its own segment: an
    extra, always-visible empty ``<pre>`` rendered right after the collapsed
    quote block. Only ever *one* trailing newline is stripped, and only when
    present, so an interior blank line (e.g. ``"hi\\n\\nbye"``) — or one
    immediately before the final newline (``"hi\\n\\nbye\\n"``) — still comes
    through inside its segment exactly as before.

    Task 9 retires this in favour of `render_plain`, which cuts the body in
    two at `find_quote_start` instead of grouping every interleaved run;
    until then the Phase 0 thread view is its one caller.
    """
    raw = (text or "").removesuffix("\n")
    lines = raw.split("\n")
    segments: list[tuple[str, bool]] = []
    current: list[str] = []
    current_quoted = False
    for i, line in enumerate(lines):
        is_quoted = line.lstrip().startswith(">")
        if i > 0 and is_quoted != current_quoted:
            segments.append(("\n".join(current), current_quoted))
            current = []
        current.append(line)
        current_quoted = is_quoted
    segments.append(("\n".join(current), current_quoted))
    return segments
