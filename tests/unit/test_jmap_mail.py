import json

import pytest
from conftest import (
    EMAIL_QUERY_PLUS_GET_RESPONSE,
    EMPTY_SET_RESPONSE,
    IMPORT_RESPONSE,
    MAILBOX_CREATE_RESPONSE,
    MAILBOX_NOT_CREATED_RESPONSE,
    MAILBOXES_GET_RESPONSE,
    NOT_UPDATED_RESPONSE,
    QUERY_PAGE_RESPONSE,
    THREAD_GET_PLUS_EMAIL_RESPONSE,
)

from mailosh.jmap.client import find_inbox
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Mailbox


async def test_get_mailboxes(client, api_mock):
    api_mock.respond(json=MAILBOXES_GET_RESPONSE)
    boxes = await client.get_mailboxes()
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][0] == "Mailbox/get"
    assert body["methodCalls"][0][1]["accountId"] == client.account_id
    assert [m.id for m in boxes] == ["mb-inbox", "mb-label1"]
    assert boxes[0].role == "inbox"
    assert boxes[0].unread_emails == 3


async def test_query_inbox_single_roundtrip(client, api_mock):
    api_mock.respond(json=EMAIL_QUERY_PLUS_GET_RESPONSE)  # fixture dict in conftest
    rows = await client.query_inbox("mb-inbox", limit=50)
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    q, g = body["methodCalls"][0], body["methodCalls"][1]
    assert q[0] == "Email/query" and q[1]["collapseThreads"] is True
    assert q[1]["sort"] == [{"property": "receivedAt", "isAscending": False}]
    assert g[0] == "Email/get"
    assert g[1]["#ids"] == {"resultOf": q[2], "name": "Email/query", "path": "/ids"}
    assert rows[0].thread_id and rows[0].preview


async def test_get_thread_single_roundtrip(client, api_mock):
    api_mock.respond(json=THREAD_GET_PLUS_EMAIL_RESPONSE)
    msgs = await client.get_thread("t-1")
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    t, g = body["methodCalls"][0], body["methodCalls"][1]
    assert t[0] == "Thread/get" and t[1]["ids"] == ["t-1"]
    assert g[1]["#ids"]["path"] == "/list/*/emailIds/*"  # correct backref shape per RFC 8620 §3.7
    assert g[1]["fetchTextBodyValues"] is True
    assert msgs[0].text_body


async def test_set_keyword_patch_syntax(client, api_mock):
    api_mock.respond(json=EMPTY_SET_RESPONSE)
    await client.set_keyword("e1", "$seen", True)
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][1]["update"] == {"e1": {"keywords/$seen": True}}


async def test_set_keyword_not_updated_raises(client, api_mock):
    # Review finding: RFC 8620 §5.3 reports a per-object update failure in
    # `notUpdated`, not as a batch-level `error` response, so a rejected
    # update must not silently return None the way a success does.
    api_mock.respond(json=NOT_UPDATED_RESPONSE)
    with pytest.raises(JmapError) as exc_info:
        await client.set_keyword("e1", "$seen", True)
    assert "e1" in str(exc_info.value)
    assert "notFound" in str(exc_info.value)


async def test_move_add_and_remove_patch_syntax(client, api_mock):
    api_mock.respond(json=EMPTY_SET_RESPONSE)
    await client.move("e1", add={"mb-label1"}, remove={"mb-inbox"})
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][0] == "Email/set"
    assert body["methodCalls"][0][1]["update"] == {
        "e1": {"mailboxIds/mb-label1": True, "mailboxIds/mb-inbox": None}
    }


async def test_move_not_updated_raises(client, api_mock):
    api_mock.respond(json=NOT_UPDATED_RESPONSE)
    with pytest.raises(JmapError) as exc_info:
        await client.move("e1", add={"mb-label1"})
    assert "e1" in str(exc_info.value)
    assert "notFound" in str(exc_info.value)


async def test_import_email_multi_mailbox(client, api_mock, upload_mock):
    upload_mock.respond(json={"blobId": "b1", "type": "message/rfc822", "size": 3})
    api_mock.respond(json=IMPORT_RESPONSE)
    blob = await client.upload(b"raw", "message/rfc822")
    eid = await client.import_email(blob, {"mb-inbox", "mb-label1"}, {"$seen"}, None)
    body = json.loads(api_mock.calls[0].request.content)
    creation = body["methodCalls"][0][1]["emails"]["i0"]
    assert creation["mailboxIds"] == {"mb-inbox": True, "mb-label1": True}
    assert eid == "e-imported"


async def test_create_mailbox_request_and_response(client, api_mock):
    api_mock.respond(json=MAILBOX_CREATE_RESPONSE)
    mailbox_id = await client.create_mailbox("SpikeLabel")
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][0] == "Mailbox/set"
    assert body["methodCalls"][0][1]["accountId"] == client.account_id
    assert body["methodCalls"][0][1]["create"] == {"m0": {"name": "SpikeLabel"}}
    assert mailbox_id == "mb-new"


async def test_create_mailbox_carries_a_role_only_when_one_is_asked_for(client, api_mock):
    # The role is what makes a created folder *the* Archive rather than a
    # label that happens to be named one: `build_nav` resolves every system
    # item by role and never by name, and RFC 8621 §2 makes the role unique
    # per account, which is what stops a second one being created.
    api_mock.respond(json=MAILBOX_CREATE_RESPONSE)
    assert await client.create_mailbox("Archive", role="archive") == "mb-new"
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][1]["create"] == {"m0": {"name": "Archive", "role": "archive"}}


async def test_create_mailbox_not_created_raises(client, api_mock):
    # Mirrors import_email's created/notCreated check (RFC 8620 §5.3): a
    # rejected create must raise, not return as if a mailbox id came back.
    api_mock.respond(json=MAILBOX_NOT_CREATED_RESPONSE)
    with pytest.raises(JmapError) as exc_info:
        await client.create_mailbox("SpikeLabel")
    assert "invalidArguments" in str(exc_info.value)


# ---------------------------------------------------------------------------
# find_inbox (phase0 final-review FIX 4: the one shared inbox-resolution
# helper, replacing three separately-duplicated idioms — see this
# function's own docstring in mailosh/jmap/client.py for the full story).
# ---------------------------------------------------------------------------

#: A mailbox that IS the real, role-tagged inbox.
_ROLE_INBOX = Mailbox(
    id="mb-real-inbox",
    name="Inbox",
    parent_id=None,
    role="inbox",
    sort_order=10,
    total_emails=5,
    unread_emails=1,
)

#: A mailbox merely NAMED "inbox" (any case), with no role set — the shape
#: that shadowed the real inbox under the old `{m.role or m.name: m}["inbox"]`
#: idiom, since both mailboxes mapped to the same dict key.
_ROLELESS_MAILBOX_NAMED_INBOX = Mailbox(
    id="mb-fake",
    name="inbox",
    parent_id=None,
    role=None,
    sort_order=5,
    total_emails=2,
    unread_emails=2,
)


def test_find_inbox_returns_the_role_tagged_mailbox():
    other = _ROLE_INBOX.model_copy(update={"id": "mb-other", "name": "Archive", "role": "archive"})
    assert find_inbox([other, _ROLE_INBOX]) is _ROLE_INBOX


def test_find_inbox_ignores_a_same_named_mailbox_with_no_role():
    """Regression test for the FIX 4 bug: a mailbox named "inbox" with no
    role must never shadow the real role-tagged inbox, in either list
    order — the old dict-keyed-by-`role or name` idiom depended on
    iteration/insertion order to (sometimes) get this right; role-based
    matching must be correct regardless of order.
    """
    assert find_inbox([_ROLELESS_MAILBOX_NAMED_INBOX, _ROLE_INBOX]) is _ROLE_INBOX
    assert find_inbox([_ROLE_INBOX, _ROLELESS_MAILBOX_NAMED_INBOX]) is _ROLE_INBOX


def test_find_inbox_raises_when_no_role_tagged_inbox_exists():
    # Only a roleless "inbox"-named mailbox is present — must still raise,
    # not fall back to matching by name.
    with pytest.raises(JmapError):
        find_inbox([_ROLELESS_MAILBOX_NAMED_INBOX])


# ---------------------------------------------------------------------------
# query_page (Task 6): the RFC 8621 §4.10 four-call chain, one HTTP request,
# its result-reference wiring, and its per-mailbox-key filter shapes. The
# higher-level "which filter a nav key needs" mapping has its own coverage
# in tests/unit/test_thread_list.py (via build_page); these tests are
# scoped to query_page's own request/response contract in isolation.
# ---------------------------------------------------------------------------


async def test_query_page_single_roundtrip_and_result_ref_chain(client, api_mock):
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    page = await client.query_page(mailbox_id="mb-inbox", position=0, limit=50)
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    calls = body["methodCalls"]
    assert [c[0] for c in calls] == ["Email/query", "Email/get", "Thread/get", "Email/get"]

    q, g, t, e = calls
    assert q[1]["filter"] == {"inMailbox": "mb-inbox"}
    assert q[1]["sort"] == [{"property": "receivedAt", "isAscending": False}]
    assert q[1]["collapseThreads"] is True
    assert q[1]["calculateTotal"] is True
    assert q[1]["position"] == 0 and q[1]["limit"] == 50

    # Each hop's result reference (RFC 8620 §3.7) points at the previous
    # call's own id/name/path -- same idiom test_query_inbox_single_
    # roundtrip/test_get_thread_single_roundtrip already use above.
    assert g[1]["#ids"] == {"resultOf": q[2], "name": "Email/query", "path": "/ids"}
    assert g[1]["properties"] == ["threadId"]
    assert t[1]["#ids"] == {"resultOf": g[2], "name": "Email/get", "path": "/list/*/threadId"}
    assert e[1]["#ids"] == {"resultOf": t[2], "name": "Thread/get", "path": "/list/*/emailIds/*"}
    assert e[1]["properties"] == [
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

    assert page.thread_order == ["t-work", "t-ci", "t-hike"]
    assert page.total == 1284 and page.position == 0
    assert {tid: len(msgs) for tid, msgs in page.emails_by_thread.items()} == {
        "t-work": 3,
        "t-ci": 1,
        "t-hike": 1,
    }


async def test_query_page_starred_filter_shape(client, api_mock):
    # mailbox_id=None + has_keyword="$flagged" + exclude_mailbox_ids -- the
    # "Starred" virtual view's filter (design spec §5.2).
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    await client.query_page(
        mailbox_id=None,
        position=0,
        limit=50,
        exclude_mailbox_ids={"mb-junk", "mb-trash"},
        has_keyword="$flagged",
    )
    body = json.loads(api_mock.calls[0].request.content)
    filter_ = body["methodCalls"][0][1]["filter"]
    assert filter_["hasKeyword"] == "$flagged"
    assert filter_["inMailboxOtherThan"] == ["mb-junk", "mb-trash"]  # sorted, deterministic
    assert "inMailbox" not in filter_


async def test_query_page_all_mail_filter_shape(client, api_mock):
    # mailbox_id=None + exclude_mailbox_ids only, no has_keyword -- "All
    # mail"'s filter (everything except Spam/Trash).
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    await client.query_page(
        mailbox_id=None, position=0, limit=50, exclude_mailbox_ids={"mb-trash", "mb-junk"}
    )
    body = json.loads(api_mock.calls[0].request.content)
    filter_ = body["methodCalls"][0][1]["filter"]
    assert filter_ == {"inMailboxOtherThan": ["mb-junk", "mb-trash"]}


# ---------------------------------------------------------------------------
# set_mailboxes / set_keywords (Task 6): bulk versions of move/set_keyword,
# one Email/set call across every given id.
# ---------------------------------------------------------------------------

#: A successful `Email/set` update response covering three ids at once —
#: the happy-path counterpart to `EMPTY_SET_RESPONSE`'s single-id shape.
BULK_SET_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "updated": {"e1": None, "e2": None, "e3": None},
            },
            "s0",
        ]
    ],
    "sessionState": "s1",
}

#: Same three ids, but "e2" is rejected — proves the bulk methods check
#: *every* id's own outcome (RFC 8620 §5.3), not just the first or last.
BULK_SET_PARTIAL_NOT_UPDATED_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "updated": {"e1": None, "e3": None},
                "notUpdated": {"e2": {"type": "notFound", "description": "No Email with that id."}},
            },
            "s0",
        ]
    ],
    "sessionState": "s1",
}


async def test_set_mailboxes_bulk_patch_syntax(client, api_mock):
    api_mock.respond(json=BULK_SET_RESPONSE)
    await client.set_mailboxes(["e1", "e2", "e3"], add={"mb-label1"}, remove={"mb-inbox"})
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][0] == "Email/set"
    update = body["methodCalls"][0][1]["update"]
    assert set(update.keys()) == {"e1", "e2", "e3"}
    for patch in update.values():
        assert patch == {"mailboxIds/mb-label1": True, "mailboxIds/mb-inbox": None}


async def test_set_mailboxes_raises_on_any_not_updated(client, api_mock):
    api_mock.respond(json=BULK_SET_PARTIAL_NOT_UPDATED_RESPONSE)
    with pytest.raises(JmapError) as exc_info:
        await client.set_mailboxes(["e1", "e2", "e3"], add={"mb-label1"})
    assert "e2" in str(exc_info.value)


async def test_set_keywords_bulk_patch_syntax(client, api_mock):
    api_mock.respond(json=BULK_SET_RESPONSE)
    await client.set_keywords(["e1", "e2", "e3"], "$seen", True)
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][1]["update"] == {
        "e1": {"keywords/$seen": True},
        "e2": {"keywords/$seen": True},
        "e3": {"keywords/$seen": True},
    }


async def test_set_keywords_raises_on_any_not_updated(client, api_mock):
    api_mock.respond(json=BULK_SET_PARTIAL_NOT_UPDATED_RESPONSE)
    with pytest.raises(JmapError) as exc_info:
        await client.set_keywords(["e1", "e2", "e3"], "$seen", False)
    assert "e2" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Label plumbing (`Mailbox/set` update/destroy) and the raw-filter query.
# Both are seams for Phase 1D and exist so that search and labels can be
# built without two agents editing this file at once.
# ---------------------------------------------------------------------------


async def test_update_mailbox_sends_the_patch_verbatim(client, api_mock):
    api_mock.respond(json={"methodResponses": [["Mailbox/set", {"updated": {"mb-1": None}}, "u0"]]})
    await client.update_mailbox("mb-1", {"name": "Work", "parentId": "mb-p"})
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][0] == "Mailbox/set"
    assert body["methodCalls"][0][1]["update"] == {"mb-1": {"name": "Work", "parentId": "mb-p"}}


async def test_update_mailbox_keeps_a_null_parent_because_it_means_top_level(client, api_mock):
    """`{"parentId": None}` is how a nested label is moved back to the root.

    Filtering `None` out of the patch as "unset" would silently turn that
    into a no-op, and the label would appear stuck under its parent.
    """
    api_mock.respond(json={"methodResponses": [["Mailbox/set", {"updated": {"mb-1": None}}, "u0"]]})
    await client.update_mailbox("mb-1", {"parentId": None})
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][1]["update"] == {"mb-1": {"parentId": None}}


async def test_update_mailbox_not_updated_raises(client, api_mock):
    api_mock.respond(
        json={
            "methodResponses": [
                ["Mailbox/set", {"notUpdated": {"mb-1": {"type": "invalidProperties"}}}, "u0"]
            ]
        }
    )
    with pytest.raises(JmapError) as exc_info:
        await client.update_mailbox("mb-1", {"name": "Work"})
    assert "invalidProperties" in str(exc_info.value)


async def test_destroying_a_label_does_not_delete_the_mail_in_it(client, api_mock):
    """The `onDestroyRemoveEmails` default is a safety property, not a detail.

    RFC 8621 §2.5 makes this flag the whole difference between "remove this
    label" and "delete every message that carried it". Design spec §10 asks
    for the first — "conversations keep their other labels" — so the
    destructive form has to be asked for explicitly rather than arrived at
    by forgetting an argument.
    """
    api_mock.respond(json={"methodResponses": [["Mailbox/set", {"destroyed": ["mb-1"]}, "d0"]]})
    await client.destroy_mailbox("mb-1")
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][1]["destroy"] == ["mb-1"]
    assert body["methodCalls"][0][1]["onDestroyRemoveEmails"] is False


async def test_destroy_mailbox_not_destroyed_raises(client, api_mock):
    api_mock.respond(
        json={
            "methodResponses": [
                ["Mailbox/set", {"notDestroyed": {"mb-1": {"type": "mailboxHasEmail"}}}, "d0"]
            ]
        }
    )
    with pytest.raises(JmapError) as exc_info:
        await client.destroy_mailbox("mb-1")
    assert "mailboxHasEmail" in str(exc_info.value)


async def test_query_search_passes_the_filter_through_untouched(client, api_mock):
    """A `FilterOperator` tree reaches the server exactly as built.

    `query_page`'s three structured arguments cannot express an OR, and
    rewriting or "normalising" the tree here would put a second opinion
    about what a query means next to the parser that is supposed to be the
    only one.
    """
    api_mock.respond(json=QUERY_PAGE_RESPONSE)
    tree = {
        "operator": "AND",
        "conditions": [
            {"from": "ada@x.test"},
            {"operator": "NOT", "conditions": [{"hasKeyword": "$seen"}]},
        ],
    }
    await client.query_search(filter=tree, position=0, limit=25)
    body = json.loads(api_mock.calls[0].request.content)
    assert body["methodCalls"][0][0] == "Email/query"
    assert body["methodCalls"][0][1]["filter"] == tree
    assert body["methodCalls"][0][1]["collapseThreads"] is True
