"""Where a reply ends and the quoted history begins (spec §7).

Two unrelated problems share this module because they answer the same
question for the two body types a message can arrive in.

**HTML** (`split_html`). Every mail client marks its own quote block, and
between them they use ten different markers — `QUOTE_MATCHERS` is
ihasmail's selector list, verbatim and in its order. The split works by
**offset slicing, never re-serialisation**: an `HTMLParser` subclass walks
the already-sanitised fragment and records the character offset of the
first start tag that matches, and the two halves are `html[:offset]` and
`html[offset:]`. Nothing is re-emitted, so the split cannot introduce
markup the sanitiser did not already approve — the strongest property this
module has, and the reason it is not built on a DOM library that would
have to serialise a tree back out again. The price is that each half may be
unbalanced (the visible half holds unclosed tags, the quoted half stray
closing ones), which the browser closes for us inside the sandboxed frame.
That is the same bargain ihasmail strikes, and it follows from what a quote
marker means: *everything from here down is the quote*, not "this one
element is".

When no selector matches anywhere in the body, three textual heuristics run
over the text content of each `<div>`/`<p>` — an attribution line ("On …
wrote:"), an `--- Original Message ---` divider, and a forwarded-header
block (`From:` / `Sent:` / `To:` / `Subject:`, which is all current Outlook
for Windows marks its history with: no class, no id, no divider). They are
a fallback rather than a first resort because they read *text a sender
controls*: a selector is markup the sending client emitted, but any
correspondent can type "On Monday I wrote:" mid-sentence. Anchoring all
three against the whole text content of one element is what keeps that
sentence from cutting a reply in half — and a heuristic cut is refused
outright when there is no reply above it to keep, because a body that is
nothing but a forward is one the reader wants to *see*, not one to hide
behind a toggle.

They have one job beyond that fallback: an attribution line sitting
**immediately above** a marker is pulled into the quote with it, so the
cut lands in the same place whether the sending client wrapped that line
in its own marker (Gmail, Thunderbird) or left it outside one (Apple
Mail). The plain-text half has always folded it away; this is what makes
the HTML half agree. `_attribution_above` carries the reasoning and the
one condition that keeps it safe.

**Plain text** (`find_quote_start`). No markup to lean on, so five separate
families of marker are each located independently and the *earliest* index
wins. Not "the first pattern that matches" — a body commonly carries two
(an attribution line above an Outlook header block), and the cut belongs at
the one the reader meets first, which has nothing to do with the order the
patterns happen to be written in.

Neither half of this module renders anything: `mailosh.render.plain_text`
turns the plain-text halves into escaped, linkified lines, and the HTML
halves go to the frame document. Callers get strings and indices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = [
    "QUOTE_MATCHERS",
    "QuoteMatcher",
    "find_quote_start",
    "quote_depth",
    "split_html",
    "split_plain",
]


@dataclass(frozen=True)
class QuoteMatcher:
    """One entry of the selector list, as data rather than as a CSS string.

    A field left ``None`` is not tested at all; every field that *is* set
    must hold, so ``QuoteMatcher(tag="div", id_prefix="appendonsend")`` is
    the CSS ``div[id^=appendonsend]`` and nothing looser. Deliberately not a
    CSS selector engine: the ten selectors below need exactly these five
    predicates, and a real engine would mean either a DOM (which forces the
    re-serialisation this module exists to avoid) or a parser of its own.

    ``klass`` matches a whitespace-separated *token* of the ``class``
    attribute, never a substring — ``gmail_quoteish`` is somebody else's
    class name, and cutting a reply on it would hide text the reader wrote.
    ``attr``'s value is compared case-insensitively (``type="Cite"`` is what
    some clients send); ``klass``, ``id_exact`` and ``id_prefix`` are
    case-sensitive, matching how a browser resolves class and id selectors
    in a standards-mode document.
    """

    tag: str | None = None
    klass: str | None = None
    id_exact: str | None = None
    id_prefix: str | None = None
    attr: tuple[str, str] | None = None


#: Spec §7, verbatim and in its order: `.gmail_quote`,
#: `blockquote[type=cite]`, `.moz-cite-prefix`, `#divRplyFwdMsg`,
#: `.yahoo_quoted`, `div[id^=appendonsend]`,
#: `.ms-outlook-mobile-reference-message`, `#OLK_SRC_BODY_SECTION`,
#: `.protonmail_quote`, `.mailosh_quote` (our own, so a reply this app
#: composes trims the same way in the next client that sees it).
#:
#: The order is documentation, not precedence: `split_html` cuts at the
#: first matching tag in *document order*, whichever entry matched it. A
#: body carrying a `.mailosh_quote` above a `.gmail_quote` cuts at the
#: `.mailosh_quote`, because that is where the history starts.
QUOTE_MATCHERS: tuple[QuoteMatcher, ...] = (
    QuoteMatcher(klass="gmail_quote"),
    QuoteMatcher(tag="blockquote", attr=("type", "cite")),
    QuoteMatcher(klass="moz-cite-prefix"),
    QuoteMatcher(id_exact="divRplyFwdMsg"),
    QuoteMatcher(klass="yahoo_quoted"),
    QuoteMatcher(tag="div", id_prefix="appendonsend"),
    QuoteMatcher(klass="ms-outlook-mobile-reference-message"),
    QuoteMatcher(id_exact="OLK_SRC_BODY_SECTION"),
    QuoteMatcher(klass="protonmail_quote"),
    QuoteMatcher(klass="mailosh_quote"),
)

#: The elements whose text content the textual heuristics are allowed to
#: read. A block-level container is what a client wraps an attribution line
#: in; testing every element would mean testing the `<body>` too, whose text
#: content is the whole message.
_HEURISTIC_TAGS = frozenset({"div", "p"})

#: "On Tue, Sep 1, 2026 at 8:41 PM Dan Okafor <dan@example.test> wrote:" —
#: anchored at both ends against the element's *entire* text content, so a
#: sentence that merely contains the word ("I wrote: nothing came of it")
#: is not a marker. `DOTALL` lets the line wrap, which real attributions do;
#: the 300-character ceiling is what stops a wrapped match from swallowing
#: half a message looking for a later "wrote:".
_ATTRIBUTION_RE = re.compile(r"^\s*On\b.{0,300}\bwrote:\s*$", re.DOTALL)

#: "-----Original Message-----", in any dash count and any case.
_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)

#: The header names a forwarded/replied-to block is built from. Current
#: Outlook for Windows opens its quoted history with nothing else — an
#: unmarked `<div>`, a bordered `<div>` inside it, and a paragraph reading
#: `From: … Sent: … To: … Subject: …`. No class, no id, no
#: `-----Original Message-----`, so `QUOTE_MATCHERS` and both other
#: heuristics walk straight past it and the whole thread renders inline.
_HEADER_LABEL_RE = re.compile(
    r"^(From|Sent|Date|To|Cc|Bcc|Reply-To|Subject|Importance|Attachments)\s*:",
    re.IGNORECASE,
)

#: How many header lines a block needs before it reads as one. Two is what
#: the plain-text side asks for, because there it has the rest of the body
#: for context; here the element's *entire* text content has to be header
#: lines and nothing else, and three is what every client that emits this
#: shape actually sends (Word and OWA both send four).
_MIN_HEADER_FIELDS = 3

#: Longest a single header line may be. A header line is a name, a date or a
#: subject; a line long enough to be a paragraph is a paragraph, whatever
#: word it opens with. The trade is stated rather than hidden: a genuine
#: `To:` line listing more than about eight recipients trips this and the
#: message goes untrimmed, which is the direction to fail in.
_MAX_HEADER_LINE = 200


def _is_header_block(text: str) -> bool:
    """Whether one element's whole text content is a forwarded-header block.

    Deliberately strict in three separate ways, because this is the loosest
    marker in the module — it reads words a correspondent could type, with
    no client-emitted markup behind it, and an over-eager cut here hides
    text the reader wrote:

    - **Every** non-blank line must open with a recognised header name, so
      an element carrying a header block *and* a sentence is not a match.
      That is this heuristic's version of the anchoring `_ATTRIBUTION_RE`
      gets from `^…$`. `<br>` and the end of a `<div>`/`<p>` both count as
      the line breaks they are (see `_QuoteFinder`), so the rule reads the
      block the way it renders rather than the way its client happened to
      wrap the source: Word writes one `<p>` with `<br>` between the
      fields, and a client that writes one element per field means the
      same thing.
    - `From:` must come **first**, and one of `Sent:`/`Date:` must be
      present — the same pair the plain-text `_header_block_start` requires,
      for the same reason: `From:` alone is prose ("From: the desk of…").
    - Three lines minimum, each of them short. "From: the desk of Dan. Sent:
      whenever. To: whoever, and anyway Thursday works for me" is one line,
      not three, and does not match.
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    labels: list[str] = []
    for line in lines:
        if not line:
            continue
        if len(line) > _MAX_HEADER_LINE:
            return False
        match = _HEADER_LABEL_RE.match(line)
        if match is None:
            return False
        labels.append(match.group(1).lower())
    if len(labels) < _MIN_HEADER_FIELDS or labels[0] != "from":
        return False
    return bool({"sent", "date"} & set(labels))


def _line_starts(html: str) -> list[int]:
    """Character offset of the first character of every line.

    `HTMLParser.getpos()` reports (1-based line, 0-based column), and this
    table is what turns that back into an index into the original string.
    It counts `"\\n"` only — exactly as `HTMLParser.updatepos` does — so a
    `"\\r\\n"` body's `"\\r"` is a column like any other character and the
    two agree.
    """
    starts = [0]
    index = html.find("\n")
    while index != -1:
        starts.append(index + 1)
        index = html.find("\n", index + 1)
    return starts


def _matches(matcher: QuoteMatcher, tag: str, attrs: dict[str, str]) -> bool:
    """Whether one start tag satisfies every predicate `matcher` sets."""
    if matcher.tag is not None and tag != matcher.tag:
        return False
    if matcher.klass is not None and matcher.klass not in attrs.get("class", "").split():
        return False
    if matcher.id_exact is not None and attrs.get("id") != matcher.id_exact:
        return False
    if matcher.id_prefix is not None and not attrs.get("id", "").startswith(matcher.id_prefix):
        return False
    if matcher.attr is not None:
        name, value = matcher.attr
        found = attrs.get(name)
        if found is None or found.casefold() != value.casefold():
            return False
    return True


@dataclass
class _Open:
    """A `<div>`/`<p>` the parser is currently inside: where it started, and
    the text content accumulated for it so far.
    """

    tag: str
    offset: int
    text: list[str]


class _QuoteFinder(HTMLParser):
    """Records where the quote starts, without emitting anything.

    Four answers are collected in a single pass and chosen between at the
    end: `marker_offset` (a `QUOTE_MATCHERS` hit, always preferred),
    `heuristic_offset` (an attribution line, a divider or a forwarded-header
    block, used only when no selector matched anywhere in the body —
    including *later* in the body, which is why the choice cannot be made
    until the parse is over), `heuristic_spans`, which is every such element
    rather than the earliest, with where it closed, and
    `first_content_offset`.

    The spans are what lets `split_html` pull a cut *back* onto the
    attribution line sitting immediately above a marker. That needs all of
    them and their end offsets, not the first one and its start.

    `first_content_offset` is where the reader's eye first lands: the start
    of the first non-whitespace text run or the first image. It is what
    `split_html` uses to refuse a heuristic cut with nothing above it.
    """

    def __init__(self, line_starts: list[int]) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = line_starts
        self._open: list[_Open] = []
        self.marker_offset: int | None = None
        self.heuristic_offset: int | None = None
        self.heuristic_spans: list[tuple[int, int]] = []
        self.first_content_offset: int | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        offset = self._offset()
        if self.marker_offset is None:
            # First value wins for a repeated attribute, as the HTML parsing
            # spec requires — `<div class="a" class="gmail_quote">` carries
            # one class list, "a", and is not a quote marker.
            flat: dict[str, str] = {}
            for name, value in attrs:
                flat.setdefault(name, value or "")
            if any(_matches(matcher, tag, flat) for matcher in QUOTE_MATCHERS):
                self.marker_offset = offset
        if tag == "br":
            # A `<br>` is a line break, and `_is_header_block` reads lines.
            # Word wraps its `From:`/`Sent:`/`To:`/`Subject:` block in one
            # `<p>` with `<br>` between the fields, so without this the
            # block is a single line and never looks like a header block —
            # in a body whose source happens to put no newline after the
            # tag, which is the sender's choice and not a fact about the
            # message.
            for open_element in self._open:
                open_element.text.append("\n")
        elif tag == "img" and self.first_content_offset is None:
            # A reply that is only an image ("see the chart") is still a
            # reply, so it counts as content the same way text does.
            self.first_content_offset = offset
        if tag in _HEURISTIC_TAGS:
            self._open.append(_Open(tag, offset, []))

    def handle_endtag(self, tag: str) -> None:
        if tag not in _HEURISTIC_TAGS:
            return
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                closed = self._open[index]
                # Anything still open inside it was never closed; drop it
                # rather than let it collect the rest of the document.
                del self._open[index:]
                break
        else:
            return
        # A block container ends a line, the same way `<br>` starts one: an
        # ancestor reading `<div><div>From: …</div><div>Sent: …</div></div>`
        # sees two lines, not one run-together string. `_is_header_block` is
        # the reader of those lines; `_ATTRIBUTION_RE` absorbs the extra
        # newline in its own `\s*`.
        for open_element in self._open:
            open_element.text.append("\n")
        text = "".join(closed.text)
        if not (
            _ATTRIBUTION_RE.match(text)
            or _ORIGINAL_MESSAGE_RE.match(text)
            or _is_header_block(text)
        ):
            return
        # `self._offset()` is the `<` of this element's end tag; where that
        # tag *ends* is resolved in `split_html`, which has the string.
        self.heuristic_spans.append((closed.offset, self._offset()))
        if self.heuristic_offset is None or closed.offset < self.heuristic_offset:
            self.heuristic_offset = closed.offset

    def handle_data(self, data: str) -> None:
        if data.strip() and self.first_content_offset is None:
            self.first_content_offset = self._offset()
        # An element's text content includes its descendants', so every open
        # element gets the run — that is what makes an outer `<div>` holding
        # a reply *and* an attribution line fail to match while the inner
        # `<div>` holding only the attribution line matches.
        for open_element in self._open:
            open_element.text.append(data)


def _attribution_above(html: str, spans: list[tuple[int, int]], marker: int) -> int | None:
    """Where the attribution line **immediately above** `marker` starts, or
    `None` if there is nothing but the quote block itself up there.

    "On Tue, Sep 1, Dan wrote:" belongs to the quote under it, not to the
    reply above it: it names a person and repeats a date the card header
    already shows, and in a long thread every reply carries one. Some
    clients put it inside their own marker (Gmail's `.gmail_quote`,
    Thunderbird's `.moz-cite-prefix` *is* one) and some leave it outside —
    Apple Mail's sits above a bare `blockquote[type=cite]` — so without
    this the same line is folded away in one client's mail and left
    standing in another's. The plain-text side has always folded it
    (`_TEXT_ATTRIBUTION_RE` is one of `find_quote_start`'s candidates);
    this is the HTML side agreeing.

    **Only when nothing but whitespace separates the two.** That is what
    keeps this from swallowing a reply: an element qualifies only if its
    own entire text content is an attribution line (`handle_endtag` already
    required that) *and* the quote block begins right after it closes, so a
    sentence that happens to read "On Monday I wrote:" with the reader's
    own paragraphs under it can never be the cut.

    At most one element can pass that test, which is why this returns the
    first rather than choosing between them: anything that closed earlier
    has another element's end tag standing between it and the marker. A
    nested pair (`<div><p>On … wrote:</p></div>`) matches the regex at both
    depths, and it is the container that qualifies — cutting at the inner
    one would strand the container's opening tag in the visible half.

    `spans` holds every heuristic match, not only attributions, so a
    divider or a forwarded-header block directly above a marker folds in
    the same way and for the same reason: whatever it is, it is a label on
    the quote below it rather than something the sender wrote.
    """
    for start, end_tag in spans:
        close = html.find(">", end_tag)
        if close != -1 and close + 1 <= marker and not html[close + 1 : marker].strip():
            return start
    return None


def split_html(html: str) -> tuple[str, str]:
    """Slice an already-sanitised fragment into `(visible, quoted)`.

    `quoted` is `""` when the body carries no quote at all, and
    `visible + quoted` is always exactly the input — the halves are slices,
    not a re-rendering, so no attribute, tag or entity can differ from what
    the sanitiser approved.
    """
    if not html:
        return "", ""
    finder = _QuoteFinder(_line_starts(html))
    finder.feed(html)
    finder.close()
    offset = finder.marker_offset
    if offset is None:
        offset = finder.heuristic_offset
        # A heuristic cut with nothing above it is not taken. The two
        # failure modes here are not symmetrical: missing a quote costs the
        # reader some scrolling, while a cut in the wrong place hides text
        # somebody wrote, and a cut at the very top of the body hides *all*
        # of it. A body that is nothing but a forwarded message — no note
        # above it, which is how people forward things — is exactly that
        # shape, and the reader wants to read it, not to find a toggle where
        # the message should be. A selector is exempt: there the sending
        # client said "this is a quote" in markup of its own, and a body
        # that is one `.gmail_quote` really is all history.
        if offset is not None and not (
            finder.first_content_offset is not None and finder.first_content_offset < offset
        ):
            offset = None
    else:
        offset = _attribution_above(html, finder.heuristic_spans, offset) or offset
    if offset is None:
        return html, ""
    return html[:offset], html[offset:]


# ---------------------------------------------------------------------------
# Plain text.
# ---------------------------------------------------------------------------

#: "On Tue, Sep 1, 2026 at 8:41 PM Dan Okafor <dan@example.test> wrote:",
#: possibly wrapped over two lines (`DOTALL`), never over more than 300
#: characters. Non-greedy on purpose: with `DOTALL` a greedy body would run
#: to the *last* "wrote:" within reach rather than the first.
_TEXT_ATTRIBUTION_RE = re.compile(r"^[ \t]*On\b.{0,300}?\bwrote:[ \t]*$", re.MULTILINE | re.DOTALL)

#: "-----Original Message-----" on a line of its own.
_TEXT_ORIGINAL_MESSAGE_RE = re.compile(
    r"^[ \t]*-{2,}[ \t]*Original Message[ \t]*-{2,}[ \t]*$", re.MULTILINE | re.IGNORECASE
)

#: Outlook's horizontal rule above a forwarded header block.
_UNDERSCORE_RULE_RE = re.compile(r"^[ \t]*_{10,}[ \t]*$")

#: The header block itself. `From:` alone is prose ("From: the desk of…");
#: it only reads as a quoted header when a `Sent:`/`Date:` follows it.
_FROM_RE = re.compile(r"^[ \t]*From:\s")
_SENT_RE = re.compile(r"^[ \t]*(?:Sent|Date):\s")

#: How far after a rule or a `From:` line its confirming line may sit — an
#: Outlook header block puts `Sent:` on the next line, but a wrapped
#: address can push it down one or two more.
_LOOKAHEAD = 3

#: Deepest quote level with a colour of its own (`.q1`-`.q4` in
#: `styles/input.css`); anything deeper is drawn as `.q4`.
_MAX_DEPTH = 4


def quote_depth(line: str) -> int:
    """How many `>` markers open `line`, capped at 4.

    Whitespace is allowed before and between the markers, since clients
    disagree about it (`">> a"`, `"> > a"`, `"  >  >  > a"` all mean depth
    2, 2 and 3). The count stops at the first character that is neither a
    marker nor a space, so `"a > b"` is depth 0 — a `>` inside a sentence is
    a greater-than sign, not a quote level.
    """
    depth = 0
    for character in line:
        if character in " \t":
            continue
        if character != ">":
            break
        depth += 1
    return min(depth, _MAX_DEPTH)


def _header_block_start(lines: list[str], offsets: list[int]) -> int | None:
    """The earliest Outlook-style forwarded header block, if any.

    Two shapes, both confirmed by a following line rather than taken on
    faith: a `____________` rule with a `From:` under it (the rule is the
    marker, so the cut keeps the rule with the quote), and a bare `From:`
    with a `Sent:`/`Date:` under it.
    """
    for index, line in enumerate(lines):
        window = lines[index + 1 : index + 1 + _LOOKAHEAD]
        if _UNDERSCORE_RULE_RE.match(line) and any(_FROM_RE.match(nxt) for nxt in window):
            return offsets[index]
        if _FROM_RE.match(line) and any(_SENT_RE.match(nxt) for nxt in window):
            return offsets[index]
    return None


def _trailing_quote_run(lines: list[str], offsets: list[int]) -> int | None:
    """The start of the maximal *trailing* run of `>`-quoted lines.

    Trailing is the whole point: a `>` run with the reader's own text under
    it is an inline quote they replied beneath, and cutting there would hide
    live reply text. Blank lines inside the run are tolerated (a client that
    quotes a blank line often emits a truly empty one), but only once a
    quoted line has already been seen below them.
    """
    index = len(lines)
    while index > 0 and not lines[index - 1].strip():
        index -= 1
    start: int | None = None
    while index > 0:
        line = lines[index - 1]
        if quote_depth(line) > 0:
            start = index - 1
        elif not (line.strip() == "" and start is not None):
            break
        index -= 1
    return None if start is None else offsets[start]


def find_quote_start(text: str) -> int | None:
    """The character offset where the quoted history begins, or `None`.

    Every family of marker is located independently and the smallest index
    wins — never the first pattern that happens to match. A reply carrying
    both an attribution line and an Outlook divider cuts at whichever the
    reader meets first, which is a fact about the message and not about the
    order these patterns are written in.
    """
    if not text:
        return None
    lines = text.split("\n")
    offsets = _line_starts(text)
    candidates = [
        match.start()
        for match in (
            _TEXT_ATTRIBUTION_RE.search(text),
            _TEXT_ORIGINAL_MESSAGE_RE.search(text),
        )
        if match is not None
    ]
    candidates += [
        offset
        for offset in (
            _header_block_start(lines, offsets),
            _trailing_quote_run(lines, offsets),
        )
        if offset is not None
    ]
    return min(candidates) if candidates else None


def split_plain(text: str) -> tuple[str, str]:
    """Cut a plain-text body into `(visible, quoted)` at `find_quote_start`.

    As with `split_html`, both halves are slices: `visible + quoted` is the
    input, and `quoted` is `""` when there is no quote.
    """
    index = find_quote_start(text)
    if index is None:
        return text, ""
    return text[:index], text[index:]
