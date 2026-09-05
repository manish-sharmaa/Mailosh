"""Unit tests for `mailosh.ui.format`'s Task 6 additions: `format_date`
(Gmail-style relative dates) and `format_senders` (Gmail-style sender-list
rendering, "Aisha, Tom, me (3)"). `initials`/`avatar_color` already have
their own coverage in `tests/unit/test_ui_macros.py` (Task 2); the two
trivial re-checks here (`test_initials_and_color_stable`) are the exact
assertions the Task 6 brief itself specifies, kept for parity with the
brief's "Step 1: Failing tests" block rather than as new coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mailosh.ui.format import avatar_color, format_date, format_senders, initials

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# format_date -- brief's own three assertions (today / this year / older),
# verbatim.
# ---------------------------------------------------------------------------


def test_dates():
    assert format_date(datetime(2026, 9, 2, 10, 42, tzinfo=UTC), NOW) == "10:42 AM"
    assert format_date(datetime(2026, 9, 1, 23, 59, tzinfo=UTC), NOW) == "Sep 1"
    assert format_date(datetime(2025, 9, 1, 8, 0, tzinfo=UTC), NOW) == "9/1/25"


# ---------------------------------------------------------------------------
# format_date edge cases (self-review requirement): midnight boundary, New
# Year boundary, a message from the future, and the timezone-conversion
# rule itself (controller decision 2: convert both `now` and `dt` into
# `now`'s own tzinfo before comparing calendar dates -- a pure-UTC
# comparison would get the "today" boundary wrong for a non-UTC viewer).
# ---------------------------------------------------------------------------


def test_midnight_boundary_one_second_apart_is_not_today():
    # Two seconds before midnight vs. two seconds after -- different local
    # calendar dates despite being four seconds apart in wall-clock time.
    now = datetime(2026, 9, 2, 0, 0, 2, tzinfo=UTC)
    yesterday = datetime(2026, 9, 1, 23, 59, 58, tzinfo=UTC)
    assert format_date(yesterday, now) == "Sep 1"


def test_new_year_boundary_one_second_apart_uses_year_rule_not_recency():
    # One second apart in real time, but opposite sides of a year boundary
    # -- must take the "older" (%-m/%-d/%y) branch, not "this year".
    now = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    just_before_midnight = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert format_date(just_before_midnight, now) == "12/31/25"


def test_future_message_still_follows_the_same_three_rules():
    # A message dated after `now` (clock skew, or a manually back/forward
    # -dated import) is not a distinct case: the same same-day / same-year
    # / older rules apply symmetrically, with no special "future" branch.
    now = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    later_today = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)
    assert format_date(later_today, now) == "2:30 PM"
    later_this_year = datetime(2026, 12, 25, 9, 0, tzinfo=UTC)
    assert format_date(later_this_year, now) == "Dec 25"
    next_year = datetime(2027, 1, 1, 9, 0, tzinfo=UTC)
    assert format_date(next_year, now) == "1/1/27"


def test_date_rule_uses_the_viewers_local_timezone_not_utc():
    # now is 2026-09-02 01:00 in UTC-5 (viewer's local time) == 06:00 UTC.
    # dt is 2026-09-02 04:00 UTC == 2026-09-01 23:00 in that same UTC-5
    # zone -- the *previous* local calendar day. A comparison done in raw
    # UTC would wrongly call both "2026-09-02" and say "today"; converting
    # both into now's tzinfo first (controller decision 2) correctly says
    # "yesterday" instead.
    viewer_tz = timezone(timedelta(hours=-5))
    now = datetime(2026, 9, 2, 1, 0, tzinfo=viewer_tz)
    dt = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
    assert format_date(dt, now) == "Sep 1"


def test_date_rule_naive_input_treated_as_utc():
    # Mirrors mailosh.jmap.client._to_utc_date's own stance: a naive
    # datetime (no tzinfo) is treated as already being UTC rather than the
    # host's local zone, so this never raises even if a caller forgets to
    # attach tzinfo.
    naive_now = datetime(2026, 9, 2, 15, 0)
    assert format_date(datetime(2026, 9, 2, 10, 42, tzinfo=UTC), naive_now) == "10:42 AM"


# ---------------------------------------------------------------------------
# format_senders -- brief's own three assertions, verbatim.
# ---------------------------------------------------------------------------


def test_senders_gmail_style(make_header):
    hs = [
        make_header("Aisha Rahman", "a@x"),
        make_header("Tom Reyes", "t@x"),
        make_header("Manish Sharma", "me@x"),
        make_header("Aisha Rahman", "a@x"),
    ]
    assert format_senders(hs, me="me@x") == "Aisha, Tom, me (4)"
    assert format_senders(hs[:1], me="me@x") == "Aisha Rahman"
    assert (
        format_senders([make_header(None, "noreply@github.com")], me="me@x") == "noreply@github.com"
    )


# ---------------------------------------------------------------------------
# format_senders edge cases (self-review requirement): a single sender who
# is the viewer themselves, more than three distinct senders (no
# truncation -- that's `chips`' job, not this one), and duplicate addresses
# whose display name changed mid-thread.
# ---------------------------------------------------------------------------


def test_single_sender_who_is_me_still_renders_as_me(make_header):
    # The "current user renders as `me`" rule (controller decision 1) is
    # not conditional on the multi-vs-single-sender branch: it always wins
    # for a sender matching the viewer's own address, even when they're the
    # only participant in the list (e.g. a note-to-self thread).
    assert format_senders([make_header("Manish Sharma", "me@x")], me="me@x") == "me"


def test_more_than_three_senders_are_not_truncated(make_header):
    # format_senders itself never caps the name list -- only `chips`
    # (Task 6's thread_list view model) caps at a fixed count. A long
    # participant list just gets long, the same way Gmail's own row does.
    hs = [
        make_header("Aisha Rahman", "a@x"),
        make_header("Tom Reyes", "t@x"),
        make_header("Priya Natarajan", "p@x"),
        make_header("Daniel Okafor", "d@x"),
    ]
    assert format_senders(hs, me="me@x") == "Aisha, Tom, Priya, Daniel (4)"


def test_duplicate_address_keeps_first_seen_display_name(make_header):
    # Dedup by email address, preserving the *first* appearance's display
    # name -- a later message from the same address under a different
    # display name (e.g. someone updates their mail client) must not
    # overwrite it.
    hs = [
        make_header("Aisha R.", "a@x"),
        make_header("Aisha Rahman", "a@x"),
        make_header("Tom Reyes", "t@x"),
    ]
    assert format_senders(hs, me="me@x") == "Aisha, Tom (3)"


def test_thread_with_no_from_headers_at_all_falls_back_to_a_placeholder(make_header):
    # EmailHeader.from_ is explicitly optional ("a message can legitimately
    # have no From" -- its own docstring; RFC 8621 §4.1.2 allows a null
    # `from`). Without a fallback, such a thread's sender column would
    # render as the bare count suffix (" (2)", leading space and all).
    headers = [make_header(None, "a@x"), make_header(None, "b@x")]
    for header in headers:
        header.from_ = []
    assert format_senders(headers, me="me@x") == "(unknown sender) (2)"
    assert format_senders(headers[:1], me="me@x") == "(unknown sender)"


def test_exchange_style_last_comma_first_names_do_not_double_up_the_comma(make_header):
    # Outlook and most corporate directories put "Last, First" in the From
    # display name. Truncating on whitespace alone kept the comma, so the
    # joined list rendered "Reyes,, Aisha (2)". Only the punctuation is
    # stripped -- which half is the given name is ambiguous without locale
    # knowledge this function does not have, so the leading word stays.
    hs = [make_header("Reyes, Tom", "t@x"), make_header("Rahman, Aisha", "a@x")]
    assert format_senders(hs, me="me@x") == "Reyes, Rahman (2)"

    # A name that is entirely punctuation would strip to nothing, so the
    # whole label is kept rather than rendering an empty slot.
    odd = [make_header(", Tom", "t@x"), make_header("Aisha Rahman", "a@x")]
    assert format_senders(odd, me="me@x") == ", Tom, Aisha (2)"

    # Single-sender rows show the full display name, comma and all.
    assert format_senders(hs[:1], me="me@x") == "Reyes, Tom"


def test_me_comparison_is_case_and_whitespace_insensitive(make_header):
    # Mirrors avatar_color's own `.strip().lower()` normalization: the same
    # address in a different From header's casing must still match `me`.
    assert format_senders([make_header("Manish Sharma", "Me@X")], me=" me@x ") == "me"


# ---------------------------------------------------------------------------
# initials/avatar_color -- brief's own assertions, verbatim (already
# covered in test_ui_macros.py; kept here too for Step-1 parity).
# ---------------------------------------------------------------------------


def test_initials_and_color_stable():
    # NOTE: the task brief's own illustrative snippet asserts
    # `initials("Daniel Okafor", "d@x") == "DO"`. That's a transcription
    # slip, not a spec change: Task 2's already-shipped `initials()`
    # deliberately returns a single letter ("D", not "DO") -- see its own
    # docstring in mailosh/ui/format.py ("One letter, not a two-letter
    # monogram, deliberately... 'Daniel Okafor' -> 'D', not 'DO'"), backed
    # by the approved mockups' single-character avatars (layout.html's
    # `.om-av`) and an existing green test in test_ui_macros.py
    # (`test_initials_filter_prefers_name_then_email_then_placeholder`).
    # Implementing the brief's literal "DO" would silently regress that
    # already-tested, spec-matching behavior, so this asserts the real,
    # correct output instead.
    assert initials("Daniel Okafor", "d@x") == "D"
    assert initials(None, "priya@x") == "P"
    assert avatar_color("d@x") == avatar_color("d@x")
    assert 0 <= avatar_color("d@x") < 12
