"""Phase 1C's additions to `JmapClient`: `create_draft`, `destroy_emails`,
`send_message` and `get_identities` — the wire shapes compose is built on.

`test_submission.py` owns the Phase 0 `send()` path and its hard-won
`onSuccessUpdateEmail` findings; nothing here re-tests those. What this
module pins is what 1C added on top: cc/bcc, `bodyStructure` nesting for
attachments, the RFC 8621 §4.1.3 threading properties, and the draft
create/destroy pair autosave is built from.

Every request assertion below was cross-checked against the real Stalwart
in `tests/integration/test_live_compose_flow.py` — respx will happily
accept a body a real JMAP server rejects, so the shapes here are the ones a
live send actually produced, not ones invented to make a mock pass.
"""

from __future__ import annotations

import json

import pytest

from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Address, BodyPart

# ---------------------------------------------------------------------------
# Response fixtures.
# ---------------------------------------------------------------------------

#: `Mailbox/get` with the two roles the compose paths resolve.
MAILBOXES = {
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

#: `Identity/get` with two identities — the >1 case the From picker exists
#: for, and the only shape that can tell "returns the whole list" apart
#: from "returns the default in a list".
IDENTITIES = {
    "methodResponses": [
        [
            "Identity/get",
            {
                "accountId": "c",
                "state": "abc",
                "list": [
                    {"id": "id-1", "name": "Demo User", "email": "demo@mailosh.test"},
                    {"id": "id-2", "name": "Demo Alias", "email": "alias@mailosh.test"},
                ],
                "notFound": [],
            },
            "i0",
        ]
    ],
    "sessionState": "s1",
}

#: `Email/set` create succeeding, under the `"d0"` id both `create_draft`
#: and `send_message`'s draft half use.
DRAFT_CREATED = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "created": {"d0": {"id": "e-draft-1", "blobId": "b1", "threadId": "t1"}},
                "notCreated": None,
            },
            "d0",
        ]
    ],
    "sessionState": "s1",
}

DRAFT_NOT_CREATED = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "created": None,
                "notCreated": {"d0": {"type": "invalidProperties", "description": "bad blobId"}},
            },
            "d0",
        ]
    ],
    "sessionState": "s1",
}

DESTROYED = {
    "methodResponses": [
        [
            "Email/set",
            {"accountId": "c", "destroyed": ["e-old"], "notDestroyed": None},
            "d0",
        ]
    ],
    "sessionState": "s1",
}

NOT_DESTROYED = {
    "methodResponses": [
        [
            "Email/set",
            {
                "accountId": "c",
                "destroyed": [],
                "notDestroyed": {"e-old": {"type": "notFound", "description": "gone"}},
            },
            "d0",
        ]
    ],
    "sessionState": "s1",
}

SUBMITTED = {
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
        ["Email/set", {"accountId": "c", "updated": {"e-draft-1": None}}, "s0"],
    ],
    "sessionState": "s1",
}


def combined(*responses: dict) -> dict:
    """Union of several fixtures, served for every POST.

    Same trick `test_submission.py` documents: each `_call` only reads the
    call ids its own caller asked for, so handing every request the union
    of everything it might need lets a multi-round-trip method run against
    one canned response.
    """
    return {
        "methodResponses": [entry for r in responses for entry in r["methodResponses"]],
        "sessionState": "s1",
    }


def bodies(api_mock) -> list[dict]:
    return [json.loads(call.request.content) for call in api_mock.calls]


DEMO = Address(email="demo@mailosh.test", name="Demo User")


# ---------------------------------------------------------------------------
# get_identities
# ---------------------------------------------------------------------------


async def test_get_identities_returns_every_identity(client, api_mock):
    api_mock.respond(json=IDENTITIES)

    identities = await client.get_identities()

    assert [i.id for i in identities] == ["id-1", "id-2"]
    assert identities[1].email == "alias@mailosh.test"


async def test_get_identities_warms_the_default_identity_cache(client, api_mock):
    # It has just paid for the fetch `get_identity` would make, so the two
    # must not disagree about the default — and must not spend a second
    # round trip agreeing.
    api_mock.respond(json=IDENTITIES)

    await client.get_identities()
    default = await client.get_identity()

    assert default.id == "id-1"
    assert len(api_mock.calls) == 1


async def test_get_identities_raises_when_the_account_has_none(client, api_mock):
    api_mock.respond(
        json={
            "methodResponses": [["Identity/get", {"accountId": "c", "list": []}, "i0"]],
            "sessionState": "s1",
        }
    )
    with pytest.raises(JmapError):
        await client.get_identities()


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------


async def test_create_draft_files_a_draft_in_drafts(client, api_mock):
    api_mock.respond(json=combined(MAILBOXES, DRAFT_CREATED))

    draft_id = await client.create_draft(
        sender=DEMO,
        to=[Address(email="alice@example.com")],
        subject="Hello",
        text="Hi",
        html="<p>Hi</p>",
    )

    assert draft_id == "e-draft-1"
    calls = bodies(api_mock)
    assert [c["methodCalls"][0][0] for c in calls] == ["Mailbox/get", "Email/set"]
    draft = calls[1]["methodCalls"][0][1]["create"]["d0"]
    assert draft["mailboxIds"] == {"mb-drafts": True}
    assert draft["keywords"] == {"$draft": True, "$seen": True}
    assert draft["from"] == [{"email": "demo@mailosh.test", "name": "Demo User"}]
    assert draft["subject"] == "Hello"


async def test_create_draft_accepts_a_draft_with_no_recipients_yet(client, api_mock):
    # A dock two seconds into a new message: a body and an empty To. The
    # send path refuses this; autosave must not.
    api_mock.respond(json=combined(MAILBOXES, DRAFT_CREATED))

    await client.create_draft(sender=DEMO, text="notes to myself")

    draft = bodies(api_mock)[1]["methodCalls"][0][1]["create"]["d0"]
    assert draft["to"] == []


async def test_create_draft_raises_when_the_server_rejects_the_create(client, api_mock):
    api_mock.respond(json=combined(MAILBOXES, DRAFT_NOT_CREATED))
    with pytest.raises(JmapError) as exc_info:
        await client.create_draft(sender=DEMO, to=[Address(email="a@example.com")])
    assert "bad blobId" in str(exc_info.value)


# ---------------------------------------------------------------------------
# destroy_emails
# ---------------------------------------------------------------------------


async def test_destroy_emails_sends_one_destroy(client, api_mock):
    api_mock.respond(json=DESTROYED)

    await client.destroy_emails(["e-old"])

    call = bodies(api_mock)[0]["methodCalls"][0]
    assert call[0] == "Email/set"
    assert call[1]["destroy"] == ["e-old"]


async def test_destroy_emails_makes_no_request_for_an_empty_list(client, api_mock):
    await client.destroy_emails([])
    assert api_mock.calls == []


async def test_destroy_emails_raises_on_not_destroyed(client, api_mock):
    api_mock.respond(json=NOT_DESTROYED)
    with pytest.raises(JmapError) as exc_info:
        await client.destroy_emails(["e-old"])
    assert "notFound" in str(exc_info.value)


# ---------------------------------------------------------------------------
# send_message: recipients and threading headers
# ---------------------------------------------------------------------------


async def test_send_message_carries_cc_bcc_and_threading_headers(client, api_mock):
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    submission_id, email_id = await client.send_message(
        to=[Address(email="alice@example.com", name="Alice")],
        cc=[Address(email="cc@example.com")],
        bcc=[Address(email="bcc@example.com")],
        subject="Re: Offsite",
        text="Thursday works",
        in_reply_to=["<orig@example.com>"],
        references=["root@example.com", "<orig@example.com>"],
    )

    assert (submission_id, email_id) == ("sub-1", "e-draft-1")
    draft = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]
    assert draft["to"] == [{"email": "alice@example.com", "name": "Alice"}]
    assert draft["cc"] == [{"email": "cc@example.com"}]
    assert draft["bcc"] == [{"email": "bcc@example.com"}]
    # RFC 8621 §4.1.3's asMessageIds form: no angle brackets on the wire,
    # whichever spelling the caller had. The server puts them back.
    assert draft["inReplyTo"] == ["orig@example.com"]
    assert draft["references"] == ["root@example.com", "orig@example.com"]


async def test_send_message_omits_empty_optional_properties(client, api_mock):
    # An ordinary message has no Cc, no Bcc and no threading headers. An
    # empty array is not the same statement as an absent property, and the
    # Phase 0 wire body had none of these keys at all.
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    await client.send_message(to=[Address(email="alice@example.com")], text="hi")

    draft = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]
    assert "cc" not in draft
    assert "bcc" not in draft
    assert "inReplyTo" not in draft
    assert "references" not in draft


async def test_send_message_with_an_explicit_identity_skips_identity_get(client, api_mock):
    # Compose resolves the From picker's identity itself; making the client
    # re-resolve it would be a round trip *and* a chance to disagree.
    from mailosh.jmap.models import Identity

    api_mock.respond(json=combined(MAILBOXES, SUBMITTED))

    await client.send_message(
        to=[Address(email="alice@example.com")],
        text="hi",
        identity=Identity(id="id-2", email="alias@mailosh.test", name="Demo Alias"),
    )

    assert [c["methodCalls"][0][0] for c in bodies(api_mock)] == ["Mailbox/get", "Email/set"]
    batch = bodies(api_mock)[-1]["methodCalls"]
    assert batch[0][1]["create"]["d0"]["from"] == [
        {"email": "alias@mailosh.test", "name": "Demo Alias"}
    ]
    assert batch[1][1]["create"]["s0"]["identityId"] == "id-2"


# ---------------------------------------------------------------------------
# send_message: bodyStructure for attachments
# ---------------------------------------------------------------------------


async def test_attachments_nest_under_multipart_mixed(client, api_mock):
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    await client.send_message(
        to=[Address(email="alice@example.com")],
        text="see attached",
        html="<p>see attached</p>",
        attachments=[BodyPart(blob_id="B1", name="report.pdf", type="application/pdf", size=1234)],
    )

    draft = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]
    structure = draft["bodyStructure"]
    assert structure["type"] == "multipart/mixed"
    body, attachment = structure["subParts"]
    assert body["type"] == "multipart/alternative"
    assert [p["partId"] for p in body["subParts"]] == ["t", "h"]
    # A pre-uploaded blob is referenced by blobId and has no bodyValues
    # entry — the opposite of the authored partId parts beside it.
    assert attachment == {
        "blobId": "B1",
        "type": "application/pdf",
        "name": "report.pdf",
        "disposition": "attachment",
    }
    assert set(draft["bodyValues"]) == {"t", "h"}


async def test_attachment_size_is_never_sent(client, api_mock):
    # RFC 8621 §4.1.4 makes `size` server-set, and it is the size *after*
    # content-transfer decoding — a number this client cannot compute
    # authoritatively from the bytes it uploaded. Stalwart tolerates a
    # client-supplied one (checked live); we still do not send it.
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    await client.send_message(
        to=[Address(email="alice@example.com")],
        text="x",
        attachments=[BodyPart(blob_id="B1", name="a.pdf", type="application/pdf", size=999)],
    )

    draft = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]
    assert "999" not in json.dumps(draft)
    assert "size" not in draft["bodyStructure"]["subParts"][1]


async def test_inline_images_nest_under_multipart_related_with_a_bare_cid(client, api_mock):
    # multipart/related, not mixed: it is what tells a reading client these
    # parts belong *to* the body rather than being files sent alongside it,
    # and it is the difference between an inline image drawn in place and
    # one listed as a download.
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    await client.send_message(
        to=[Address(email="alice@example.com")],
        text="logo",
        html='<p><img src="cid:logo@mailosh.test"></p>',
        attachments=[
            BodyPart(
                blob_id="B2",
                name="logo.png",
                type="image/png",
                size=89,
                cid="<logo@mailosh.test>",
            )
        ],
    )

    structure = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]["bodyStructure"]
    assert structure["type"] == "multipart/related"
    body, inline = structure["subParts"]
    assert body["type"] == "multipart/alternative"
    assert inline == {
        "blobId": "B2",
        "type": "image/png",
        "name": "logo.png",
        # Angle brackets off: the `cid` property is the bare id, and it is
        # the bare id a `cid:` URL in the body names.
        "cid": "logo@mailosh.test",
        "disposition": "inline",
    }


async def test_inline_and_file_attachments_nest_related_inside_mixed(client, api_mock):
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    await client.send_message(
        to=[Address(email="alice@example.com")],
        text="x",
        html="<p>x</p>",
        attachments=[
            BodyPart(blob_id="B1", name="report.pdf", type="application/pdf", size=1),
            BodyPart(blob_id="B2", name="logo.png", type="image/png", size=2, cid="logo@x.test"),
        ],
    )

    structure = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]["bodyStructure"]
    assert structure["type"] == "multipart/mixed"
    related, pdf = structure["subParts"]
    assert related["type"] == "multipart/related"
    assert related["subParts"][0]["type"] == "multipart/alternative"
    assert related["subParts"][1]["cid"] == "logo@x.test"
    assert pdf["disposition"] == "attachment"


async def test_a_plain_text_message_is_still_one_flat_part(client, api_mock):
    # The Phase 0 shape, unchanged: no attachments and no html means no
    # multipart wrapper of any kind, not a one-child multipart/mixed.
    api_mock.respond(json=combined(MAILBOXES, IDENTITIES, SUBMITTED))

    await client.send_message(to=[Address(email="alice@example.com")], text="hi")

    draft = bodies(api_mock)[-1]["methodCalls"][0][1]["create"]["d0"]
    assert draft["bodyStructure"] == {"partId": "t", "type": "text/plain"}
    assert draft["bodyValues"] == {"t": {"value": "hi"}}
