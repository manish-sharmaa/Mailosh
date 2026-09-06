"""Outbound delivery tracking: what happened to a message after "Sent".

`JmapClient.send_message` creates an RFC 8621 §7 ``EmailSubmission`` and
hands back its id, and until this module nothing ever read it again — the
UI said "Sent" whether the message was delivered, was still sitting in the
server's outbound queue (the live situation on a host whose port 25 is
blocked: messages queue for days), or bounced. This module closes that
loop:

- `record` writes an `OutboundSubmission` row the moment a send succeeds;
- `refresh` polls ``EmailSubmission/get`` for the rows whose outcome is
  still open and maps each ``undoStatus``/``deliveryStatus`` onto one of
  four `OutboundState`s (`classify`);
- `refresh_if_due` is the request-path entry point (`mailosh.web.mail`
  calls it on every list render, so a Stalwart push that re-fetches the
  list refreshes the state too), `sweep` the background one (the 300 s
  maintenance loop in `mailosh.web.app`);
- `states_for` is the one-query lookup a page of rows draws its pills from;
- `take_unannounced_bounces` hands out each bounce exactly once, for the
  toast.

**What Stalwart actually reports** (verified live against 0.16, and the
reason the mapping below is shaped the way it is): the instant a message
is accepted into the queue, ``undoStatus`` is already ``final`` and every
recipient's ``deliveryStatus`` is ``{delivered: "unknown", smtpReply: "250
2.1.5 Queued", displayed: "unknown"}`` — and it *stays* that way, even for
a recipient on the same server whose copy was delivered immediately. That
"unknown + 250 Queued" pair is therefore the baseline for every message,
not evidence of anything, and is mapped to `UNKNOWN` (which renders
nothing). The states that mean something arrive later, when Stalwart
processes a DSN for the submission: a delay notice moves ``delivered`` to
``queued`` and a failure to ``no`` with the remote server's reply in
``smtpReply``. A ``delivered: "yes"`` is mapped too, for a server that
reports it. `UNKNOWN` rows keep being polled — that is what lets a later
DSN change the answer — until `TRACK_FOR` has passed since the send.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db.models import OutboundSubmission
from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import EmailSubmission

logger = logging.getLogger(__name__)

#: How long after a send a row whose outcome is still open keeps being
#: polled. Stalwart's own queue gives up after five days by default
#: (``queue.schedule.expire``), and a DSN for that expiry is the last thing
#: that can change a row's state, so a week covers it with margin. Past
#: this a row is left exactly as it is — never deleted, just no longer
#: asked about.
TRACK_FOR = timedelta(days=7)

#: The request path (`refresh_if_due`) will not poll the server again
#: within this many seconds of the previous poll. A Stalwart push, the
#: coalesced ``mail:changed`` it turns into, a second tab, and the sweep
#: can all land within a second of one another; one ``EmailSubmission/get``
#: per burst is plenty, since the answer changes on the timescale of DSNs
#: (minutes to hours), not milliseconds.
MIN_POLL_INTERVAL = timedelta(seconds=10)

#: Cap on the SMTP reply kept as `detail` and shown in a tooltip/toast — a
#: remote server's reply is a stranger's text, and one that pastes a whole
#: bounce page into it must not become a multi-kilobyte ``title``.
_DETAIL_MAX = 300


class OutboundState(StrEnum):
    """The complete vocabulary of `OutboundSubmission.state` — defined next
    to the code that writes it, as `mailosh.db.models.Visibility` is.

    `QUEUED`/`FAILED` are the two the UI draws a pill for; `DELIVERED` and
    `UNKNOWN` both render nothing, and differ only in whether the server
    positively confirmed delivery (rare — see the module docstring) or
    simply has not said anything yet.
    """

    QUEUED = "queued"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


#: The states an `OutboundSubmission` can still move out of. `FAILED` and
#: `DELIVERED` are terminal: a bounce does not un-bounce, and a confirmed
#: delivery is the end of the story.
OPEN_STATES = (OutboundState.QUEUED, OutboundState.UNKNOWN)


@dataclass(frozen=True)
class Outcome:
    """`classify`'s answer: the state, and the detail worth keeping (the
    failing recipient and the server's reply, for a bounce; `None`
    otherwise)."""

    state: OutboundState
    detail: str | None = None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def classify(submission: EmailSubmission) -> Outcome:
    """Map one ``EmailSubmission`` onto an `Outcome`.

    Precedence, when recipients disagree: any recipient refused
    (``delivered: "no"``) makes the whole submission `FAILED`, since the
    sender has something to act on either way; otherwise any recipient
    still ``queued`` makes it `QUEUED`; otherwise every recipient must be
    ``yes`` for `DELIVERED`, and anything short of that — an ``unknown``, or
    a server that reported no ``deliveryStatus`` at all — is `UNKNOWN`.

    Two ``undoStatus`` cases come first because they are not about
    recipients: ``pending`` is the undo-send window, during which the
    message has not left the server (`QUEUED`, honestly), and ``canceled``
    means it never will (`FAILED`, with a detail that says so rather than
    quoting an SMTP reply that does not exist).

    The detail for a bounce is ``"<recipient>: <smtpReply>"`` — the
    recipient because a multi-recipient message may have bounced for one
    address only, the reply because it is the only diagnostic the server
    has. It is clipped to `_DETAIL_MAX` here, once, so every consumer
    (tooltip, toast, log line) shows the same text.
    """
    if submission.undo_status == "pending":
        return Outcome(OutboundState.QUEUED)
    if submission.undo_status == "canceled":
        return Outcome(OutboundState.FAILED, "Cancelled before it was sent")

    statuses = submission.delivery_status
    if not statuses:
        return Outcome(OutboundState.UNKNOWN)

    for recipient, status in statuses.items():
        if status.delivered == "no":
            reply = status.smtp_reply.strip() or "delivery failed"
            return Outcome(OutboundState.FAILED, _clip(f"{recipient}: {reply}"))
    if any(status.delivered == "queued" for status in statuses.values()):
        return Outcome(OutboundState.QUEUED)
    if all(status.delivered == "yes" for status in statuses.values()):
        return Outcome(OutboundState.DELIVERED)
    return Outcome(OutboundState.UNKNOWN)


@dataclass
class RefreshResult:
    """What one `refresh` did: every row whose state changed, and the
    subset that changed *to* `FAILED` — the ones a caller may want to
    announce. Both empty when nothing was open, or nothing moved."""

    checked: int = 0
    changed: list[OutboundSubmission] = field(default_factory=list)
    bounced: list[OutboundSubmission] = field(default_factory=list)

    @property
    def any_change(self) -> bool:
        return bool(self.changed)


async def record(
    db: AsyncSession,
    *,
    user_id: int,
    account_id: str,
    submission_id: str,
    email_id: str,
    thread_id: str | None = None,
    now: datetime | None = None,
) -> OutboundSubmission:
    """Start tracking a submission `send_message` just created — `QUEUED`,
    never checked. Committed here: the send has already happened, and a
    row that only exists until the request's session is rolled back is
    a message the tracker silently forgets.

    `thread_id` is whatever the caller knows, which for a fresh send is
    nothing (`send_message` returns no thread id); `refresh` fills it in
    from the first ``EmailSubmission/get``.
    """
    row = OutboundSubmission(
        user_id=user_id,
        account_id=account_id,
        submission_id=submission_id,
        email_id=email_id,
        thread_id=thread_id,
        created_at=now or datetime.now(UTC),
        state=OutboundState.QUEUED.value,
    )
    db.add(row)
    await db.commit()
    return row


def _aware(dt: datetime) -> datetime:
    """SQLite hands `DateTime(timezone=True)` values back naive; Postgres
    does not. Compare in UTC either way."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def open_rows(
    db: AsyncSession, user_id: int, account_id: str, *, now: datetime
) -> list[OutboundSubmission]:
    """The rows still worth asking the server about: this user's, in an
    `OPEN_STATES` state, sent within `TRACK_FOR`. One query."""
    since = now - TRACK_FOR
    result = await db.execute(
        select(OutboundSubmission)
        .where(
            OutboundSubmission.user_id == user_id,
            OutboundSubmission.account_id == account_id,
            OutboundSubmission.state.in_([state.value for state in OPEN_STATES]),
            OutboundSubmission.created_at >= since,
        )
        .order_by(OutboundSubmission.created_at.desc())
    )
    return list(result.scalars())


def _apply(row: OutboundSubmission, submission: EmailSubmission, now: datetime) -> bool:
    """Write `classify(submission)` onto `row`; True if its state moved."""
    outcome = classify(submission)
    changed = row.state != outcome.state.value
    row.state = outcome.state.value
    row.detail = outcome.detail
    row.last_checked_at = now
    if submission.thread_id and not row.thread_id:
        row.thread_id = submission.thread_id
    return changed


async def refresh(
    client: JmapClient,
    db: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
    rows: Sequence[OutboundSubmission] | None = None,
) -> RefreshResult:
    """Poll ``EmailSubmission/get`` for this user's open rows and record
    what came back. One JMAP request for the whole set, none if there is
    nothing open.

    A submission the server no longer returns (``notFound``) is marked
    `UNKNOWN` with a detail saying so, and its `last_checked_at` still
    advances — it stays open in case the server is briefly inconsistent,
    and falls out of the polling window with everything else after
    `TRACK_FOR`. A `JmapError`/`TransportError` from the poll is *not*
    caught here: the request-path caller treats it as "no news" and the
    sweep logs it; neither wants a half-applied result.

    `rows` lets a caller that already selected the open rows (the sweep,
    which needs them to know which users to visit) hand them in rather than
    have them selected again.
    """
    now = now or datetime.now(UTC)
    if rows is None:
        rows = await open_rows(db, user_id, client.account_id, now=now)
    result = RefreshResult(checked=len(rows))
    if not rows:
        return result

    by_id = {row.submission_id: row for row in rows}
    submissions = await client.get_submissions(list(by_id))
    seen: set[str] = set()
    for submission in submissions:
        row = by_id.get(submission.id)
        if row is None:
            continue
        seen.add(submission.id)
        was = row.state
        if _apply(row, submission, now):
            result.changed.append(row)
            if row.state == OutboundState.FAILED.value and was != OutboundState.FAILED.value:
                result.bounced.append(row)
    for submission_id, row in by_id.items():
        if submission_id in seen:
            continue
        row.last_checked_at = now
        if row.state != OutboundState.UNKNOWN.value:
            row.state = OutboundState.UNKNOWN.value
            row.detail = "The server no longer reports this submission"
            result.changed.append(row)
    await db.commit()
    return result


async def refresh_if_due(
    client: JmapClient, db: AsyncSession, user_id: int, *, now: datetime | None = None
) -> RefreshResult:
    """`refresh`, rate-limited for the request path: polls only when some
    open row has not been checked within `MIN_POLL_INTERVAL` (or ever).

    Called on every list render (`mailosh.web.mail._list_context`), which
    is also what a Stalwart ``EmailSubmission`` push turns into — so the
    cost for the common case, a user with nothing in flight, is one indexed
    SELECT and no JMAP call at all. A failure to reach the server is
    swallowed here (logged, empty result): the page being rendered is
    about the user's mail, and a delivery pill that is a few minutes stale
    is not worth failing it over.
    """
    now = now or datetime.now(UTC)
    rows = await open_rows(db, user_id, client.account_id, now=now)
    due = [
        row
        for row in rows
        if row.last_checked_at is None or now - _aware(row.last_checked_at) >= MIN_POLL_INTERVAL
    ]
    if not due:
        return RefreshResult(checked=0)
    try:
        return await refresh(client, db, user_id, now=now, rows=rows)
    except JmapError:
        logger.warning("outbound: delivery-state poll failed for user %s", user_id, exc_info=True)
        await db.rollback()
        return RefreshResult(checked=0)


async def states_for(
    db: AsyncSession, user_id: int, email_ids: Iterable[str]
) -> dict[str, OutboundSubmission]:
    """The tracked submission for each of `email_ids` that has one, keyed by
    email id — one query for a whole page of rows, never one per row.

    An email id can in principle carry two submissions (a message sent,
    then sent again); the newer one wins, since it is the attempt whose
    outcome the sender is waiting on.
    """
    ids = list(dict.fromkeys(email_ids))
    if not ids:
        return {}
    result = await db.execute(
        select(OutboundSubmission)
        .where(OutboundSubmission.user_id == user_id, OutboundSubmission.email_id.in_(ids))
        .order_by(OutboundSubmission.created_at.asc())
    )
    return {row.email_id: row for row in result.scalars()}


async def take_unannounced_bounces(
    db: AsyncSession, user_id: int, *, now: datetime | None = None
) -> list[OutboundSubmission]:
    """This user's `FAILED` rows nobody has been told about yet, marked as
    told. Each bounce is handed out exactly once, whichever request happens
    to render first — the toast that announces it is a one-time event, not
    a banner, and the row's pill carries the state from then on."""
    now = now or datetime.now(UTC)
    result = await db.execute(
        select(OutboundSubmission).where(
            OutboundSubmission.user_id == user_id,
            OutboundSubmission.state == OutboundState.FAILED.value,
            OutboundSubmission.notified_at.is_(None),
        )
    )
    rows = list(result.scalars())
    if not rows:
        return []
    for row in rows:
        row.notified_at = now
    await db.commit()
    return rows


def bounce_toast(row: OutboundSubmission) -> str:
    """The one sentence a bounce toast says. `detail` is
    ``"<recipient>: <reply>"`` (see `classify`), already clipped."""
    if row.detail:
        return f"Couldn't deliver to {row.detail}"
    return "A message you sent couldn't be delivered"


async def sweep(app: object) -> None:
    """The maintenance-loop entry point: refresh every user who has an open
    row and a live session, and tell their open tabs when anything moved.

    Takes the FastAPI ``app`` (typed loosely to spare `mailosh.web.app` a
    circular import) for the four things on its ``state`` this needs:
    ``sessionmaker``, ``settings``, ``pool`` and ``hubs``. A user with open
    rows but no live session is skipped — there is no credential to poll
    with, and nobody looking; their rows are refreshed on their next list
    render. Never raises: the loop that calls this treats any exception as
    fatal to itself, so each user's failure is logged and the sweep moves
    on.
    """
    # Imported here rather than at module top: `mailosh.security.sessions`
    # pulls in the app's settings/crypto stack, which the pure functions
    # above (and their tests) have no need of.
    from mailosh.security import sessions

    state = getattr(app, "state", None)
    sessionmaker = getattr(state, "sessionmaker", None)
    if sessionmaker is None:
        return
    now = datetime.now(UTC)
    try:
        async with sessionmaker() as db:
            result = await db.execute(
                select(OutboundSubmission.user_id, OutboundSubmission.account_id)
                .where(
                    OutboundSubmission.state.in_([s.value for s in OPEN_STATES]),
                    OutboundSubmission.created_at >= now - TRACK_FOR,
                )
                .distinct()
            )
            targets = list(result.all())
    except Exception:
        logger.exception("outbound sweep: could not list open submissions")
        return

    for user_id, _account_id in targets:
        try:
            async with sessionmaker() as db:
                session = await sessions.live_session_for_user(db, user_id, state.settings, now)
                if session is None:
                    continue
                client = await state.pool.get(session, state.settings)
                outcome = await refresh(client, db, user_id, now=now)
            if outcome.any_change:
                state.hubs.notify(user_id, ["EmailSubmission"])
                for row in outcome.bounced:
                    logger.info(
                        "outbound: submission %s for user %s bounced: %s",
                        row.submission_id,
                        user_id,
                        row.detail,
                    )
        except Exception:
            logger.exception("outbound sweep: refresh failed for user %s", user_id)
