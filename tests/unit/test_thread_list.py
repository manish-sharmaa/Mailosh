"""Unit tests for `mailosh.services.thread_list.build_page`: the batched
JMAP page query (RFC 8621 §4.10 chain, via `JmapClient.query_page`) and the
per-thread row aggregation (senders/count/unread/starred/chips/date) spec
§5.3 describes.

Uses the real `client`/`api_mock` respx fixtures from `conftest.py` (same
as `test_jmap_mail.py`) rather than a fake client -- unlike
`test_mailbox_tree.py`'s single `get_mailboxes()` call, this module's whole
point is the *shape of the HTTP request* `query_page` sends, which only a
real (mocked-transport) `JmapClient` can exercise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from conftest import QUERY_PAGE_RESPONSE

from mailosh.services.mailbox_tree import LabelNode, NavItem, NavModel, hidden_in_nav
from mailosh.services.thread_list import build_page
from mailosh.ui.format import avatar_color

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)

#: The demo account's own address -- the "me" `EMAIL_QUERY_PLUS_GET_RESPONSE`
#: and friends already use elsewhere in this file's conftest.py, reused here
#: for consistency rather than the task brief's generic "me@x" placeholder.
ME = "demo@mailosh.test"

#: Spec §4.1's 12-colour label palette, exact names/order -- mirrors
#: `mailosh.services.thread_list`'s own fallback-colour constant, used
#: here only to independently recompute the expected deterministic
#: fallback for `test_chip_color_falls_back_deterministically...` below.
_LABEL_PALETTE = (
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


def _nav() -> NavModel:
    """A realistic nav: the same six role mailboxes `test_mailbox_tree.py`
    uses, plus two labels ("Work", with a colour; "Receipts", without one
    -- exercising the chip fallback-colour path).
    """
    return NavModel(
        system=[
            NavItem(
                key="inbox",
                label="Inbox",
                icon="inbox",
                mailbox_id="mb-inbox",
                count=12,
                active=True,
            ),
            NavItem(
                key="starred",
                label="Starred",
                icon="star",
                mailbox_id=None,
                count=None,
                active=False,
            ),
            NavItem(
                key="sent",
                label="Sent",
                icon="send",
                mailbox_id="mb-sent",
                count=None,
                active=False,
            ),
            NavItem(
                key="drafts",
                label="Drafts",
                icon="file",
                mailbox_id="mb-drafts",
                count=2,
                active=False,
            ),
        ],
        more=[
            NavItem(
                key="all", label="All mail", icon="inbox", mailbox_id=None, count=None, active=False
            ),
            NavItem(
                key="archive",
                label="Archive",
                icon="archive",
                mailbox_id="mb-archive",
                count=None,
                active=False,
            ),
            NavItem(
                key="spam",
                label="Spam",
                icon="shield-alert",
                mailbox_id="mb-junk",
                count=None,
                active=False,
            ),
            NavItem(
                key="trash",
                label="Trash",
                icon="trash-2",
                mailbox_id="mb-trash",
                count=None,
                active=False,
            ),
        ],
        labels=[
            LabelNode(
                mailbox_id="m-work",
                name="Work",
                color="indigo",
                count=3,
                visibility="show",
                children=[],
            ),
            LabelNode(
                mailbox_id="m-receipts",
                name="Receipts",
                color=None,
                count=0,
                visibility="show",
                children=[],
            ),
        ],
        inbox_id="mb-inbox",
    )


def _page_response(
    thread_id: str,
    messages: list[dict],
    *,
    total: int,
    position: int = 0,
    representative: str | None = None,
) -> dict:
    """Build a minimal-but-wire-shaped `query_page` 4-call response for one
    thread, for the edge-case tests below that don't need the full
    multi-thread realism of `QUERY_PAGE_RESPONSE`. `messages` is oldest
    -> newest; the representative id `Email/query` "returns" defaults to the
    newest (RFC 8621 §4.10's collapseThreads semantics, sorted `receivedAt
    desc`).

    `representative` overrides that with an explicit id, because
    collapseThreads returns the newest message *matching the filter*, not
    the newest in the thread. The scoping tests below turn on exactly that
    distinction, so they name it rather than letting the fixture imply a
    representative the server would never have chosen.
    """
    newest = (
        next(m for m in messages if m["id"] == representative)
        if representative is not None
        else max(messages, key=lambda m: m["received_at"])
    )
    email_list = []
    for m in messages:
        raw: dict[str, object] = {
            "id": m["id"],
            "threadId": thread_id,
            "mailboxIds": {mid: True for mid in m["mailbox_ids"]},
            "keywords": {kw: True for kw in m["keywords"]},
            "hasAttachment": m.get("has_attachment", False),
            "from": [{"name": n, "email": e} for n, e in m["from"]],
            "receivedAt": m["received_at"],
            "preview": m.get("preview", ""),
        }
        if "subject" in m:
            raw["subject"] = m["subject"]
        email_list.append(raw)
    return {
        "methodResponses": [
            [
                "Email/query",
                {
                    "accountId": "c",
                    "queryState": "qs",
                    "canCalculateChanges": False,
                    "position": position,
                    "ids": [newest["id"]],
                    "total": total,
                },
                "q0",
            ],
            [
                "Email/get",
                {
                    "accountId": "c",
                    "state": "qs",
                    "list": [{"id": newest["id"], "threadId": thread_id}],
                    "notFound": [],
                },
                "g0",
            ],
            [
                "Thread/get",
                {
                    "accountId": "c",
                    "state": "ts",
                    "list": [{"id": thread_id, "emailIds": [m["id"] for m in messages]}],
                    "notFound": [],
                },
                "t0",
            ],
            [
                "Email/get",
                {"accountId": "c", "state": "qs", "list": email_list, "notFound": []},
                "e0",
            ],
        ],
        "sessionState": "s1",
    }


# ---------------------------------------------------------------------------
# The brief's own assertions, verbatim (plus a few directly-related ones
# appended to the same request/response round trip: the exact filter body,
# sort order, and the rest of row 0's fields "nothing the template would
# have to compute itself" implies must already be right).
# ---------------------------------------------------------------------------


async def test_page_rows_from_batched_query(client, api_mock):
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    nav = _nav()
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)["methodCalls"]
    assert [m[0] for m in body] == ["Email/query", "Email/get", "Thread/get", "Email/get"]
    assert body[0][1]["collapseThreads"] is True and body[0][1]["calculateTotal"] is True
    row = page.rows[0]
    assert (
        row.senders == "Aisha, Tom, me (3)"
        and row.count == 3
        and row.unread
        and row.chips[0].name == "Work"
        and row.date_display == "10:42 AM"
    )
    assert page.total == 1284 and page.next_position == 50

    # Request-shape detail beyond the brief's own assertions.
    assert body[0][1]["filter"] == {"inMailbox": "mb-inbox"}
    assert body[0][1]["sort"] == [{"property": "receivedAt", "isAscending": False}]
    assert body[1][1]["properties"] == ["threadId"]
    assert body[3][1]["properties"] == [
        "id",
        "threadId",
        "mailboxIds",
        "keywords",
        "from",
        "subject",
        "receivedAt",
        "preview",
        "hasAttachment",
    ]

    # Row-0 fields the brief's own assertion line doesn't spell out, but
    # "nothing the template would have to compute itself" (self-review
    # criterion) requires be right too.
    assert row.thread_id == "t-work"
    assert row.email_ids == ["e-work-1", "e-work-2", "e-work-3"]  # oldest -> newest
    assert row.latest_email_id == "e-work-3"
    assert row.subject == "Re: Offsite agenda"
    assert row.preview == "Let's lock the Thursday slot and send the invite today"
    assert row.has_attachment is False
    assert row.starred is False
    assert row.received_at == datetime(2026, 9, 2, 10, 42, tzinfo=UTC)
    assert row.chips[0].mailbox_id == "m-work" and row.chips[0].color == "indigo"


async def test_starred_is_a_keyword_query(client, api_mock):
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    nav = _nav()
    await build_page(client, mailbox_key="starred", nav=nav, position=0, limit=50, me=ME, now=NOW)
    body = json.loads(api_mock.calls[0].request.content)["methodCalls"]
    filter_ = body[0][1]["filter"]
    assert filter_["hasKeyword"] == "$flagged"
    assert set(filter_["inMailboxOtherThan"]) == {"mb-junk", "mb-trash"}
    assert "inMailbox" not in filter_


async def test_all_mail_excludes_spam_and_trash(client, api_mock):
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    nav = _nav()
    await build_page(client, mailbox_key="all", nav=nav, position=0, limit=50, me=ME, now=NOW)
    body = json.loads(api_mock.calls[0].request.content)["methodCalls"]
    filter_ = body[0][1]["filter"]
    assert set(filter_["inMailboxOtherThan"]) == {"mb-junk", "mb-trash"}
    assert "hasKeyword" not in filter_
    assert "inMailbox" not in filter_


async def test_a_label_mailbox_id_used_as_the_key_queries_inMailbox(client, api_mock):
    # A raw label mailbox id (as when a user clicks "Work" in the sidebar)
    # behaves exactly like a role key: resolve_mailbox passes it through
    # unchanged and it becomes a plain `inMailbox` filter.
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    nav = _nav()
    await build_page(client, mailbox_key="m-work", nav=nav, position=0, limit=50, me=ME, now=NOW)
    body = json.loads(api_mock.calls[0].request.content)["methodCalls"]
    assert body[0][1]["filter"] == {"inMailbox": "m-work"}


# ---------------------------------------------------------------------------
# Row ordering + the two secondary rows' shape (read, unlabelled;
# has-attachment).
# ---------------------------------------------------------------------------


async def test_row_order_and_secondary_rows_shape(client, api_mock):
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    nav = _nav()
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert [r.thread_id for r in page.rows] == ["t-work", "t-ci", "t-hike"]

    ci = page.rows[1]
    assert ci.senders == "GitHub" and ci.count == 1 and not ci.unread and ci.chips == []
    assert ci.has_attachment is False

    hike = page.rows[2]
    assert hike.senders == "Lena Fischer" and hike.has_attachment is True and not hike.unread
    assert hike.chips == []  # unlabelled


# ---------------------------------------------------------------------------
# Chips: nav order, capped at 3, hidden labels never produce one, and a
# deterministic fallback colour when a visible label has none of its own.
# ---------------------------------------------------------------------------


async def test_chips_are_capped_at_three_in_nav_order(client, api_mock):
    nav = NavModel(
        system=_nav().system,
        more=_nav().more,
        labels=[
            LabelNode(
                mailbox_id="m-a",
                name="Alpha",
                color="indigo",
                count=0,
                visibility="show",
                children=[],
            ),
            LabelNode(
                mailbox_id="m-b",
                name="Bravo",
                color="emerald",
                count=0,
                visibility="show",
                children=[],
            ),
            LabelNode(
                mailbox_id="m-c",
                name="Charlie",
                color="rose",
                count=0,
                visibility="show",
                children=[],
            ),
            LabelNode(
                mailbox_id="m-d",
                name="Delta",
                color="amber",
                count=0,
                visibility="show",
                children=[],
            ),
        ],
        inbox_id="mb-inbox",
    )
    resp = _page_response(
        "t-many-labels",
        [
            {
                "id": "e1",
                "mailbox_ids": [
                    "mb-inbox",
                    "m-d",
                    "m-b",
                    "m-a",
                    "m-c",
                ],  # deliberately out of nav order
                "keywords": ["$seen"],
                "from": [("Priya Natarajan", "priya@example.com")],
                "subject": "Q3 roadmap review",
                "received_at": "2026-09-02T10:42:00Z",
                "preview": "Attaching the deck we walked through",
            }
        ],
        total=1,
    )
    api_mock.respond(json=resp)
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert [c.name for c in page.rows[0].chips] == ["Alpha", "Bravo", "Charlie"]


async def test_label_absent_from_nav_never_produces_a_chip(client, api_mock):
    # "m-secret" isn't in nav.labels at all -- as if build_nav had already
    # excluded it (visibility="hide"). A message carrying it must not grow
    # a chip for it regardless.
    nav = _nav()
    resp = _page_response(
        "t-secret",
        [
            {
                "id": "e1",
                "mailbox_ids": ["mb-inbox", "m-secret"],
                "keywords": ["$seen"],
                "from": [("Someone", "someone@example.com")],
                "subject": "Quiet",
                "received_at": "2026-09-02T10:00:00Z",
                "preview": "…",
            }
        ],
        total=1,
    )
    api_mock.respond(json=resp)
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows[0].chips == []


async def test_chip_color_falls_back_deterministically_when_label_has_no_color(client, api_mock):
    nav = _nav()  # "Receipts" (m-receipts) has color=None on its LabelNode
    resp = _page_response(
        "t-receipt",
        [
            {
                "id": "e1",
                "mailbox_ids": ["mb-inbox", "m-receipts"],
                "keywords": ["$seen"],
                "from": [("Hetzner Cloud", "billing@hetzner.example")],
                "subject": "Your invoice for August 2026",
                "received_at": "2026-09-02T09:15:00Z",
                "preview": "Invoice #2026-08-14421 is available in the console",
            }
        ],
        total=1,
    )
    api_mock.respond(json=resp)
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    chip = page.rows[0].chips[0]
    assert chip.name == "Receipts"
    assert chip.color == _LABEL_PALETTE[avatar_color("m-receipts")]


# ---------------------------------------------------------------------------
# Fields that must never be left for the template to compute: a missing
# subject, and the last-page/no-more-rows case.
# ---------------------------------------------------------------------------


async def test_subject_falls_back_to_no_subject_placeholder(client, api_mock):
    nav = _nav()
    resp = _page_response(
        "t-nosubject",
        [
            {
                "id": "e1",
                "mailbox_ids": ["mb-inbox"],
                "keywords": ["$seen"],
                "from": [("Someone", "someone@example.com")],
                "received_at": "2026-09-02T10:00:00Z",
                "preview": "…",
                # deliberately no "subject" key at all
            }
        ],
        total=1,
    )
    api_mock.respond(json=resp)
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows[0].subject == "(no subject)"


async def test_next_position_is_none_on_last_page(client, api_mock):
    nav = _nav()
    resp = _page_response(
        "t-only",
        [
            {
                "id": "e1",
                "mailbox_ids": ["mb-inbox"],
                "keywords": ["$seen"],
                "from": [("Someone", "someone@example.com")],
                "subject": "Last one",
                "received_at": "2026-09-02T10:00:00Z",
                "preview": "…",
            }
        ],
        total=1,
    )
    api_mock.respond(json=resp)
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert page.next_position is None


async def test_unprovisioned_role_mailbox_yields_an_empty_page_not_the_whole_account(
    client, api_mock
):
    # build_nav is deliberately lenient about a missing *secondary* role
    # mailbox (NavItem.mailbox_id=None rather than a hard failure), so
    # resolve_mailbox("archive") can legitimately be None. Passing that
    # straight to query_page would drop `inMailbox` from the filter and
    # return every message in the account under an "Archive" heading;
    # an unprovisioned mailbox contains nothing, so this must be an empty
    # page -- and no HTTP request at all.
    nav = _nav()
    for item in nav.more:
        if item.key == "archive":
            item.mailbox_id = None
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    page = await build_page(
        client, mailbox_key="archive", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows == [] and page.total == 0 and page.next_position is None
    assert page.position == 0 and page.limit == 50
    assert len(api_mock.calls) == 0


async def test_starred_true_when_any_message_is_flagged(client, api_mock):
    nav = _nav()
    resp = _page_response(
        "t-flagged",
        [
            {
                "id": "e1",
                "mailbox_ids": ["mb-inbox"],
                "keywords": ["$seen"],
                "from": [("Someone", "someone@example.com")],
                "subject": "First",
                "received_at": "2026-09-01T10:00:00Z",
                "preview": "…",
            },
            {
                "id": "e2",
                "mailbox_ids": ["mb-inbox"],
                "keywords": ["$seen", "$flagged"],
                "from": [("Someone Else", "else@example.com")],
                "subject": "Re: First",
                "received_at": "2026-09-02T10:00:00Z",
                "preview": "…",
            },
        ],
        total=1,
    )
    api_mock.respond(json=resp)
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows[0].starred is True


# ---------------------------------------------------------------------------
# Review finding 1: `Thread/get` returns every member of a thread regardless
# of mailbox, so a row must be aggregated over only the messages the query
# itself matched. Otherwise a trashed/sent/archived/draft member supplies the
# Inbox row's content -- and, because Email/query+collapseThreads orders rows
# by the newest *matching* message, the rendered date column stops agreeing
# with the order the rows are drawn in.
# ---------------------------------------------------------------------------

#: The review's own probe thread, verbatim: one Inbox message, one Sent reply
#: that also carries the "Work" label, and one message the user moved to Trash
#: which is both the newest in the thread and unread.
_MIXED_MAILBOX_THREAD = [
    {
        "id": "e1",
        "mailbox_ids": ["mb-inbox"],
        "keywords": ["$seen"],
        "from": [("Aisha Rahman", "aisha@example.com")],
        "subject": "Offsite agenda",
        "received_at": "2026-09-01T09:00:00Z",
        "preview": "Quick thought on where we should hold the offsite this year",
    },
    {
        "id": "e2",
        "mailbox_ids": ["mb-sent", "m-work"],
        "keywords": ["$seen"],
        "from": [("Demo", ME)],
        "subject": "Re: Offsite agenda",
        "received_at": "2026-09-02T11:00:00Z",
        "preview": "Thursday works for me",
    },
    {
        "id": "e3",
        "mailbox_ids": ["mb-trash"],
        "keywords": [],
        "from": [("Spammer", "spam@bad.example")],
        "subject": "trashed",
        "received_at": "2026-09-02T12:00:00Z",
        "preview": "TRASHED preview",
    },
]


async def test_inbox_row_ignores_trashed_and_sent_members_of_the_same_thread(client, api_mock):
    # Aggregating the whole thread gave: senders "Aisha, me, Spammer (3)",
    # subject "trashed", preview "TRASHED preview", date "12:00 PM",
    # latest_email_id "e3", unread True -- a message the user deleted
    # speaking for the Inbox row it was deleted from. Scoped to the Inbox,
    # only e1 exists.
    api_mock.respond(
        json=_page_response("t-probe", _MIXED_MAILBOX_THREAD, total=1, representative="e1")
    )
    page = await build_page(
        client, mailbox_key="inbox", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    row = page.rows[0]
    assert row.senders == "Aisha Rahman"  # single scoped sender -> full name, no "(3)"
    assert row.count == 1
    assert row.subject == "Offsite agenda"
    assert row.preview == "Quick thought on where we should hold the offsite this year"
    assert row.latest_email_id == "e1"
    assert row.received_at == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert row.unread is False  # e3 is unread, but e3 is not in the Inbox
    assert row.chips == []  # the "Work" label rides on the Sent copy only

    # ThreadRow.email_ids is the deliberate exception: actions operate on the
    # whole conversation, so it still spans every member, oldest -> newest.
    assert row.email_ids == ["e1", "e2", "e3"]
    assert row.count != len(row.email_ids)


async def test_sent_reply_does_not_move_the_inbox_rows_date_out_of_sort_order(client, api_mock):
    # Email/query + collapseThreads placed this row by e1 (2026-09-01, the
    # newest message matching inMailbox=Inbox). If date_display came from the
    # newest message in the *thread* it would read "11:00 AM" today while
    # sitting in yesterday's position -- a visibly unsorted date column.
    thread = [m for m in _MIXED_MAILBOX_THREAD if m["id"] in {"e1", "e2"}]
    api_mock.respond(json=_page_response("t-sent", thread, total=1, representative="e1"))
    page = await build_page(
        client, mailbox_key="inbox", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows[0].date_display == "Sep 1"
    assert page.rows[0].latest_email_id == "e1"


async def test_draft_member_does_not_make_the_inbox_row_read_as_unread(client, api_mock):
    # Drafts never carry $seen, so once compose lands every thread with a
    # draft reply would have read as unread forever in every view.
    thread = [
        _MIXED_MAILBOX_THREAD[0],
        {
            "id": "e-draft",
            "mailbox_ids": ["mb-drafts"],
            "keywords": [],
            "from": [("Demo", ME)],
            "subject": "Re: Offsite agenda",
            "received_at": "2026-09-02T13:00:00Z",
            "preview": "half-written reply",
        },
    ]
    api_mock.respond(json=_page_response("t-draft", thread, total=1, representative="e1"))
    page = await build_page(
        client, mailbox_key="inbox", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows[0].unread is False
    assert page.rows[0].count == 1

    # ...and the Drafts view sees the mirror image: only the draft.
    api_mock.reset()
    api_mock.respond(json=_page_response("t-draft", thread, total=1, representative="e-draft"))
    drafts = await build_page(
        client, mailbox_key="drafts", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    assert drafts.rows[0].unread is True and drafts.rows[0].latest_email_id == "e-draft"


async def test_starred_scope_counts_only_flagged_messages(client, api_mock):
    # The Starred filter is hasKeyword:$flagged AND inMailboxOtherThan, so the
    # row's scope has to be both conditions, not just "in the thread".
    thread = [
        {
            "id": "e1",
            "mailbox_ids": ["mb-inbox"],
            "keywords": ["$seen", "$flagged"],
            "from": [("Aisha Rahman", "aisha@example.com")],
            "subject": "Offsite agenda",
            "received_at": "2026-09-01T09:00:00Z",
            "preview": "starred one",
        },
        {
            "id": "e2",
            "mailbox_ids": ["mb-inbox"],
            "keywords": ["$seen"],
            "from": [("Tom Reyes", "tom@example.com")],
            "subject": "Re: Offsite agenda",
            "received_at": "2026-09-02T09:00:00Z",
            "preview": "not starred",
        },
    ]
    api_mock.respond(json=_page_response("t-star", thread, total=1, representative="e1"))
    page = await build_page(
        client, mailbox_key="starred", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    row = page.rows[0]
    assert row.count == 1 and row.senders == "Aisha Rahman" and row.latest_email_id == "e1"
    assert row.starred is True


async def test_all_mail_scope_mirrors_in_mailbox_other_than_semantics(client, api_mock):
    # RFC 8621 §4.4.1: inMailboxOtherThan matches an Email in at least one
    # mailbox NOT in the list -- so a message filed in both Inbox and Trash
    # still matches, while one sitting solely in Trash does not. The scope
    # predicate reproduces the server's rule rather than the looser "not in
    # spam/trash" reading, because a scope that disagrees with the filter
    # reintroduces the very mismatch it exists to prevent.
    thread = [
        {
            "id": "e-both",
            "mailbox_ids": ["mb-inbox", "mb-trash"],
            "keywords": ["$seen"],
            "from": [("Aisha Rahman", "aisha@example.com")],
            "subject": "Offsite agenda",
            "received_at": "2026-09-01T09:00:00Z",
            "preview": "in inbox and trash",
        },
        {
            "id": "e-trash-only",
            "mailbox_ids": ["mb-trash"],
            "keywords": [],
            "from": [("Spammer", "spam@bad.example")],
            "subject": "trashed",
            "received_at": "2026-09-02T12:00:00Z",
            "preview": "TRASHED preview",
        },
    ]
    api_mock.respond(json=_page_response("t-all", thread, total=1, representative="e-both"))
    page = await build_page(
        client, mailbox_key="all", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    row = page.rows[0]
    assert row.count == 1 and row.latest_email_id == "e-both" and row.unread is False


async def test_thread_with_no_message_in_scope_is_skipped_not_rendered_blank(client, api_mock):
    # Only reachable if a message moves between the Email/query and the final
    # Email/get of the same batch. A row with nothing to show is skipped.
    api_mock.respond(
        json=_page_response(
            "t-gone",
            [_MIXED_MAILBOX_THREAD[2]],  # trash-only
            total=1,
            representative="e3",
        )
    )
    page = await build_page(
        client, mailbox_key="inbox", nav=_nav(), position=0, limit=50, me=ME, now=NOW
    )
    assert page.rows == [] and page.total == 1


# ---------------------------------------------------------------------------
# Review finding 2: a "show if unread" label at zero unread must still chip.
# ---------------------------------------------------------------------------


async def test_show_if_unread_label_at_zero_unread_still_produces_a_chip(client, api_mock):
    # build_nav keeps such a node in nav.labels (only "hide" is filtered
    # structurally) precisely so its chip does not blink out whenever some
    # unrelated message elsewhere gets read. mailbox_tree.hidden_in_nav is
    # what suppresses its sidebar row instead.
    nav = _nav()
    nav.labels.append(
        LabelNode(
            mailbox_id="m-quiet",
            name="Newsletters Quiet",
            color="amber",
            count=0,
            visibility="show_if_unread",
            children=[],
        )
    )
    assert hidden_in_nav(nav.labels[-1]) is True
    api_mock.respond(
        json=_page_response(
            "t-quiet",
            [
                {
                    "id": "e1",
                    "mailbox_ids": ["mb-inbox", "m-quiet"],
                    "keywords": ["$seen"],
                    "from": [("The Browser", "hello@thebrowser.example")],
                    "subject": "Browser Weekly #612",
                    "received_at": "2026-09-02T08:00:00Z",
                    "preview": "Five things worth reading this week",
                }
            ],
            total=1,
        )
    )
    page = await build_page(
        client, mailbox_key="inbox", nav=nav, position=0, limit=50, me=ME, now=NOW
    )
    assert [c.name for c in page.rows[0].chips] == ["Newsletters Quiet"]
