"""`JmapClient.get_submissions` / `query_submissions` (RFC 8621 §7.1/§7.3)
and the push-type subscription that lets an `EmailSubmission` change reach
the browser.

Response bodies below are the exact shape Stalwart 0.16 returned live
(`EmailSubmission/query` -> `EmailSubmission/get`, see
`tests/unit/test_outbound.py::LIVE_STALWART_ENTRY`); the request-side
assertions pin what this client sends, since respx accepts anything.
"""

from __future__ import annotations

import json

import pytest

from mailosh.jmap.errors import MethodError

ENTRY = {
    "id": "b",
    "emailId": "ryaaaaeo",
    "threadId": "eo",
    "sendAt": "2026-09-05T06:46:40Z",
    "undoStatus": "final",
    "deliveryStatus": {
        "demo@mailosh.test": {
            "delivered": "unknown",
            "smtpReply": "250 2.1.5 Queued",
            "displayed": "unknown",
        }
    },
    "dsnBlobIds": [],
}

GET_RESPONSE = {
    "methodResponses": [
        [
            "EmailSubmission/get",
            {"accountId": "c", "state": "s12bq", "list": [ENTRY], "notFound": ["zz"]},
            "s0",
        ]
    ],
    "sessionState": "12cc1dc8",
}

QUERY_PLUS_GET_RESPONSE = {
    "methodResponses": [
        [
            "EmailSubmission/query",
            {
                "accountId": "c",
                "queryState": "s12bq",
                "canCalculateChanges": True,
                "position": 0,
                "ids": ["b"],
                "limit": 5,
            },
            "q0",
        ],
        [
            "EmailSubmission/get",
            {"accountId": "c", "state": "s12bq", "list": [ENTRY], "notFound": []},
            "s0",
        ],
    ],
    "sessionState": "12cc1dc8",
}


def _request_body(route) -> dict:
    return json.loads(route.calls.last.request.content)


async def test_get_submissions_sends_ids_and_properties_and_parses_the_list(client, api_mock):
    api_mock.respond(json=GET_RESPONSE)
    got = await client.get_submissions(["b", "zz", "b"])

    (call,) = _request_body(api_mock)["methodCalls"]
    assert call[0] == "EmailSubmission/get"
    assert call[1]["ids"] == ["b", "zz"], "duplicates collapsed, order kept"
    assert set(call[1]["properties"]) >= {
        "id",
        "emailId",
        "threadId",
        "undoStatus",
        "sendAt",
        "deliveryStatus",
    }
    assert "urn:ietf:params:jmap:submission" in _request_body(api_mock)["using"]

    (sub,) = got
    assert (sub.id, sub.email_id, sub.thread_id, sub.undo_status) == (
        "b",
        "ryaaaaeo",
        "eo",
        "final",
    )
    assert sub.delivery_status["demo@mailosh.test"].delivered == "unknown"
    # `zz` was notFound: absent from the result, not an error.


async def test_get_submissions_with_no_ids_makes_no_request(client, api_mock):
    api_mock.respond(json=GET_RESPONSE)
    assert await client.get_submissions([]) == []
    assert not api_mock.called


async def test_get_submissions_raises_on_a_method_error(client, api_mock):
    api_mock.respond(
        json={
            "methodResponses": [["error", {"type": "unknownMethod"}, "s0"]],
            "sessionState": "x",
        }
    )
    with pytest.raises(MethodError):
        await client.get_submissions(["b"])


async def test_query_submissions_chains_query_into_get_with_a_result_reference(client, api_mock):
    api_mock.respond(json=QUERY_PLUS_GET_RESPONSE)
    got = await client.query_submissions(undo_status="pending", email_ids=["e1"], limit=5)

    query, get = _request_body(api_mock)["methodCalls"]
    assert query[0] == "EmailSubmission/query"
    assert query[1]["filter"] == {"undoStatus": "pending", "emailIds": ["e1"]}
    assert query[1]["sort"] == [{"property": "sendAt", "isAscending": False}]
    assert query[1]["limit"] == 5
    assert get[0] == "EmailSubmission/get"
    assert get[1]["#ids"] == {"resultOf": "q0", "name": "EmailSubmission/query", "path": "/ids"}
    assert [s.id for s in got] == ["b"]


async def test_query_submissions_omits_the_filter_when_nothing_was_asked(client, api_mock):
    api_mock.respond(json=QUERY_PLUS_GET_RESPONSE)
    await client.query_submissions()
    query, _ = _request_body(api_mock)["methodCalls"]
    assert "filter" not in query[1]


def test_event_stream_asks_stalwart_for_email_submission_pushes():
    """`_PUSH_TYPES` is what `event_stream` substitutes into the session's
    `eventSourceUrl` `{types}`; a Sent row's delivery pill refreshes on the
    list re-GET that push triggers, so the type must be requested."""
    from mailosh.jmap.client import _PUSH_TYPES

    assert set(_PUSH_TYPES.split(",")) == {"Email", "Mailbox", "EmailSubmission"}
