"""HTTP-level tests for the outbound delivery pill and bounce toast
(`mailosh.services.outbound` as `mailosh.web.mail` renders it).

Same harness as `tests/unit/test_mail_routes.py` — a real `create_app` over
a file-backed aiosqlite db, `deps.client_for` swapped for a `FakeClient` —
imported from there rather than copied, so the row contract those tests pin
is the one these render against. The fake grows one method,
`get_submissions`, which is the whole of what the tracker asks a client.

What is pinned: a Sent row whose message is queued or bounced carries the
pill (and its tooltip is the server's own reply, escaped); a delivered or
never-heard-of row carries nothing; the Inbox never renders one; a bounce
discovered while rendering reaches the reader once, as the app's existing
`om:error` toast, and only on an htmx request; the conversation header
shows the same pill when its newest message is the reader's own.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_mail_routes import ACCOUNT, ME, FakeAdmin, FakeClient, _login, _mailboxes, _row_html

from mailosh.db.models import AppUser, OutboundSubmission
from mailosh.jmap.models import Address, DeliveryStatus, EmailHeader, EmailSubmission
from mailosh.security.exchange import VerifiedAccount
from mailosh.web import deps
from mailosh.web.app import create_app

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _sent(email_id: str, thread_id: str, *, sender: str = ME, minute: int = 0) -> EmailHeader:
    return EmailHeader(
        id=email_id,
        thread_id=thread_id,
        mailbox_ids={"mb-sent"},
        keywords={"$seen"},
        from_=[Address(name="Demo", email=sender)],
        subject="Re: Q3 roadmap review",
        received_at=datetime(2026, 9, 6, 11, minute, tzinfo=UTC),
        preview="Attaching the deck",
        has_attachment=False,
    )


class TrackingClient(FakeClient):
    """`FakeClient` plus the one method the tracker polls, scripted per id."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.submissions: dict[str, EmailSubmission] = {}
        self.polls: list[list[str]] = []

    async def get_submissions(self, ids):
        self.polls.append(list(ids))
        return [self.submissions[i] for i in ids if i in self.submissions]


@pytest.fixture
def fake() -> TrackingClient:
    # Three Sent conversations: one message each, so `latest_email_id` is
    # unambiguous. The Inbox threads `FakeClient` serves by default are
    # gone here — this file is about Sent.
    return TrackingClient(
        threads={
            "t-q": [_sent("e-q", "t-q", minute=3)],
            "t-b": [_sent("e-b", "t-b", minute=2)],
            "t-ok": [_sent("e-ok", "t-ok", minute=1)],
        },
        mailboxes=_mailboxes(),
    )


@pytest.fixture
def app(monkeypatch, sqlite_url, fake):
    async def fake_verify(url, u, p):
        return VerifiedAccount(u, "acc1", u) if p == "right" else None

    monkeypatch.setattr("mailosh.web.auth.verify_password", fake_verify)
    application = create_app(settings=make_settings(sqlite_url))
    application.state.admin = FakeAdmin()
    application.dependency_overrides[deps.client_for] = lambda: fake
    with TestClient(application):
        yield application


def _seed(sqlite_url: str, rows: list[dict[str, object]]) -> None:
    """Insert `OutboundSubmission` rows for the (already logged-in) user,
    over a throwaway engine on the same sqlite file — same reasoning as
    `test_mail_routes._seed_label_meta`."""

    async def go() -> None:
        engine = create_async_engine(sqlite_url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = (await db.execute(select(AppUser))).scalars().one()
            for row in rows:
                db.add(
                    OutboundSubmission(
                        user_id=user.id,
                        account_id=ACCOUNT,
                        created_at=NOW,
                        **row,
                    )
                )
            await db.commit()
        await engine.dispose()

    asyncio.run(go())


def _rows(sqlite_url: str) -> dict[str, OutboundSubmission]:
    async def go():
        engine = create_async_engine(sqlite_url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            rows = list((await db.execute(select(OutboundSubmission))).scalars())
        await engine.dispose()
        return {row.submission_id: row for row in rows}

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Sent rows
# ---------------------------------------------------------------------------


def test_sent_rows_render_queued_and_bounced_pills_and_nothing_for_the_rest(app, sqlite_url):
    client = _login(app)
    _seed(
        sqlite_url,
        [
            {"submission_id": "s-q", "email_id": "e-q", "state": "queued", "last_checked_at": NOW},
            {
                "submission_id": "s-b",
                "email_id": "e-b",
                "state": "failed",
                "detail": "bob@x.test: 550 5.1.1 <bob@x.test> user unknown",
                "notified_at": NOW,
                "last_checked_at": NOW,
            },
            {
                "submission_id": "s-ok",
                "email_id": "e-ok",
                "state": "delivered",
                "last_checked_at": NOW,
            },
        ],
    )
    body = client.get("/mail/sent").text

    queued = _row_html(body, "t-q")
    assert 'class="pill-outbound is-queued"' in queued and ">Queued<" in queued

    bounced = _row_html(body, "t-b")
    assert 'class="pill-outbound is-failed"' in bounced and ">Bounced<" in bounced
    # The reply is a stranger's text and lands in a `title`, escaped.
    assert 'title="bob@x.test: 550 5.1.1 &lt;bob@x.test&gt; user unknown"' in bounced
    assert "<bob@x.test>" not in bounced

    assert "pill-outbound" not in _row_html(body, "t-ok")


def test_the_inbox_never_renders_a_pill(app, sqlite_url, fake):
    client = _login(app)
    _seed(sqlite_url, [{"submission_id": "s-q", "email_id": "e-q", "state": "queued"}])
    # Make the fake serve the same messages under the Inbox filter.
    for thread in fake.threads.values():
        for header in thread:
            header.mailbox_ids = {"mb-inbox"}
    body = client.get("/mail/inbox").text
    assert "pill-outbound" not in body


def test_rendering_a_list_polls_open_submissions_once_and_updates_the_pill(app, sqlite_url, fake):
    """The request path is what a Stalwart `EmailSubmission` push turns
    into (sse.js -> `mail:changed` -> `#list` re-GET): the render polls
    the open rows, one request for all of them, and draws what came back."""
    client = _login(app)
    _seed(
        sqlite_url,
        [
            {"submission_id": "s-q", "email_id": "e-q", "state": "queued"},
            {"submission_id": "s-b", "email_id": "e-b", "state": "unknown"},
        ],
    )
    fake.submissions = {
        "s-q": EmailSubmission(
            id="s-q",
            email_id="e-q",
            delivery_status={"a@x": DeliveryStatus(delivered="queued", smtp_reply="")},
        ),
        "s-b": EmailSubmission(
            id="s-b",
            email_id="e-b",
            delivery_status={"bob@x": DeliveryStatus(delivered="no", smtp_reply="550 nope")},
        ),
    }
    body = client.get("/mail/sent/rows", headers={"HX-Request": "true"})

    assert len(fake.polls) == 1 and sorted(fake.polls[0]) == ["s-b", "s-q"]
    assert ">Bounced<" in _row_html(body.text, "t-b")
    assert ">Queued<" in _row_html(body.text, "t-q")
    assert _rows(sqlite_url)["s-b"].state == "failed"


def test_a_bounce_discovered_on_render_is_toasted_once_through_om_error(app, sqlite_url, fake):
    client = _login(app)
    _seed(sqlite_url, [{"submission_id": "s-b", "email_id": "e-b", "state": "queued"}])
    fake.submissions = {
        "s-b": EmailSubmission(
            id="s-b",
            email_id="e-b",
            delivery_status={
                "bob@x.test": DeliveryStatus(delivered="no", smtp_reply="550 5.1.1 user unknown")
            },
        ),
    }
    first = client.get("/mail/inbox/rows", headers={"HX-Request": "true"})
    trigger = json.loads(first.headers["HX-Trigger"])
    assert trigger == {
        "om:error": {
            "toast": "Couldn't deliver to bob@x.test: 550 5.1.1 user unknown",
            "retry": False,
        }
    }

    again = client.get("/mail/inbox/rows", headers={"HX-Request": "true"})
    assert "HX-Trigger" not in again.headers, "each bounce is announced exactly once"
    assert _rows(sqlite_url)["s-b"].notified_at is not None


def test_a_full_page_render_never_carries_the_toast_header(app, sqlite_url):
    client = _login(app)
    _seed(
        sqlite_url,
        [{"submission_id": "s-b", "email_id": "e-b", "state": "failed", "detail": "x: 550"}],
    )
    r = client.get("/mail/sent")
    assert "HX-Trigger" not in r.headers
    assert ">Bounced<" in r.text


def test_a_user_with_nothing_in_flight_costs_no_poll(app, fake):
    _login(app).get("/mail/sent")
    assert fake.polls == []


# ---------------------------------------------------------------------------
# The conversation header
# ---------------------------------------------------------------------------


def test_thread_header_shows_the_pill_when_the_newest_message_is_mine(app, sqlite_url):
    client = _login(app)
    _seed(
        sqlite_url,
        [
            {
                "submission_id": "s-b",
                "email_id": "e-b",
                "state": "failed",
                "detail": "bob@x.test: 550 nope",
                "notified_at": NOW,
            }
        ],
    )
    body = client.get("/t/t-b").text
    start = body.index('<header class="thread-head">')
    head = body[start : body.index("</header>", start)]
    assert ">Bounced<" in head and 'title="bob@x.test: 550 nope"' in head


def test_thread_header_stays_quiet_when_someone_else_answered(app, sqlite_url, fake):
    """A reply that has since arrived answers "did it get through" better
    than any pill could, so the pill steps aside for it."""
    client = _login(app)
    _seed(sqlite_url, [{"submission_id": "s-b", "email_id": "e-b", "state": "failed"}])
    fake.threads["t-b"].append(_sent("e-reply", "t-b", sender="priya@example.com", minute=9))
    body = client.get("/t/t-b").text
    assert "pill-outbound" not in body
