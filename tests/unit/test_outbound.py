"""Unit tests for `mailosh.services.outbound` — outbound delivery tracking.

Four properties, each pinned so it goes red when the property breaks rather
than when an implementation detail moves:

1. **The mapping is honest about what Stalwart says.** The exact
   ``EmailSubmission/get`` shape observed live against Stalwart 0.16 (final,
   every recipient ``unknown`` + ``250 2.1.5 Queued``) is `UNKNOWN` — not
   "queued", not "delivered" — and only a DSN-driven ``queued``/``no``/
   ``yes`` moves it. A ``no`` carries the SMTP reply as its detail.
2. **A refresh is one request for the whole open set**, never one per row,
   and nothing at all when nothing is open or the last poll was seconds ago.
3. **A bounce is announced once.** `take_unannounced_bounces` hands each
   failed row out exactly one time, whichever render happens to ask.
4. **The schema and the migration agree**, column for column.

`FakeClient` follows `tests/unit/test_labels.py`'s pattern: it records every
call, so "how many requests did that cost" is an assertion.
"""

from __future__ import annotations

import importlib.util
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mailosh.db import repo
from mailosh.db.models import OutboundSubmission
from mailosh.jmap.errors import TransportError
from mailosh.jmap.models import DeliveryStatus, EmailSubmission
from mailosh.services import outbound
from mailosh.services.outbound import OutboundState, Outcome, classify

ACCOUNT = "c"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

#: `EmailSubmission/get` exactly as Stalwart 0.16 answered it live (one
#: entry of the response recorded in the commit that added this module):
#: `undoStatus` already `final`, both recipients `unknown` with the queue's
#: own acceptance reply, no DSNs yet.
LIVE_STALWART_ENTRY = {
    "id": "b",
    "emailId": "ryaaaaeo",
    "identityId": "b",
    "threadId": "eo",
    "envelope": {
        "mailFrom": {"email": "demo@mailosh.test", "parameters": None},
        "rcptTo": [
            {"email": "demo@mailosh.test", "parameters": None},
            {"email": "probe-cc@example.test", "parameters": None},
        ],
    },
    "sendAt": "2026-09-05T06:46:40Z",
    "undoStatus": "final",
    "deliveryStatus": {
        "demo@mailosh.test": {
            "delivered": "unknown",
            "smtpReply": "250 2.1.5 Queued",
            "displayed": "unknown",
        },
        "probe-cc@example.test": {
            "delivered": "unknown",
            "smtpReply": "250 2.1.5 Queued",
            "displayed": "unknown",
        },
    },
    "dsnBlobIds": [],
    "mdnBlobIds": [],
}


def _submission(
    submission_id: str = "s1",
    *,
    email_id: str = "e1",
    undo_status: str = "final",
    status: dict[str, tuple[str, str]] | None = None,
    thread_id: str | None = "t1",
) -> EmailSubmission:
    """`status` maps recipient -> (delivered, smtpReply); `None` means the
    server reported no `deliveryStatus` at all."""
    delivery = None
    if status is not None:
        delivery = {
            rcpt: DeliveryStatus(delivered=delivered, smtp_reply=reply)
            for rcpt, (delivered, reply) in status.items()
        }
    return EmailSubmission(
        id=submission_id,
        email_id=email_id,
        thread_id=thread_id,
        undo_status=undo_status,
        delivery_status=delivery,
    )


# ---------------------------------------------------------------------------
# Models: the live wire shape parses, and null-safe
# ---------------------------------------------------------------------------


def test_email_submission_parses_the_live_stalwart_shape():
    parsed = EmailSubmission.model_validate(LIVE_STALWART_ENTRY)
    assert (parsed.id, parsed.email_id, parsed.thread_id) == ("b", "ryaaaaeo", "eo")
    assert parsed.undo_status == "final"
    assert parsed.send_at == datetime(2026, 9, 5, 6, 46, 40, tzinfo=UTC)
    assert set(parsed.delivery_status) == {"demo@mailosh.test", "probe-cc@example.test"}
    assert parsed.delivery_status["demo@mailosh.test"].smtp_reply == "250 2.1.5 Queued"
    assert parsed.dsn_blob_ids == []


def test_email_submission_tolerates_null_status_and_dsns():
    parsed = EmailSubmission.model_validate(
        {"id": "x", "emailId": "e", "undoStatus": "pending", "deliveryStatus": None}
    )
    assert parsed.delivery_status == {}
    assert parsed.dsn_blob_ids == []
    assert parsed.thread_id is None


# ---------------------------------------------------------------------------
# classify: the mapping
# ---------------------------------------------------------------------------


def test_the_live_stalwart_baseline_is_unknown_not_queued():
    """Every message Stalwart accepts looks exactly like this until a DSN
    says otherwise, so reading it as "Queued" would pin a pill on every
    Sent row forever, and reading it as "Delivered" would claim something
    the server never said."""
    assert classify(EmailSubmission.model_validate(LIVE_STALWART_ENTRY)) == Outcome(
        OutboundState.UNKNOWN
    )


def test_undo_window_pending_is_queued():
    assert classify(_submission(undo_status="pending")).state is OutboundState.QUEUED


def test_canceled_is_failed_with_a_plain_english_detail():
    outcome = classify(_submission(undo_status="canceled"))
    assert outcome.state is OutboundState.FAILED
    assert "Cancelled" in (outcome.detail or "")


def test_a_refused_recipient_is_a_bounce_carrying_the_smtp_reply():
    outcome = classify(
        _submission(
            status={
                "ok@x.test": ("yes", "250 OK"),
                "gone@x.test": ("no", "550 5.1.1 The email account does not exist"),
            }
        )
    )
    assert outcome.state is OutboundState.FAILED
    assert outcome.detail == "gone@x.test: 550 5.1.1 The email account does not exist"


def test_a_refusal_outranks_a_queued_recipient():
    outcome = classify(_submission(status={"a@x": ("queued", ""), "b@x": ("no", "554 nope")}))
    assert outcome.state is OutboundState.FAILED


def test_any_queued_recipient_makes_the_submission_queued():
    outcome = classify(_submission(status={"a@x": ("yes", "250"), "b@x": ("queued", "")}))
    assert outcome == Outcome(OutboundState.QUEUED)


def test_all_delivered_is_delivered_but_one_unknown_is_unknown():
    assert classify(_submission(status={"a@x": ("yes", ""), "b@x": ("yes", "")})).state is (
        OutboundState.DELIVERED
    )
    assert classify(_submission(status={"a@x": ("yes", ""), "b@x": ("unknown", "")})).state is (
        OutboundState.UNKNOWN
    )


def test_no_delivery_status_at_all_is_unknown():
    assert classify(_submission(status=None)) == Outcome(OutboundState.UNKNOWN)


def test_a_bounce_detail_is_clipped_and_whitespace_collapsed():
    reply = "550 " + "x " * 400
    outcome = classify(_submission(status={"a@x": ("no", reply)}))
    assert outcome.detail is not None
    assert len(outcome.detail) <= 300
    assert "\n" not in outcome.detail and "  " not in outcome.detail


# ---------------------------------------------------------------------------
# The service, against a FakeClient and the aiosqlite `db` fixture
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    name: str
    kwargs: dict


class FakeClient:
    """The one `JmapClient` method the service calls, with a scripted
    answer per submission id and a call log."""

    def __init__(self, submissions: dict[str, EmailSubmission] | None = None) -> None:
        self.submissions = submissions or {}
        self.calls: list[_Call] = []
        self.error: Exception | None = None

    @property
    def account_id(self) -> str:
        return ACCOUNT

    async def get_submissions(self, ids):
        self.calls.append(_Call("get_submissions", {"ids": list(ids)}))
        if self.error is not None:
            raise self.error
        return [self.submissions[i] for i in ids if i in self.submissions]


async def _user(db):
    return await repo.get_or_create_user(db, "demo@mailosh.test", "demo@mailosh.test")


async def _record(db, user_id, submission_id, email_id, *, at=NOW):
    return await outbound.record(
        db,
        user_id=user_id,
        account_id=ACCOUNT,
        submission_id=submission_id,
        email_id=email_id,
        now=at,
    )


async def test_record_starts_a_queued_never_checked_row(db):
    user = await _user(db)
    row = await _record(db, user.id, "s1", "e1")
    assert (row.state, row.last_checked_at, row.thread_id, row.notified_at) == (
        "queued",
        None,
        None,
        None,
    )
    stored = (await db.execute(select(OutboundSubmission))).scalars().one()
    assert (stored.user_id, stored.submission_id, stored.email_id) == (user.id, "s1", "e1")


async def test_refresh_is_one_request_for_every_open_row(db):
    user = await _user(db)
    for n in range(1, 4):
        await _record(db, user.id, f"s{n}", f"e{n}")
    client = FakeClient(
        {
            "s1": _submission("s1", status={"a@x": ("no", "550 no such user")}),
            "s2": _submission("s2", status={"a@x": ("queued", "")}),
            "s3": _submission("s3", status={"a@x": ("yes", "250 OK")}, thread_id="t3"),
        }
    )
    result = await outbound.refresh(client, db, user.id, now=NOW)

    assert [c.name for c in client.calls] == ["get_submissions"]
    assert sorted(client.calls[0].kwargs["ids"]) == ["s1", "s2", "s3"]
    assert result.checked == 3
    by_id = {row.submission_id: row for row in result.changed}
    assert by_id["s1"].state == "failed" and by_id["s1"].detail == "a@x: 550 no such user"
    assert by_id["s3"].state == "delivered" and by_id["s3"].thread_id == "t3"
    # s2 was queued and stays queued: checked, not "changed".
    assert "s2" not in by_id
    assert [row.submission_id for row in result.bounced] == ["s1"]
    stored = {r.submission_id: r for r in (await db.execute(select(OutboundSubmission))).scalars()}
    assert all(r.last_checked_at is not None for r in stored.values())


async def test_refresh_skips_terminal_rows_and_rows_past_the_tracking_window(db):
    user = await _user(db)
    await _record(db, user.id, "old", "e-old", at=NOW - outbound.TRACK_FOR - timedelta(hours=1))
    await _record(db, user.id, "fresh", "e-fresh")
    done = await _record(db, user.id, "done", "e-done")
    done.state = "delivered"
    await db.commit()

    client = FakeClient({"fresh": _submission("fresh", status={"a@x": ("queued", "")})})
    result = await outbound.refresh(client, db, user.id, now=NOW)
    assert client.calls[0].kwargs["ids"] == ["fresh"]
    assert result.checked == 1


async def test_refresh_makes_no_request_when_nothing_is_open(db):
    user = await _user(db)
    client = FakeClient()
    result = await outbound.refresh(client, db, user.id, now=NOW)
    assert client.calls == [] and result.checked == 0


async def test_a_submission_the_server_forgot_becomes_unknown_with_a_reason(db):
    user = await _user(db)
    await _record(db, user.id, "s1", "e1")
    client = FakeClient({})  # notFound
    result = await outbound.refresh(client, db, user.id, now=NOW)
    (row,) = result.changed
    assert row.state == "unknown"
    assert "no longer reports" in (row.detail or "")
    assert row.last_checked_at is not None


async def test_refresh_if_due_polls_once_per_interval(db):
    user = await _user(db)
    await _record(db, user.id, "s1", "e1")
    client = FakeClient({"s1": _submission("s1", status={"a@x": ("queued", "")})})

    await outbound.refresh_if_due(client, db, user.id, now=NOW)
    await outbound.refresh_if_due(client, db, user.id, now=NOW + timedelta(seconds=3))
    assert len(client.calls) == 1, "a second render three seconds later must not poll again"

    await outbound.refresh_if_due(
        client, db, user.id, now=NOW + outbound.MIN_POLL_INTERVAL + timedelta(seconds=1)
    )
    assert len(client.calls) == 2


async def test_refresh_if_due_swallows_a_server_failure(db):
    """The page being rendered is about the user's mail; a stale pill is
    not worth failing it over."""
    user = await _user(db)
    await _record(db, user.id, "s1", "e1")
    client = FakeClient()
    client.error = TransportError("down", status_code=None)
    result = await outbound.refresh_if_due(client, db, user.id, now=NOW)
    assert result.checked == 0
    row = (await db.execute(select(OutboundSubmission))).scalars().one()
    assert row.state == "queued" and row.last_checked_at is None


async def test_states_for_is_keyed_by_email_id_and_the_newer_attempt_wins(db):
    user = await _user(db)
    await _record(db, user.id, "s-old", "e1", at=NOW - timedelta(hours=1))
    await _record(db, user.id, "s-new", "e1")
    await _record(db, user.id, "s2", "e2")
    states = await outbound.states_for(db, user.id, ["e1", "e2", "e-none"])
    assert set(states) == {"e1", "e2"}
    assert states["e1"].submission_id == "s-new"
    assert await outbound.states_for(db, user.id, []) == {}


async def test_states_for_never_crosses_users(db):
    user = await _user(db)
    other = await repo.get_or_create_user(db, "other@mailosh.test", "other@mailosh.test")
    await _record(db, other.id, "s1", "e1")
    assert await outbound.states_for(db, user.id, ["e1"]) == {}


async def test_a_bounce_is_announced_exactly_once(db):
    user = await _user(db)
    await _record(db, user.id, "s1", "e1")
    client = FakeClient({"s1": _submission("s1", status={"a@x": ("no", "550 nope")})})
    await outbound.refresh(client, db, user.id, now=NOW)

    first = await outbound.take_unannounced_bounces(db, user.id, now=NOW)
    assert [row.submission_id for row in first] == ["s1"]
    assert outbound.bounce_toast(first[0]) == "Couldn't deliver to a@x: 550 nope"
    assert await outbound.take_unannounced_bounces(db, user.id, now=NOW) == []


async def test_sweep_refreshes_users_with_a_live_session_and_notifies_their_hub(db, monkeypatch):
    """The maintenance-loop path: one refresh per user with open rows,
    through a pooled client found via any live session, and a `mail`
    event to their hub so an open tab redraws — but never a raised
    exception, which would end the loop that called it."""
    user = await _user(db)
    await _record(db, user.id, "s1", "e1")
    client = FakeClient({"s1": _submission("s1", status={"a@x": ("no", "550 nope")})})
    notified: list[tuple[int, list[str]]] = []

    class Pool:
        async def get(self, session, settings):
            assert session.id == "sess-1"
            return client

    class Hubs:
        def notify(self, user_id, types):
            notified.append((user_id, list(types)))

    class Maker:
        """`sessionmaker()` returning the test's own session; the test owns
        its lifetime, so close is a no-op."""

        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    from mailosh.security import sessions

    async def live_session(db_, user_id, settings, now=None):
        return SimpleNamespace(id="sess-1", user_id=user_id)

    monkeypatch.setattr(sessions, "live_session_for_user", live_session)
    app = SimpleNamespace(
        state=SimpleNamespace(sessionmaker=Maker(), settings=object(), pool=Pool(), hubs=Hubs())
    )
    await outbound.sweep(app)

    assert [c.name for c in client.calls] == ["get_submissions"]
    assert notified == [(user.id, ["EmailSubmission"])]
    row = (await db.execute(select(OutboundSubmission))).scalars().one()
    assert row.state == "failed"


async def test_sweep_skips_a_user_with_no_live_session_and_never_raises(db, monkeypatch):
    user = await _user(db)
    await _record(db, user.id, "s1", "e1")

    class Pool:
        async def get(self, session, settings):
            raise AssertionError("no session, so no client should be asked for")

    class Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    from mailosh.security import sessions

    async def nobody(db_, user_id, settings, now=None):
        return None

    monkeypatch.setattr(sessions, "live_session_for_user", nobody)
    app = SimpleNamespace(
        state=SimpleNamespace(sessionmaker=Maker(), settings=object(), pool=Pool(), hubs=None)
    )
    await outbound.sweep(app)  # must not raise
    await outbound.sweep(SimpleNamespace())  # an app with no state at all: also fine


# ---------------------------------------------------------------------------
# Schema and migration agree
# ---------------------------------------------------------------------------


def test_outbound_submission_schema_pins():
    table = OutboundSubmission.__table__
    assert {c.name for c in table.primary_key.columns} == {"user_id", "submission_id"}
    assert set(table.columns.keys()) == {
        "user_id",
        "submission_id",
        "account_id",
        "email_id",
        "thread_id",
        "created_at",
        "state",
        "last_checked_at",
        "notified_at",
        "detail",
    }
    assert table.columns["thread_id"].nullable is True
    assert table.columns["detail"].nullable is True


def _load_migration(name: str):
    path = pathlib.Path("migrations/versions") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0003_exists_and_follows_0002():
    module = _load_migration("0003_outbound")
    assert module.revision == "0003_outbound"
    assert module.down_revision == "0002_reading"


def test_migration_0003_creates_every_model_column():
    """The migration is hand-written (project convention), so this is the
    check `--autogenerate` would otherwise have been: every column on the
    model is named in the migration, and nothing the model lacks is."""
    source = pathlib.Path("migrations/versions/0003_outbound.py").read_text()
    model_columns = set(OutboundSubmission.__table__.columns.keys())
    for column in model_columns:
        assert f'"{column}"' in source, column
    assert '"outbound_submission"' in source
    assert "def downgrade" in source and 'op.drop_table("outbound_submission")' in source


def test_migration_0003_is_the_only_head():
    """Every revision file names the previous one; exactly one is not
    anyone's `down_revision`, and it is ours."""
    versions = pathlib.Path("migrations/versions")
    modules = [_load_migration(p.stem) for p in sorted(versions.glob("0*.py"))]
    revisions = {m.revision for m in modules}
    downs = {m.down_revision for m in modules}
    assert revisions - downs == {"0003_outbound"}


@pytest.mark.parametrize("state", list(OutboundState))
def test_every_state_fits_the_column(state):
    assert len(state.value) <= OutboundSubmission.__table__.columns["state"].type.length
