"""Unit tests for `mailosh.services.conversation.build_conversation` (design
spec §7): the ordering, the expansion rule, the scroll target, the
attachment chips and the two header-derived provenance rows.

Everything here is the pure view-model layer — no app, no HTTP, no
template. `fake_client` is a bare object with the one method the service
calls (`get_thread`), which is the whole surface the service depends on;
`tests/unit/test_thread_routes.py` covers what the route and the templates
then do with the result.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from mailosh.jmap.models import Address, BodyPart, EmailBody
from mailosh.services.conversation import build_conversation, size_display
from mailosh.services.mailbox_tree import LabelNode, NavItem, NavModel
from mailosh.ui.format import avatar_color

ME = "me@x"

_clock = itertools.count()


def msg(
    email_id,
    *,
    seen=True,
    html=None,
    text="body",
    attachments=(),
    return_path=None,
    auth_results=None,
    mailbox_ids=None,
    keywords=None,
    from_=None,
    truncated=False,
    html_truncated=False,
    preview="p",
):
    """One EmailBody, with receivedAt increasing per call so the service's
    oldest-first ordering is actually exercised rather than accidentally
    matching construction order."""
    flags = set(keywords) if keywords is not None else set()
    if seen:
        flags.add("$seen")
    return EmailBody(
        id=email_id,
        thread_id="T1",
        mailbox_ids=set(mailbox_ids) if mailbox_ids is not None else {"mb-inbox"},
        keywords=flags,
        from_=from_ if from_ is not None else [Address(name="Dan", email="d@x.test")],
        to=[Address(email=ME)],
        subject="s",
        received_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=next(_clock)),
        preview=preview,
        has_attachment=bool(attachments),
        text_body=text,
        html_body=html,
        text_truncated=truncated,
        html_truncated=html_truncated,
        attachments=[BodyPart(**a) for a in attachments],
        return_path=return_path,
        auth_results=auth_results,
    )


class FakeClient:
    """`JmapClient`'s one method this service uses. `thread(...)` registers
    what a given thread id fetches back."""

    def __init__(self) -> None:
        self.threads: dict[str, list[EmailBody]] = {}

    def thread(self, thread_id: str, messages: list[EmailBody]) -> None:
        self.threads[thread_id] = messages

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
        return self.threads.get(thread_id, [])


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def nav() -> NavModel:
    """One Inbox item and one "Work" label — enough for the chip walk to
    have both something to find and something to skip."""
    return NavModel(
        system=[
            NavItem(
                key="inbox",
                label="Inbox",
                icon="inbox",
                mailbox_id="mb-inbox",
                count=0,
                active=True,
            )
        ],
        more=[],
        labels=[
            LabelNode(
                mailbox_id="m-work",
                name="Work",
                color="emerald",
                count=0,
                visibility="show",
                children=[],
            )
        ],
        inbox_id="mb-inbox",
    )


async def build(fake_client, nav, thread_id="T1"):
    return await build_conversation(
        fake_client,
        thread_id=thread_id,
        me=ME,
        now=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        label_meta={},
        nav=nav,
    )


# ---------------------------------------------------------------------------
# Ordering and the expansion rule (spec §7)
# ---------------------------------------------------------------------------


async def test_expansion_is_unread_union_last(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=True), msg("E2", seen=False), msg("E3", seen=True)])
    view = await build(fake_client, nav)
    assert [m.id for m in view.messages] == ["E1", "E2", "E3"]  # oldest first
    assert [m.expanded for m in view.messages] == [False, True, True]
    assert view.unread_ids == ["E2"]
    assert view.first_unread_id == "E2"


async def test_messages_are_ordered_by_receipt_not_by_fetch_order(fake_client, nav):
    older, newer = msg("E1"), msg("E2")
    fake_client.thread("T1", [newer, older])
    view = await build(fake_client, nav)
    assert [m.id for m in view.messages] == ["E1", "E2"]
    assert view.email_ids == ["E1", "E2"]


async def test_a_single_message_thread_is_always_expanded(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=True)])
    view = await build(fake_client, nav)
    assert view.messages[0].expanded is True


async def test_an_all_read_thread_expands_only_the_last_and_scrolls_there(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=True), msg("E2", seen=True)])
    view = await build(fake_client, nav)
    assert [m.expanded for m in view.messages] == [False, True]
    assert view.unread_ids == [] and view.first_unread_id == "E2"


async def test_every_unread_message_is_expanded_not_only_the_first(fake_client, nav):
    fake_client.thread("T1", [msg("E1", seen=False), msg("E2", seen=True), msg("E3", seen=False)])
    view = await build(fake_client, nav)
    assert [m.expanded for m in view.messages] == [True, False, True]
    assert view.unread_ids == ["E1", "E3"]
    # The scroll target is the *first* unread one, not the newest.
    assert view.first_unread_id == "E1"


# ---------------------------------------------------------------------------
# Subject, chips, senders
# ---------------------------------------------------------------------------


async def test_subject_comes_from_the_newest_message_and_has_a_placeholder(fake_client, nav):
    first = msg("E1")
    first.subject = "Offsite agenda"
    second = msg("E2")
    second.subject = "Re: Offsite agenda"
    fake_client.thread("T1", [first, second])
    assert (await build(fake_client, nav)).subject == "Re: Offsite agenda"

    bare = msg("E9")
    bare.subject = None
    fake_client.thread("T2", [bare])
    assert (await build(fake_client, nav, "T2")).subject == "(no subject)"


async def test_chips_are_the_conversations_own_labels_in_nav_order(fake_client, nav):
    fake_client.thread(
        "T1",
        [msg("E1", mailbox_ids={"mb-inbox"}), msg("E2", mailbox_ids={"mb-inbox", "m-work"})],
    )
    view = await build(fake_client, nav)
    assert [(chip.mailbox_id, chip.color) for chip in view.chips] == [("m-work", "emerald")]


async def test_a_conversation_with_no_user_labels_has_no_chips(fake_client, nav):
    fake_client.thread("T1", [msg("E1", mailbox_ids={"mb-inbox"})])
    assert (await build(fake_client, nav)).chips == []


async def test_the_viewers_own_messages_are_bylined_me(fake_client, nav):
    fake_client.thread(
        "T1",
        [
            msg("E1", from_=[Address(name="Dan", email="d@x.test")]),
            msg("E2", from_=[Address(name="Manish Sharma", email="ME@X")]),
        ],
    )
    view = await build(fake_client, nav)
    assert [m.from_name for m in view.messages] == ["Dan", "me"]
    # The avatar still seeds off the *address*, not the rewritten byline —
    # otherwise every message you ever sent would share one "m" colour with
    # every correspondent called Maria.
    assert view.messages[1].avatar_color == avatar_color("ME@X")
    assert view.messages[1].initials == "M"


async def test_a_message_with_no_from_header_still_renders_a_byline(fake_client, nav):
    fake_client.thread("T1", [msg("E1", from_=[])])
    view = await build(fake_client, nav)
    assert view.messages[0].from_name == "(unknown sender)"
    assert view.messages[0].from_email == ""
    assert view.messages[0].initials == "E"  # seeded from the message id


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


async def test_a_text_body_is_split_into_visible_and_quoted_lines(fake_client, nav):
    fake_client.thread(
        "T1",
        [msg("E1", text="Sounds good.\n\nOn Mon, Dan wrote:\n> earlier\n> > older")],
    )
    body = (await build(fake_client, nav)).messages[0]
    assert body.has_html is False
    assert [line.depth for line in body.visible_lines] == [0, 0]
    assert [line.depth for line in body.quoted_lines] == [0, 1, 2]


async def test_an_html_body_is_left_to_the_frame_and_produces_no_text_lines(fake_client, nav):
    fake_client.thread("T1", [msg("E1", html="<p>rich</p>", text="rich")])
    body = (await build(fake_client, nav)).messages[0]
    assert body.has_html is True
    assert body.visible_lines == [] and body.quoted_lines == []


async def test_truncation_describes_the_body_actually_shown(fake_client, nav):
    fake_client.thread(
        "T1",
        [
            msg("E1", truncated=True),
            msg("E2", html="<p>x</p>", truncated=True, html_truncated=False),
            msg("E3", html="<p>x</p>", html_truncated=True),
        ],
    )
    assert [m.truncated for m in (await build(fake_client, nav)).messages] == [
        True,
        False,
        True,
    ]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


async def test_cid_parts_are_excluded_from_the_attachment_chips(fake_client, nav):
    fake_client.thread(
        "T1",
        [
            msg(
                "E1",
                attachments=[
                    {
                        "blobId": "B1",
                        "type": "image/png",
                        "cid": "logo@m",
                        "disposition": "inline",
                        "name": "logo.png",
                        "size": 10,
                    },
                    {
                        "blobId": "B2",
                        "type": "application/pdf",
                        "cid": None,
                        "disposition": "attachment",
                        "name": "spec.pdf",
                        "size": 2048,
                    },
                ],
            )
        ],
    )
    view = await build(fake_client, nav)
    assert [a.name for a in view.messages[0].attachments] == ["spec.pdf"]
    assert view.messages[0].attachments[0].size_display == "2 KB"
    assert view.messages[0].attachments[0].preview == "pdf"


async def test_a_part_with_no_blob_is_not_offered_as_a_chip(fake_client, nav):
    fake_client.thread(
        "T1",
        [msg("E1", attachments=[{"blobId": None, "type": "application/zip", "size": 5}])],
    )
    assert (await build(fake_client, nav)).messages[0].attachments == []


async def test_a_nameless_attachment_falls_back_to_its_mime_type(fake_client, nav):
    fake_client.thread(
        "T1",
        [msg("E1", attachments=[{"blobId": "B7", "type": "application/zip", "size": 5}])],
    )
    chip = (await build(fake_client, nav)).messages[0].attachments[0]
    assert chip.name == "application/zip"
    assert chip.preview is None


def test_size_display_steps_from_bytes_to_one_decimal_above_a_megabyte():
    assert size_display(0) == "0 B"
    assert size_display(123) == "123 B"
    assert size_display(1023) == "1023 B"
    assert size_display(2048) == "2 KB"
    assert size_display(1024 * 1024) == "1.0 MB"
    assert size_display(1468006) == "1.4 MB"
    assert size_display(3 * 1024**3) == "3.0 GB"


# ---------------------------------------------------------------------------
# mailed-by / signed-by
# ---------------------------------------------------------------------------


async def test_signed_by_and_mailed_by_come_from_headers_and_are_optional(fake_client, nav):
    fake_client.thread(
        "T1",
        [
            msg(
                "E1",
                return_path="<b@bounce.test>",
                auth_results="mx.test; dkim=pass header.d=news.test",
            )
        ],
    )
    view = await build(fake_client, nav)
    assert view.messages[0].mailed_by == "bounce.test"
    assert view.messages[0].signed_by == "news.test"

    fake_client.thread("T2", [msg("E9")])
    view2 = await build(fake_client, nav, "T2")
    assert view2.messages[0].mailed_by is None and view2.messages[0].signed_by is None


async def test_a_dkim_fail_does_not_produce_a_signed_by_claim(fake_client, nav):
    fake_client.thread("T1", [msg("E1", auth_results="mx.test; dkim=fail header.d=news.test")])
    assert (await build(fake_client, nav)).messages[0].signed_by is None


async def test_a_null_return_path_yields_no_mailed_by_row(fake_client, nav):
    # `<>` is what every bounce carries. There is no domain in it, so there
    # is no row — not an empty one.
    fake_client.thread("T1", [msg("E1", return_path="<>")])
    assert (await build(fake_client, nav)).messages[0].mailed_by is None


async def test_a_pass_in_one_clause_does_not_lend_its_verdict_to_a_later_one(fake_client, nav):
    fake_client.thread(
        "T1", [msg("E1", auth_results="mx.test; spf=pass smtp.mailfrom=a.test; dkim=fail")]
    )
    assert (await build(fake_client, nav)).messages[0].signed_by is None


# ---------------------------------------------------------------------------
# Missing thread
# ---------------------------------------------------------------------------


async def test_missing_thread_returns_none(fake_client, nav):
    fake_client.thread("T9", [])
    assert await build(fake_client, nav, "T9") is None
