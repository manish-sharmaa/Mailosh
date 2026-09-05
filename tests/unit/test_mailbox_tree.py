"""Unit tests for `mailosh.services.mailbox_tree`: `build_nav` (the left
nav's view model -- spec §5.2's system items, "More" items, and the label
tree) and `resolve_mailbox` (nav key -> JMAP mailbox id).

`_FakeClient` stands in for `JmapClient` here rather than a respx-mocked
real one: `build_nav` only ever calls `client.get_mailboxes()`, so a tiny
duck-typed double is simpler than standing up an HTTP mock for a single
method call, and (unlike the Phase-0 `FakeClient` these fixtures' own
docstrings still mention -- deleted in Task 5, see test_web_thread.py's
docstring) this one is local to this file since nothing else needs it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from mailosh.db.models import LabelMeta, Visibility
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Mailbox
from mailosh.services.mailbox_tree import (
    build_nav,
    ensure_role_mailbox,
    hidden_in_nav,
    resolve_mailbox,
)


class _FakeClient:
    """`JmapClient` for the two methods this module calls: `get_mailboxes`
    and (only from `ensure_role_mailbox`) `create_mailbox`.

    The create models a *compliant* server, which is the case that matters:
    RFC 8621 §2 makes a role unique within an account, so a second create
    for a role already in use is rejected. Both methods yield to the event
    loop (`sleep(0)`) so `asyncio.gather` genuinely interleaves them —
    without that a "concurrent" test would just run one coroutine to
    completion before starting the other and prove nothing.
    """

    def __init__(self, mailboxes: list[Mailbox], *, account: str = "acct-nav") -> None:
        self._mailboxes = list(mailboxes)
        self.account_id = account
        self.creates: list[tuple[str, str | None]] = []

    async def get_mailboxes(self) -> list[Mailbox]:
        await asyncio.sleep(0)
        return list(self._mailboxes)

    async def create_mailbox(self, name: str, *, role: str | None = None) -> str:
        self.creates.append((name, role))
        await asyncio.sleep(0)
        if role is not None and any(m.role == role for m in self._mailboxes):
            raise JmapError(f"Mailbox/set create failed: {{'type': 'invalidProperties'}} {role!r}")
        created = _mailbox(f"mb-made-{len(self._mailboxes)}", name, role=role)
        self._mailboxes.append(created)
        return created.id


def _mailbox(
    id: str,
    name: str,
    *,
    role: str | None = None,
    parent_id: str | None = None,
    sort_order: int = 0,
    total: int = 0,
    unread: int = 0,
) -> Mailbox:
    return Mailbox(
        id=id,
        name=name,
        parent_id=parent_id,
        role=role,
        sort_order=sort_order,
        total_emails=total,
        unread_emails=unread,
    )


#: A full, realistic account: the six role mailboxes the brief names
#: (inbox/sent/drafts/trash/junk/archive) plus a label tree (Work with a
#: nested Design child, a flat Receipts, and three labels exercising each
#: `visibility` value: "Newsletters Loud" (show_if_unread, has unread ->
#: visible), "Newsletters Quiet" (show_if_unread, zero unread -> hidden),
#: "Secret" (hide -> hidden regardless of its own unread count).
_MAILBOXES = [
    _mailbox("mb-inbox", "Inbox", role="inbox", sort_order=10, total=48, unread=12),
    _mailbox("mb-sent", "Sent", role="sent", sort_order=20, total=210, unread=0),
    _mailbox("mb-drafts", "Drafts", role="drafts", sort_order=30, total=2, unread=0),
    _mailbox("mb-trash", "Trash", role="trash", sort_order=40, total=5, unread=0),
    _mailbox("mb-junk", "Junk", role="junk", sort_order=50, total=31, unread=0),
    _mailbox("mb-archive", "Archive", role="archive", sort_order=60, total=412, unread=0),
    _mailbox("m-work", "Work", sort_order=100, total=27, unread=3),
    _mailbox("m-work-design", "Design", parent_id="m-work", sort_order=110, total=9, unread=0),
    _mailbox("m-receipts", "Receipts", sort_order=120, total=64, unread=0),
    _mailbox("m-news-loud", "Newsletters Loud", sort_order=130, total=8, unread=4),
    _mailbox("m-news-quiet", "Newsletters Quiet", sort_order=140, total=5, unread=0),
    _mailbox("m-secret", "Secret", sort_order=150, total=3, unread=1),
]

_LABEL_META = {
    "m-work": LabelMeta(color="indigo", visibility="show"),
    "m-news-loud": LabelMeta(color="amber", visibility="show_if_unread"),
    "m-news-quiet": LabelMeta(color="amber", visibility="show_if_unread"),
    "m-secret": LabelMeta(color="rose", visibility="hide"),
}


@pytest.fixture
def fake_client_with_mailboxes() -> _FakeClient:
    """`_MAILBOXES` behind a `get_mailboxes()`-only fake client -- the
    common case every test below except the three that deliberately vary
    the mailbox list (a missing role, an orphaned label) needs.
    """
    return _FakeClient(_MAILBOXES)


# ---------------------------------------------------------------------------
# build_nav / resolve_mailbox -- the brief's own assertions, verbatim.
# ---------------------------------------------------------------------------


async def test_nav_roles_counts_and_label_tree(fake_client_with_mailboxes):
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta=_LABEL_META)
    assert [i.key for i in nav.system] == ["inbox", "starred", "sent", "drafts"]  # no "snoozed"
    assert [i.key for i in nav.more] == ["all", "archive", "spam", "trash"]
    assert nav.system[0].count == 12 and nav.system[0].active
    work = next(label for label in nav.labels if label.name == "Work")
    assert work.color == "indigo" and work.children[0].name == "Design"
    assert resolve_mailbox(nav, "inbox") == nav.inbox_id
    assert resolve_mailbox(nav, "m-work") == "m-work"
    assert resolve_mailbox(nav, "starred") is None


# ---------------------------------------------------------------------------
# Additional coverage (self-review): every system/more nav item's identity
# and count semantics, label visibility rules, alphabetical + nested
# ordering, "no Snoozed/Important anywhere", and resolve_mailbox for the
# full key vocabulary.
# ---------------------------------------------------------------------------


async def test_system_and_more_items_resolve_to_the_right_role_mailboxes(
    fake_client_with_mailboxes,
):
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta={})
    by_key = {i.key: i for i in (*nav.system, *nav.more)}
    assert by_key["inbox"].mailbox_id == "mb-inbox"
    assert by_key["sent"].mailbox_id == "mb-sent"
    assert by_key["drafts"].mailbox_id == "mb-drafts"
    assert by_key["archive"].mailbox_id == "mb-archive"
    assert by_key["spam"].mailbox_id == "mb-junk"  # JMAP role is "junk"; nav key is "spam"
    assert by_key["trash"].mailbox_id == "mb-trash"
    # "starred"/"all" are virtual views, not real JMAP mailboxes.
    assert by_key["starred"].mailbox_id is None
    assert by_key["all"].mailbox_id is None


async def test_no_snoozed_or_important_anywhere(fake_client_with_mailboxes):
    # Design spec §3/§5.2: Snoozed/Important only appear once those
    # features ship -- the nav must never advertise a control for them.
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta={})
    all_keys = {i.key for i in (*nav.system, *nav.more)}
    deferred_keys = {"snoozed", "important"}
    assert not (all_keys & deferred_keys)


async def test_counts_are_unread_except_drafts_which_is_total(fake_client_with_mailboxes):
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta={})
    by_key = {i.key: i for i in (*nav.system, *nav.more)}
    assert by_key["inbox"].count == 12  # unreadEmails
    assert by_key["drafts"].count == 2  # totalEmails, not unreadEmails (0)
    # Starred/Sent/All/Archive/Spam/Trash carry no badge in the approved
    # mockups (layout.html only badges Inbox and Drafts).
    for key in ("starred", "sent", "all", "archive", "spam", "trash"):
        assert by_key[key].count is None


async def test_active_flag_set_only_on_the_matching_item(fake_client_with_mailboxes):
    nav = await build_nav(fake_client_with_mailboxes, active_key="sent", label_meta={})
    actives = [i.key for i in (*nav.system, *nav.more) if i.active]
    assert actives == ["sent"]


async def test_label_visibility_hide_is_structural_show_if_unread_is_not(
    fake_client_with_mailboxes,
):
    # Only visibility="hide" drops a label out of nav.labels entirely --
    # that absence is what enforces "hidden labels never produce chips"
    # (controller decision 3). A show_if_unread label STAYS in the tree even
    # at zero unread: that preference governs its sidebar row, not its
    # identity on a conversation, and dropping it here would make its chips
    # blink in and out for reasons unrelated to the thread.
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta=_LABEL_META)
    names = {label.name for label in nav.labels}
    assert "Secret" not in names  # visibility="hide": never in the tree at all
    assert "Newsletters Quiet" in names  # show_if_unread, 0 unread: present...
    assert "Newsletters Loud" in names  # show_if_unread, unread>0: present
    assert "Receipts" in names  # no LabelMeta row at all -> defaults to "show"

    by_name = {label.name: label for label in nav.labels}
    # ...but flagged for the nav template to skip its row.
    assert hidden_in_nav(by_name["Newsletters Quiet"]) is True
    assert hidden_in_nav(by_name["Newsletters Loud"]) is False
    assert hidden_in_nav(by_name["Receipts"]) is False
    assert hidden_in_nav(by_name["Work"]) is False


async def test_labels_ordered_alphabetically_top_level(fake_client_with_mailboxes):
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta=_LABEL_META)
    assert [label.name for label in nav.labels] == [
        "Newsletters Loud",
        "Newsletters Quiet",
        "Receipts",
        "Work",
    ]


async def test_label_with_no_meta_row_defaults_to_show_and_no_color(fake_client_with_mailboxes):
    # Receipts has no entry in label_meta at all -- mailosh.db.repo.
    # label_meta_map's own contract: "a mailbox with no LabelMeta row simply
    # has no entry" and callers fall back to defaults, not an invented row.
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta=_LABEL_META)
    receipts = next(label for label in nav.labels if label.name == "Receipts")
    assert receipts.visibility == "show" and receipts.color is None


async def test_label_counts_are_unread_emails(fake_client_with_mailboxes):
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta=_LABEL_META)
    work = next(label for label in nav.labels if label.name == "Work")
    assert work.count == 3
    loud = next(label for label in nav.labels if label.name == "Newsletters Loud")
    assert loud.count == 4


async def test_resolve_mailbox_covers_every_reserved_key(fake_client_with_mailboxes):
    nav = await build_nav(fake_client_with_mailboxes, active_key="inbox", label_meta=_LABEL_META)
    assert resolve_mailbox(nav, "inbox") == "mb-inbox"
    assert resolve_mailbox(nav, "sent") == "mb-sent"
    assert resolve_mailbox(nav, "drafts") == "mb-drafts"
    assert resolve_mailbox(nav, "archive") == "mb-archive"
    assert resolve_mailbox(nav, "spam") == "mb-junk"
    assert resolve_mailbox(nav, "trash") == "mb-trash"
    assert resolve_mailbox(nav, "starred") is None
    assert resolve_mailbox(nav, "all") is None
    # An arbitrary raw mailbox id (e.g. a label clicked in the sidebar) is
    # not one of the reserved keys above, so it passes through unchanged.
    assert resolve_mailbox(nav, "m-receipts") == "m-receipts"


async def test_missing_inbox_role_raises_loudly():
    # Same "fail loudly, don't guess" stance as find_inbox itself (reused
    # here, not reimplemented) -- an account with no role="inbox" mailbox
    # is a real error, not a nav with a blank Inbox item.
    no_inbox = [m for m in _MAILBOXES if m.role != "inbox"]
    with pytest.raises(JmapError):
        await build_nav(_FakeClient(no_inbox), active_key="inbox", label_meta={})


async def test_missing_non_inbox_role_is_hidden_not_advertised():
    # Unlike Inbox, a missing secondary role mailbox (Stalwart's defaults
    # have no Archive at all) must not take down the whole nav. Nor may it
    # be offered: `mailosh.web.mail._valid_keys` builds the app's URL
    # vocabulary out of these very items, so a rendered "Archive" row on an
    # account with no Archive folder is a link the sidebar invites the
    # reader to click and the router then 404s.
    no_archive = [m for m in _MAILBOXES if m.role != "archive"]
    nav = await build_nav(_FakeClient(no_archive), active_key="inbox", label_meta={})
    assert [i.key for i in nav.more] == ["all", "spam", "trash"]
    assert [i.key for i in nav.system] == ["inbox", "starred", "sent", "drafts"]


async def test_a_virtual_view_keeps_its_row_while_an_unbacked_role_loses_one():
    # The distinction is "role declared but not found" vs "no role by
    # design" -- NOT "mailbox_id is None", which is true of all four items
    # below. Starred is a `$flagged` filter and All mail is
    # `inMailboxOtherThan`; `thread_list.build_page` builds both from the
    # key alone, so neither has a mailbox to miss. Sent/Drafts/Archive/
    # Spam/Trash name real roles, and this account has none of them.
    only_inbox_and_labels = [m for m in _MAILBOXES if m.role in (None, "inbox")]
    nav = await build_nav(_FakeClient(only_inbox_and_labels), active_key="inbox", label_meta={})
    assert [i.key for i in nav.system] == ["inbox", "starred"]
    assert [i.key for i in nav.more] == ["all"]
    assert [i.mailbox_id for i in nav.system if i.key == "starred"] == [None]
    assert [i.mailbox_id for i in nav.more] == [None]
    # ...and the labels are untouched by any of this.
    assert {label.name for label in nav.labels} >= {"Work", "Receipts"}


async def test_a_reserved_key_with_no_row_resolves_to_none_never_to_itself():
    # `resolve_mailbox`'s pass-through exists for labels: "m-work" IS its
    # own mailbox id. A reserved key must never take that branch -- handing
    # `Email/query` (or an `Email/set` patch) the literal string "archive"
    # as a mailbox id is how a missing folder turns into a silent mis-file
    # instead of a visible failure. Nothing rendered the item, so the scan
    # over `nav.system`/`nav.more` cannot answer this on its own.
    only_inbox_and_labels = [m for m in _MAILBOXES if m.role in (None, "inbox")]
    nav = await build_nav(_FakeClient(only_inbox_and_labels), active_key="inbox", label_meta={})
    for key in ("archive", "spam", "trash", "sent", "drafts", "starred", "all"):
        assert resolve_mailbox(nav, key) is None, key
    assert resolve_mailbox(nav, "inbox") == "mb-inbox"
    assert resolve_mailbox(nav, "m-work") == "m-work"


# ---------------------------------------------------------------------------
# ensure_role_mailbox: resolve, or create -- the folder archive needs on an
# account whose server never provisioned one.
# ---------------------------------------------------------------------------


async def test_ensure_role_mailbox_returns_an_existing_one_without_creating():
    fake = _FakeClient(_MAILBOXES)
    assert await ensure_role_mailbox(fake, "archive") == "mb-archive"
    assert fake.creates == []


async def test_ensure_role_mailbox_creates_the_folder_with_its_role_and_name():
    fake = _FakeClient([m for m in _MAILBOXES if m.role != "archive"])
    created = await ensure_role_mailbox(fake, "archive")

    # The role is what makes it *the* Archive rather than a label that
    # happens to be called one: every later `build_nav` resolves it by role.
    assert fake.creates == [("Archive", "archive")]
    made = [m for m in await fake.get_mailboxes() if m.role == "archive"]
    assert [m.id for m in made] == [created]
    assert made[0].name == "Archive"

    # ...and it is now an ordinary nav row again, resolvable like any other.
    nav = await build_nav(fake, active_key="inbox", label_meta={})
    assert resolve_mailbox(nav, "archive") == created
    assert [i.key for i in nav.more] == ["all", "archive", "spam", "trash"]


async def test_ensure_role_mailbox_is_idempotent():
    fake = _FakeClient([m for m in _MAILBOXES if m.role != "archive"])
    first = await ensure_role_mailbox(fake, "archive")
    second = await ensure_role_mailbox(fake, "archive")
    assert first == second
    assert len(fake.creates) == 1


async def test_two_concurrent_callers_create_exactly_one_mailbox():
    # The failure this prevents is a *pair* of Archive folders, which would
    # then split the account's archived mail between them for good. The
    # in-process lock is what holds the second caller until the first has
    # committed, so only one create is ever attempted.
    fake = _FakeClient([m for m in _MAILBOXES if m.role != "archive"])
    first, second = await asyncio.gather(
        ensure_role_mailbox(fake, "archive"), ensure_role_mailbox(fake, "archive")
    )
    assert first == second
    assert len(fake.creates) == 1
    assert [m.id for m in await fake.get_mailboxes() if m.role == "archive"] == [first]


async def test_losing_the_create_race_resolves_to_the_winner_rather_than_failing():
    # The cross-process half: another worker created the folder between our
    # `Mailbox/get` and our `Mailbox/set`, so the server rejects ours (RFC
    # 8621 §2: a role is unique per account). The folder the caller wanted
    # now exists, so that rejection is not a failure -- it re-resolves.
    class _Loser(_FakeClient):
        async def create_mailbox(self, name: str, *, role: str | None = None) -> str:
            self.creates.append((name, role))
            self._mailboxes.append(_mailbox("mb-theirs", name, role=role))
            raise JmapError("Mailbox/set create failed: {'type': 'invalidProperties'}")

    fake = _Loser([m for m in _MAILBOXES if m.role != "archive"])
    assert await ensure_role_mailbox(fake, "archive") == "mb-theirs"
    assert len(fake.creates) == 1
    assert len([m for m in await fake.get_mailboxes() if m.role == "archive"]) == 1


async def test_a_create_that_fails_for_a_real_reason_still_raises():
    # Only "someone else already made it" is recoverable. A create refused
    # for any other reason leaves the account with no Archive folder, and
    # the caller must hear about it rather than being handed an id that
    # does not exist -- or, worse, proceeding without one.
    class _Refused(_FakeClient):
        async def create_mailbox(self, name: str, *, role: str | None = None) -> str:
            self.creates.append((name, role))
            raise JmapError("Mailbox/set create failed: {'type': 'forbidden'}")

    fake = _Refused([m for m in _MAILBOXES if m.role != "archive"])
    with pytest.raises(JmapError, match="forbidden"):
        await ensure_role_mailbox(fake, "archive")
    assert [m for m in await fake.get_mailboxes() if m.role == "archive"] == []


@pytest.mark.parametrize("key", ["starred", "all", "not-a-key"])
async def test_ensure_role_mailbox_refuses_a_key_with_no_role_of_its_own(key):
    # Starred and All mail are virtual views by design. Inventing a folder
    # for one would not be a fallback, it would be a bug -- and it would
    # put a mailbox in the account that nothing ever reads.
    fake = _FakeClient(_MAILBOXES)
    with pytest.raises(KeyError):
        await ensure_role_mailbox(fake, key)
    assert fake.creates == []


async def test_orphaned_child_of_a_hidden_parent_is_promoted_not_dropped():
    # If a label's parent is itself hidden, its (still-visible) child must
    # not silently disappear -- it's promoted to top level instead.
    mailboxes = [
        *_MAILBOXES,
        _mailbox("m-hidden-parent", "Hidden Parent", sort_order=200, total=1, unread=0),
        _mailbox(
            "m-visible-child",
            "Visible Child",
            parent_id="m-hidden-parent",
            sort_order=210,
            total=1,
            unread=0,
        ),
    ]
    label_meta = {**_LABEL_META, "m-hidden-parent": LabelMeta(visibility="hide")}
    nav = await build_nav(_FakeClient(mailboxes), active_key="inbox", label_meta=label_meta)
    names = {label.name for label in nav.labels}
    assert "Hidden Parent" not in names
    assert "Visible Child" in names


# ---------------------------------------------------------------------------
# Visibility vocabulary (review finding 3): the three legal tokens live in
# mailosh.db.models.Visibility, and an unrecognised one must never be
# silently read as "show".
# ---------------------------------------------------------------------------


def test_visibility_enum_is_the_single_source_of_the_three_tokens():
    # The hand-written migration writes a "show" default and the ORM column
    # default is Visibility.SHOW.value; if those drift, a fresh row's own
    # default stops parsing. Every value must also fit the String(16) column.
    assert {v.value for v in Visibility} == {"show", "hide", "show_if_unread"}
    assert Visibility.SHOW.value == "show"
    assert all(len(v.value) <= 16 for v in Visibility)


def test_unrecognised_visibility_token_does_not_silently_mean_show(caplog):
    # The failure this guards against: a later label-visibility control
    # writing "hidden" or "show-if-unread" into an unconstrained String(16)
    # column. Resolving that to "show" would leave a label the user
    # explicitly hid fully visible, with no signal anywhere. It resolves to
    # HIDE instead -- the recoverable direction -- and says so in the log.
    with caplog.at_level(logging.WARNING, logger="mailosh.db.models"):
        assert Visibility.parse("hidden") is Visibility.HIDE
        assert Visibility.parse("show-if-unread") is Visibility.HIDE
    assert "hidden" in caplog.text and "show-if-unread" in caplog.text

    # None (no LabelMeta row at all) is the documented default, not a bug,
    # and must not warn.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mailosh.db.models"):
        assert Visibility.parse(None) is Visibility.SHOW
    assert caplog.text == ""


async def test_label_with_unrecognised_visibility_is_hidden_not_shown():
    # End to end through build_nav: the bad token keeps the label out of
    # nav.labels entirely, exactly as an explicit "hide" would -- never
    # visible-by-accident.
    label_meta = {**_LABEL_META, "m-receipts": LabelMeta(color="teal", visibility="Hidden")}
    nav = await build_nav(_FakeClient(_MAILBOXES), active_key="inbox", label_meta=label_meta)
    assert "Receipts" not in {label.name for label in nav.labels}
