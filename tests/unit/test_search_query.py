"""Unit tests for `mailosh.services.search_query` — the Gmail-operator
grammar (design spec §10).

Three kinds of test live here, and they are deliberately different in
shape:

- **Grammar** tests pin the decisions the module docstring writes down —
  precedence, repeated operators, date boundaries, size units — by
  asserting whole filter trees rather than fragments, because a fragment
  assertion cannot tell `(a OR b) AND c` from `a OR (b AND c)`.
- **Never-raises** tests run every malformed thing a person can type
  through `parse_query` and assert it came back as a `ParseResult`. A
  search box that can 500 is the failure §10 names explicitly.
- **Allow-list** tests are property-style over thousands of generated and
  adversarial queries: `JmapClient.query_search` sends this filter to the
  server verbatim, so "no key a reader typed can become a filter key" is
  checked over a corpus, not over three examples.

The resolver is a plain stub: `MailboxResolver` is a two-method protocol
precisely so the grammar can be tested with no server, no client and no
event loop.
"""

from __future__ import annotations

import dataclasses
import inspect
import random
import re
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from mailosh.services.search_query import (
    ALLOWED_FILTER_KEYS,
    FILTER_OPERATOR_KEYS,
    FILTER_OPERATORS,
    HINT_APPROXIMATED,
    HINT_BAD_DATE,
    HINT_BAD_SIZE,
    HINT_SCOPE_UNCERTAIN,
    HINT_UNBALANCED,
    HINT_UNKNOWN_LABEL,
    HINT_UNKNOWN_OPERATOR,
    MAX_GROUP_DEPTH,
    MAX_QUERY_CHARS,
    MAX_TERMS,
    Hint,
    ParseResult,
    parse_query,
)

#: A fixed clock. Every relative-date assertion below is written against
#: this instant, which is what `now` exists for -- a test that froze the
#: real clock would be testing the freezing.
NOW = datetime(2026, 9, 5, 12, 30, 45, tzinfo=UTC)

#: The six `kind` values the contract allows. Hints are rendered inline in
#: the search pill, so a seventh kind would render as nothing at all.
HINT_KINDS = frozenset(
    {
        HINT_UNKNOWN_OPERATOR,
        HINT_BAD_DATE,
        HINT_BAD_SIZE,
        HINT_UNKNOWN_LABEL,
        HINT_UNBALANCED,
        HINT_APPROXIMATED,
        HINT_SCOPE_UNCERTAIN,
    }
)

#: What a filter that cannot match anything looks like: a message can not
#: both have and not have `$seen`. Spelled out here rather than imported so
#: a change to the private constant has to be re-justified against the
#: tests that depend on "an unresolvable label matches *nothing*".
MATCH_NOTHING = {
    "operator": "AND",
    "conditions": [{"hasKeyword": "$seen"}, {"notKeyword": "$seen"}],
}


class _Resolver:
    """`MailboxResolver` over two dicts, recording what it was asked."""

    def __init__(
        self,
        *,
        roles: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.roles = {
            "inbox": "mb-inbox",
            "sent": "mb-sent",
            "drafts": "mb-drafts",
            "archive": "mb-archive",
            "spam": "mb-spam",
            "trash": "mb-trash",
        }
        if roles is not None:
            self.roles = roles
        self.labels = {"work": "mb-work", "work/urgent": "mb-urgent", "café": "mb-cafe"}
        if labels is not None:
            self.labels = labels
        self.asked_roles: list[str] = []
        self.asked_labels: list[str] = []

    def by_role(self, role: str) -> str | None:
        self.asked_roles.append(role)
        return self.roles.get(role)

    def by_label_name(self, name: str) -> str | None:
        self.asked_labels.append(name)
        return self.labels.get(name.lower())


@pytest.fixture
def resolver() -> _Resolver:
    return _Resolver()


def parse(text: str, resolver: _Resolver, *, now: datetime | None = NOW) -> ParseResult:
    return parse_query(text, resolver=resolver, now=now)


def filter_for(text: str, resolver: _Resolver, *, now: datetime | None = NOW):
    return parse(text, resolver, now=now).filter


def kinds(result: ParseResult) -> list[str]:
    return [hint.kind for hint in result.hints]


def filter_keys(node: object) -> set[str]:
    """Every dict key anywhere in a filter tree.

    Written iteratively and independently of the module's own walker: a
    validator that agreed with the code it validates because it *is* that
    code would prove nothing.
    """
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            found.update(str(key) for key in current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def assert_well_formed(node: object) -> None:
    """A filter is a `FilterOperator` tree over allow-listed conditions.

    This is the whole security property in one assertion, so it is checked
    structurally rather than by key set alone: operator nodes carry exactly
    `operator`/`conditions`, condition nodes carry only allow-listed keys,
    and nothing empty (an empty condition object matches everything).
    """
    if node is None:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        assert isinstance(current, dict), f"filter node is not an object: {current!r}"
        assert current, "empty filter object would match every message"
        if "operator" in current or "conditions" in current:
            assert set(current) == FILTER_OPERATOR_KEYS
            assert current["operator"] in FILTER_OPERATORS
            assert isinstance(current["conditions"], list)
            assert current["conditions"], "empty conditions list"
            stack.extend(current["conditions"])
            continue
        for key, value in current.items():
            assert key in ALLOWED_FILTER_KEYS, f"filter key {key!r} is not allow-listed"
            assert isinstance(value, str | bool | int), f"{key!r} carries {value!r}"


def assert_sane(result: ParseResult) -> None:
    """Everything that must hold of every `ParseResult`, whatever went in."""
    assert isinstance(result, ParseResult)
    assert_well_formed(result.filter)
    assert isinstance(result.hints, tuple)
    for hint in result.hints:
        assert isinstance(hint, Hint)
        assert hint.kind in HINT_KINDS, f"undocumented hint kind {hint.kind!r}"
        assert hint.message and hint.message[-1] in ".!", f"not a sentence: {hint.message!r}"
        assert hint.token is None or isinstance(hint.token, str)
    assert isinstance(result.exclude_mailbox_ids, tuple)
    assert all(isinstance(mailbox_id, str) for mailbox_id in result.exclude_mailbox_ids)
    assert isinstance(result.scope_was_explicit, bool)
    if result.scope_was_explicit:
        assert result.exclude_mailbox_ids == ()


# ---------------------------------------------------------------------------
# The contract two other modules are coded against
# ---------------------------------------------------------------------------


def test_the_public_shapes_are_what_callers_were_promised():
    """`ParseResult`/`Hint` field names, order and frozen-ness, and
    `parse_query`'s keyword-only arguments.

    The search UI and this parser were built in parallel against this
    signature, so a rename here is a broken caller elsewhere, not a
    refactor.
    """
    assert [f.name for f in dataclasses.fields(ParseResult)] == [
        "filter",
        "hints",
        "exclude_mailbox_ids",
        "scope_was_explicit",
    ]
    assert [f.name for f in dataclasses.fields(Hint)] == ["kind", "message", "token"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Hint(kind=HINT_BAD_DATE, message="x.", token=None).kind = "other"

    signature = inspect.signature(parse_query)
    assert list(signature.parameters) == ["text", "resolver", "now"]
    assert signature.parameters["text"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["resolver"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["now"].default is None


def test_the_allow_list_is_a_subset_of_rfc_8621_filter_condition():
    """The allow-list may shrink; it may not grow past what `Email/query`
    defines (RFC 8621 §4.4.1). `inMailboxOtherThan` is deliberately absent:
    the default-scope exclusion travels as `exclude_mailbox_ids`."""
    rfc_8621_conditions = {
        "inMailbox",
        "inMailboxOtherThan",
        "before",
        "after",
        "minSize",
        "maxSize",
        "allInThreadHaveKeyword",
        "someInThreadHaveKeyword",
        "noneInThreadHaveKeyword",
        "hasKeyword",
        "notKeyword",
        "hasAttachment",
        "text",
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "header",
    }
    assert ALLOWED_FILTER_KEYS <= rfc_8621_conditions
    assert "inMailboxOtherThan" not in ALLOWED_FILTER_KEYS
    assert "header" not in ALLOWED_FILTER_KEYS


# ---------------------------------------------------------------------------
# Free text, phrases, and the shape of an empty query
# ---------------------------------------------------------------------------


def test_an_empty_query_constrains_nothing_but_still_carries_the_default_scope(resolver):
    for text in ("", "   ", "\t\n", "\x00"):
        result = parse(text, resolver)
        assert result.filter is None, text
        assert result.hints == ()
        assert result.exclude_mailbox_ids == ("mb-spam", "mb-trash")
        assert result.scope_was_explicit is False


def test_free_text_becomes_text_and_several_words_are_anded(resolver):
    assert filter_for("hello", resolver) == {"text": "hello"}
    assert filter_for("hello world", resolver) == {
        "operator": "AND",
        "conditions": [{"text": "hello"}, {"text": "world"}],
    }


def test_a_quoted_phrase_is_one_condition_without_its_quotes(resolver):
    """The quotes are grammar, not payload: RFC 8621 leaves string matching
    server-defined, so forwarding `"a b"` would be guessing at another
    parser's phrase syntax."""
    assert filter_for('"quarterly report"', resolver) == {"text": "quarterly report"}
    assert filter_for('subject:"quarterly report"', resolver) == {"subject": "quarterly report"}


def test_lowercase_or_and_capitalised_and_are_ordinary_words(resolver):
    """Gmail's rule, kept: only a capitalised `OR` is the operator, and §10
    names no `AND` keyword at all."""
    assert filter_for("cat or dog", resolver) == {
        "operator": "AND",
        "conditions": [{"text": "cat"}, {"text": "or"}, {"text": "dog"}],
    }
    assert filter_for("cat AND dog", resolver) == {
        "operator": "AND",
        "conditions": [{"text": "cat"}, {"text": "AND"}, {"text": "dog"}],
    }


def test_a_hyphenated_word_is_a_word_not_a_negation(resolver):
    assert filter_for("follow-up", resolver) == {"text": "follow-up"}


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("from:a@b.test", {"from": "a@b.test"}),
        ("to:a@b.test", {"to": "a@b.test"}),
        ("cc:a@b.test", {"cc": "a@b.test"}),
        ("bcc:a@b.test", {"bcc": "a@b.test"}),
        ("subject:invoice", {"subject": "invoice"}),
        ("body:invoice", {"body": "invoice"}),
        ("has:attachment", {"hasAttachment": True}),
        ("is:unread", {"notKeyword": "$seen"}),
        ("is:read", {"hasKeyword": "$seen"}),
        ("is:starred", {"hasKeyword": "$flagged"}),
    ],
)
def test_each_operator_maps_to_its_jmap_condition(query, expected, resolver):
    assert filter_for(query, resolver) == expected


def test_operator_names_and_values_are_case_insensitive(resolver):
    assert filter_for("FROM:a@b.test", resolver) == {"from": "a@b.test"}
    assert filter_for("IS:Unread", resolver) == {"notKeyword": "$seen"}
    assert filter_for("Has:ATTACHMENT", resolver) == {"hasAttachment": True}
    assert filter_for("In:INBOX", resolver) == {"inMailbox": "mb-inbox"}


@pytest.mark.parametrize(
    "query",
    ["in:inbox", "in:sent", "in:drafts", "in:archive", "in:spam", "in:trash"],
)
def test_every_system_folder_resolves_through_the_resolver(query, resolver):
    role = query.split(":", 1)[1]
    assert filter_for(query, resolver) == {"inMailbox": resolver.roles[role]}
    assert role in resolver.asked_roles


def test_filename_is_approximated_as_text_and_says_so(resolver):
    """§10's own wording. Without the hint the reader would believe they
    had a file-name search and be quietly wrong about the results."""
    result = parse("filename:report.pdf", resolver)
    assert result.filter == {"text": "report.pdf"}
    assert kinds(result) == [HINT_APPROXIMATED]
    assert result.hints[0].token == "filename:report.pdf"
    assert "report.pdf" in result.hints[0].message


# ---------------------------------------------------------------------------
# Negation
# ---------------------------------------------------------------------------


def test_negation_applies_to_its_own_term_not_the_whole_query(resolver):
    """`-from:a@b.test invoice` is "not from that address, and mentions
    invoice" -- the NOT wraps one condition, and its sibling is untouched.
    """
    assert filter_for("-from:a@b.test invoice", resolver) == {
        "operator": "AND",
        "conditions": [
            {"operator": "NOT", "conditions": [{"from": "a@b.test"}]},
            {"text": "invoice"},
        ],
    }


def test_negation_works_on_free_text_phrases_and_groups(resolver):
    assert filter_for("-spam", resolver) == {
        "operator": "NOT",
        "conditions": [{"text": "spam"}],
    }
    assert filter_for('-"out of office"', resolver) == {
        "operator": "NOT",
        "conditions": [{"text": "out of office"}],
    }
    assert filter_for("-(a b)", resolver) == {
        "operator": "NOT",
        "conditions": [{"operator": "AND", "conditions": [{"text": "a"}, {"text": "b"}]}],
    }


def test_double_negation_collapses_instead_of_nesting(resolver):
    """RFC 8621's NOT over a single condition is an exact double negative,
    so collapsing is free -- and it is what stops a row of dashes from
    building a tree as deep as the reader has patience for."""
    assert filter_for("--x", resolver) == {"text": "x"}
    assert filter_for("---x", resolver) == {"operator": "NOT", "conditions": [{"text": "x"}]}
    assert filter_for("-(-x)", resolver) == {"text": "x"}


def test_a_wall_of_dashes_neither_recurses_nor_nests(resolver):
    """Parity, not nesting: two thousand dashes are the same as none, and
    two thousand and one are one NOT deep -- so no query can build a tree
    deeper than the reader's own brackets."""
    assert parse("-" * 2_000 + "x", resolver).filter == {"text": "x"}
    assert parse("-" * 2_001 + "x", resolver).filter == {
        "operator": "NOT",
        "conditions": [{"text": "x"}],
    }
    assert_sane(parse("-" * 20_000 + "x", resolver))


# ---------------------------------------------------------------------------
# Precedence and repetition -- the two decisions §10 leaves open
# ---------------------------------------------------------------------------


def test_or_binds_tighter_than_the_implicit_and(resolver):
    """`a OR b c` is `(a OR b) AND c`, Gmail's precedence.

    Asserted as a whole tree on purpose: the wrong reading
    (`a OR (b AND c)`) contains all the same conditions and would pass any
    assertion that only looked at the leaves.
    """
    assert filter_for("a OR b c", resolver) == {
        "operator": "AND",
        "conditions": [
            {"operator": "OR", "conditions": [{"text": "a"}, {"text": "b"}]},
            {"text": "c"},
        ],
    }
    assert filter_for("c a OR b", resolver) == {
        "operator": "AND",
        "conditions": [
            {"text": "c"},
            {"operator": "OR", "conditions": [{"text": "a"}, {"text": "b"}]},
        ],
    }


def test_parentheses_override_the_precedence(resolver):
    assert filter_for("a OR (b c)", resolver) == {
        "operator": "OR",
        "conditions": [
            {"text": "a"},
            {"operator": "AND", "conditions": [{"text": "b"}, {"text": "c"}]},
        ],
    }


def test_the_precedence_is_written_down_where_a_reader_of_the_code_will_find_it():
    """The brief's requirement: decide it, *document it*, test it. If the
    behaviour above ever changes, this fails until the docstring does
    too."""
    import mailosh.services.search_query as module

    assert "`a OR b c` means `(a OR b) AND c`" in module.__doc__
    assert "Repeated operators AND." in module.__doc__


def test_a_repeated_operator_is_anded_not_ored(resolver):
    """`from:a from:b` means both, which in practice means nothing.

    The union is available as `from:a OR from:b`; reading the repeat as OR
    would quietly widen a search past what was typed, which is the one
    direction this module never takes.
    """
    assert filter_for("from:a from:b", resolver) == {
        "operator": "AND",
        "conditions": [{"from": "a"}, {"from": "b"}],
    }
    assert filter_for("from:a OR from:b", resolver) == {
        "operator": "OR",
        "conditions": [{"from": "a"}, {"from": "b"}],
    }


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-05", "2026-09-05T00:00:00Z"),
        ("2026/09/05", "2026-09-05T00:00:00Z"),
        ("2026/9/5", "2026-09-05T00:00:00Z"),
        ("9/5/2026", "2026-09-05T00:00:00Z"),
        ("09/05/2026", "2026-09-05T00:00:00Z"),
    ],
)
def test_every_documented_date_format_parses_to_utc_midnight(value, expected, resolver):
    assert filter_for(f"after:{value}", resolver) == {"after": expected}


def test_a_slashed_date_is_read_month_first_like_gmail(resolver):
    """`1/2/2026` is 2 January, not 1 February -- which is why §10 spells
    the format `M/D/YYYY`."""
    assert filter_for("after:1/2/2026", resolver) == {"after": "2026-01-02T00:00:00Z"}


def test_after_is_inclusive_and_before_is_exclusive(resolver):
    """Both boundaries are RFC 8621 §4.4.1's own: `after` matches "the same
    as or after", `before` matches strictly earlier. So the pair below is
    the whole of 5 September and everything before it, and
    `before:2026-09-05` excludes the 5th entirely.
    """
    assert filter_for("after:2026-09-05", resolver) == {"after": "2026-09-05T00:00:00Z"}
    assert filter_for("before:2026-09-06", resolver) == {"before": "2026-09-06T00:00:00Z"}
    assert filter_for("after:2026-09-05 before:2026-09-06", resolver) == {
        "operator": "AND",
        "conditions": [{"after": "2026-09-05T00:00:00Z"}, {"before": "2026-09-06T00:00:00Z"}],
    }


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("older_than:7d", {"before": "2026-08-29T12:30:45Z"}),
        ("newer_than:7d", {"after": "2026-08-29T12:30:45Z"}),
        ("older_than:2w", {"before": "2026-08-22T12:30:45Z"}),
        ("older_than:3m", {"before": "2026-06-05T12:30:45Z"}),
        ("older_than:1y", {"before": "2025-09-05T12:30:45Z"}),
        ("newer_than:1Y", {"after": "2025-09-05T12:30:45Z"}),
    ],
)
def test_relative_dates_are_measured_from_the_injected_now(query, expected, resolver):
    assert filter_for(query, resolver) == expected


def test_months_and_years_are_calendar_arithmetic_with_the_day_clamped(resolver):
    """ "A month ago" is a date on a calendar to everyone who types it, not
    30 days. 31 March minus one month is the last day of February -- the
    29th in a leap year."""
    march = datetime(2026, 3, 31, 9, 0, 0, tzinfo=UTC)
    assert filter_for("older_than:1m", resolver, now=march) == {"before": "2026-02-28T09:00:00Z"}
    leap = datetime(2024, 3, 31, 9, 0, 0, tzinfo=UTC)
    assert filter_for("older_than:1m", resolver, now=leap) == {"before": "2024-02-29T09:00:00Z"}
    assert filter_for("older_than:1y", resolver, now=datetime(2024, 2, 29, tzinfo=UTC)) == {
        "before": "2023-02-28T00:00:00Z"
    }


def test_a_relative_date_that_falls_off_the_calendar_clamps_instead_of_raising(resolver):
    for query in ("older_than:999999y", "newer_than:999999m", "older_than:999999w"):
        result = parse(query, resolver)
        assert_sane(result)
        assert result.filter in (
            {"before": "0001-01-01T00:00:00Z"},
            {"after": "0001-01-01T00:00:00Z"},
        )


@pytest.mark.parametrize(
    "query",
    [
        "before:notadate",
        "after:2026-02-30",
        "before:13/45/2026",
        "after:20260905",
        "before:2026",
        "older_than:7",
        "newer_than:days",
        "older_than:7x",
        "newer_than:99999999999999d",
    ],
)
def test_a_date_that_cannot_be_read_is_hinted_and_dropped(query, resolver):
    """Dropped, not "matches nothing": unlike a misspelled label there is
    no narrower reading to apply, and the hint is what stops the drop being
    silent."""
    result = parse(query, resolver)
    assert result.filter is None
    assert kinds(result) == [HINT_BAD_DATE]
    assert result.hints[0].token == query


def test_a_naive_now_is_read_as_utc_and_an_aware_one_is_converted(resolver):
    naive = datetime(2026, 9, 5, 12, 30, 45)
    assert filter_for("older_than:1d", resolver, now=naive) == {"before": "2026-09-04T12:30:45Z"}
    tokyo = datetime(2026, 9, 5, 21, 30, 45, tzinfo=timezone(timedelta(hours=9)))
    assert filter_for("older_than:1d", resolver, now=tokyo) == {"before": "2026-09-04T12:30:45Z"}


def test_omitting_now_uses_the_current_clock(resolver):
    result = parse_query("newer_than:0d", resolver=resolver)
    stamp = datetime.strptime(result.filter["after"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 60


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("larger:5m", {"minSize": 5 * 1024 * 1024}),
        ("larger:5M", {"minSize": 5 * 1024 * 1024}),
        ("larger:5mb", {"minSize": 5 * 1024 * 1024}),
        ("larger:10k", {"minSize": 10 * 1024}),
        ("larger:2g", {"minSize": 2 * 1024 * 1024 * 1024}),
        ("larger:1500", {"minSize": 1500}),
        ("larger:1500b", {"minSize": 1500}),
        ("smaller:5m", {"maxSize": 5 * 1024 * 1024}),
        ("smaller:512k", {"maxSize": 512 * 1024}),
    ],
)
def test_sizes_are_1024_based_and_pick_the_right_bound(query, expected, resolver):
    assert filter_for(query, resolver) == expected


def test_a_megabyte_is_1024_kibibytes_not_a_million_bytes(resolver):
    """Written as its own test because it is the kind of decision that gets
    silently "fixed" later: 5m is 5_242_880, not 5_000_000."""
    assert filter_for("larger:5m", resolver) == {"minSize": 5_242_880}
    assert filter_for("larger:5m", resolver) != {"minSize": 5_000_000}


def test_the_size_boundary_is_jmaps_own_asymmetry(resolver):
    """`minSize` is ">=" and `maxSize` is "<" (RFC 8621 §4.4.1), so a
    message of exactly 5 MiB matches `larger:5m` and does not match
    `smaller:5m`. Kept rather than papered over -- the parser cannot make
    the server's boundary symmetric, so it documents it."""
    assert filter_for("larger:5m", resolver)["minSize"] == 5_242_880
    assert filter_for("smaller:5m", resolver)["maxSize"] == 5_242_880


@pytest.mark.parametrize("query", ["larger:12x", "smaller:big", "larger:-5m", "smaller:5.5m"])
def test_a_size_that_cannot_be_read_is_hinted_and_dropped(query, resolver):
    result = parse(query, resolver)
    assert result.filter is None
    assert HINT_BAD_SIZE in kinds(result)


def test_an_impossible_size_is_clamped_to_what_jmap_can_carry(resolver):
    """JMAP's `UnsignedInt` stops at 2^53-1; sending more would be an error
    the reader sees as a broken search rather than a silly one."""
    result = parse("larger:999999999999g", resolver)
    assert result.filter == {"minSize": 2**53 - 1}
    assert kinds(result) == [HINT_APPROXIMATED]


# ---------------------------------------------------------------------------
# Labels, folders and scope
# ---------------------------------------------------------------------------


def test_the_default_scope_excludes_spam_and_trash_without_touching_the_filter(resolver):
    """§10's "everything except Spam/Trash". It travels beside the filter,
    not inside it, because the caller applies it as `inMailboxOtherThan`
    exactly the way `thread_list` already does for its own views."""
    result = parse("invoice", resolver)
    assert result.filter == {"text": "invoice"}
    assert result.exclude_mailbox_ids == ("mb-spam", "mb-trash")
    assert result.scope_was_explicit is False
    assert filter_keys(result.filter) == {"text"}


def test_in_anywhere_clears_the_exclusion_and_constrains_nothing(resolver):
    result = parse("in:anywhere", resolver)
    assert result.filter is None
    assert result.exclude_mailbox_ids == ()
    assert result.scope_was_explicit is True


def test_naming_a_folder_or_label_hands_the_scope_to_the_reader(resolver):
    for query in ("in:inbox", "in:trash", "label:Work", "invoice label:Work"):
        result = parse(query, resolver)
        assert result.scope_was_explicit is True, query
        assert result.exclude_mailbox_ids == (), query


def test_excluding_a_folder_does_not_count_as_choosing_a_scope(resolver):
    """`-in:spam` narrows; it does not select where to search. Treating it
    as explicit would pull Spam and Trash *into* a query that was trying to
    push one of them out."""
    for query in ("-in:spam", "-label:Work", "-(label:Work)", "-(in:inbox)"):
        result = parse(query, resolver)
        assert result.scope_was_explicit is False, query
        assert result.exclude_mailbox_ids == ("mb-spam", "mb-trash"), query


def test_a_negation_cancelled_by_another_negation_is_positive_again(resolver):
    result = parse("-(-label:Work)", resolver)
    assert result.filter == {"inMailbox": "mb-work"}
    assert result.scope_was_explicit is True


def test_an_account_without_spam_or_trash_simply_has_nothing_to_exclude(resolver):
    empty = _Resolver(roles={"inbox": "mb-inbox"})
    result = parse("invoice", empty)
    assert result.exclude_mailbox_ids == ()
    assert result.scope_was_explicit is False


def test_labels_resolve_through_the_resolver_case_insensitively(resolver):
    assert filter_for("label:Work", resolver) == {"inMailbox": "mb-work"}
    assert filter_for("label:work", resolver) == {"inMailbox": "mb-work"}
    assert filter_for('label:"Work/Urgent"', resolver) == {"inMailbox": "mb-urgent"}
    assert filter_for("label:café", resolver) == {"inMailbox": "mb-cafe"}


def test_in_falls_through_to_a_label_of_that_name(resolver):
    """Gmail's own courtesy: a reader who types `in:Receipts` for a label
    expects it to work. It cannot widen anything -- an unresolvable name
    still matches nothing."""
    assert filter_for("in:Work", resolver) == {"inMailbox": "mb-work"}


def test_an_unknown_label_matches_nothing_and_never_everything(resolver):
    """The dangerous direction, spelled out: a misspelled label must not
    quietly turn into a search of the whole account. Nothing + a hint is
    recoverable; everything is not."""
    result = parse("label:Wrok", resolver)
    assert result.filter == MATCH_NOTHING
    assert result.filter is not None
    assert kinds(result) == [HINT_UNKNOWN_LABEL]
    assert result.hints[0].token == "label:Wrok"
    assert "Wrok" in result.hints[0].message


def test_an_unknown_label_beside_other_terms_still_kills_the_whole_and(resolver):
    result = parse("invoice label:Wrok", resolver)
    assert result.filter == {
        "operator": "AND",
        "conditions": [{"text": "invoice"}, MATCH_NOTHING],
    }
    assert kinds(result) == [HINT_UNKNOWN_LABEL]


def test_a_folder_this_account_does_not_have_matches_nothing_too(resolver):
    """`thread_list` makes the same call for the nav (a missing Archive
    returns an empty page rather than every message in the account); a
    search must not be the looser path."""
    thin = _Resolver(roles={"inbox": "mb-inbox"})
    result = parse("in:archive", thin)
    assert result.filter == MATCH_NOTHING
    assert kinds(result) == [HINT_UNKNOWN_LABEL]
    assert "Archive" in result.hints[0].message


def test_an_unresolvable_branch_of_an_or_cannot_widen_the_other_branch(resolver):
    """An OR branch that produced nothing is left out rather than read as
    "match everything" -- otherwise one unfinished term would turn a narrow
    search into a search of the entire mailbox."""
    assert filter_for("from:a@b.test OR label:", resolver) == {"from": "a@b.test"}
    assert filter_for("from:a@b.test OR before:notadate", resolver) == {"from": "a@b.test"}
    assert filter_for("from:a@b.test OR ()", resolver) == {"from": "a@b.test"}


def test_in_anywhere_cannot_be_negated_and_says_so(resolver):
    result = parse("-in:anywhere", resolver)
    assert result.filter is None
    assert result.scope_was_explicit is False
    assert kinds(result) == [HINT_UNKNOWN_OPERATOR]


# ---------------------------------------------------------------------------
# Unknown operators -- the free-text fall-through
# ---------------------------------------------------------------------------


def test_an_unknown_operator_is_searched_as_text_and_hinted(resolver):
    result = parse("accountId:x", resolver)
    assert result.filter == {"text": "accountId:x"}
    assert kinds(result) == [HINT_UNKNOWN_OPERATOR]
    assert result.hints[0].token == "accountId:x"


def test_a_known_operator_with_a_value_it_does_not_take_is_text_too(resolver):
    for query in ("is:blue", "has:banana", "in:", "is:"):
        result = parse(query, resolver)
        assert_sane(result)
        assert kinds(result) == [HINT_UNKNOWN_OPERATOR], query
        assert filter_keys(result.filter) <= {"text"}, query


def test_text_that_merely_contains_a_colon_draws_no_spurious_hint(resolver):
    """A URL and a clock time are not failed operators, and hinting at them
    would train the reader to ignore the hints."""
    for query in ("https://example.test/a", "10:30", "3:4:5", "12:x"):
        result = parse(query, resolver)
        assert result.hints == (), query
        assert filter_keys(result.filter) == {"text"}, query


def test_an_operator_with_nothing_after_it_is_hinted_and_dropped(resolver):
    """Unfinished, not unresolvable: the reader is mid-type, so there is
    nothing to narrow to -- but the term is never dropped in silence."""
    for query in ("label:", "from:", "before:", "larger:", 'subject:""'):
        result = parse(query, resolver)
        assert result.filter is None, query
        assert len(result.hints) >= 1, query


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------

MALFORMED = [
    "",
    " ",
    "-",
    "--",
    "- ",
    "-)",
    "(",
    ")",
    "()",
    "( )",
    "(a",
    "a)",
    "((a)",
    "(a))",
    ")(",
    '"',
    '""',
    '"a',
    'a"',
    '""""',
    "OR",
    "OR OR",
    "a OR",
    "OR a",
    "a OR OR b",
    "-OR",
    "(OR)",
    "label:",
    "label: ",
    'label:""',
    "in:",
    "is:",
    "has:",
    "before:",
    "before:notadate",
    "after:2026-13-45",
    "older_than:",
    "older_than:d",
    "older_than:-7d",
    "larger:",
    "larger:12x",
    "smaller:-1",
    "filename:",
    ":",
    "::",
    ":::",
    ":x",
    "x:",
    "-:",
    "-label:",
    "-(",
    "-)",
    "- -",
    "\x00\x01\x02",
    "\\",
    "\\\\",
    "%s",
    "{}",
    '{"inMailbox": "mb-1"}',
    '{"operator": "OR", "conditions": []}',
    "operator:OR conditions:x",
    "__proto__:1",
    "constructor:x",
    "a" * 5000,
    "(" * 100 + ")" * 100,
    "-" * 100,
    "OR " * 100,
    '"' * 101,
    "\u202e \ufeff",
    "🙂 label:🙂",
    "in:anywhere in:inbox label:x -in:spam",
]


@pytest.mark.parametrize("query", MALFORMED)
def test_nothing_a_person_can_type_raises(query, resolver):
    """§10: "unknown operators produce an inline hint, never a 500" --
    extended, as the brief requires, to every malformed thing a search box
    can receive."""
    assert_sane(parse(query, resolver))


def test_a_resolver_that_raises_is_a_hint_not_a_500(resolver):
    """Whose bug it is matters less than not taking the page down with it,
    and a resolver failure fails towards *fewer* results."""

    class Exploding:
        def by_role(self, role: str) -> str | None:
            raise RuntimeError("boom")

        def by_label_name(self, name: str) -> str | None:
            raise RuntimeError("boom")

    result = parse_query("label:Work invoice", resolver=Exploding(), now=NOW)
    assert_sane(result)
    assert result.filter == {
        "operator": "AND",
        "conditions": [MATCH_NOTHING, {"text": "invoice"}],
    }
    assert kinds(result) == [HINT_UNKNOWN_LABEL]


def test_a_resolver_that_returns_something_other_than_an_id_is_not_trusted(resolver):
    """A non-string id would reach the wire as an `inMailbox` value the
    server rejects, so it is read as "no such mailbox" instead."""

    class Wrong:
        def by_role(self, role: str):
            return 17

        def by_label_name(self, name: str):
            return ["mb-work"]

    result = parse_query("label:Work", resolver=Wrong(), now=NOW)
    assert_sane(result)
    assert result.filter == MATCH_NOTHING
    assert parse_query("hi", resolver=Wrong(), now=NOW).exclude_mailbox_ids == ()


@pytest.mark.parametrize("broken", [None, object(), "not a resolver", 17])
def test_a_caller_that_passes_something_that_is_not_a_resolver_gets_a_result(broken):
    """`_spam_and_trash` runs outside the catch-all around the parse, so
    the resolver guard has to cover "there is no such method" as well as
    "the method raised"."""
    plain = parse_query("invoice", resolver=broken, now=NOW)
    assert_sane(plain)
    assert plain.filter == {"text": "invoice"}
    assert plain.exclude_mailbox_ids == ()

    scoped = parse_query("label:Work invoice", resolver=broken, now=NOW)
    assert_sane(scoped)
    assert scoped.exclude_mailbox_ids == ()


@pytest.mark.parametrize("text", [None, 12345, ["a"], {"a": 1}, object()])
def test_a_caller_that_passes_something_other_than_a_string_gets_a_result(text, resolver):
    assert_sane(parse_query(text, resolver=resolver, now=NOW))


@pytest.mark.parametrize("now", ["nonsense", 0, object()])
def test_a_caller_that_passes_a_nonsense_clock_gets_a_result(now, resolver):
    assert_sane(parse_query("older_than:1d", resolver=resolver, now=now))


# ---------------------------------------------------------------------------
# Bounds: nothing on the request path may hang or recurse away
# ---------------------------------------------------------------------------


def test_pathological_nesting_neither_blows_the_stack_nor_takes_a_second(resolver):
    started = time.perf_counter()
    for query in (
        "(" * 10_000,
        "(" * 5_000 + "a" + ")" * 5_000,
        ")" * 10_000,
        "(a" * 3_000,
        "-(" * 3_000,
        '"(' * 3_000,
    ):
        assert_sane(parse(query, resolver))
    assert time.perf_counter() - started < 2.0


def test_content_inside_a_reasonable_nest_survives_the_depth_cap(resolver):
    """The cap drops the *bracket*, never the terms inside it: at
    `MAX_GROUP_DEPTH` the group stops recursing but its contents still
    parse, so no term the reader typed disappears."""
    deep = "(" * (MAX_GROUP_DEPTH + 10) + "invoice" + ")" * (MAX_GROUP_DEPTH + 10)
    result = parse(deep, resolver)
    assert result.filter == {"text": "invoice"}
    assert HINT_UNBALANCED in kinds(result)

    fine = "(" * MAX_GROUP_DEPTH + "invoice" + ")" * MAX_GROUP_DEPTH
    assert parse(fine, resolver).hints == ()


def test_a_query_longer_than_the_cap_is_truncated_and_says_so(resolver):
    result = parse("word " * 2_000, resolver)
    assert_sane(result)
    assert HINT_APPROXIMATED in kinds(result)
    assert len(result.filter["conditions"]) == MAX_TERMS


def test_the_number_of_conditions_is_bounded_whatever_arrives(resolver):
    for query in ("x " * 1_000, "from:a " * 500, "-y " * 1_000, "(a) " * 800):
        result = parse(query, resolver)
        assert_sane(result)
        conditions = result.filter.get("conditions", [result.filter])
        assert len(conditions) <= MAX_TERMS, query


def test_a_query_past_the_character_cap_is_cut_and_says_so(resolver):
    """One enormous word, so the *character* cap is the only thing that can
    trim it -- the term cap has nothing to count. A query that long is a
    script, not a reader, but it still gets told what happened rather than
    quietly searching for something shorter than it asked for.
    """
    result = parse("a" * (MAX_QUERY_CHARS + 500), resolver)
    assert_sane(result)
    assert kinds(result) == [HINT_APPROXIMATED]
    assert len(result.filter["text"]) <= MAX_QUERY_CHARS
    assert parse("a" * (MAX_QUERY_CHARS - 1), resolver).hints == ()


# ---------------------------------------------------------------------------
# The allow-list, property-style
# ---------------------------------------------------------------------------

#: Fragments chosen to be adversarial rather than representative: every
#: JMAP condition name, the two `FilterOperator` keys, JSON punctuation,
#: and the grammar's own metacharacters, so the generator spends most of
#: its time on strings that are *trying* to become a filter key.
_FUZZ_PIECES = [
    "from:", "to:", "cc:", "bcc:", "subject:", "body:", "label:", "in:", "is:", "has:",
    "before:", "after:", "older_than:", "newer_than:", "larger:", "smaller:", "filename:",
    "inMailbox", "inMailboxOtherThan", "hasKeyword", "notKeyword", "hasAttachment",
    "minSize", "maxSize", "header", "operator", "conditions", "accountId", "text",
    "inMailbox:", "operator:", "conditions:", "accountId:", "header:", "minSize:",
    "OR", "or", "AND", "-", "(", ")", '"', ":", "::", "\\", "{", "}", "[", "]", ",",
    "a", "Work", "work", "inbox", "anywhere", "unread", "starred", "attachment",
    "5m", "7d", "2026-09-05", "1/2/2026", "12x", "notadate", "$seen", "true", "null",
    "0", "-1", "999999999", "e" * 40, "é", "日本語", "🙂", "\x00", "\t", "\n", " ",
    "__proto__", "%s", "%(x)s", "{}", "*", "?", "|", "&", "!", "=", "<", ">", "/", "//",
]  # fmt: skip


def _generated_queries(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    queries = []
    for _ in range(count):
        pieces = [rng.choice(_FUZZ_PIECES) for _ in range(rng.randint(1, 24))]
        separators = [rng.choice(("", " ", "  ")) for _ in pieces]
        queries.append("".join(piece + sep for piece, sep in zip(pieces, separators, strict=True)))
    return queries


def _random_strings(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    alphabet = "abzAZ09 \t()\"'-:/\\{}[],.|&!=<>*?$%#@~`^+;\x00\x1bé日🙂"
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80))) for _ in range(count)]


CORPUS = _generated_queries(20260905, 2_500) + _random_strings(902, 1_000) + MALFORMED


def test_no_generated_query_can_produce_a_filter_key_outside_the_allow_list(resolver):
    """The property the whole module exists for.

    `JmapClient.query_search` sends this filter to the server verbatim and
    its docstring says this parser is the only thing deciding what a query
    may mean -- so "a reader cannot invent a filter key" is asserted over
    thousands of queries built out of the exact names that would be worth
    inventing (`inMailbox`, `header`, `accountId`, `operator`), not over
    three hand-picked examples.
    """
    permitted = ALLOWED_FILTER_KEYS | FILTER_OPERATOR_KEYS
    for query in CORPUS:
        result = parse(query, resolver)
        assert_sane(result)
        assert filter_keys(result.filter) <= permitted, query


def test_every_generated_query_terminates_quickly(resolver):
    started = time.perf_counter()
    for query in CORPUS:
        parse(query, resolver)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"{len(CORPUS)} queries took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "query",
    [
        '{"inMailbox": "mb-other"}',
        "inMailbox:mb-other",
        "inMailboxOtherThan:mb-other",
        "header:x",
        "accountId:other-account",
        "operator:OR",
        "conditions:[]",
        'text:"a" hasKeyword:$admin',
        "hasAttachment:false",
        "minSize:0",
        "__proto__:x",
    ],
)
def test_a_reader_typing_a_jmap_property_name_gets_a_text_search(query, resolver):
    """Not a filter key, not an error: ordinary text, the way any other
    word is."""
    result = parse(query, resolver)
    assert_sane(result)
    assert filter_keys(result.filter) <= {"text", "operator", "conditions"}


def test_a_label_named_after_a_jmap_property_is_still_only_a_value(resolver):
    """The resolver's answer lands in a *value*; the key comes from this
    module either way."""
    named = _Resolver(labels={'{"operator": "or"}': "mb-odd"})
    result = parse('label:"{\\"operator\\": \\"or\\"}"', named)
    assert_sane(result)
    assert filter_keys(result.filter) <= {"hasKeyword", "notKeyword", "operator", "conditions"}


def test_every_hint_kind_the_corpus_can_produce_is_one_of_the_documented_six(resolver):
    produced = set()
    for query in CORPUS:
        produced.update(kinds(parse(query, resolver)))
    assert produced <= HINT_KINDS
    assert len(produced) >= 5, "the corpus should be exercising most hint kinds"


# ---------------------------------------------------------------------------
# Hints
# ---------------------------------------------------------------------------


def test_identical_hints_are_said_once_and_different_ones_are_all_said(resolver):
    once = parse(")" * 50, resolver)
    assert len(once.hints) == 1

    several = parse("before:nope larger:12x label:Wrok", resolver)
    assert kinds(several) == [HINT_BAD_DATE, HINT_BAD_SIZE, HINT_UNKNOWN_LABEL]


def test_a_hint_points_at_the_text_that_caused_it(resolver):
    result = parse("invoice before:soon", resolver)
    assert result.hints[0].token == "before:soon"
    assert result.filter == {"text": "invoice"}


def test_hint_messages_are_one_plain_sentence(resolver):
    """They are rendered inline under a search box, so: no tracebacks, no
    JMAP vocabulary, no stack of clauses."""
    for query in CORPUS:
        for hint in parse(query, resolver).hints:
            assert hint.message.endswith((".", "!"))
            assert len(hint.message) <= 160, hint.message
            assert not re.search(r"\s\s|[\n\t]", hint.message), hint.message
            assert not re.search(r"JMAP|Filter(Operator|Condition)|Traceback", hint.message)


# ---------------------------------------------------------------------------
# The result is the caller's to keep
# ---------------------------------------------------------------------------


def test_two_parses_do_not_share_a_dict(resolver):
    """Callers mutate filters (chips rows edit the query, `query_search`
    hands the dict to a JSON encoder); a shared module-level constant would
    let one search corrupt the next."""
    first = parse("label:Wrok", resolver).filter
    first["conditions"].append({"text": "injected"})
    assert parse("label:Wrok", resolver).filter == MATCH_NOTHING

    literal = parse("hello", resolver).filter
    literal["text"] = "changed"
    assert parse("hello", resolver).filter == {"text": "hello"}


def test_a_realistic_query_end_to_end(resolver):
    """One query using most of the grammar at once, asserted whole -- the
    integration test this pure module can have."""
    result = parse(
        'from:ana@example.test (invoice OR "purchase order") '
        "-label:Work has:attachment is:unread after:2026-01-01 larger:1m",
        resolver,
    )
    assert result.hints == ()
    assert result.scope_was_explicit is False
    assert result.exclude_mailbox_ids == ("mb-spam", "mb-trash")
    assert result.filter == {
        "operator": "AND",
        "conditions": [
            {"from": "ana@example.test"},
            {
                "operator": "OR",
                "conditions": [{"text": "invoice"}, {"text": "purchase order"}],
            },
            {"operator": "NOT", "conditions": [{"inMailbox": "mb-work"}]},
            {"hasAttachment": True},
            {"notKeyword": "$seen"},
            {"after": "2026-01-01T00:00:00Z"},
            {"minSize": 1024 * 1024},
        ],
    }


# ---------------------------------------------------------------------------
# The validator itself
# ---------------------------------------------------------------------------

# Reached directly, unlike everything else here. `_is_allowed_filter` is the
# check that has to hold when the rest of the module has already gone wrong,
# so it is tested against trees the parser cannot currently produce -- which
# is exactly the code a future edit might start producing.
from mailosh.services.search_query import _is_allowed_filter  # noqa: E402


@pytest.mark.parametrize(
    "node",
    [
        {"text": "a"},
        {"hasAttachment": True},
        {"minSize": 0},
        {"inMailbox": "mb-1"},
        {"operator": "NOT", "conditions": [{"text": "a"}]},
        {
            "operator": "AND",
            "conditions": [{"operator": "OR", "conditions": [{"to": "a"}, {"cc": "b"}]}],
        },
    ],
)
def test_the_validator_accepts_a_well_formed_filter(node):
    assert _is_allowed_filter(node) is True


@pytest.mark.parametrize(
    "node",
    [
        {"accountId": "x"},  # a key that is not a filter condition at all
        {"header": ["a", "b"]},  # a real JMAP condition this module never emits
        {"inMailboxOtherThan": ["mb-1"]},  # travels as exclude_mailbox_ids instead
        {"text": 1},  # right key, wrong type
        {"minSize": "5"},
        {"minSize": True},  # a bool is not a size, even though bool is an int
        {"hasAttachment": "true"},
        {},  # an empty condition matches every message
        {"operator": "XOR", "conditions": [{"text": "a"}]},
        {"operator": "AND"},
        {"conditions": [{"text": "a"}]},
        {"operator": "AND", "conditions": []},
        {"operator": "AND", "conditions": [{}]},
        {"operator": "AND", "conditions": [{"text": "a"}], "accountId": "x"},
        {"operator": "AND", "conditions": {"text": "a"}},
        {"operator": "AND", "conditions": [{"text": "a"}, "raw"]},
        "text:a",
        [{"text": "a"}],
        None,
    ],
)
def test_the_validator_rejects_anything_else(node):
    assert _is_allowed_filter(node) is False


def test_the_validator_refuses_a_tree_too_large_to_have_come_from_here():
    node = {"text": "a"}
    for _ in range(MAX_TERMS * 4 + 1):
        node = {"operator": "NOT", "conditions": [node]}
    assert _is_allowed_filter(node) is False


# ---------------------------------------------------------------------------
# The default scope is a promise, and a broken resolver breaks it silently
# ---------------------------------------------------------------------------


class _RaisingResolver:
    def by_role(self, role: str) -> str | None:
        raise RuntimeError("jmap is down")

    def by_label_name(self, name: str) -> str | None:
        raise RuntimeError("jmap is down")


class _NoSpamOrTrash:
    """An account that genuinely has neither. `None` is the truthful answer."""

    def by_role(self, role: str) -> str | None:
        return None

    def by_label_name(self, name: str) -> str | None:
        return None


class _AnswersWithNonsense:
    def by_role(self, role: str) -> object:
        return 42

    def by_label_name(self, name: str) -> str | None:
        return None


def test_a_resolver_that_raises_says_so_rather_than_widening_in_silence():
    """Default scope excludes Spam and Trash. A resolver that cannot name
    them produces no exclusion — which looks exactly like success and
    quietly searches deleted mail.

    This module's rule is that nothing a reader typed is dropped silently;
    the same rule has to hold in the other direction, where scope is *added*
    silently.
    """
    result = parse_query("hello", resolver=_RaisingResolver())
    assert result.exclude_mailbox_ids == ()
    assert HINT_SCOPE_UNCERTAIN in {hint.kind for hint in result.hints}


def test_an_account_with_no_spam_folder_is_not_an_error():
    """The distinction that makes the hint worth having: `None` is a
    truthful "no such mailbox", and there is then genuinely nothing to
    exclude. Hinting here would cry wolf on every search for those accounts.
    """
    result = parse_query("hello", resolver=_NoSpamOrTrash())
    assert result.exclude_mailbox_ids == ()
    assert HINT_SCOPE_UNCERTAIN not in {hint.kind for hint in result.hints}


def test_a_resolver_answering_with_a_non_id_is_treated_as_misbehaviour():
    result = parse_query("hello", resolver=_AnswersWithNonsense())
    assert result.exclude_mailbox_ids == ()
    assert HINT_SCOPE_UNCERTAIN in {hint.kind for hint in result.hints}


def test_an_explicit_scope_does_not_warn_about_spam_and_trash():
    """`in:anywhere` asked for everything, so there is no broken promise to
    report and the resolver is never consulted for the default exclusion.
    """
    result = parse_query("in:anywhere hello", resolver=_RaisingResolver())
    assert HINT_SCOPE_UNCERTAIN not in {hint.kind for hint in result.hints}
