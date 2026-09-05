"""The search grammar (design spec §10): what a reader types -> a JMAP
`Email/query` filter.

Pure, synchronous, no I/O and no imports from the rest of the app: the
whole grammar is exercisable from a unit test with a two-method stub, which
is the point — everything below is a decision about what a *query means*,
and none of it needs a server to be checked.

Security
--------
`JmapClient.query_search` passes the filter it is handed to the server
verbatim, and says so in its own docstring: this parser is the only thing
that decides what a reader's query is allowed to mean. So the rule here is
absolute — **no key in the returned filter ever comes from user text**.
Every key is a constant declared in this module (`ALLOWED_FILTER_KEYS`,
`FILTER_OPERATOR_KEYS`); reader text only ever *chooses* one of them, and
only ever lands in a filter *value*. `_is_allowed_filter` re-checks the
finished tree against that allow-list before it is returned, so a future
edit that broke the rule would fail closed (match nothing) rather than hand
the server a key a reader invented. `accountId:x`, `{"inMailbox":` and
every other shape of that attempt are free text, nothing more.

Never raises
------------
Spec §10: "unknown operators produce an inline hint, never a 500". That
extends to every malformed thing a person can type — unbalanced brackets,
`before:notadate`, `larger:12x`, a bare `-`, 10 KB of nested parens, a
resolver that throws. Every one of them returns a `ParseResult` carrying
`Hint`s. `MAX_QUERY_CHARS`/`MAX_TERMS`/`MAX_GROUP_DEPTH` bound the work so
that nothing pathological can blow the stack or the clock on the request
path, and the parser collapses double negation instead of nesting it.

The failure this is written to avoid is *silently* dropping a term the
reader typed: a search that quietly looks for less than was asked is worse
than one that says it did not understand. So every term that cannot be
honoured produces a hint, and the two shapes of "cannot" are treated
differently on purpose (`_compile_term`):

- a term that is *complete but unresolvable* (`label:Nope`, `in:Nope`)
  matches nothing, because widening a search back out to everything
  because a label was misspelled is the dangerous direction;
- a term that is *unfinished or malformed* (`label:`, `before:notadate`,
  `larger:12x`) is hinted and dropped, because there is no narrower
  reading of it to apply and the reader is usually mid-type.

Grammar and precedence
----------------------
    query    := or_seq*                  # juxtaposition = implicit AND
    or_seq   := unary ("OR" unary)*      # OR binds tighter than AND
    unary    := "-"* (group | term)      # "-" binds tightest
    group    := "(" query ")"
    term     := "name:value" | "phrase" | word

**`a OR b c` means `(a OR b) AND c`** — Gmail's precedence, matched
deliberately for a UI whose whole thesis is that a Gmail user recognises
it. Parentheses override it. `OR` is only an operator spelled in capitals
(Gmail's rule); lowercase `or` is an ordinary word. There is no `AND`
keyword — §10 names `OR` as the only connector, and juxtaposition already
means AND — so a capitalised `AND` is searched as text.

**Repeated operators AND.** `from:a from:b` is "from a *and* from b" (so,
in practice, nothing), not "from a or from b". Juxtaposition means AND for
every term, and an operator is not special; the reader who wants the union
writes `from:a OR from:b`. The alternative — reading a repeat as OR —
would quietly widen a search past what was typed, which is the direction
this module never takes.

Dates and sizes
---------------
Dates are interpreted at UTC midnight (the server stores `receivedAt` in
UTC and the parser has no reader timezone to consult) and inherit RFC 8621
§4.4.1's own boundaries: **`after:` is inclusive, `before:` is exclusive**.
`after:2026-09-05` matches mail that arrived during 5 September;
`before:2026-09-05` does not. `older_than:7d`/`newer_than:7d` are the same
two keys measured from `now` (injectable, so relative dates are testable
without freezing the clock), with `m`/`y` counted as calendar months and
years, clamping the day (31 March minus one month is 28 February).

Sizes are **1024-based**: `larger:5m` is 5 MiB = 5_242_880 bytes, not five
million. `larger:` becomes `minSize`, which RFC 8621 defines as ">=", and
`smaller:` becomes `maxSize`, defined as "<" — so a message of exactly
5 MiB matches `larger:5m` and does not match `smaller:5m`. The asymmetry is
JMAP's, kept rather than papered over.

Scope
-----
The default scope is everything except Spam and Trash. That exclusion is
*not* part of the returned filter: it is `exclude_mailbox_ids`, for the
caller to apply as `inMailboxOtherThan` the way `thread_list` already does
for its own views. A **positive** `in:`/`label:` term hands scope to the
reader (`scope_was_explicit`), which empties the exclusion list; `-in:spam`
does not, because excluding a folder is not choosing a scope, and treating
it as one would silently pull Spam and Trash *into* a search that was
trying to narrow.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import MINYEAR, UTC, datetime, timedelta
from typing import Protocol

log = logging.getLogger(__name__)

#: RFC 8621 §4.1.1 keywords, repeated here rather than imported from
#: `mailosh.services.actions` so this module keeps its "no app imports"
#: property (importing that would drag in the JMAP client and httpx for two
#: string constants).
_SEEN = "$seen"
_FLAGGED = "$flagged"

#: Every `FilterCondition` property this parser may emit, with the exact
#: Python type its value must have. This is the allow-list the module
#: docstring's security note is about: it is deliberately *narrower* than
#: RFC 8621 §4.4.1's full condition (no `header`, no `*InThreadHaveKeyword`,
#: and notably no `inMailboxOtherThan` — the default-scope exclusion travels
#: as `ParseResult.exclude_mailbox_ids` for the caller to apply, so this
#: parser never needs to emit it and therefore is not permitted to).
_FILTER_KEY_TYPES: dict[str, type] = {
    "text": str,
    "from": str,
    "to": str,
    "cc": str,
    "bcc": str,
    "subject": str,
    "body": str,
    "hasAttachment": bool,
    "hasKeyword": str,
    "notKeyword": str,
    "inMailbox": str,
    "before": str,
    "after": str,
    "minSize": int,
    "maxSize": int,
}

#: The public form of the allow-list, derived from the type map above so the
#: two cannot drift apart.
ALLOWED_FILTER_KEYS: frozenset[str] = frozenset(_FILTER_KEY_TYPES)

#: RFC 8621 §4.4.1's `FilterOperator`: exactly these two keys, exactly these
#: three operators. Anything else in an operator node is a bug, not a filter.
FILTER_OPERATOR_KEYS: frozenset[str] = frozenset({"operator", "conditions"})
FILTER_OPERATORS: frozenset[str] = frozenset({"AND", "OR", "NOT"})

#: The six `Hint.kind` values, named so callers and tests share one
#: vocabulary instead of retyping the strings.
HINT_UNKNOWN_OPERATOR = "unknown-operator"
HINT_BAD_DATE = "bad-date"
HINT_BAD_SIZE = "bad-size"
HINT_UNKNOWN_LABEL = "unknown-label"
HINT_UNBALANCED = "unbalanced"
HINT_APPROXIMATED = "approximated"
HINT_SCOPE_UNCERTAIN = "scope-uncertain"

#: Bounds. This runs on the request path against text from a search box, so
#: every dimension a person (or a script) can grow is capped: the input
#: itself, the number of conditions the tree may hold, and how deep bracket
#: nesting may recurse. `MAX_GROUP_DEPTH` is what keeps recursion shallow —
#: three frames per level, so ~72 at the cap, against Python's default 1000.
MAX_QUERY_CHARS = 4096
MAX_TERMS = 256
MAX_GROUP_DEPTH = 24

#: JMAP `UnsignedInt` tops out at 2^53-1 (RFC 8620 §1.3); a size past it is
#: clamped rather than sent, so `larger:999999g` cannot become a request the
#: server rejects outright.
_MAX_SIZE = 2**53 - 1

_MIN_DATETIME = datetime(MINYEAR, 1, 1, tzinfo=UTC)

#: C0 controls and DEL, normalised to spaces before tokenising: a NUL or a
#: stray escape inside a search box is not a term, and passing one through
#: to the server's query parser is a needless thing to find out about.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_ISO_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_US_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_RELATIVE_RE = re.compile(r"(\d{1,6})\s*([dwmy])", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d{1,12})\s*([kmg]?)b?", re.IGNORECASE)
#: What makes `foo:bar` look like a *failed operator* rather than ordinary
#: text with a colon in it: a short, purely alphabetic name. `10:30` and
#: `https://example.test` are text and draw no hint; `accountId:x` draws one
#: and is still searched as text.
_OPERATOR_NAME_RE = re.compile(r"[A-Za-z_]{1,20}")


class MailboxResolver(Protocol):
    """The two lookups the grammar needs from the caller's mailbox list.

    A protocol rather than the `NavModel` itself so this module stays free
    of app imports (and so tests can hand it a dict). Both return `None`
    for "no such thing", never raise — and if an implementation does raise,
    `_Ctx._resolve` treats it as `None` rather than letting a 500 out.
    """

    def by_role(self, role: str) -> str | None:
        """Mailbox id for a JMAP role: `inbox`, `sent`, `drafts`,
        `archive`, `spam` (the `junk` role, under §5.2's nav name) or
        `trash`."""

    def by_label_name(self, name: str) -> str | None:
        """Mailbox id for a label, matched case-insensitively."""


@dataclass(frozen=True)
class Hint:
    """One thing the parser could not do, in a sentence a reader can act on.

    `token` is the text that caused it, for the UI to highlight in the
    search pill; `None` when the hint is about the query as a whole.
    """

    kind: str
    message: str
    token: str | None = None


@dataclass(frozen=True)
class ParseResult:
    """A parsed query.

    `filter` is `None` when the query constrains nothing (an empty box, or
    `in:anywhere` alone) — the caller should read that as "no constraint",
    not as "match nothing". `exclude_mailbox_ids` carries the default
    Spam/Trash exclusion for the caller to apply as `inMailboxOtherThan`,
    and is empty exactly when `scope_was_explicit` is true.
    """

    filter: dict[str, object] | None
    hints: tuple[Hint, ...]
    exclude_mailbox_ids: tuple[str, ...]
    scope_was_explicit: bool


def _match_nothing() -> dict[str, object]:
    """A filter no message can satisfy, built from allow-listed keys only.

    A message cannot both have and not have `$seen`, so this is a genuine
    server-side contradiction rather than a sentinel the server has to
    understand. The alternative — an `inMailbox` pointing at an invented id
    — would depend on how the server treats an unknown mailbox, which is a
    coin toss between "no results" and "an error the reader sees as a 500".

    Freshly built on every call: the result is handed to a caller that may
    well mutate it, and a shared module-level dict would let one search
    corrupt the next.
    """
    return {"operator": "AND", "conditions": [{"hasKeyword": _SEEN}, {"notKeyword": _SEEN}]}


def _and(nodes: list[dict[str, object] | None]) -> dict[str, object] | None:
    return _combine("AND", nodes)


def _or(nodes: list[dict[str, object] | None]) -> dict[str, object] | None:
    """The union of the branches that produced a constraint.

    Branches that produced nothing (a dropped `label:`, a dangling `OR`)
    are left out rather than treated as "match everything" — which is what
    an unconstrained branch of an OR technically means, and which would let
    one unfinished term quietly turn a narrow search into a search for the
    whole mailbox. Whatever emptied the branch has already left a hint.
    """
    return _combine("OR", nodes)


def _combine(operator: str, nodes: list[dict[str, object] | None]) -> dict[str, object] | None:
    real = [node for node in nodes if node is not None]
    if not real:
        return None
    if len(real) == 1:
        return real[0]
    return {"operator": operator, "conditions": real}


def _not(node: dict[str, object] | None) -> dict[str, object] | None:
    """`NOT node`, collapsing `NOT NOT x` back to `x`.

    RFC 8621's NOT is "none of the conditions match", so with a single
    condition the double negative is exactly the original — and collapsing
    it is what stops `----------x` (or ten thousand dashes) from building a
    tree as deep as the reader has patience for.
    """
    if node is None:
        return None
    if _is_operator_node(node) and node.get("operator") == "NOT":
        conditions = node.get("conditions")
        if isinstance(conditions, list) and len(conditions) == 1:
            inner = conditions[0]
            if isinstance(inner, dict):
                return inner
    return {"operator": "NOT", "conditions": [node]}


def _is_operator_node(node: dict[str, object]) -> bool:
    return "operator" in node or "conditions" in node


def _text(value: str) -> dict[str, object] | None:
    value = value.strip()
    if not value:
        return None
    return {"text": value}


def _excerpt(value: str, limit: int = 40) -> str:
    """Reader text, made safe to quote inside a one-sentence hint.

    A hint sits under the search pill, so what it quotes has to fit on a
    line: newlines (legal inside a quoted phrase) are collapsed and
    anything long is cut. `Hint.token` keeps the text exactly as typed --
    that one is for highlighting the input, and highlighting needs the
    original.
    """
    collapsed = " ".join(value.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "\u2026"
    return collapsed


class _Ctx:
    """Everything the parse accumulates besides the tree: hints, the term
    budget, whether the reader chose a scope, and the resolver."""

    def __init__(self, resolver: MailboxResolver, now: datetime) -> None:
        self.resolver = resolver
        self.now = now
        self.terms = 0
        self.scope_explicit = False
        self._hints: list[Hint] = []
        self._seen: set[tuple[str, str, str | None]] = set()
        #: Set when the resolver *misbehaved* -- raised, or answered with
        #: something that is not a mailbox id. Deliberately NOT set when it
        #: answers `None`, which is the honest "this account has no such
        #: mailbox". The two look identical downstream and mean opposite
        #: things for the default scope: no Spam folder means there is
        #: nothing to exclude, while a resolver that could not tell us means
        #: Spam and Trash are about to be searched without anyone saying so.
        self.resolver_misbehaved = False

    def hint(self, kind: str, message: str, token: str | None = None) -> None:
        """Record a hint, ignoring an exact repeat.

        De-duplication is by (kind, message, token), so two different bad
        dates both get said and fifty identical unmatched brackets get said
        once — the reader needs to know what went wrong, not how often.
        """
        key = (kind, message, token)
        if key in self._seen:
            return
        self._seen.add(key)
        self._hints.append(Hint(kind=kind, message=message, token=token))

    def hints(self) -> tuple[Hint, ...]:
        return tuple(self._hints)

    def role(self, role: str) -> str | None:
        return self._resolve("by_role", role)

    def label(self, name: str) -> str | None:
        return self._resolve("by_label_name", name)

    def _resolve(self, method: str, key: str) -> str | None:
        """Call the caller's resolver without trusting it.

        A resolver that raises is a caller bug, but "never a 500" wins over
        being right about whose bug it is, so it is logged and read as
        "no such mailbox" (which fails towards *fewer* results, via
        `_match_nothing`). A resolver that returns something that is not a
        non-empty string is read the same way, so a non-string id can never
        reach the wire as an `inMailbox` value.

        The method is looked up *inside* the guard rather than bound by the
        caller, so an object that is not a `MailboxResolver` at all -- the
        wrong type, `None`, a half-built stub -- is the same tolerated
        nothing as a mailbox that does not exist. `_spam_and_trash` runs
        outside `parse_query`'s own catch-all, so this has to be total.
        """
        try:
            found = getattr(self.resolver, method)(key)
        # Deliberately broad: whose bug it is matters less than not 500ing a search.
        except Exception:
            log.exception("search_query: mailbox resolver failed for %r", key)
            self.resolver_misbehaved = True
            return None
        if isinstance(found, str) and found:
            return found
        if found is not None:
            # Answered, but with something that is not an id. Distinct from a
            # plain `None`, which is a truthful "no such mailbox".
            self.resolver_misbehaved = True
        return None


# --------------------------------------------------------------------------
# Tokenising
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # "term" | "or" | "neg" | "(" | ")"
    raw: str


def _tokenize(text: str, ctx: _Ctx) -> list[_Token]:
    """Split the query into terms, brackets, `OR`s and negations.

    Quotes suspend the term boundaries, so `subject:"a b"` and `"a (b)"`
    are each one term. A `-` is a negation only at the *start* of a token,
    which is what keeps `follow-up` a word and `-from:a` a negated term.
    An unclosed quote runs to the end of the input and says so.
    """
    tokens: list[_Token] = []
    i = 0
    size = len(text)
    while i < size:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char in "()":
            tokens.append(_Token(char, char))
            i += 1
            continue
        if char == "-":
            tokens.append(_Token("neg", "-"))
            i += 1
            continue
        start = i
        in_quotes = False
        while i < size:
            char = text[i]
            if char == '"':
                in_quotes = not in_quotes
                i += 1
                continue
            if not in_quotes and (char.isspace() or char in "()"):
                break
            i += 1
        raw = text[start:i]
        if in_quotes:
            ctx.hint(
                HINT_UNBALANCED,
                "A quotation mark was left open, so everything after it was read as one phrase.",
                raw,
            )
        tokens.append(_Token("or" if raw == "OR" else "term", raw))
    return tokens


def _unquote(value: str) -> str:
    """Strip one wrapping pair of quotes and any surrounding space.

    The quotes do not travel to the server: RFC 8621 leaves string matching
    server-defined, so passing `"a b"` through would be guessing at another
    parser's phrase syntax. The phrase is sent as its own text.
    """
    if value.startswith('"'):
        value = value[1:]
    if value.endswith('"'):
        value = value[:-1]
    return value.strip()


# --------------------------------------------------------------------------
# Terms
# --------------------------------------------------------------------------

#: `name:` -> the JMAP condition key it fills. The key emitted into the
#: filter is always the *value* from this table (a constant in this file);
#: the reader's text only ever selects the row.
_FIELD_KEYS: dict[str, str] = {
    "from": "from",
    "to": "to",
    "cc": "cc",
    "bcc": "bcc",
    "subject": "subject",
    "body": "body",
}

#: `is:` -> (condition key, keyword). Both halves are constants here.
_IS_CONDITIONS: dict[str, tuple[str, str]] = {
    "unread": ("notKeyword", _SEEN),
    "read": ("hasKeyword", _SEEN),
    "starred": ("hasKeyword", _FLAGGED),
}

#: `in:` -> (role passed to the resolver, name to use when telling the
#: reader that folder does not exist).
_IN_ROLES: dict[str, tuple[str, str]] = {
    "inbox": ("inbox", "Inbox"),
    "sent": ("sent", "Sent"),
    "drafts": ("drafts", "Drafts"),
    "archive": ("archive", "Archive"),
    "spam": ("spam", "Spam"),
    "trash": ("trash", "Trash"),
}

_DATE_KEYS: dict[str, str] = {"before": "before", "after": "after"}
#: `older_than:7d` is "received before seven days ago"; `newer_than:` is the
#: same instant read from the other side.
_RELATIVE_KEYS: dict[str, str] = {"older_than": "before", "newer_than": "after"}
_SIZE_KEYS: dict[str, str] = {"larger": "minSize", "smaller": "maxSize"}
_SIZE_UNITS: dict[str, int] = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _op_field(
    name: str, value: str, raw: str, ctx: _Ctx, negated: bool
) -> dict[str, object] | None:
    return {_FIELD_KEYS[name]: value}


def _op_has(name: str, value: str, raw: str, ctx: _Ctx, negated: bool) -> dict[str, object] | None:
    if value.lower() == "attachment":
        return {"hasAttachment": True}
    return _unknown_value(name, value, raw, ctx)


def _op_is(name: str, value: str, raw: str, ctx: _Ctx, negated: bool) -> dict[str, object] | None:
    condition = _IS_CONDITIONS.get(value.lower())
    if condition is None:
        return _unknown_value(name, value, raw, ctx)
    key, keyword = condition
    return {key: keyword}


def _op_in(name: str, value: str, raw: str, ctx: _Ctx, negated: bool) -> dict[str, object] | None:
    """`in:` — a system folder, `anywhere`, or (Gmail's own courtesy) a
    label by name.

    §10 lists only the seven folder words, but a Gmail user types
    `in:Receipts` for a label and expects it to work; falling through to
    the label lookup costs nothing and cannot widen anything, since an
    unresolvable name still matches nothing.
    """
    lowered = value.lower()
    if lowered == "anywhere":
        if negated:
            ctx.hint(
                HINT_UNKNOWN_OPERATOR,
                'Searching everywhere is not something that can be excluded, so "-in:anywhere" '
                "was ignored.",
                raw,
            )
            return None
        ctx.scope_explicit = True
        return None
    if not negated:
        ctx.scope_explicit = True
    role = _IN_ROLES.get(lowered)
    if role is None:
        return _label_condition(value, raw, ctx)
    role_name, display = role
    mailbox_id = ctx.role(role_name)
    if mailbox_id is None:
        ctx.hint(
            HINT_UNKNOWN_LABEL,
            f"This account has no {display} folder, so that part of the search cannot match "
            "anything.",
            raw,
        )
        return _match_nothing()
    return {"inMailbox": mailbox_id}


def _op_label(
    name: str, value: str, raw: str, ctx: _Ctx, negated: bool
) -> dict[str, object] | None:
    if not negated:
        ctx.scope_explicit = True
    return _label_condition(value, raw, ctx)


def _label_condition(value: str, raw: str, ctx: _Ctx) -> dict[str, object] | None:
    """A label by name, or a filter that matches nothing.

    Nothing, rather than no constraint at all: `label:Wrok` must not
    quietly become a search of the entire account. Spelled-wrong searches
    return nothing and say why; that is recoverable, and the opposite is
    not.
    """
    mailbox_id = ctx.label(value)
    if mailbox_id is None:
        ctx.hint(
            HINT_UNKNOWN_LABEL,
            f'There is no label called "{_excerpt(value)}", so that part of the search cannot '
            "match anything.",
            raw,
        )
        return _match_nothing()
    return {"inMailbox": mailbox_id}


def _op_date(name: str, value: str, raw: str, ctx: _Ctx, negated: bool) -> dict[str, object] | None:
    when = _parse_date(value)
    if when is None:
        ctx.hint(
            HINT_BAD_DATE,
            f'"{_excerpt(value)}" is not a date this can read, so that part of the search was '
            "ignored — try 2026-09-05.",
            raw,
        )
        return None
    return {_DATE_KEYS[name]: _utc_string(when)}


def _op_relative(
    name: str, value: str, raw: str, ctx: _Ctx, negated: bool
) -> dict[str, object] | None:
    match = _RELATIVE_RE.fullmatch(value)
    if match is None:
        ctx.hint(
            HINT_BAD_DATE,
            f'"{_excerpt(value)}" is not an age this can read, so that part of the search was '
            "ignored — try 7d, 2w, 3m or 1y.",
            raw,
        )
        return None
    when = _shift_back(ctx.now, int(match.group(1)), match.group(2).lower())
    return {_RELATIVE_KEYS[name]: _utc_string(when)}


def _op_size(name: str, value: str, raw: str, ctx: _Ctx, negated: bool) -> dict[str, object] | None:
    match = _SIZE_RE.fullmatch(value)
    if match is None:
        ctx.hint(
            HINT_BAD_SIZE,
            f'"{_excerpt(value)}" is not a size this can read, so that part of the search was '
            "ignored — try 5m for 5 MB.",
            raw,
        )
        return None
    size = int(match.group(1)) * _SIZE_UNITS[match.group(2).lower()]
    if size > _MAX_SIZE:
        size = _MAX_SIZE
        ctx.hint(
            HINT_APPROXIMATED,
            "That size is larger than any message can be, so the largest possible size was used "
            "instead.",
            raw,
        )
    return {_SIZE_KEYS[name]: size}


def _op_filename(
    name: str, value: str, raw: str, ctx: _Ctx, negated: bool
) -> dict[str, object] | None:
    """`filename:` — approximated, and said so.

    JMAP's `Email/query` has no attachment-name condition (RFC 8621
    §4.4.1), so the honest options are "search the whole message for that
    word" or "refuse". §10 picks the first and requires the hint; without
    it the reader would think they had a file-name search and be quietly
    wrong about what came back.
    """
    ctx.hint(
        HINT_APPROXIMATED,
        f'File names cannot be searched directly, so "{_excerpt(value)}" was looked for '
        "anywhere in the message instead.",
        raw,
    )
    return _text(value)


#: Every operator §10 names, and nothing else. One uniform signature
#: (`name, value, raw, ctx, negated`) so the dispatch in `_compile_term` has
#: no special cases -- several handlers ignore most of it, which is cheaper
#: than a table of differently shaped callables. `name` selects a key from a
#: table above; it never *becomes* one.
_OPERATOR_HANDLERS: dict[str, Callable[[str, str, str, _Ctx, bool], dict[str, object] | None]] = {
    **{name: _op_field for name in _FIELD_KEYS},
    "has": _op_has,
    "is": _op_is,
    "in": _op_in,
    "label": _op_label,
    **{name: _op_date for name in _DATE_KEYS},
    **{name: _op_relative for name in _RELATIVE_KEYS},
    **{name: _op_size for name in _SIZE_KEYS},
    "filename": _op_filename,
}


def _unknown_value(name: str, value: str, raw: str, ctx: _Ctx) -> dict[str, object] | None:
    """A known operator with a value it does not take (`is:blue`).

    Searched as plain text, like an unknown operator: it narrows rather
    than widens, and "you typed something I search for literally" is a
    result the reader can make sense of next to the hint.
    """
    ctx.hint(
        HINT_UNKNOWN_OPERATOR,
        f'"{_excerpt(value)}" is not something that can follow "{name}:", so '
        f'"{_excerpt(raw)}" was searched as ordinary text.',
        raw,
    )
    return _text(raw)


def _compile_term(raw: str, ctx: _Ctx, *, negated: bool) -> dict[str, object] | None:
    """One term -> one condition (or `None`, always with a hint saying so)."""
    if raw.startswith('"'):
        phrase = _unquote(raw)
        if not phrase:
            ctx.hint(
                HINT_UNBALANCED,
                "There is nothing between those quotation marks, so they were ignored.",
                raw,
            )
            return None
        return _text(phrase)

    name, separator, value = raw.partition(":")
    handler = _OPERATOR_HANDLERS.get(name.lower()) if separator else None
    if handler is None:
        if separator and _OPERATOR_NAME_RE.fullmatch(name) and not value.startswith("/"):
            ctx.hint(
                HINT_UNKNOWN_OPERATOR,
                f'"{_excerpt(name, 20)}:" is not a search word this understands, so '
                f'"{_excerpt(raw)}" was searched as ordinary text.',
                raw,
            )
        return _text(raw)

    value = _unquote(value)
    if not value:
        ctx.hint(
            HINT_UNKNOWN_OPERATOR,
            f'"{_excerpt(name.lower(), 20)}:" needs something after it, so it was ignored.',
            raw,
        )
        return None
    return handler(name.lower(), value, raw, ctx, negated)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def _parse_date(value: str) -> datetime | None:
    """`YYYY-MM-DD`, `YYYY/MM/DD` or `M/D/YYYY` at UTC midnight.

    Four leading digits mean year-first; otherwise the month leads, which
    makes `1/2/2026` the 2nd of January — Gmail's reading of the same
    string, and the reason `M/D/YYYY` is in §10's list at all. Single-digit
    months and days are accepted in both shapes; a date that does not exist
    (`2026-02-30`) is rejected here rather than rounded into February.
    """
    match = _ISO_DATE_RE.fullmatch(value)
    if match is not None:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = _US_DATE_RE.fullmatch(value)
        if match is None:
            return None
        month, day, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _shift_back(when: datetime, amount: int, unit: str) -> datetime:
    """`when` minus `amount` days/weeks/months/years, never out of range.

    Months and years are calendar arithmetic with the day clamped (31 March
    minus one month is 28 February, or 29 in a leap year) rather than
    30- and 365-day approximations, because "a month ago" is a date on a
    calendar to everyone who types it. Anything that would fall off the
    front of the calendar lands on year 1 instead of raising — the reader
    who types `older_than:999999y` gets "everything", which is what they
    asked for.
    """
    try:
        if unit == "d":
            return when - timedelta(days=amount)
        if unit == "w":
            return when - timedelta(weeks=amount)
        months = amount * 12 if unit == "y" else amount
        return _shift_months(when, months)
    except (OverflowError, ValueError):
        return _MIN_DATETIME


def _shift_months(when: datetime, months: int) -> datetime:
    total = when.year * 12 + (when.month - 1) - months
    if total < MINYEAR * 12:
        return _MIN_DATETIME
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def _utc_string(when: datetime) -> str:
    """RFC 8621 `UTCDate`, formatted by hand.

    `strftime("%Y")` is platform-dependent below year 1000 (and refuses
    year 1 outright on some libcs), and the clamped `older_than:` path can
    reach exactly there.
    """
    when = when.astimezone(UTC)
    return (
        f"{when.year:04d}-{when.month:02d}-{when.day:02d}"
        f"T{when.hour:02d}:{when.minute:02d}:{when.second:02d}Z"
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class _Parser:
    """Recursive descent over the token list, bounded in both directions.

    Depth is capped by `MAX_GROUP_DEPTH` (below), breadth by `MAX_TERMS`,
    and every branch that does not consume a token is guarded, so the
    parser cannot loop on input it does not understand.
    """

    def __init__(self, tokens: list[_Token], ctx: _Ctx) -> None:
        self._tokens = tokens
        self._pos = 0
        self._ctx = ctx

    def parse(self) -> dict[str, object] | None:
        return self._sequence(depth=0, in_group=False, negated=False)

    def _peek(self) -> _Token | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> None:
        self._pos += 1

    def _sequence(self, *, depth: int, in_group: bool, negated: bool) -> dict[str, object] | None:
        """A run of OR-sequences joined by implicit AND."""
        branches: list[dict[str, object] | None] = []
        while True:
            token = self._peek()
            if token is None:
                break
            if token.kind == ")":
                if in_group:
                    break
                self._advance()
                self._ctx.hint(
                    HINT_UNBALANCED,
                    "There is a closing bracket with nothing to close, so it was ignored.",
                    ")",
                )
                continue
            before = self._pos
            branches.append(self._or_sequence(depth=depth, negated=negated))
            if self._pos == before:  # unreachable; a stalled parser would hang a request
                self._advance()
        return _and(branches)

    def _or_sequence(self, *, depth: int, negated: bool) -> dict[str, object] | None:
        """`a OR b OR c` — tighter than the implicit AND around it."""
        parts = [self._unary(depth=depth, negated=negated)]
        while True:
            token = self._peek()
            if token is None or token.kind != "or":
                break
            self._advance()
            following = self._peek()
            if following is None or following.kind in (")", "or"):
                self._ctx.hint(
                    HINT_UNBALANCED,
                    'A word is missing on one side of "OR", so the "OR" was ignored.',
                    "OR",
                )
                continue
            parts.append(self._unary(depth=depth, negated=negated))
        return _or(parts)

    def _unary(self, *, depth: int, negated: bool) -> dict[str, object] | None:
        """Any number of leading `-`, then a bracketed group or a term."""
        negations = 0
        while True:
            token = self._peek()
            if token is None or token.kind != "neg":
                break
            self._advance()
            negations += 1
        flipped = negated != (negations % 2 == 1)

        token = self._peek()
        if token is None or token.kind == ")":
            if negations:
                self._ctx.hint(
                    HINT_UNBALANCED,
                    "A minus sign needs a word straight after it, so it was ignored.",
                    "-",
                )
            return None
        if token.kind == "or":
            self._advance()
            self._ctx.hint(
                HINT_UNBALANCED,
                'A word is missing on one side of "OR", so the "OR" was ignored.',
                "OR",
            )
            return None
        if token.kind == "(":
            return self._group(depth=depth, negations=negations, flipped=flipped)

        self._advance()
        if self._ctx.terms >= MAX_TERMS:
            self._ctx.hint(
                HINT_APPROXIMATED,
                "That search is too long, so only the first part of it was used.",
            )
            return None
        self._ctx.terms += 1
        return _negate(_compile_term(token.raw, self._ctx, negated=flipped), negations)

    def _group(self, *, depth: int, negations: int, flipped: bool) -> dict[str, object] | None:
        """A bracketed sub-query.

        Past `MAX_GROUP_DEPTH` the bracket itself is dropped instead of
        recursed into: the contents still parse (at this level), so nothing
        the reader typed is lost, but the recursion cannot grow with the
        number of brackets in the input. Twenty-four is far past any query
        a person writes and far short of anything Python minds.
        """
        self._advance()
        if depth >= MAX_GROUP_DEPTH:
            self._ctx.hint(
                HINT_UNBALANCED,
                "That search has too many brackets, so the extra ones were ignored.",
                "(",
            )
            return None
        inside = self._sequence(depth=depth + 1, in_group=True, negated=flipped)
        closing = self._peek()
        if closing is not None and closing.kind == ")":
            self._advance()
        else:
            self._ctx.hint(
                HINT_UNBALANCED,
                "A bracket was left open, so it was closed at the end of the search.",
                "(",
            )
        if inside is None:
            self._ctx.hint(
                HINT_UNBALANCED,
                "There is nothing inside those brackets, so they were ignored.",
                "()",
            )
        return _negate(inside, negations)


def _negate(node: dict[str, object] | None, negations: int) -> dict[str, object] | None:
    return _not(node) if negations % 2 == 1 else node


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_query(
    text: str, *, resolver: MailboxResolver, now: datetime | None = None
) -> ParseResult:
    """Parse a reader's search into a JMAP filter, hints and a scope.

    See the module docstring for the grammar, its precedence (`OR` binds
    tighter than the implicit AND), the date and size boundaries, and why
    this never raises. `now` is injectable so `older_than:`/`newer_than:`
    can be asserted without freezing the clock; a naive `now` is read as
    UTC.
    """
    ctx = _Ctx(resolver, _normalise_now(now))
    try:
        node = _Parser(_tokenize(_prepare(text, ctx), ctx), ctx).parse()
        if node is not None and not _is_allowed_filter(node):
            # Unreachable by construction: every key written above is a
            # constant from this module. It is checked anyway because the
            # cost of being wrong is a reader-authored key on the wire.
            log.error("search_query built a filter outside the allow-list; discarding it")
            ctx.hint(
                HINT_UNKNOWN_OPERATOR,
                "That search could not be understood, so nothing was searched for.",
            )
            node = _match_nothing()
    # Deliberately broad: a search box must not be able to 500 the app, and a bug
    # reached from here would otherwise do exactly that.
    except Exception:
        log.exception("search_query failed to parse a query; returning an empty result")
        ctx.hint(
            HINT_UNKNOWN_OPERATOR,
            "That search could not be understood, so nothing was searched for.",
        )
        node = _match_nothing()

    exclude = () if ctx.scope_explicit else _spam_and_trash(ctx)
    return ParseResult(
        filter=node,
        hints=ctx.hints(),
        exclude_mailbox_ids=exclude,
        scope_was_explicit=ctx.scope_explicit,
    )


def _prepare(text: object, ctx: _Ctx) -> str:
    """Coerce, de-control and cap the raw input.

    Non-`str` input is a caller bug rather than a reader one, but coercing
    it costs a line and keeps the "never raises" promise total.
    """
    if not isinstance(text, str):
        try:
            text = "" if text is None else str(text)
        # Deliberately broad: a hostile __str__ is still not a reason to 500.
        except Exception:
            log.exception("search_query: query text could not be read as a string")
            text = ""
    text = _CONTROL_RE.sub(" ", text)
    if len(text) > MAX_QUERY_CHARS:
        cut = text[:MAX_QUERY_CHARS]
        spaced = cut.rsplit(" ", 1)[0]
        text = spaced or cut
        ctx.hint(
            HINT_APPROXIMATED,
            "That search is too long, so only the first part of it was used.",
        )
    return text


def _normalise_now(now: datetime | None) -> datetime:
    if not isinstance(now, datetime):
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _spam_and_trash(ctx: _Ctx) -> tuple[str, ...]:
    """The default scope's exclusion: Spam and Trash, if this account has
    them. Sorted, matching `JmapClient.query_page`'s own deterministic wire
    order for `inMailboxOtherThan`."""
    before = ctx.resolver_misbehaved
    found = {ctx.role("spam"), ctx.role("trash")}
    if ctx.resolver_misbehaved and not before:
        # This module's rule is that nothing a reader typed is dropped
        # silently. The same rule has to hold in the other direction: the
        # default scope is a promise that Spam and Trash stay out of the
        # results, and failing to identify them breaks that promise while
        # looking exactly like success. An account that simply has no Spam
        # folder is not this case -- the resolver answered `None`, and there
        # is genuinely nothing to exclude.
        ctx.hint(
            HINT_SCOPE_UNCERTAIN,
            "Spam and Trash could not be identified, so they may appear in "
            "these results. Add in:anywhere to search everything on purpose.",
        )
    return tuple(sorted(mailbox_id for mailbox_id in found if mailbox_id is not None))


def _is_allowed_filter(node: object) -> bool:
    """Whether `node` is a filter tree built only from allow-listed parts.

    Iterative on purpose: this is the check that has to hold when
    everything else has gone wrong, so it does not itself depend on the
    tree being shallow. Condition values are checked by exact type, not
    `isinstance`, so a `bool` cannot pass as a size and an id cannot pass
    as anything but a string.
    """
    stack: list[object] = [node]
    visited = 0
    while stack:
        visited += 1
        if visited > MAX_TERMS * 4:
            return False
        current = stack.pop()
        if not isinstance(current, dict) or not current:
            return False
        if _is_operator_node(current):
            if set(current) != FILTER_OPERATOR_KEYS:
                return False
            if current["operator"] not in FILTER_OPERATORS:
                return False
            conditions = current["conditions"]
            if not isinstance(conditions, list) or not conditions:
                return False
            stack.extend(conditions)
            continue
        for key, value in current.items():
            expected = _FILTER_KEY_TYPES.get(key)
            if expected is None or type(value) is not expected:
                return False
    return True
