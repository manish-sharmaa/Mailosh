"""Phase 1C's compose pipeline, end to end, against the running stack.

`tests/unit/test_compose_service.py` and `test_jmap_send.py` prove the
shapes this code *builds*. Neither can answer the question this module
exists for: does a message built that way survive **Stalwart's own MIME
generator, the SMTP loop, and `Email/get`'s parse back out**? respx will
accept any JSON body at all, so every mocked assertion about
`bodyStructure`, `inReplyTo` or a `cid` is an assertion about what we
believe a JMAP server does. This file is where that belief is checked.

Four things here are only meaningful on the live path:

* the RFC 8621 §4.1.3 threading properties round-trip — a real server
  accepts bracket-less `inReplyTo`/`references` on a create and writes
  `In-Reply-To: <...>` / `References: <...> <...>` into the generated MIME,
  which is the only thing that makes a reply thread in anybody else's mail
  client. The raw message is fetched and the headers read out of it;
* an attachment attaches by `blobId` alone, and an inline part keeps its
  Content-ID through generation *and* re-parse, coming back as an
  `attachments` entry with `disposition: "inline"`;
* a forward re-attaches **another message's** blobs — the parts are never
  downloaded and re-uploaded, they are referenced across messages, and only
  a real server can say whether that is allowed;
* draft autosave's create-then-destroy actually leaves one draft in Drafts,
  not two and not zero.

**Hermetic and self-cleaning**, on the same terms as
`test_live_reading_flow.py`: every run stamps a fresh `uuid4` into the
`Message-ID` and `Subject` of everything it creates, so two runs never
collide in Stalwart's References-based threading, and the `finally`
destroys precisely the ids this run created and then verifies that it did.

Run with `make itest` (only; `pyproject.toml`'s default `addopts`
deselects `integration`), against `make up`/`make dev` plus
`bash scripts/stalwart-init.sh`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient, find_inbox
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import EmailBody
from mailosh.services.compose import (
    FORWARD,
    QUOTE_CLASS,
    REPLY_ALL,
    AttachmentRef,
    DraftInput,
    Recipient,
    build_reply,
    discard_draft,
    save_draft,
    send_draft,
)

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

#: An 8x8 greyscale PNG, 89 bytes — the same real image
#: `test_live_reading_flow.py` uses, for the same reason: an inline part has
#: to be something a browser would actually draw, not bytes with a `.png`
#: name.
INLINE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAA"
    "DklEQVQI12P4AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII="
)

#: How long to wait for Stalwart to loop a submitted message back into the
#: Inbox. Local delivery on the same server is near-instant; this is slack
#: for a loaded machine, not an expected wait.
_DELIVERY_TIMEOUT_SECONDS = 45


def _original(run_id: str, subject: str, cid: str) -> bytes:
    """The message every reply and forward in this run is built from.

    Deliberately awkward on four axes at once, because each is a thing
    `build_reply` has to get right against a *real* parse rather than a
    hand-written `EmailBody`:

    * two `To` recipients and two `Cc`, one of which is the demo account
      itself **spelled in a different case** — the reply-all exclusion is
      case-insensitive, and a mail server preserves whatever case a sender
      typed, so this is how the bug actually presents;
    * a `References` header, so the reply's own chain has something real to
      extend;
    * a hostile HTML body, so the sanitiser has something to strip on the
      way *out*;
    * an inline image and a file attachment, so a forward has two different
      kinds of part to carry.
    """
    msg = EmailMessage()
    msg["From"] = "Dan Okafor <dan@example.test>"
    msg["To"] = "demo@mailosh.test, Tom Reyes <tom@example.test>"
    msg["Cc"] = "Aisha Rahman <aisha@example.test>, DEMO@Mailosh.TEST"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<compose-{run_id}-orig@example.test>"
    msg["References"] = f"<compose-{run_id}-root@example.test>"
    msg["Date"] = "Tue, 1 Sep 2026 20:41:00 +0000"
    msg.set_content("COMPOSE-ORIGINAL-TEXT: Thursday works for me.\n")
    msg.add_alternative(
        "<html><body>"
        "<p>COMPOSE-ORIGINAL-HTML: Thursday works for me.</p>"
        f'<img src="cid:{cid}" width="8" height="8" alt="Mark">'
        "<script>alert('COMPOSE-PAYLOAD-SCRIPT')</script>"
        '<img src="x" onerror="alert(\'COMPOSE-PAYLOAD-ONERROR\')">'
        "<a href=\"javascript:alert('COMPOSE-PAYLOAD-HREF')\">link</a>"
        "</body></html>",
        subtype="html",
    )
    html_part = msg.get_payload()[1]
    html_part.add_related(
        INLINE_PNG,
        maintype="image",
        subtype="png",
        cid=f"<{cid}>",
        filename="mark.png",
        disposition="inline",
    )
    msg.add_attachment(
        b"COMPOSE-ATTACHED-BYTES\n",
        maintype="text",
        subtype="plain",
        filename="notes.txt",
    )
    return bytes(msg)


async def _destroy_emails(client: JmapClient, ids: list[str]) -> None:
    """Best-effort `Email/set destroy` for exactly the ids this run created.

    `client._call` directly, the same as the other live modules' cleanup
    helpers: this test destroys mail as *cleanup*, and `destroy_emails` —
    which this module is partly here to test — would make a cleanup failure
    look like a product failure.
    """
    if not ids:
        return
    try:
        await client._call(
            [("Email/set", {"accountId": client.account_id, "destroy": list(ids)}, "d0")]
        )
    except JmapError:
        _log.warning("cleanup: Email/set destroy failed for %r", ids, exc_info=True)


async def _fetch(client: JmapClient, email_id: str, properties: list[str]) -> dict | None:
    """One `Email/get`, returning `None` when the server does not have it.

    Used both to assert on what was created and to assert that a superseded
    draft is *gone* — the negative is the interesting one, and it needs a
    call that distinguishes "not found" from "found and empty".
    """
    out = await client._call(
        [
            (
                "Email/get",
                {"accountId": client.account_id, "ids": [email_id], "properties": properties},
                "g0",
            )
        ]
    )
    listing = out["g0"].get("list") or []
    return listing[0] if listing else None


async def _await_delivery(client: JmapClient, mailbox_id: str, subject: str) -> str:
    """The id of the message with `subject` once it lands in `mailbox_id`.

    Polls rather than sleeping a fixed interval: local delivery is normally
    done before the first query, and a fixed sleep would be both slower and
    flakier than asking.
    """
    deadline = asyncio.get_running_loop().time() + _DELIVERY_TIMEOUT_SECONDS
    while True:
        for row in await client.query_inbox(mailbox_id, limit=20):
            if row.subject == subject:
                return row.id
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"{subject!r} was submitted but never delivered to the inbox "
                f"within {_DELIVERY_TIMEOUT_SECONDS}s"
            )
        await asyncio.sleep(1.0)


def _header(raw: bytes, name: str) -> str:
    """One header of a raw RFC 5322 message, unfolded onto a single line.

    Parsed rather than string-matched, and unfolded rather than compared
    verbatim: Stalwart folds a long `References` across continuation lines
    (RFC 5322 §2.2.3), which is correct and which a naive `in` check on the
    raw bytes fails on — as this test did on its first live run. Collapsing
    all whitespace to single spaces is exactly the unfolding an RFC 5322
    parser does, and it is what the receiving client will see.
    """
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    value = parsed[name]
    return " ".join(str(value).split()) if value is not None else ""


async def _sweep(client: JmapClient, run_id: str) -> int:
    """Destroy anything left in the account whose subject names this run,
    and return how many remain afterwards.

    `created` covers everything the *successful* path makes. A run that
    fails partway leaves whatever came after the failure — most obviously
    the copy the SMTP loop delivers a second after the assertion that
    stopped the test — so cleanup asks the server what is actually there
    rather than trusting a list built by code that just stopped running.
    """
    found = await client._call(
        [
            (
                "Email/query",
                {
                    "accountId": client.account_id,
                    "filter": {"subject": f"Mailosh compose {run_id}"},
                    "limit": 50,
                },
                "q0",
            )
        ]
    )
    ids = found["q0"].get("ids") or []
    await _destroy_emails(client, list(ids))
    remaining = await client._call(
        [
            (
                "Email/query",
                {
                    "accountId": client.account_id,
                    "filter": {"subject": f"Mailosh compose {run_id}"},
                    "calculateTotal": True,
                    "limit": 0,
                },
                "q0",
            )
        ]
    )
    return remaining["q0"]["total"]


async def test_composing_sending_and_saving_against_the_real_server():
    """One test, one `finally`.

    Three tests would be three chances to leave mail behind in a shared
    account, which is the failure `make itest` has already been broken by
    once — and they would share an expensive setup (a connection, an
    import, two uploads) for no benefit.
    """
    settings = Settings()
    if settings.demo_user is None or settings.demo_password is None:
        pytest.skip("MAILOSH_DEMO_USER/MAILOSH_DEMO_PASSWORD not set")

    run_id = uuid.uuid4().hex[:8]
    me = settings.demo_user
    original_subject = f"Mailosh compose {run_id} original"
    inline_cid = f"compose.{run_id}@mailosh.test"

    jmap = await JmapClient.connect(
        settings.stalwart_url, settings.demo_user, settings.demo_password
    )
    created: list[str] = []
    try:
        inbox = find_inbox(await jmap.get_mailboxes())
        blob = await jmap.upload(_original(run_id, original_subject, inline_cid), "message/rfc822")
        original_id = await jmap.import_email(blob, {inbox.id}, set(), datetime.now(UTC))
        created.append(original_id)

        # --- what build_reply reads, as a real server hands it over ------
        # Asserted before compose is involved at all: if `messageId` or
        # `references` do not survive an import, every threading assertion
        # below would fail for a reason that has nothing to do with 1C.
        thread_id = (await jmap.get_email_states([original_id]))[0].thread_id
        thread: list[EmailBody] = await jmap.get_thread(thread_id)
        original = next(message for message in thread if message.id == original_id)
        assert original.message_id == [f"compose-{run_id}-orig@example.test"], (
            f"Stalwart spelled messageId {original.message_id!r}"
        )
        assert original.references == [f"compose-{run_id}-root@example.test"]
        assert original.html_body and "COMPOSE-ORIGINAL-HTML" in original.html_body
        assert {part.name for part in original.attachments} == {"mark.png", "notes.txt"}

        # --- reply-all, built from that real message ---------------------
        reply = build_reply(thread, original_id, REPLY_ALL, me)
        assert [r.email for r in reply.to] == ["dan@example.test", "tom@example.test"]
        assert [r.email for r in reply.cc] == ["aisha@example.test"], (
            "the demo account's own address, in the case the sender typed it, "
            f"survived into Cc: {reply.cc!r}"
        )
        assert reply.in_reply_to == f"compose-{run_id}-orig@example.test"
        assert reply.references == (
            f"compose-{run_id}-root@example.test",
            f"compose-{run_id}-orig@example.test",
        )
        assert reply.subject == f"Re: {original_subject}"
        # The quote came out of a message a stranger wrote, through a real
        # parse, and is about to be mailed to somebody else.
        assert "COMPOSE-ORIGINAL-HTML" in reply.html
        assert QUOTE_CLASS in reply.html
        for payload in ("<script", "onerror", "javascript:", "COMPOSE-PAYLOAD"):
            assert payload not in reply.html, payload
        assert reply.attachments == (), "a reply must not carry the original's attachments"

        # --- send it ------------------------------------------------------
        # Addressed to the demo account so the message actually completes
        # the SMTP loop and can be read back as *delivered* mail, not only
        # as the copy we filed in Sent. Cc keeps the address reply-all
        # chose, so the header under test is one compose produced.
        attachment_blob = await jmap.upload(b"COMPOSE-SENT-ATTACHMENT\n", "text/plain")
        sent_subject = f"Mailosh compose {run_id} reply"
        outgoing = replace(
            reply,
            to=(Recipient(email=me, name="Demo"),),
            subject=sent_subject,
            # Re-spelled **with** angle brackets, which `build_reply` never
            # produces but a hidden form field round-tripping through a
            # browser very well might. RFC 8621 §4.1.3 wants the bare form;
            # Stalwart rejects nothing here, it just writes the brackets it
            # is given *into* the header, producing `<<id>>` and a reply
            # that threads nowhere. The header assertions below are what
            # catch that, so they have to be fed the awkward spelling.
            in_reply_to=f"<compose-{run_id}-orig@example.test>",
            references=(
                f"<compose-{run_id}-root@example.test>",
                f"<compose-{run_id}-orig@example.test>",
            ),
            attachments=(
                AttachmentRef(
                    blob_id=attachment_blob, name="notes.txt", type="text/plain", size=24
                ),
            ),
        )
        result = await send_draft(jmap, outgoing)
        created.append(result.email_id)
        assert result.submission_id and result.email_id

        sent_copy = await _fetch(
            jmap,
            result.email_id,
            ["id", "to", "cc", "inReplyTo", "references", "keywords", "attachments", "blobId"],
        )
        assert sent_copy is not None, "the submitted message is not in the account"
        assert [a["email"] for a in sent_copy["to"]] == [me]
        assert [a["email"] for a in sent_copy["cc"]] == ["aisha@example.test"]
        assert sent_copy["inReplyTo"] == [f"compose-{run_id}-orig@example.test"]
        assert sent_copy["references"] == [
            f"compose-{run_id}-root@example.test",
            f"compose-{run_id}-orig@example.test",
        ]
        # onSuccessUpdateEmail cleared $draft on the way to Sent.
        assert "$draft" not in (sent_copy["keywords"] or {})
        assert [part["name"] for part in sent_copy["attachments"]] == ["notes.txt"]

        # The generated MIME itself, which is what every other mail client
        # will thread on. A JMAP property round-tripping proves the server
        # stored our value; only the header proves it wrote one.
        raw = await jmap.fetch_blob(
            sent_copy["blobId"], mime_type="message/rfc822", name="sent.eml", max_bytes=1_000_000
        )
        assert _header(raw, "In-Reply-To") == f"<compose-{run_id}-orig@example.test>"
        assert _header(raw, "References") == (
            f"<compose-{run_id}-root@example.test> <compose-{run_id}-orig@example.test>"
        )
        assert _header(raw, "Cc") == "Aisha Rahman <aisha@example.test>"

        # --- and through the SMTP loop, as delivered mail ------------------
        delivered_id = await _await_delivery(jmap, inbox.id, sent_subject)
        created.append(delivered_id)
        delivered = await _fetch(
            jmap, delivered_id, ["id", "inReplyTo", "references", "cc", "attachments"]
        )
        assert delivered is not None
        assert delivered["inReplyTo"] == [f"compose-{run_id}-orig@example.test"]
        assert [part["name"] for part in delivered["attachments"]] == ["notes.txt"]

        # --- autosave: create first, destroy second ------------------------
        drafting = DraftInput(
            to=(Recipient(email="dan@example.test"),),
            subject=f"Mailosh compose {run_id} draft",
            text="first revision",
        )
        first_id = await save_draft(jmap, drafting)
        created.append(first_id)
        first = await _fetch(jmap, first_id, ["id", "keywords", "mailboxIds"])
        assert first is not None and first["keywords"].get("$draft") is True

        second_id = await save_draft(
            jmap, replace(drafting, text="second revision", draft_id=first_id)
        )
        created.append(second_id)
        assert second_id != first_id, "an immutable JMAP body cannot be edited in place"
        assert await _fetch(jmap, first_id, ["id"]) is None, (
            "the superseded revision is still in Drafts"
        )
        assert await _fetch(jmap, second_id, ["id"]) is not None

        # --- forward: another message's blobs, re-attached ------------------
        forward = build_reply(thread, original_id, FORWARD, me)
        assert {a.name for a in forward.attachments} == {"mark.png", "notes.txt"}
        forward_id = await save_draft(
            jmap,
            replace(
                forward,
                to=(Recipient(email="dan@example.test"),),
                subject=f"Mailosh compose {run_id} forward",
            ),
        )
        created.append(forward_id)
        forwarded = await _fetch(jmap, forward_id, ["id", "attachments", "hasAttachment"])
        assert forwarded is not None
        parts = {part["name"]: part for part in forwarded["attachments"]}
        assert set(parts) == {"mark.png", "notes.txt"}
        # The inline part is still inline and still carries its Content-ID
        # after being referenced by blob into a *different* message.
        assert parts["mark.png"]["disposition"] == "inline"
        assert parts["mark.png"]["cid"].strip("<>") == inline_cid
        assert parts["notes.txt"]["disposition"] == "attachment"
        assert parts["mark.png"]["size"] == len(INLINE_PNG)

        # --- discard --------------------------------------------------------
        await discard_draft(jmap, second_id)
        assert await _fetch(jmap, second_id, ["id"]) is None
    finally:
        try:
            await _destroy_emails(jmap, created)
            # Self-verify the cleanup: a run that leaves its own fixtures
            # live is silent unless something looks, and this account is
            # shared with every other live test.
            leftover = await _sweep(jmap, run_id)
            assert leftover == 0, f"cleanup left {leftover} message(s) of run {run_id} live"
        finally:
            await jmap.close()
