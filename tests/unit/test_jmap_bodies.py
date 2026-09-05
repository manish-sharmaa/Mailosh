import json

import pytest
from conftest import THREAD_GET_PLUS_EMAIL_RESPONSE  # the repo's existing import style:

# tests/ is not a package, pytest puts
# tests/ on sys.path via its conftest
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import EmailBody, Session

RAW = {
    "id": "E1",
    "threadId": "T1",
    "mailboxIds": {"mb1": True},
    "keywords": {},
    "from": [{"name": "A", "email": "a@x"}],
    "to": [{"email": "b@y"}],
    "cc": None,
    "bcc": None,
    "replyTo": [{"email": "r@x"}],
    "subject": "s",
    "receivedAt": "2026-09-01T10:00:00Z",
    "sentAt": "2026-09-01T09:59:00Z",
    "blobId": "B0",
    "preview": "p",
    "hasAttachment": True,
    "textBody": [{"partId": "1"}],
    "htmlBody": [{"partId": "2"}],
    "bodyValues": {
        "1": {"value": "hello", "isTruncated": False},
        "2": {"value": "<p>hello</p>", "isTruncated": True},
    },
    "attachments": [
        {
            "partId": "3",
            "blobId": "B3",
            "size": 12,
            "type": "image/png",
            "name": "logo.png",
            "cid": "logo@mail",
            "disposition": "inline",
        },
        {
            "partId": "4",
            "blobId": "B4",
            "size": 99,
            "type": "application/pdf",
            "name": "spec.pdf",
            "cid": None,
            "disposition": "attachment",
        },
    ],
    "header:Return-Path:asText": "<bounce@x>",
    "header:Authentication-Results:asText": "mx.test; dkim=pass header.d=x.test",
}


def test_email_body_resolves_html_and_attachments():
    body = EmailBody.model_validate(RAW)
    assert body.text_body == "hello" and body.text_truncated is False
    assert body.html_body == "<p>hello</p>" and body.html_truncated is True
    assert [a.blob_id for a in body.attachments] == ["B3", "B4"]
    assert body.attachments[0].cid == "logo@mail"
    assert body.attachments[0].disposition == "inline"
    assert body.blob_id == "B0"
    assert [a.email for a in body.reply_to] == ["r@x"]
    assert body.bcc == []
    assert body.return_path == "<bounce@x>"
    assert body.auth_results.startswith("mx.test")


def test_email_body_without_html_is_none_not_error():
    raw = {k: v for k, v in RAW.items() if k not in ("htmlBody", "bodyValues", "attachments")}
    body = EmailBody.model_validate(raw)
    assert body.html_body is None and body.text_body is None and body.attachments == []


def test_html_resolves_even_when_text_body_is_absent():
    # Pins the models.py docstring's stated reason _resolve_text_body/
    # _resolve_html_body are two separate validators, not one merged pass:
    # a single function with one early-return guard keyed on textBody's
    # shape would (by accident, sharing that guard) also skip html
    # resolution whenever textBody is missing/malformed. Dropping textBody
    # entirely here -- while htmlBody/bodyValues stay fully populated --
    # exercises exactly that failure mode.
    raw = {k: v for k, v in RAW.items() if k != "textBody"}
    body = EmailBody.model_validate(raw)
    assert body.text_body is None
    assert body.html_body == "<p>hello</p>" and body.html_truncated is True


def test_session_carries_and_rebases_download_url():
    session = Session.from_jmap(
        {
            "apiUrl": "https://mail.test/jmap",
            "uploadUrl": "https://mail.test/upload/{accountId}",
            "downloadUrl": "https://mail.test/download/{accountId}/{blobId}/{name}?accept={type}",
            "eventSourceUrl": "https://mail.test/events",
            "primaryAccounts": {"urn:ietf:params:jmap:mail": "acct"},
        }
    )
    rebased = session.rebase("http://stalwart:8080")
    assert (
        rebased.download_url
        == "http://stalwart:8080/download/{accountId}/{blobId}/{name}?accept={type}"
    )


async def test_get_thread_requests_html_values_and_headers(client, api_mock):
    # Same shape as tests/unit/test_jmap_mail.py::test_get_thread_single_roundtrip:
    # respond with a canned body, then read the request that was actually sent.
    api_mock.respond(json=THREAD_GET_PLUS_EMAIL_RESPONSE)  # existing conftest fixture
    await client.get_thread("t-1")
    assert len(api_mock.calls) == 1
    body = json.loads(api_mock.calls[0].request.content)
    args = body["methodCalls"][1][1]
    assert args["fetchHTMLBodyValues"] is True and args["fetchTextBodyValues"] is True
    assert args["maxBodyValueBytes"] == 512 * 1024
    for prop in (
        "htmlBody",
        "attachments",
        "blobId",
        "bcc",
        "replyTo",
        "sentAt",
        "header:Return-Path:asText",
        "header:Authentication-Results:asText",
    ):
        assert prop in args["properties"], prop


async def test_blob_url_substitutes_all_four_placeholders(client):
    url = client.blob_url("B3", mime_type="image/png", name="a b.png")
    assert "{accountId}" not in url and "{blobId}" not in url
    assert "{type}" not in url and "{name}" not in url
    assert "a%20b.png" in url or "a+b.png" in url


async def test_fetch_blob_caps_size(client, download_mock):
    download_mock(content=b"x" * 100)
    with pytest.raises(JmapError):
        await client.fetch_blob("B3", mime_type="image/png", name="a.png", max_bytes=10)
    assert (
        await client.fetch_blob("B3", mime_type="image/png", name="a.png", max_bytes=1000)
        == b"x" * 100
    )
