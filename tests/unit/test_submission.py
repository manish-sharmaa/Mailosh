"""Tests for compose + send (Task 9, SPK-1 gate): `JmapClient.get_identity`/
`send` (RFC 8621 §6.1 Identity, §7.5 EmailSubmission), the `html_to_text`
plain-text fallback, and the `GET`/`POST /compose` routes.

Response fixtures for `send`'s batch live in *this* file, not `conftest.py`
(the brief leaves that choice to the implementer) — nothing outside this
module needs Drafts/Sent-mailbox or Identity response shapes, unlike e.g.
`MAILBOXES_GET_RESPONSE`, which `test_jmap_mail.py` and (via `FAKE_INBOX`)
the web-layer test modules both actually share.

Design note carried from `JmapClient.send`'s own docstring (see
`mailosh/jmap/client.py`): `Identity/get`, when not cached, is its own
`_call` — a third HTTP round trip, after `Mailbox/get` and before the final
two-method `Email/set` + `EmailSubmission/set` batch — rather than folded
into that same batch as the task brief's literal wording suggested. RFC 8620
gives no mechanism to inject a fetched value into a *nested* create
property (`create.d0.from`, `create.s0.identityId`) from an earlier call's
response within one request, so the tests below assert three sequential
`api_mock` calls (Mailbox/get, Identity/get, the 2-method batch) for the
uncached path, not one combined three-method call.

Controller review (post-implementation) flagged a second issue in that same
area: `send`'s batch call originally read only the deduplicated `_call`
result, so a *failed* implicit `onSuccessUpdateEmail` update (reusing the
`EmailSubmission/set` call's own `"s0"` id) was silently discarded rather
than surfaced — `send` would report success even if the Drafts->Sent move
never actually happened. Fixed by having `send` use the new `_call_raw`
(every response tuple verbatim, no dedup, no raise-on-error) and inspect
any *additional* `"s0"`-tagged response itself; a rejected or errored
implicit update now logs a `logger.warning` rather than either being lost
or turned into a spurious `JmapError` for an send that actually succeeded.
`test_send_logs_warning_when_implicit_update_reports_not_updated` and
`test_send_logs_warning_when_implicit_update_is_an_error_tuple` below cover
that; `test_send_survives_stalwart_reusing_the_submission_call_id_for_its_implicit_update`
is the sibling "implicit update succeeds cleanly, no warning" case.

Task 5 (design spec §9, controller ruling #3) deletes the Phase 0 `GET`/
`POST /compose` routes this file used to test at HTTP level (along with the
route-only `FakeClient`/`deps.get_client` plumbing they depended on) — real
compose is a later 1B/1C task, built on the new design-system layout, not
Phase 0's `compose.html`. `html_to_text` itself survives that cut
(controller ruling #3 explicitly keeps it, still defined in
`mailosh.web.app` for that later task to reuse): it's a pure function with
no route dependency of its own.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from mailosh.jmap.errors import JmapError
from mailosh.web.app import html_to_text

# ---------------------------------------------------------------------------
# JmapClient.get_identity / send: response fixtures.
# ---------------------------------------------------------------------------

#: `Mailbox/get` with Drafts + Sent (this file's own send()-focused mailbox
#: set — deliberately narrower than conftest.py's MAILBOXES_GET_RESPONSE,
#: which has no drafts/sent roles at all).
MAILBOXES_FOR_SEND_RESPONSE = {
    "methodResponses": [
        [
            "Mailbox/get",
            {
                "accountId": "c",
                "state": "abc",
                "list": [
                    {
                        "id": "mb-drafts",
                        "name": "Drafts",
                        "parentId": None,
                        "role": "drafts",
                        "sortOrder": 30,
                        "totalEmails": 0,
                        "unreadEmails": 0,
                    },
                    {
                        "id": "mb-sent",
                        "name": "Sent",
                        "parentId": None,
                        "role": "sent",
                        "sortOrder": 40,
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

#: `Identity/get`: one identity, the account's own demo address.
IDENTITY_GET_RESPONSE = {
    "methodResponses": [
        [
            "Identity/get",
            {
                "accountId": "c",
                "state": "abc",
                "list": [{"id": "id-1", "name": "Demo User", "email": "demo@mailosh.test"}],
                "notFound": [],
            },
            "i0",
        ]
    ],
    "sessionState": "s1",
}


def _combined(*responses: dict) -> dict:
    """Merge several one-entry `methodResponses` fixtures into one dict.

    `api_mock.respond(json=...)` returns the *same* body for every POST to
    the mocked endpoint — since `send()`/`get_identity()` make up to three
    separate HTTP calls (`Mailbox/get`, `Identity/get`, the create batch),
    and `JmapClient._call` only ever reads the specific call id its caller
    asked for out of whatever `methodResponses` array comes back, handing
    every one of those calls the *union* of all the responses they might
    individually need works: each call simply ignores the entries under
    call ids it didn't ask for.
    """
    return {
        "methodResponses": [r for resp in responses for r in resp["methodResponses"]],
        "sessionState": "s1",
    }


#: `Email/set` create ("d0") + `EmailSubmission/set` create ("s0"), both
#: succeeding — the happy-path tail of `send()`'s final batched call.
SEND_CREATE_OK_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "created": {
                    "d0": {"id": "e-draft-1", "blobId": "b1", "threadId": "t-new", "size": 128}
                },
                "notCreated": None,
            },
            "d0",
        ],
        [
            "EmailSubmission/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "created": {"s0": {"id": "sub-1"}},
                "notCreated": None,
                "updated": {"e-draft-1": None},
            },
            "s0",
        ],
    ],
    "sessionState": "s1",
}

#: Same shape, but `EmailSubmission/set`'s create is rejected — proves the
#: `notCreated` check on the *submission* half of `send()` raises.
SEND_SUBMISSION_NOT_CREATED_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s1",
                "created": {
                    "d0": {"id": "e-draft-1", "blobId": "b1", "threadId": "t-new", "size": 128}
                },
                "notCreated": None,
            },
            "d0",
        ],
        [
            "EmailSubmission/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s0",
                "created": None,
                "notCreated": {
                    "s0": {"type": "invalidProperties", "description": "no such identity"}
                },
            },
            "s0",
        ],
    ],
    "sessionState": "s1",
}

#: `Email/set`'s own create rejected instead — proves the `notCreated` check
#: on the *draft* half of `send()` raises too ("for either object" per the
#: task's requirements), without ever reaching the submission check.
SEND_DRAFT_NOT_CREATED_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s0",
                "created": None,
                "notCreated": {
                    "d0": {"type": "invalidProperties", "description": "bad bodyStructure"}
                },
            },
            "d0",
        ],
        [
            "EmailSubmission/set",
            {
                "accountId": "c",
                "oldState": "s0",
                "newState": "s0",
                "created": None,
                "notCreated": {"s0": {"type": "invalidProperties", "description": "emailId #d0"}},
            },
            "s0",
        ],
    ],
    "sessionState": "s1",
}

#: Both creates succeed, but the *implicit* onSuccessUpdateEmail-triggered
#: Email/set update (third entry, also tagged "s0" — see client.py's
#: `_call_raw`/`send` docstrings) reports the draft's real id in
#: `notUpdated`: the submission genuinely went out, but the local
#: Drafts->Sent move was rejected (ACL/quota/race, mocked here as
#: "forbidden"). `send` must still return the submission id and log a
#: warning — not raise.
SEND_IMPLICIT_UPDATE_NOT_UPDATED_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {"accountId": "c", "created": {"d0": {"id": "e-draft-1", "blobId": "b1"}}},
            "d0",
        ],
        [
            "EmailSubmission/set",
            {"accountId": "c", "created": {"s0": {"id": "sub-1"}}},
            "s0",
        ],
        [
            "Email/set",
            {
                "accountId": "c",
                "updated": None,
                "notUpdated": {"e-draft-1": {"type": "forbidden", "description": "quota exceeded"}},
            },
            "s0",  # same id as the EmailSubmission/set call above
        ],
    ],
    "sessionState": "s1",
}

#: Same scenario, but the implicit update comes back as a batch-level
#: "error" tuple (RFC 8620 §3.5.1) instead of a normal Email/set response
#: with `notUpdated` — `send` must handle this shape too, the same way:
#: warn, don't raise.
SEND_IMPLICIT_UPDATE_ERROR_RESPONSE = {
    "methodResponses": [
        [
            "Email/set",
            {"accountId": "c", "created": {"d0": {"id": "e-draft-1", "blobId": "b1"}}},
            "d0",
        ],
        [
            "EmailSubmission/set",
            {"accountId": "c", "created": {"s0": {"id": "sub-1"}}},
            "s0",
        ],
        ["error", {"type": "serverFail", "description": "transient failure"}, "s0"],
    ],
    "sessionState": "s1",
}

SEND_OK_RESPONSE = _combined(
    MAILBOXES_FOR_SEND_RESPONSE, IDENTITY_GET_RESPONSE, SEND_CREATE_OK_RESPONSE
)
SEND_SUBMISSION_NOT_CREATED = _combined(
    MAILBOXES_FOR_SEND_RESPONSE, IDENTITY_GET_RESPONSE, SEND_SUBMISSION_NOT_CREATED_RESPONSE
)
SEND_DRAFT_NOT_CREATED = _combined(
    MAILBOXES_FOR_SEND_RESPONSE, IDENTITY_GET_RESPONSE, SEND_DRAFT_NOT_CREATED_RESPONSE
)
SEND_IMPLICIT_UPDATE_NOT_UPDATED = _combined(
    MAILBOXES_FOR_SEND_RESPONSE, IDENTITY_GET_RESPONSE, SEND_IMPLICIT_UPDATE_NOT_UPDATED_RESPONSE
)
# No combined variant of SEND_IMPLICIT_UPDATE_ERROR_RESPONSE: that fixture's
# "error" tuple would also trip up get_mailboxes()/get_identity()'s own
# _call if served from the shared-response trick every other fixture here
# uses (see test_send_logs_warning_when_implicit_update_is_an_error_tuple's
# comment) — that one test builds its responses sequentially instead.


# ---------------------------------------------------------------------------
# JmapClient.send — request shape.
# ---------------------------------------------------------------------------


async def test_send_with_html_batches_email_set_and_submission(client, api_mock):
    api_mock.respond(json=SEND_OK_RESPONSE)

    sub_id = await client.send(
        to=["alice@example.com"],
        subject="Hello",
        text="Hi there",
        html="<p>Hi <b>there</b></p>",
    )

    assert sub_id == "sub-1"
    # Mailbox/get, then Identity/get (uncached), then the 2-method batch —
    # see this module's docstring for why Identity/get isn't folded into
    # that same batch.
    assert len(api_mock.calls) == 3

    mailbox_body = json.loads(api_mock.calls[0].request.content)
    assert mailbox_body["methodCalls"][0][0] == "Mailbox/get"

    identity_body = json.loads(api_mock.calls[1].request.content)
    assert identity_body["methodCalls"][0][0] == "Identity/get"

    batch_body = json.loads(api_mock.calls[2].request.content)
    calls = batch_body["methodCalls"]
    assert [c[0] for c in calls] == ["Email/set", "EmailSubmission/set"]

    draft = calls[0][1]["create"]["d0"]
    assert draft["mailboxIds"] == {"mb-drafts": True}
    assert draft["keywords"] == {"$draft": True, "$seen": True}
    assert draft["from"] == [{"email": "demo@mailosh.test", "name": "Demo User"}]
    assert draft["to"] == [{"email": "alice@example.com"}]
    assert draft["subject"] == "Hello"
    assert draft["bodyStructure"] == {
        "type": "multipart/alternative",
        "subParts": [
            {"partId": "t", "type": "text/plain"},
            {"partId": "h", "type": "text/html"},
        ],
    }
    assert draft["bodyValues"] == {
        "t": {"value": "Hi there"},
        "h": {"value": "<p>Hi <b>there</b></p>"},
    }

    submission_args = calls[1][1]
    assert submission_args["create"]["s0"] == {"emailId": "#d0", "identityId": "id-1"}
    assert submission_args["onSuccessUpdateEmail"] == {
        "#s0": {
            "mailboxIds/mb-drafts": None,
            "mailboxIds/mb-sent": True,
            "keywords/$draft": None,
        }
    }


async def test_send_text_only_single_part_no_html_bodyvalue(client, api_mock):
    api_mock.respond(json=SEND_OK_RESPONSE)

    await client.send(to=["alice@example.com"], subject="Hello", text="Hi there")

    batch_body = json.loads(api_mock.calls[2].request.content)
    draft = batch_body["methodCalls"][0][1]["create"]["d0"]
    assert draft["bodyStructure"] == {"partId": "t", "type": "text/plain"}
    assert draft["bodyValues"] == {"t": {"value": "Hi there"}}
    assert "h" not in draft["bodyValues"]


async def test_send_survives_stalwart_reusing_the_submission_call_id_for_its_implicit_update(
    client, api_mock, caplog
):
    # Regression test for a bug found live against Stalwart (Task 9, see
    # JmapClient._call's docstring): the onSuccessUpdateEmail-triggered
    # implicit Email/set update is appended to methodResponses *after* the
    # explicit EmailSubmission/set response, reusing that same call id
    # ("s0") rather than a distinct one. This fixture reproduces that exact
    # three-entry shape (captured verbatim from the live server) so this
    # test would have caught the bug: before the fix, `out["s0"]` ended up
    # holding the *implicit update's* body (no `created` key at all),
    # making `send()` raise a spurious JmapError even though the send
    # genuinely succeeded.
    api_mock.respond(
        json=_combined(
            MAILBOXES_FOR_SEND_RESPONSE,
            IDENTITY_GET_RESPONSE,
            {
                "methodResponses": [
                    [
                        "Email/set",
                        {
                            "accountId": "c",
                            "created": {
                                "d0": {"id": "e-draft-1", "blobId": "b1", "threadId": "t-new"}
                            },
                        },
                        "d0",
                    ],
                    [
                        "EmailSubmission/set",
                        {
                            "accountId": "c",
                            "created": {
                                "s0": {
                                    "id": "sub-1",
                                    "sendAt": "2026-09-01T07:22:42Z",
                                    "undoStatus": "pending",
                                }
                            },
                        },
                        "s0",
                    ],
                    [
                        "Email/set",
                        {"accountId": "c", "updated": {"e-draft-1": None}},
                        "s0",  # same id as the EmailSubmission/set call above
                    ],
                ],
                "sessionState": "s1",
            },
        )
    )

    with caplog.at_level(logging.WARNING, logger="mailosh.jmap.client"):
        sub_id = await client.send(to=["alice@example.com"], subject="Hi", text="hi")

    assert sub_id == "sub-1"
    # A clean implicit update (real "updated", no notUpdated for our draft)
    # must not log anything — only a rejected/errored one should.
    assert caplog.records == []


async def test_send_logs_warning_when_implicit_update_reports_not_updated(client, api_mock, caplog):
    # Controller-review regression test: the implicit onSuccessUpdateEmail
    # update can itself be rejected (ACL/quota/race) even though the
    # EmailSubmission/set create succeeded — the mail was genuinely
    # submitted, so send() must still return the submission id, but it must
    # also surface the local Drafts->Sent-move failure via logger.warning
    # rather than silently discarding it (what _call's dedup used to do) or
    # raising (which would misreport a successful send as a failure).
    api_mock.respond(json=SEND_IMPLICIT_UPDATE_NOT_UPDATED)

    with caplog.at_level(logging.WARNING, logger="mailosh.jmap.client"):
        sub_id = await client.send(to=["alice@example.com"], subject="Hi", text="hi")

    assert sub_id == "sub-1"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "e-draft-1" in message  # the draft's real (server-assigned) id
    assert "forbidden" in message  # the notUpdated SetError's type
    assert "quota exceeded" in message  # ... and its description


async def test_send_logs_warning_when_implicit_update_is_an_error_tuple(client, api_mock, caplog):
    # Same scenario, but the implicit update comes back as a batch-level
    # "error" tuple (RFC 8620 3.5.1) instead of a normal Email/set response
    # carrying notUpdated — send() must handle this shape the same way.
    #
    # Sequential (not shared/combined) responses here, unlike every other
    # test in this module: `_call` (used by get_mailboxes/get_identity)
    # raises on the *first* "error"-named tuple anywhere in whatever
    # response it's handed, regardless of which call id that tuple actually
    # tags — so the one shared "return everything for every POST" response
    # every other test uses would make even the Mailbox/get lookup trip
    # over this fixture's error tuple before send() ever reaches the batch
    # call that's actually supposed to see it.
    api_mock.side_effect = [
        httpx.Response(200, json=MAILBOXES_FOR_SEND_RESPONSE),
        httpx.Response(200, json=IDENTITY_GET_RESPONSE),
        httpx.Response(200, json=SEND_IMPLICIT_UPDATE_ERROR_RESPONSE),
    ]

    with caplog.at_level(logging.WARNING, logger="mailosh.jmap.client"):
        sub_id = await client.send(to=["alice@example.com"], subject="Hi", text="hi")

    assert sub_id == "sub-1"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "e-draft-1" in message
    assert "serverFail" in message


async def test_send_reuses_cached_identity_skips_identity_get(client, api_mock):
    api_mock.respond(json=SEND_OK_RESPONSE)

    await client.get_identity()  # pre-warms the cache: 1 call
    await client.send(to=["alice@example.com"], subject="Hi", text="hi")

    assert len(api_mock.calls) == 3  # get_identity + Mailbox/get + the 2-method batch
    bodies = [json.loads(c.request.content) for c in api_mock.calls]
    assert [[m[0] for m in b["methodCalls"]] for b in bodies] == [
        ["Identity/get"],
        ["Mailbox/get"],
        ["Email/set", "EmailSubmission/set"],
    ]


# ---------------------------------------------------------------------------
# JmapClient.send / get_identity — error handling.
# ---------------------------------------------------------------------------


async def test_send_submission_not_created_raises(client, api_mock):
    api_mock.respond(json=SEND_SUBMISSION_NOT_CREATED)
    with pytest.raises(JmapError) as exc_info:
        await client.send(to=["alice@example.com"], subject="Hello", text="Hi")
    assert "invalidProperties" in str(exc_info.value)


async def test_send_draft_not_created_raises(client, api_mock):
    # Requirement text: "on notCreated for either object -> JmapError" —
    # this is the other half of that (the submission's own notCreated is
    # never reached since the draft check raises first).
    api_mock.respond(json=SEND_DRAFT_NOT_CREATED)
    with pytest.raises(JmapError) as exc_info:
        await client.send(to=["alice@example.com"], subject="Hello", text="Hi")
    assert "bad bodyStructure" in str(exc_info.value)


async def test_get_identity_caches_after_first_fetch(client, api_mock):
    api_mock.respond(json=IDENTITY_GET_RESPONSE)

    id1 = await client.get_identity()
    id2 = await client.get_identity()

    assert id1 is id2
    assert len(api_mock.calls) == 1
    assert id1.email == "demo@mailosh.test"
    assert id1.id == "id-1"


# ---------------------------------------------------------------------------
# html_to_text: plain-text fallback for a Squire-authored html body.
# ---------------------------------------------------------------------------


def test_html_to_text_strips_tags():
    result = html_to_text("<p>Hello <b>world</b></p>")
    assert "<" not in result
    assert ">" not in result
    assert "Hello world" in result


def test_html_to_text_decodes_entities():
    result = html_to_text("<p>Tom &amp; Jerry &lt;3&gt;</p>")
    assert "Tom & Jerry <3>" in result
    assert "&amp;" not in result
    assert "&lt;" not in result


def test_html_to_text_block_elements_become_line_breaks():
    result = html_to_text("<div>line one</div><div>line two</div>")
    lines = [line for line in result.splitlines() if line.strip()]
    assert lines == ["line one", "line two"]


def test_html_to_text_drops_script_and_style_content():
    result = html_to_text(
        "<style>p{color:red}</style><p>visible</p><script>evil_marker_xyz()</script>"
    )
    assert "visible" in result
    assert "color:red" not in result
    assert "evil_marker_xyz" not in result


def test_html_to_text_empty_input_returns_empty_string():
    assert html_to_text("") == ""


def test_html_to_text_no_tags_passthrough():
    assert html_to_text("just plain text") == "just plain text"
