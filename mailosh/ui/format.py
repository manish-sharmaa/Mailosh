"""View-model formatting helpers.

Task 2 landed `initials`/`avatar_color` — both are pure functions of data
the design system already has an opinion about (a name/email string, the
12-colour label palette), so they landed with the token system itself, and
`mailosh.ui.env.build_env` registers those two as Jinja filters
(`initials`, `avatar_color`) for templates to call directly.

Task 6 adds `format_date`/`format_senders`, which are **not** registered as
filters and are not called from a template at all: both need
`mailosh.jmap.models.EmailHeader` objects and a "now" convention that a
template has no way to supply, so `mailosh.services.thread_list.build_page`
calls them while building the view model and templates render the finished
`ThreadRow.date_display`/`ThreadRow.senders` strings.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from mailosh.jmap.models import EmailHeader

#: Spec §4.1's 12-colour label palette, in order — `avatar_color` returns an
#: index into this, and `label_color` narrows an arbitrary stored string to a
#: member of it. The palette's actual hex values live in `styles/input.css`
#: as `--label-<name>` custom properties (and, per theme, are swapped there);
#: this module only ever deals in the *names*, so a template can compose a
#: `var(--label-...)` reference without ever interpolating a colour value —
#: or anything else user-controlled — into a `style` attribute.
LABEL_COLORS: tuple[str, ...] = (
    "indigo",
    "emerald",
    "rose",
    "amber",
    "sky",
    "violet",
    "teal",
    "orange",
    "pink",
    "lime",
    "slate",
    "red",
)

_LABEL_PALETTE_SIZE = len(LABEL_COLORS)

#: What `format_senders` renders when *no* message in a thread carries a
#: usable `From` address. `EmailHeader.from_` is explicitly optional ("a
#: message can legitimately have no `From`" — its own docstring, and RFC
#: 8621 §4.1.2 allows a null `from`), so this is reachable, not theoretical:
#: without it the sender column of such a row would render as the bare
#: message-count suffix (`" (2)"`, leading space and all) or an empty
#: string. Same house style as
#: `mailosh.services.thread_list._NO_SUBJECT`'s `"(no subject)"`, and the
#: same string Gmail itself uses for the case.
_UNKNOWN_SENDER = "(unknown sender)"


def initials(name: str | None, email: str) -> str:
    """A single uppercase letter for an avatar: the first letter of `name`
    if there is one (stripped of surrounding whitespace), else the first
    letter of `email`'s local part, else `"?"` for a genuinely empty local
    part (e.g. an `email` of `"@example.com"` — malformed, but this
    function has no reason to raise over it).

    One letter, not a two-letter monogram, deliberately — every avatar in
    the approved mockups (`docs/design/mockups/layout.html`'s
    `.om-av`, `visual-style.html`'s `.ms-av`) renders exactly one character
    ("M", "D", ...), including for multi-word names ("Daniel Okafor" ->
    "D", not "DO").
    """
    source = (name or "").strip()
    if source:
        return source[:1].upper()
    local = email.split("@", 1)[0].strip()
    return local[:1].upper() if local else "?"


def avatar_color(email: str) -> int:
    """A stable index (`0` to `11`) into the 12-colour label palette
    (spec §4.1), deterministic across processes and restarts — so the same
    person's avatar is always the same colour everywhere they appear, for
    everyone, without storing a per-contact colour assignment anywhere.

    Built from a SHA-256 digest rather than Python's built-in `hash()`:
    `hash()` on a `str` is salted per-process (`PYTHONHASHSEED`) precisely
    so it's *not* stable across runs, which is exactly wrong here.
    `email` is lower-cased and stripped first so the same address in a
    different `From` header's casing (domains are case-insensitive in
    practice, and most providers treat the local part the same way,
    whatever RFC 5321 technically allows) still lands on the same color.
    """
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).digest()
    return digest[0] % _LABEL_PALETTE_SIZE


def label_color(value: str | None, seed: str | None = None) -> str:
    """Narrow a stored `LabelMeta.color` to one of `LABEL_COLORS`, so a
    template can safely build `var(--label-{{ x | label_color }})`.

    Registered as the `label_color` Jinja filter (`mailosh.ui.env`). The
    column is an unconstrained `String(32)` with no CHECK behind it, and its
    value reaches a `style` attribute — the one place in this UI where a
    stored string would otherwise be interpolated into CSS. Anything that
    isn't a known palette name (a legacy hex, a typo, `None`, or an outright
    injection attempt) never reaches the stylesheet.

    `seed` is what an uncoloured label falls back to: the same stable
    `avatar_color` hash `mailosh.services.thread_list` already uses to give
    such a label's *chips* a colour, so passing the mailbox id here makes a
    sidebar dot and that label's chips agree instead of one being a hashed
    colour and the other a flat grey. Without a seed the fallback is the
    palette's neutral, `"slate"`.
    """
    if value in LABEL_COLORS:
        return value  # type: ignore[return-value]
    return LABEL_COLORS[avatar_color(seed)] if seed else "slate"


def _as_aware(dt: datetime) -> datetime:
    """Treat a naive `dt` as already being UTC, mirroring
    `mailosh.jmap.client._to_utc_date`'s identical stance for outgoing
    JMAP UTCDate values: reinterpreting a naive value through some other
    zone would be a worse surprise for a caller than assuming UTC. Every
    `EmailHeader.received_at` this module actually sees is already aware
    (parsed from a JMAP UTCDate, which always carries a "Z"), so this only
    matters for a `now` a caller forgot to attach tzinfo to.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def format_date(dt: datetime, now: datetime) -> str:
    """Render `dt` the way spec §5.3's row date column does: `"10:42 AM"`
    if `dt` falls on `now`'s local calendar date, `"Sep 1"` if it's earlier
    this same local calendar year, else `"9/1/25"`.

    Controller decision 2 (Task 6 brief): comparisons happen in *`now`'s own
    timezone*, not UTC — both `dt` and `now` are converted into `now.
    tzinfo` before their calendar dates/years are compared. A pure-UTC
    comparison gets this wrong near midnight for any viewer not in UTC: a
    message stamped a few hours either side of midnight UTC can land on the
    *other* local calendar day for that viewer (see
    `tests/unit/test_format.py::test_date_rule_uses_the_viewers_local_timezone_not_utc`
    for a worked example) — "today" has to mean the viewer's today, not the
    server's.

    No special-casing for a `dt` *after* `now` (clock skew, or a
    deliberately back/forward-dated import): the same three rules apply
    symmetrically in either time direction, which already renders sensibly
    (see `test_future_message_still_follows_the_same_three_rules`) without
    a fourth branch.

    The `%-I`/`%-d`/`%-m` no-leading-zero `strftime` codes are a glibc/macOS
    extension, not a portable one (Windows needs `%#I` etc.) — fine here
    since this always runs server-side in the project's Linux containers,
    and the brief specifies these exact codes.
    """
    now = _as_aware(now)
    tz = now.tzinfo
    now_local = now.astimezone(tz)
    dt_local = _as_aware(dt).astimezone(tz)

    if dt_local.date() == now_local.date():
        return dt_local.strftime("%-I:%M %p")
    if dt_local.year == now_local.year:
        return dt_local.strftime("%b %-d")
    return dt_local.strftime("%-m/%-d/%y")


def format_full(dt: datetime, now: datetime) -> str:
    """The whole timestamp, spelled out: `Tue, Sep 1, 2026, 10:42 AM`.

    What `format_date` above leaves out. That one is the *column*: three
    characters wide where it can be, because a list of fifty rows is read by
    scanning it. This is the answer to "yes, but when exactly" — the message
    card's `<time title>`, the details popover's Date row and the list row's
    own tooltip, all of which are read one at a time and deliberately.

    Converted into `now`'s timezone for the same reason `format_date`
    compares in it: the reader's "when" is their own clock's, and a tooltip
    that disagreed with the timestamp it annotates would be worse than
    either being slightly wrong.

    It lives here rather than in `mailosh.services.conversation`, where it
    started, because the list rows want the same string and a second
    implementation of "the long form of a date" is how a conversation comes
    to disagree with the row it was opened from.
    """
    now = _as_aware(now)
    tz = now.tzinfo
    return _as_aware(dt).astimezone(tz).strftime("%a, %b %-d, %Y, %-I:%M %p")


def _first_name(label: str) -> str:
    """The leading word of a display name, for the multi-sender case.

    The trailing-punctuation strip is what makes Exchange-style
    `"Reyes, Tom"` (Last, First — the form Outlook and most corporate
    directories put in a `From` display name) render as `"Reyes"` rather
    than `"Reyes,"`, which joined into a sender list produced a visible
    `"Reyes,, Aisha (2)"`. Only the punctuation is stripped: which half of
    a comma-separated name is the *given* name is genuinely ambiguous
    without locale knowledge this function does not have, so this keeps the
    leading word and does not try to detect and re-order Last-First names.

    Falls back to the whole label if stripping would leave nothing (a name
    that is entirely punctuation, e.g. `", Tom"`).
    """
    words = label.split()
    if not words:
        return label
    return words[0].rstrip(",;") or label


def format_senders(emails: list[EmailHeader], me: str) -> str:
    """Render a thread's sender list the way spec §5.3's row does:
    `"Aisha, Tom, me (3)"` — unique senders (by address) in the order they
    first appear, `me` for the viewer's own address, first names only when
    more than one sender is shown, and a trailing `" (N)"` (N = message
    count, not distinct-sender count) whenever there's more than one
    message.

    `emails` is expected oldest -> newest (controller decision 1) — the
    caller (`mailosh.services.thread_list.build_page`) already sorts a
    thread's messages that way before calling this, both because that's
    the order a reader would encounter each sender in and because it makes
    "first appearance" well-defined when the same address shows up more
    than once (e.g. a duplicate row in the brief's own test fixture, or the
    same person replying twice) — later duplicates are dropped, keeping
    the *first* message's display name even if a later one used a
    different one (someone changing their mail client's display name
    mid-thread must not retroactively rewrite an earlier appearance).

    Every `EmailHeader.from_` address is considered (not just the first),
    matching the model's own list-typed `from_` field (RFC 8621 §4.1.2.3
    technically allows more than one), though in practice this is almost
    always a single address per message.

    "me" is an unconditional substitution, not merely the multi-sender
    "first name" rule coincidentally producing the word "me": it applies
    even in the single-sender case (a thread where the only sender shown is
    the viewer themself renders as literally `"me"`, not their own display
    name) — see
    `test_single_sender_who_is_me_still_renders_as_me`. `me` is compared
    case-/whitespace-insensitively, the same normalization `avatar_color`
    already applies to an address.

    A message whose `from_` is empty contributes no sender at all; if
    *every* message in the thread is like that, the whole list falls back
    to `_UNKNOWN_SENDER` rather than rendering as a bare `" (2)"` suffix.

    No sender-list truncation lives here even with many distinct senders —
    Gmail's own row doesn't truncate this list either; capping to a fixed
    count is `chips`' job (`mailosh.services.thread_list.ThreadRow.chips`),
    a different field entirely.
    """
    me_norm = me.strip().lower()
    labels_by_address: dict[str, str] = {}
    for header in emails:
        for addr in header.from_:
            address_norm = addr.email.strip().lower()
            if address_norm in labels_by_address:
                continue
            if address_norm == me_norm:
                labels_by_address[address_norm] = "me"
            else:
                labels_by_address[address_norm] = (addr.name or "").strip() or addr.email

    labels = list(labels_by_address.values())
    multiple_senders = len(labels) > 1

    parts = []
    for label in labels:
        if label == "me" or not multiple_senders:
            parts.append(label)
        else:
            parts.append(_first_name(label))

    text = ", ".join(parts) if parts else _UNKNOWN_SENDER
    message_count = len(emails)
    if message_count > 1:
        text += f" ({message_count})"
    return text
