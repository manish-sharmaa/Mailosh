"""Shared pytest fixtures for the JMAP client's unit tests.

``client``/``api_mock``/``upload_mock``/``download_mock`` give
``tests/unit/test_jmap_mail.py``/``test_jmap_bodies.py`` a connected
``JmapClient`` plus its respx-mocked HTTP endpoints without each test
re-deriving the rebased URLs by hand. The response-dict constants
below (``EMAIL_QUERY_PLUS_GET_RESPONSE`` etc.) are plain module-level names —
pytest's default "prepend" import mode puts this file's directory on
``sys.path`` because ``tests/`` has no ``__init__.py``, so sibling test
modules pull them in with a plain ``from conftest import ...``, the same way
they receive the fixtures below.
"""

from __future__ import annotations

import itertools
import json
import pathlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.config import Settings
from mailosh.db.base import Base
from mailosh.jmap.client import JmapClient
from mailosh.jmap.models import Address, EmailBody, EmailHeader, Mailbox

SESSION = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())

# session.json's apiUrl is "https://mail.mailosh.test/jmap/" (trailing
# slash), uploadUrl is ".../jmap/upload/{accountId}/", and downloadUrl is
# ".../jmap/download/{accountId}/{blobId}/{name}?accept={type}".
# Session.rebase swaps scheme+host only (see tests/unit/test_jmap_request.py),
# and the fixture's primary mail account id is "c" — so against base
# "http://s" the three client-facing URLs resolve to exactly these,
# substituting the account id into the upload/download templates the same
# way JmapClient.upload()/blob_url() must.
API_URL = "http://s/jmap/"
UPLOAD_URL = "http://s/jmap/upload/c/"
DOWNLOAD_URL = "http://s/jmap/download/c/"


@pytest.fixture
async def client():
    """A ``JmapClient`` connected against a respx-mocked session.

    Opens the respx mock context itself (rather than requiring every test to
    stack its own ``@respx.mock``) so ``api_mock``/``upload_mock`` below can
    register additional routes on this same active router just by depending
    on this fixture — respx's bare ``respx.get``/``respx.post`` module-level
    calls always target whichever router is currently active.
    """
    with respx.mock:
        respx.get("http://s/.well-known/jmap").respond(json=SESSION)
        c = await JmapClient.connect("http://s", "u", "p")
        yield c
        await c.close()


@pytest.fixture
def api_mock(client):
    """The batched JMAP endpoint route; tests set its response with `.respond(json=...)`.

    Depends on ``client`` purely for ordering — so its respx mock context is
    already active — not for the client instance itself.
    """
    return respx.post(API_URL)


@pytest.fixture
def upload_mock(client):
    """The blob-upload endpoint route (``uploadUrl`` with ``{accountId}`` substituted)."""
    return respx.post(UPLOAD_URL)


@pytest.fixture
def download_mock(client):
    """The blob-download endpoint route (``downloadUrl`` with all four
    placeholders substituted).

    Unlike ``upload_mock``'s single fixed URL, the request path and
    ``?accept=`` query vary by blob id/type/name, so this hands back a
    small callable a test invokes with the bytes to serve — defaulting to
    the blob id/type/name ``test_fetch_blob_caps_size`` itself calls
    ``fetch_blob``/``blob_url`` with, so most callers only need to pass
    ``content``.
    """

    def _respond(
        *, content: bytes, blob_id: str = "B3", mime_type: str = "image/png", name: str = "a.png"
    ):
        url = f"{DOWNLOAD_URL}{quote(blob_id, safe='')}/{quote(name, safe='')}"
        return respx.get(url, params={"accept": mime_type}).respond(content=content)

    return _respond


# ---------------------------------------------------------------------------
# Task 6: ``make_header`` builds an ``EmailHeader`` for a given
# ``(from_name, from_email)`` pair, with every other required field
# defaulted to a plausible placeholder. ``test_format.py``'s
# ``format_senders`` tests only care about ``from_``, so this fixture
# spares each test case the ceremony of filling in an ``id``/``thread_id``/
# ``received_at``/``preview``/``has_attachment`` that isn't the point of
# what's being asserted. Each call gets a fresh, unique ``id`` (an
# ever-increasing counter) purely so two headers built by the same test are
# never accidentally identical objects -- ``format_senders`` itself never
# keys off ``id``.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_header():
    counter = itertools.count(1)

    def _make(from_name: str | None, from_email: str) -> EmailHeader:
        n = next(counter)
        return EmailHeader(
            id=f"e{n}",
            thread_id="t1",
            mailbox_ids={"mb-inbox"},
            keywords={"$seen"},
            from_=[Address(name=from_name, email=from_email)],
            subject="Test subject",
            received_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
            preview="preview text",
            has_attachment=False,
        )

    return _make


# ---------------------------------------------------------------------------
# Mocked JMAP response bodies, shaped per RFC 8621's own illustrative
# examples (field names/nesting modeled on the RFC, values chosen to read
# like a real mailbox rather than "a"/"b" placeholders). Each call id below
# ("q0", "g0", "t0", "e0", "s0", "c0", "m0") matches the id the corresponding
# JmapClient method actually sends, since _call() maps responses by
# whatever call id the (mocked) server echoes back — see test_jmap_request.py.
# ---------------------------------------------------------------------------

#: `query_inbox`'s Email/query -> Email/get chain: two collapsed thread rows.
EMAIL_QUERY_PLUS_GET_RESPONSE = {
    "methodResponses": [
        [
            "Email/query",
            {
                "accountId": "c",
                "queryState": "abcdefg",
                "canCalculateChanges": False,
                "position": 0,
                "ids": ["M1", "M2"],
                "total": 2,
            },
            "q0",
        ],
        [
            "Email/get",
            {
                "accountId": "c",
                "state": "abcdefg",
                "list": [
                    {
                        "id": "M1",
                        "blobId": "G1",
                        "threadId": "T1",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {"$seen": False},
                        "hasAttachment": False,
                        "from": [{"name": "Alice Example", "email": "alice@example.com"}],
                        "to": [{"name": "Demo", "email": "demo@mailosh.test"}],
                        "subject": "Spike thread",
                        "receivedAt": "2026-08-30T09:00:00Z",
                        "preview": "Hi, this is the first message in the spike thread…",
                    },
                    {
                        "id": "M2",
                        "blobId": "G2",
                        "threadId": "T2",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {"$seen": True, "$flagged": True},
                        "hasAttachment": True,
                        "from": [{"name": "Bob Example", "email": "bob@example.com"}],
                        "to": [{"name": "Demo", "email": "demo@mailosh.test"}],
                        "subject": "Re: another thread",
                        "receivedAt": "2026-08-29T18:30:00Z",
                        "preview": "Second row preview text, unrelated thread…",
                    },
                ],
                "notFound": [],
            },
            "g0",
        ],
    ],
    "sessionState": "s1",
}

#: `get_thread`'s Thread/get -> Email/get chain: one thread, two messages,
#: each with `textBody`+`bodyValues` populated (fetchTextBodyValues=True shape).
THREAD_GET_PLUS_EMAIL_RESPONSE = {
    "methodResponses": [
        [
            "Thread/get",
            {
                "accountId": "c",
                "state": "abc",
                "list": [{"id": "t-1", "emailIds": ["M1", "M2"]}],
                "notFound": [],
            },
            "t0",
        ],
        [
            "Email/get",
            {
                "accountId": "c",
                "state": "abc",
                "list": [
                    {
                        "id": "M1",
                        "threadId": "t-1",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {"$seen": True},
                        "hasAttachment": False,
                        "from": [{"name": "Alice Example", "email": "alice@example.com"}],
                        "to": [{"name": "Demo", "email": "demo@mailosh.test"}],
                        "cc": [],
                        "subject": "Spike thread",
                        "receivedAt": "2026-08-30T09:00:00Z",
                        "preview": "Hi, this is the first message…",
                        "textBody": [{"partId": "1", "type": "text/plain"}],
                        "bodyValues": {
                            "1": {
                                "value": "Hi, first message in the spike thread.",
                                "isEncodingProblem": False,
                                "isTruncated": False,
                            }
                        },
                    },
                    {
                        "id": "M2",
                        "threadId": "t-1",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {},
                        "hasAttachment": False,
                        "from": [{"name": "Demo", "email": "demo@mailosh.test"}],
                        "to": [{"name": "Alice Example", "email": "alice@example.com"}],
                        "cc": [],
                        "subject": "Re: Spike thread",
                        "receivedAt": "2026-08-30T10:00:00Z",
                        "preview": "Thanks, replying now…",
                        "textBody": [{"partId": "2", "type": "text/plain"}],
                        "bodyValues": {
                            "2": {
                                "value": "Thanks, replying now.",
                                "isEncodingProblem": False,
                                "isTruncated": False,
                            }
                        },
                    },
                ],
                "notFound": [],
            },
            "e0",
        ],
    ],
    "sessionState": "s1",
}

#: `get_mailboxes`'s Mailbox/get: an inbox plus one label-like mailbox.
MAILBOXES_GET_RESPONSE = {
    "methodResponses": [
        [
            "Mailbox/get",
            {
                "accountId": "c",
                "state": "abc",
                "list": [
                    {
                        "id": "mb-inbox",
                        "name": "Inbox",
                        "parentId": None,
                        "role": "inbox",
                        "sortOrder": 10,
                        "totalEmails": 42,
                        "unreadEmails": 3,
                    },
                    {
                        "id": "mb-label1",
                        "name": "SpikeLabel",
                        "parentId": None,
                        "role": None,
                        "sortOrder": 20,
                        "totalEmails": 5,
                        "unreadEmails": 0,
                    },
                ],
                "notFound": [],
            },
            "m0",
        ]
    ],
    "sessionState": "s1",
}

#: A generic successful `Email/set` update response, reused by `set_keyword`'s
#: and `move`'s tests — neither method reads the response body at all, so its
#: exact contents just need to be a plausible RFC 8620 §5.3 Set response.
EMPTY_SET_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "updated": {"e1": None},
            },
            "s0",
        ]
    ],
    "sessionState": "s1",
}

#: A generic `Email/set` response where the update is rejected: the id shows
#: up in `notUpdated` (RFC 8620 §5.3 SetError), not `updated` — reused by
#: `set_keyword`'s and `move`'s "server rejected it" tests.
NOT_UPDATED_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s0",
                "updated": None,
                "notUpdated": {"e1": {"type": "notFound", "description": "No Email with that id."}},
            },
            "s0",
        ]
    ],
    "sessionState": "s1",
}

#: `import_email`'s Email/import: one creation ("i0") succeeding.
IMPORT_RESPONSE = {
    "methodResponses": [
        [
            "Email/import",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "created": {
                    "i0": {"id": "e-imported", "blobId": "b1", "threadId": "t-new", "size": 3}
                },
                "notCreated": None,
            },
            "c0",
        ]
    ],
    "sessionState": "s1",
}

#: `create_mailbox`'s Mailbox/set create: one creation ("m0") succeeding.
MAILBOX_CREATE_RESPONSE = {
    "methodResponses": [
        [
            "Mailbox/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "created": {
                    "m0": {"id": "mb-new", "sortOrder": 0, "totalEmails": 0, "unreadEmails": 0}
                },
                "notCreated": None,
            },
            "c0",
        ]
    ],
    "sessionState": "s1",
}

#: A `Mailbox/set` create response where the server rejects it: the creation-id
#: shows up in `notCreated` (RFC 8620 §5.3 SetError), not `created` — reused by
#: `create_mailbox`'s "server rejected it" test, mirroring `NOT_UPDATED_RESPONSE`.
MAILBOX_NOT_CREATED_RESPONSE = {
    "methodResponses": [
        [
            "Mailbox/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s0",
                "created": None,
                "notCreated": {
                    "m0": {
                        "type": "invalidArguments",
                        "description": "A mailbox named 'SpikeLabel' already exists.",
                    }
                },
            },
            "c0",
        ]
    ],
    "sessionState": "s1",
}

#: `query_page`'s four-call RFC 8621 §4.10 chain ("q0" Email/query -> "g0"
#: Email/get[threadId] -> "t0" Thread/get -> "e0" Email/get[full row
#: props]), collapsed to three threads sorted newest-first by their latest
#: message's `receivedAt` -- the shape `tests/unit/test_thread_list.py`
#: exercises against `build_page`. Deliberately covers all four fixture
#: traits the Task 6 brief calls for in one realistic page rather than four
#: separate throwaway ones:
#:   - "t-work" (thread id "t-work"): a *multi-message* thread (3 messages),
#:     *unread* (its newest message, "e-work-3", has no "$seen"), and
#:     *labelled* ("m-work", a non-role mailbox = a user label named
#:     "Work" in `QUERY_PAGE_NAV`). Its senders, oldest -> newest, are
#:     Aisha Rahman, Tom Reyes, then the demo account itself
#:     ("demo@mailosh.test") -- `format_senders` renders that last one as
#:     "me", giving the exact "Aisha, Tom, me (3)" the brief's own
#:     `test_page_rows_from_batched_query` asserts.
#:   - "t-ci" (thread id "t-ci"): a single-message, already-read, unlabelled
#:     thread (a CI notification) -- received *after* "t-work"'s oldest two
#:     messages but *before* its newest one, so it sorts as the 2nd row.
#:   - "t-hike" (thread id "t-hike"): a single-message, already-read,
#:     unlabelled thread that *has an attachment* -- the oldest of the
#:     three, so it sorts 3rd/last.
#: `total` (1284) and the three threads' subjects/senders echo the approved
#: mockup (`docs/design/mockups/layout.html`'s row list) rather
#: than being invented from scratch.
QUERY_PAGE_RESPONSE = {
    "methodResponses": [
        [
            "Email/query",
            {
                "accountId": "c",
                "queryState": "qs1",
                "canCalculateChanges": False,
                "position": 0,
                # Newest representative email id per (collapsed) thread,
                # newest-thread-first: e-work-3 (2026-09-02T10:42Z) >
                # e-ci-1 (2026-09-02T09:58Z) > e-hike-1 (2026-08-29T16:20Z).
                "ids": ["e-work-3", "e-ci-1", "e-hike-1"],
                "total": 1284,
            },
            "q0",
        ],
        [
            "Email/get",
            {
                "accountId": "c",
                "state": "qs1",
                "list": [
                    {"id": "e-work-3", "threadId": "t-work"},
                    {"id": "e-ci-1", "threadId": "t-ci"},
                    {"id": "e-hike-1", "threadId": "t-hike"},
                ],
                "notFound": [],
            },
            "g0",
        ],
        [
            "Thread/get",
            {
                "accountId": "c",
                "state": "ts1",
                "list": [
                    {"id": "t-work", "emailIds": ["e-work-1", "e-work-2", "e-work-3"]},
                    {"id": "t-ci", "emailIds": ["e-ci-1"]},
                    {"id": "t-hike", "emailIds": ["e-hike-1"]},
                ],
                "notFound": [],
            },
            "t0",
        ],
        [
            "Email/get",
            {
                "accountId": "c",
                "state": "qs1",
                "list": [
                    {
                        "id": "e-work-1",
                        "threadId": "t-work",
                        "mailboxIds": {"mb-inbox": True, "m-work": True},
                        "keywords": {"$seen": True},
                        "hasAttachment": False,
                        "from": [{"name": "Aisha Rahman", "email": "aisha@example.com"}],
                        "subject": "Offsite agenda",
                        "receivedAt": "2026-08-30T09:00:00Z",
                        "preview": "Quick thought on where we should hold the offsite this year…",
                    },
                    {
                        "id": "e-work-2",
                        "threadId": "t-work",
                        "mailboxIds": {"mb-inbox": True, "m-work": True},
                        "keywords": {"$seen": True},
                        "hasAttachment": False,
                        "from": [{"name": "Tom Reyes", "email": "tom@example.com"}],
                        "subject": "Re: Offsite agenda",
                        "receivedAt": "2026-08-30T10:15:00Z",
                        "preview": "Thursday works for me — Friday's tighter with the release…",
                    },
                    {
                        "id": "e-work-3",
                        "threadId": "t-work",
                        "mailboxIds": {"mb-inbox": True, "m-work": True},
                        # No "$seen" -- this is the thread's unread message.
                        "keywords": {},
                        "hasAttachment": False,
                        "from": [{"name": "Demo", "email": "demo@mailosh.test"}],
                        "subject": "Re: Offsite agenda",
                        "receivedAt": "2026-09-02T10:42:00Z",
                        "preview": "Let's lock the Thursday slot and send the invite today",
                    },
                    {
                        "id": "e-ci-1",
                        "threadId": "t-ci",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {"$seen": True},
                        "hasAttachment": False,
                        "from": [{"name": "GitHub", "email": "notifications@github.com"}],
                        "subject": "[mailosh/mailosh] Run #482 passed: main",
                        "receivedAt": "2026-09-02T09:58:00Z",
                        "preview": "All checks have passed — 115 unit, 1 integration",
                    },
                    {
                        "id": "e-hike-1",
                        "threadId": "t-hike",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {"$seen": True},
                        "hasAttachment": True,
                        "from": [{"name": "Lena Fischer", "email": "lena@example.com"}],
                        "subject": "Photos from the hike",
                        "receivedAt": "2026-08-29T16:20:00Z",
                        "preview": "Finally uploaded — the ridge shots came out beautifully",
                    },
                ],
                "notFound": [],
            },
            "e0",
        ],
    ],
    "sessionState": "s1",
}


# ---------------------------------------------------------------------------
# Task 6 (web layer): already-parsed model instances, not raw JMAP wire JSON
# like every fixture above — `tests/unit/test_web_inbox.py`'s `FakeClient`
# stands in for `JmapClient` itself (its `get_mailboxes`/`query_inbox` return
# model objects, same as the real client), so there's no wire response to
# shape here.
# ---------------------------------------------------------------------------

#: The one mailbox `FakeClient.get_mailboxes` returns. `role="inbox"` matters
#: — `mailosh.jmap.client.find_inbox` picks the mailbox flagged this way,
#: the same lookup it does against a real server's `Mailbox/get` response.
FAKE_INBOX = Mailbox(
    id="mb-inbox",
    name="Inbox",
    parent_id=None,
    role="inbox",
    sort_order=10,
    total_emails=1,
    unread_emails=1,
)

#: The one row `FakeClient.query_inbox` returns. `subject` is deliberately
#: raw, unescaped HTML ("Spike <b>subject</b>") — the whole point of this
#: fixture is proving `_rows.html` renders it through Jinja2 autoescaping
#: rather than trusting it (`test_inbox_renders_rows_and_escapes`'s
#: `&lt;b&gt;` assertion). `keywords` has no `"$seen"`, so it's unread per
#: the spec's `unread = "$seen" not in keywords` rule.
FAKE_ROW = EmailHeader(
    id="e1",
    thread_id="t1",
    mailbox_ids={"mb-inbox"},
    keywords=set(),
    from_=[Address(name="Alice Example", email="alice@example.com")],
    subject="Spike <b>subject</b>",
    received_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
    preview="Hi, this is the first message in the spike thread…",
    has_attachment=False,
)

# ---------------------------------------------------------------------------
# Task 7 (web layer): a fake thread of three already-parsed `EmailBody`
# messages — `tests/unit/test_web_thread.py`'s `FakeClient.get_thread`
# returns this list (or a caller-supplied override). Same "model instances,
# not wire JSON" rationale as `FAKE_INBOX`/`FAKE_ROW` above.
# ---------------------------------------------------------------------------

#: Three messages in one thread. `FAKE_THREAD[0]` ("e1") has no `"$seen"`
#: keyword — proving the thread route's mark-as-read-on-open call — and its
#: `text_body` embeds raw HTML (`<script>...`) the same way `FAKE_ROW.subject`
#: does, so the escaping assertion has real teeth instead of trivially
#: passing on a body with nothing to escape. `FAKE_THREAD[1]`/`[2]` are
#: already `"$seen"`; `[2]` is also `"$flagged"` (an already-starred message,
#: for the star button's "on" rendering path) and its `text_body` mixes a
#: plain reply line with a two-line quoted block (leading `>`), exercising
#: `split_quoted`'s grouping through the actual template, not just its own
#: unit tests.
FAKE_THREAD = [
    EmailBody(
        id="e1",
        thread_id="t1",
        mailbox_ids={"mb-inbox"},
        keywords=set(),
        from_=[Address(name="Alice Example", email="alice@example.com")],
        to=[Address(name="Demo", email="demo@mailosh.test")],
        cc=[],
        subject="Spike thread",
        received_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        preview="Hi, this is the first message in the spike thread…",
        has_attachment=False,
        text_body="Hi, this is the first message in the spike thread. <script>alert(1)</script>",
    ),
    EmailBody(
        id="e2",
        thread_id="t1",
        mailbox_ids={"mb-inbox"},
        keywords={"$seen"},
        from_=[Address(name="Demo", email="demo@mailosh.test")],
        to=[Address(name="Alice Example", email="alice@example.com")],
        cc=[],
        subject="Re: Spike thread",
        received_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
        preview="Thanks, replying now…",
        has_attachment=False,
        text_body="Thanks, replying now.",
    ),
    EmailBody(
        id="e3",
        thread_id="t1",
        mailbox_ids={"mb-inbox"},
        keywords={"$seen", "$flagged"},
        from_=[Address(name="Alice Example", email="alice@example.com")],
        to=[Address(name="Demo", email="demo@mailosh.test")],
        cc=[],
        subject="Re: Spike thread",
        received_at=datetime(2026, 8, 30, 11, 0, tzinfo=UTC),
        preview="Sounds good.…",
        has_attachment=False,
        text_body="Sounds good.\n\n> Thanks, replying now.\n> - Demo",
    ),
]

# ---------------------------------------------------------------------------
# Task 1 (db layer): an isolated in-memory schema per test, so
# tests/unit/test_db_models.py needs no live Postgres. aiosqlite rather than
# a real Postgres/asyncpg engine — the whole point (Task 1 brief's Global
# Constraints: models use dialect-neutral column types only) is that this
# same `mailosh.db.models`/`mailosh.db.base` metadata creates cleanly on
# either backend; production always runs the real thing via
# `migrations/versions/0001_foundation.py` against Postgres instead (see
# `alembic upgrade head`), never `Base.metadata.create_all` — that call
# below is unit-test-only scaffolding.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Task 5 (web layer): a real FastAPI ``create_app`` under test needs its own
# engine (opened inside the lifespan from ``Settings.database_url``, not the
# single already-open session the ``db`` fixture above hands out) plus a
# ``Settings`` that actually validates. ``sqlite_url``/``make_settings``
# below are what ``tests/unit/test_auth_routes.py`` and ``test_pool.py``
# build those from.
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_url(tmp_path: pathlib.Path) -> str:
    """A fresh, file-backed aiosqlite URL, unique per test.

    Deliberately a temp *file*, not ``sqlite+aiosqlite:///:memory:``: once
    wired into a real ``create_app``, ``app.state.sessionmaker`` hands out a
    fresh ``AsyncSession``/connection per request (``mailosh.db.session.
    get_db``), and an in-memory SQLite database is private to the one
    connection that created it -- every connection after the first would
    see a blank, tableless database instead of what the lifespan's own
    ``Base.metadata.create_all`` just built. A real temp file sidesteps
    that: every connection opens the same file, the same way any number of
    real Postgres connections all see the same database.
    """
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


def make_settings(database_url: str, **overrides: Any) -> Settings:
    """Build a ``Settings`` for a test ``create_app`` -- a plain callable
    (not a fixture; not collected as a test itself, since pytest only ever
    scans files matching ``test_*.py``/``*_test.py`` for tests, and
    ``conftest.py`` is neither), so a test can build more than one
    ``Settings`` from a single ``sqlite_url`` without needing a second
    fixture. Same "sane defaults, override anything via **kwargs" shape as
    ``tests/unit/test_sessions.py``'s own ``_settings`` helper: a throwaway
    ``secret_key`` (40 chars, doesn't start with "change-me", so it clears
    ``Settings``'s own validator) and ``cookie_secure=False`` (``TestClient``
    talks plain HTTP, so a ``Secure``-flagged cookie would never round-trip
    back on the next request).
    """
    defaults: dict[str, Any] = {
        "stalwart_admin_secret": "admin-secret-for-tests-only",
        "secret_key": "test-secret-key-not-for-production-use!!",
        "cookie_secure": False,
        "database_url": database_url,
    }
    defaults.update(overrides)
    return Settings(**defaults)
