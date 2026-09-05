import json
import pathlib

import pytest
from pydantic import ValidationError

from mailosh.jmap.models import Address, EmailBody, EmailHeader, Mailbox, Session, StateChange


def test_session_parses_fixture():
    raw = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())
    s = Session.from_jmap(raw)
    assert s.api_url.startswith("http")
    assert s.primary_account_id
    assert s.event_source_url


# Realistic RFC 8621-shaped payloads (field names/nesting/example values modeled
# on the RFC's own illustrative examples), used below so parsing is checked
# against something closer to a real server response than a minimal ad hoc dict
# — including extra fields (myRights, isSubscribed, blobId, size, ...) our
# models don't capture, to prove the reduced field set still parses correctly
# out of a full payload.

ADDRESS_RFC = {"name": "Joe Bloggs", "email": "joe@example.com"}

MAILBOX_RFC = {
    "id": "123",
    "name": "Inbox",
    "parentId": None,
    "role": "inbox",
    "sortOrder": 10,
    "totalEmails": 1234,
    "unreadEmails": 123,
    "totalThreads": 1000,
    "unreadThreads": 100,
    "myRights": {
        "mayReadItems": True,
        "mayAddItems": True,
        "mayRemoveItems": True,
        "maySetSeen": True,
        "maySetKeywords": True,
        "mayCreateChild": False,
        "mayRename": False,
        "mayDelete": False,
        "maySubmit": True,
    },
    "isSubscribed": True,
}

EMAIL_HEADER_RFC = {
    "id": "M12345",
    "blobId": "G12345",
    "threadId": "T12345",
    "mailboxIds": {"MB123": True},
    "keywords": {"$seen": True, "$flagged": True},
    "size": 4321,
    "receivedAt": "2014-10-30T14:12:00Z",
    "messageId": ["<1234567890@example.com>"],
    "from": [{"name": "Joe Bloggs", "email": "joe@example.com"}],
    "to": [{"name": "John Smith", "email": "john@example.com"}],
    "cc": [],
    "subject": "Hello world",
    "sentAt": "2014-10-30T06:12:00-08:00",
    "hasAttachment": False,
    "preview": "This is a test email for you",
}

EMAIL_BODY_RFC = {
    **EMAIL_HEADER_RFC,
    "textBody": [
        {
            "partId": "1",
            "blobId": "B1234",
            "size": 30,
            "type": "text/plain",
            "name": None,
            "charset": "us-ascii",
            "disposition": None,
            "cid": None,
        }
    ],
    "htmlBody": [
        {
            "partId": "2",
            "blobId": "B5678",
            "size": 60,
            "type": "text/html",
            "name": None,
            "charset": "us-ascii",
            "disposition": None,
            "cid": None,
        }
    ],
    "bodyValues": {
        "1": {
            "value": "This is a test email for you",
            "isEncodingProblem": False,
            "isTruncated": False,
        },
    },
}

STATE_CHANGE_RFC = {
    "@type": "StateChange",
    "changed": {
        "a3f2-71fb-2b52-8dd4": {"Mailbox": "123", "Thread": "234"},
    },
}


def test_address_from_realistic_payload():
    a = Address.model_validate(ADDRESS_RFC)
    assert a.name == "Joe Bloggs"
    assert a.email == "joe@example.com"


def test_mailbox_from_realistic_payload():
    m = Mailbox.model_validate(MAILBOX_RFC)
    assert m.id == "123"
    assert m.name == "Inbox"
    assert m.parent_id is None
    assert m.role == "inbox"
    assert m.sort_order == 10
    assert m.total_emails == 1234
    assert m.unread_emails == 123


def test_email_header_from_realistic_payload():
    e = EmailHeader.model_validate(EMAIL_HEADER_RFC)
    assert e.id == "M12345"
    assert e.thread_id == "T12345"
    assert e.mailbox_ids == {"MB123"}
    assert e.keywords == {"$seen", "$flagged"}
    assert e.from_ == [Address(name="Joe Bloggs", email="joe@example.com")]
    assert e.subject == "Hello world"
    assert e.preview == "This is a test email for you"
    assert e.has_attachment is False


def test_email_body_from_realistic_payload_resolves_text_body():
    # Finding 2: EmailBody.model_validate() on a raw Email/get-shaped response
    # (wire-format textBody list + bodyValues) must resolve down to a flat string.
    b = EmailBody.model_validate(EMAIL_BODY_RFC)
    assert b.id == "M12345"
    assert b.to == [Address(name="John Smith", email="john@example.com")]
    assert b.cc == []
    assert b.text_body == "This is a test email for you"


def test_email_body_flat_construction_path():
    # The other path later tasks may use: text_body handed over already
    # resolved, with no wire-shaped textBody/bodyValues in sight.
    b = EmailBody(
        id="e1",
        thread_id="t1",
        received_at="2024-01-01T10:00:00Z",
        preview="hi",
        has_attachment=False,
        text_body="already resolved",
    )
    assert b.text_body == "already resolved"


def test_email_body_text_body_without_body_values_resolves_to_none():
    # Re-review gap: fetchTextBodyValues defaults to false per RFC 8621, so a
    # real Email/get response commonly has a populated textBody with no
    # bodyValues key at all. That must resolve to text_body=None, not raise
    # ValidationError from handing the raw EmailBodyPart list to a str field.
    raw = {k: v for k, v in EMAIL_BODY_RFC.items() if k != "bodyValues"}
    assert raw.get("textBody")  # still populated
    assert "bodyValues" not in raw
    b = EmailBody.model_validate(raw)
    assert b.text_body is None


def test_email_body_empty_text_body_without_body_values_resolves_to_none():
    raw = {k: v for k, v in EMAIL_BODY_RFC.items() if k != "bodyValues"}
    raw["textBody"] = []
    b = EmailBody.model_validate(raw)
    assert b.text_body is None


def test_state_change_from_realistic_payload():
    s = StateChange.model_validate(STATE_CHANGE_RFC)
    assert s.changed == {"a3f2-71fb-2b52-8dd4": {"Mailbox": "123", "Thread": "234"}}


def test_mailbox_missing_required_field_raises():
    bad = {k: v for k, v in MAILBOX_RFC.items() if k != "unreadEmails"}
    with pytest.raises(ValidationError):
        Mailbox.model_validate(bad)


def test_a_body_less_message_parses_with_an_empty_preview():
    """Stalwart omits `preview` entirely for a message with no body.

    That is a correct response, not a malformed one — there is nothing to
    preview. It was unreachable until Phase 1C, because nothing created
    drafts; the compose dock now autosaves one seconds after the first
    keystroke, and while `preview` was a required field that omission raised
    `ValidationError` and took `/mail/drafts` and `/compose/{id}` down with a
    500 for as long as the draft existed. Reachable in about four seconds:
    open compose, type an address, wait for autosave, open Drafts.
    """
    header = EmailHeader.model_validate(
        {
            "id": "0aaaaahi",
            "threadId": "T1",
            "mailboxIds": {"drafts": True},
            "keywords": {"$draft": True},
            "from": [],
            "subject": "",
            "receivedAt": "2026-09-05T06:00:00Z",
            "hasAttachment": False,
        }
    )
    assert header.preview == ""


def test_has_attachment_is_still_required():
    """The `preview` default must not be read as "relax the model".

    `hasAttachment` is always sent, so an omission really would be a
    malformed response, and silently reading it as False would hide an
    attachment indicator from the row.
    """
    with pytest.raises(ValidationError):
        EmailHeader.model_validate(
            {
                "id": "e1",
                "threadId": "T1",
                "receivedAt": "2026-09-05T06:00:00Z",
                "preview": "hi",
            }
        )
