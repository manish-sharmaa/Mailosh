"""Unit tests for `mailosh.services.labels` and the `LabelMeta` accessors in
`mailosh.db.repo` (design spec §10).

Each section below pins one property the label feature can silently lose,
and every test in it is written to go *red* when that property breaks rather
than when an implementation detail moves:

1. **Deleting a label never deletes mail.** The destroy is always
   `onDestroyRemoveEmails: false`, the messages are stripped first, and a
   message that would be left in no mailbox at all lands in Archive. A
   refused destroy is surfaced, never retried destructively.
2. **A `LabelMeta` row can outlive its mailbox.** The nav must not crash or
   render a ghost, and the orphan must be reachable for cleanup.
3. **Multi-apply is one write.** Two labels across twenty conversations is
   two JMAP requests, and a partial failure reports how much actually
   landed.
4. **A nesting cycle is the server's refusal, said in English.**
5. **"Show if unread" is computed, not an N+1.**

`FakeClient` records every call it receives, so "how many requests did that
cost" is an assertion rather than a hope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.db import repo
from mailosh.db.base import Base
from mailosh.db.models import AppUser, LabelMeta
from mailosh.jmap.client import QueryPage
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import Address, EmailHeader, Mailbox
from mailosh.services import labels as service
from mailosh.services.mailbox_tree import build_nav, hidden_in_nav

ACCOUNT = "acct-labels"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _mailbox(
    mailbox_id: str,
    name: str,
    *,
    role: str | None = None,
    parent_id: str | None = None,
    total: int = 0,
    unread: int = 0,
) -> Mailbox:
    return Mailbox(
        id=mailbox_id,
        name=name,
        role=role,
        parent_id=parent_id,
        sort_order=0,
        total_emails=total,
        unread_emails=unread,
    )


def _header(email_id: str, thread_id: str, mailbox_ids: set[str]) -> EmailHeader:
    return EmailHeader(
        id=email_id,
        thread_id=thread_id,
        mailbox_ids=set(mailbox_ids),
        keywords=set(),
        from_=[Address(name="Priya", email="priya@example.com")],
        subject="Q3",
        received_at=datetime(2026, 9, 5, 9, 0, tzinfo=UTC),
        preview="",
        has_attachment=False,
    )


@dataclass
class _Call:
    name: str
    kwargs: dict


class FakeClient:
    """A `JmapClient` stand-in that keeps real mailbox membership in memory,
    so an action's effect can be asserted rather than just its wire body.

    Every method appends to `calls`, which is what makes "two labels across
    twenty conversations costs two requests" a test rather than a claim.
    """

    def __init__(
        self,
        mailboxes: list[Mailbox] | None = None,
        placement: dict[str, set[str]] | None = None,
    ) -> None:
        self.mailboxes = mailboxes if mailboxes is not None else _default_mailboxes()
        #: email id -> the mailboxes it is in, mutated by every write below.
        self.placement = placement if placement is not None else {}
        self.threads = {eid: "t-" + eid for eid in self.placement}
        self.calls: list[_Call] = []
        #: When set, the *next* `set_mailboxes_patch` raises after applying
        #: only the ids named — a partial `Email/set`, which is exactly what
        #: RFC 8620 §5.3's `notUpdated` produces.
        self.partial_after: list[str] | None = None
        #: When set, `destroy_mailbox` raises this instead of destroying.
        self.destroy_error: JmapError | None = None
        self.created: list[dict] = []

    @property
    def account_id(self) -> str:
        return ACCOUNT

    async def get_mailboxes(self) -> list[Mailbox]:
        self.calls.append(_Call("get_mailboxes", {}))
        return list(self.mailboxes)

    async def get_email_states(self, email_ids: list[str]):
        from mailosh.jmap.client import EmailState

        self.calls.append(_Call("get_email_states", {"ids": list(email_ids)}))
        out = []
        for email_id in dict.fromkeys(email_ids):
            if email_id not in self.placement:
                continue
            out.append(
                EmailState(
                    id=email_id,
                    thread_id=self.threads.get(email_id, "t-" + email_id),
                    mailbox_ids=frozenset(self.placement[email_id]),
                    keywords=frozenset(),
                )
            )
        return out

    async def set_mailboxes_patch(self, patches, *, keywords=None) -> None:
        self.calls.append(_Call("set_mailboxes_patch", {"patches": dict(patches)}))
        allowed = self.partial_after
        self.partial_after = None
        for email_id, patch in patches.items():
            if allowed is not None and email_id not in allowed:
                raise JmapError(f"Email/set update failed for {email_id!r}: forbidden")
            current = set(self.placement.get(email_id, set()))
            for mailbox_id, on in patch.items():
                if on:
                    current.add(mailbox_id)
                else:
                    current.discard(mailbox_id)
            self.placement[email_id] = current

    async def set_keywords(self, email_ids, keyword, on) -> None:
        self.calls.append(_Call("set_keywords", {"ids": list(email_ids)}))

    async def query_search(self, *, filter, position, limit) -> QueryPage:
        self.calls.append(_Call("query_search", {"filter": filter, "limit": limit}))
        mailbox_id = filter["inMailbox"]
        wanted = [eid for eid, boxes in self.placement.items() if mailbox_id in boxes]
        by_thread: dict[str, list[EmailHeader]] = {}
        for email_id in wanted[:limit]:
            thread = self.threads.get(email_id, "t-" + email_id)
            by_thread.setdefault(thread, []).append(
                _header(email_id, thread, self.placement[email_id])
            )
        return QueryPage(
            thread_order=list(by_thread),
            total=len(wanted),
            emails_by_thread=by_thread,
            position=0,
        )

    async def query_page(self, **kwargs) -> QueryPage:
        """Enough of the list route to render `/mail/inbox`, so the route
        tests in `test_label_routes.py` can reach a real page (and its nav)
        through the same fake. Serves every message it holds, in one thread
        each — the list's own shaping is `test_thread_list.py`'s subject.
        """
        self.calls.append(_Call("query_page", dict(kwargs)))
        by_thread: dict[str, list[EmailHeader]] = {}
        for email_id, boxes in self.placement.items():
            thread = self.threads.get(email_id, "t-" + email_id)
            by_thread.setdefault(thread, []).append(_header(email_id, thread, boxes))
        return QueryPage(
            thread_order=list(by_thread),
            total=len(by_thread),
            emails_by_thread=by_thread,
            position=int(kwargs.get("position", 0)),
        )

    async def create_mailbox(self, name: str, *, role: str | None = None) -> str:
        self.calls.append(_Call("create_mailbox", {"name": name, "role": role}))
        if any(m.name == name and m.parent_id is None for m in self.mailboxes):
            raise JmapError(
                "Mailbox/set create failed: "
                "{'type': 'alreadyExists', 'description': 'A mailbox with that name exists.'}"
            )
        new_id = f"new{len(self.created) + 1}"
        self.created.append({"id": new_id, "name": name, "role": role})
        self.mailboxes.append(_mailbox(new_id, name, role=role))
        return new_id

    async def update_mailbox(self, mailbox_id: str, patch: dict) -> None:
        self.calls.append(_Call("update_mailbox", {"id": mailbox_id, "patch": dict(patch)}))
        target = next(m for m in self.mailboxes if m.id == mailbox_id)
        if "parentId" in patch and patch["parentId"] == mailbox_id:
            raise JmapError(
                "Mailbox/set update failed: {'type': 'invalidProperties', "
                "'description': 'Mailbox cannot be a parent of itself.', "
                "'properties': ['parentId']}"
            )
        for key, value in patch.items():
            if key == "name":
                target.name = value
            elif key == "parentId":
                target.parent_id = value

    async def destroy_mailbox(self, mailbox_id: str, *, on_destroy_remove_emails: bool = False):
        self.calls.append(
            _Call(
                "destroy_mailbox",
                {"id": mailbox_id, "on_destroy_remove_emails": on_destroy_remove_emails},
            )
        )
        if self.destroy_error is not None:
            raise self.destroy_error
        if any(mailbox_id in boxes for boxes in self.placement.values()):
            raise JmapError(
                "Mailbox/set destroy failed: "
                "{'type': 'mailboxHasEmail', 'description': 'Mailbox is not empty.'}"
            )
        self.mailboxes = [m for m in self.mailboxes if m.id != mailbox_id]

    def named(self, name: str) -> list[_Call]:
        return [call for call in self.calls if call.name == name]


def _default_mailboxes() -> list[Mailbox]:
    return [
        _mailbox("mb-inbox", "Inbox", role="inbox", unread=3),
        _mailbox("mb-archive", "Archive", role="archive"),
        _mailbox("mb-trash", "Trash", role="trash"),
        _mailbox("m-work", "Work", total=2, unread=1),
        _mailbox("m-receipts", "Receipts", total=1),
    ]


async def _nav(client: FakeClient, label_meta: dict[str, LabelMeta] | None = None):
    return await build_nav(client, active_key="", label_meta=label_meta or {})


@pytest.fixture
async def db(tmp_path):
    """A throwaway aiosqlite session with every table created — the same
    shape `tests/unit/test_db_models.py` uses.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/labels.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def user(db) -> AppUser:
    return await repo.get_or_create_user(db, "demo@mailosh.test", "demo@mailosh.test")


# ---------------------------------------------------------------------------
# 1. Deleting a label never deletes mail
# ---------------------------------------------------------------------------


async def test_delete_strips_the_label_and_never_asks_the_server_to_remove_emails():
    """The one thing this feature must never do.

    `onDestroyRemoveEmails: true` is how JMAP says "and delete every message
    that was only in here", and no code path may reach it — not as a retry,
    not as a fallback for a mailbox that would not empty. The label comes
    off every message first; every message survives with its other labels.
    """
    client = FakeClient(
        placement={
            "e1": {"mb-inbox", "m-work"},
            "e2": {"mb-inbox", "m-work", "m-receipts"},
            "e3": {"mb-inbox"},
        }
    )
    outcome = await service.delete_label(client, await _nav(client), "m-work")

    destroys = client.named("destroy_mailbox")
    assert len(destroys) == 1
    assert destroys[0].kwargs["on_destroy_remove_emails"] is False

    # Every message still exists, and every *other* label is intact.
    assert client.placement["e1"] == {"mb-inbox"}
    assert client.placement["e2"] == {"mb-inbox", "m-receipts"}
    assert client.placement["e3"] == {"mb-inbox"}
    assert outcome.unlabelled == 2
    assert outcome.archived == 0
    assert "m-work" not in {m.id for m in client.mailboxes}


async def test_a_message_that_lived_only_in_the_label_goes_to_archive_not_nowhere():
    """RFC 8621 §4.1 forbids an empty `mailboxIds`, so "just take the label
    off" is not an option for a message that has nothing else. Archive is
    where it goes — the same answer `services.actions.archive` gives for an
    Inbox-only message — and the count is reported so the toast can say so.
    """
    client = FakeClient(placement={"e1": {"m-work"}, "e2": {"mb-inbox", "m-work"}})
    outcome = await service.delete_label(client, await _nav(client), "m-work")

    assert client.placement["e1"] == {"mb-archive"}
    assert client.placement["e2"] == {"mb-inbox"}
    assert outcome.archived == 1
    assert outcome.unlabelled == 1
    # No message was ever emptied on the way through.
    for patch_call in client.named("set_mailboxes_patch"):
        for email_id in patch_call.kwargs["patches"]:
            assert client.placement[email_id], email_id


async def test_a_refused_destroy_is_surfaced_and_never_retried_destructively():
    """A server refusal is a signal, not a prompt to try harder. If the
    destroy still fails after the drain (something filed mail into the label
    mid-delete), the reader is told and the label survives with its mail.
    """
    client = FakeClient(placement={"e1": {"mb-inbox", "m-work"}})
    client.destroy_error = JmapError(
        "Mailbox/set destroy failed: "
        "{'type': 'mailboxHasEmail', 'description': 'Mailbox is not empty.'}"
    )
    with pytest.raises(service.LabelError) as caught:
        await service.delete_label(client, await _nav(client), "m-work")

    assert "still has mail" in str(caught.value)
    # Exactly one destroy attempt, and it was the non-destructive one.
    destroys = client.named("destroy_mailbox")
    assert len(destroys) == 1
    assert destroys[0].kwargs["on_destroy_remove_emails"] is False
    assert not any(call.kwargs.get("on_destroy_remove_emails") for call in destroys)


async def test_a_label_with_children_is_refused_before_anything_is_touched():
    client = FakeClient(placement={"e1": {"mb-inbox", "m-work"}})
    client.mailboxes.append(_mailbox("m-design", "Design", parent_id="m-work"))
    with pytest.raises(service.LabelError) as caught:
        await service.delete_label(client, await _nav(client), "m-work")
    assert "nested inside it" in str(caught.value)
    # Nothing was stripped and nothing was destroyed: a refusal that has
    # already half-emptied the label would be the worst of both.
    assert client.placement["e1"] == {"mb-inbox", "m-work"}
    assert client.named("destroy_mailbox") == []


async def test_delete_plan_reports_this_labels_own_message_count():
    """The confirmation's number comes off the mailbox the nav already
    fetched — no extra query, and no thread-collapsed approximation.
    """
    client = FakeClient()
    plan = await service.delete_plan(client, "m-work")
    assert plan.name == "Work"
    assert plan.messages == 2
    assert plan.children == []
    assert plan.blocked is False


async def test_a_system_folder_is_never_a_label():
    """Inbox, Trash and friends have a JMAP role, and no label operation may
    touch one whatever id arrives in a form field.
    """
    client = FakeClient()
    for call in (
        service.delete_plan(client, "mb-inbox"),
        service.rename_label(client, "mb-trash", "Bin"),
        service.nest_label(client, "mb-archive", None),
    ):
        with pytest.raises(service.LabelError) as caught:
            await call
        assert "system folder" in str(caught.value)


# ---------------------------------------------------------------------------
# 2. A LabelMeta row can outlive its mailbox
# ---------------------------------------------------------------------------


async def test_an_orphan_label_meta_row_cannot_render_a_ghost_label():
    """A label deleted in Thunderbird (or by another session) leaves a
    `LabelMeta` row keyed on an id that no longer resolves.

    `build_nav` walks *mailboxes* and looks metadata up by id, so the row is
    simply never consulted: the nav renders the labels that exist, no more
    and no fewer, and nothing raises.
    """
    client = FakeClient()
    meta = {
        "m-work": LabelMeta(color="indigo", visibility="show"),
        # The orphan: no mailbox has this id.
        "m-deleted-elsewhere": LabelMeta(color="rose", visibility="hide"),
    }
    nav = await _nav(client, meta)
    assert [node.name for node in nav.labels] == ["Receipts", "Work"]
    assert "m-deleted-elsewhere" not in {node.mailbox_id for node in nav.labels}


async def test_prune_drops_orphans_and_leaves_live_rows_alone(db, user):
    """Cleanup exists because ids are the server's to reuse: a row keyed on
    a deleted label's id would hand its colour — or its `hide` — to whatever
    mailbox that id names next.
    """
    for mailbox_id, color in (("m-work", "indigo"), ("m-gone", "rose")):
        await repo.set_label_meta(db, user.id, ACCOUNT, mailbox_id, color=color)

    dropped = await repo.prune_label_meta(db, user.id, ACCOUNT, {"m-work", "mb-inbox"})
    assert dropped == ["m-gone"]

    remaining = await repo.label_meta_map(db, user.id, ACCOUNT)
    assert set(remaining) == {"m-work"}
    assert remaining["m-work"].color == "indigo"


async def test_prune_is_a_no_op_when_nothing_is_orphaned(db, user):
    """The ordinary case must cost nothing: no DELETE is issued at all when
    every row still names a live mailbox.
    """
    await repo.set_label_meta(db, user.id, ACCOUNT, "m-work", color="indigo")
    assert await repo.prune_label_meta(db, user.id, ACCOUNT, {"m-work"}) == []
    assert set(await repo.label_meta_map(db, user.id, ACCOUNT)) == {"m-work"}


async def test_prune_is_scoped_to_one_user_and_one_account(db, user):
    """`prune_label_meta` deletes by *absence*, so a bug in its WHERE clause
    would silently wipe another account's (or another user's) metadata.
    """
    other = await repo.get_or_create_user(db, "other@mailosh.test", "other@mailosh.test")
    await repo.set_label_meta(db, user.id, ACCOUNT, "m-work", color="indigo")
    await repo.set_label_meta(db, other.id, ACCOUNT, "m-work", color="rose")
    await repo.set_label_meta(db, user.id, "acct-other", "m-work", color="teal")

    await repo.prune_label_meta(db, user.id, ACCOUNT, set())

    assert await repo.label_meta_map(db, user.id, ACCOUNT) == {}
    assert set(await repo.label_meta_map(db, other.id, ACCOUNT)) == {"m-work"}
    assert set(await repo.label_meta_map(db, user.id, "acct-other")) == {"m-work"}


async def test_set_label_meta_touches_only_the_columns_it_is_given(db, user):
    """A colour change must not reset a visibility the reader chose, and
    clearing a colour has to be expressible — which is why "leave it alone"
    and `None` are different things.
    """
    await repo.set_label_meta(db, user.id, ACCOUNT, "m-work", visibility="show_if_unread")
    await repo.set_label_meta(db, user.id, ACCOUNT, "m-work", color="teal")
    row = await repo.label_meta(db, user.id, ACCOUNT, "m-work")
    assert row is not None
    assert (row.color, row.visibility) == ("teal", "show_if_unread")

    await repo.set_label_meta(db, user.id, ACCOUNT, "m-work", color=None)
    row = await repo.label_meta(db, user.id, ACCOUNT, "m-work")
    assert row is not None
    assert (row.color, row.visibility) == (None, "show_if_unread")


async def test_forget_label_meta_is_a_no_op_for_a_label_that_never_had_a_row(db, user):
    await repo.forget_label_meta(db, user.id, ACCOUNT, "m-never-coloured")
    assert await repo.label_meta_map(db, user.id, ACCOUNT) == {}


# ---------------------------------------------------------------------------
# 3. Multi-apply is one write
# ---------------------------------------------------------------------------


async def test_two_labels_across_twenty_conversations_cost_two_requests():
    """The property the brief names: not forty round trips, not two writes —
    one snapshot and one `Email/set`, whatever the size of the selection.
    """
    ids = [f"e{n}" for n in range(20)]
    client = FakeClient(placement={eid: {"mb-inbox"} for eid in ids})
    nav = await _nav(client)
    before = len(client.calls)

    await service.apply_labels(client, nav, ids, add=["m-work", "m-receipts"], remove=[], names={})

    made = [call.name for call in client.calls[before:]]
    assert made == ["get_email_states", "set_mailboxes_patch"], made
    for eid in ids:
        assert client.placement[eid] == {"mb-inbox", "m-work", "m-receipts"}


async def test_adds_and_removes_ride_in_the_same_write():
    ids = ["e1", "e2"]
    client = FakeClient(placement={eid: {"mb-inbox", "m-receipts"} for eid in ids})
    nav = await _nav(client)
    before = len(client.calls)
    await service.apply_labels(client, nav, ids, add=["m-work"], remove=["m-receipts"], names={})
    assert [c.name for c in client.calls[before:]].count("set_mailboxes_patch") == 1
    assert client.placement["e1"] == {"mb-inbox", "m-work"}


async def test_removing_the_only_label_a_message_has_sends_it_to_archive():
    client = FakeClient(placement={"e1": {"m-work"}})
    nav = await _nav(client)
    await service.apply_labels(client, nav, ["e1"], add=[], remove=["m-work"], names={})
    assert client.placement["e1"] == {"mb-archive"}


async def test_a_partial_failure_reports_how_much_actually_landed():
    """`Email/set` reports per-message failures alongside an otherwise
    successful response, so a rejected id in the middle leaves the rest
    genuinely changed. Reporting that as a flat failure would send the
    reader back to press the same button on a half-applied selection.
    """
    ids = ["e1", "e2", "e3"]
    client = FakeClient(placement={eid: {"mb-inbox"} for eid in ids})
    nav = await _nav(client)
    client.partial_after = ["e1"]  # e2 is rejected; e1 has already applied

    with pytest.raises(service.PartialApply) as caught:
        await service.apply_labels(client, nav, ids, add=["m-work"], remove=[], names={})

    assert caught.value.total == 3
    assert caught.value.applied == 1
    assert "1 of 3" in str(caught.value)


async def test_applying_a_label_is_undoable_and_records_the_exact_previous_placement():
    """Undo is the same machinery the six triage actions use — the result
    carries each message's previous mailbox set, which is what lets `z`
    take a label back off instead of guessing.
    """
    client = FakeClient(placement={"e1": {"mb-inbox", "m-receipts"}})
    nav = await _nav(client)
    result = await service.apply_labels(
        client, nav, ["e1"], add=["m-work"], remove=[], names={"m-work": "Work"}
    )
    assert result.spec.kind == "label"
    assert result.spec.toast == "Labelled “Work”"
    assert result.spec.prev == {"e1": ["m-receipts", "mb-inbox"]}
    # A label change never takes a row out of the list server-side.
    assert result.removed == []


async def test_move_replaces_every_mailbox_and_removes_the_row():
    client = FakeClient(placement={"e1": {"mb-inbox", "m-receipts"}})
    nav = await _nav(client)
    result = await service.move_to(client, nav, ["e1"], "m-work", name="Work")
    assert client.placement["e1"] == {"m-work"}
    assert result.spec.toast == "Moved to “Work”"
    assert result.removed == ["t-e1"]
    assert result.spec.prev == {"e1": ["m-receipts", "mb-inbox"]}


async def test_apply_refuses_a_label_that_is_both_added_and_removed():
    client = FakeClient(placement={"e1": {"mb-inbox"}})
    nav = await _nav(client)
    with pytest.raises(service.LabelError):
        await service.apply_labels(client, nav, ["e1"], add=["m-work"], remove=["m-work"], names={})
    assert client.named("set_mailboxes_patch") == []


# ---------------------------------------------------------------------------
# 4. Nesting cycles, and every other refusal, as a sentence
# ---------------------------------------------------------------------------


async def test_a_cycle_reaches_the_reader_as_english_not_a_jmap_error_string():
    """RFC 8621 §2 makes the server the authority on what a cycle is, and
    `update_mailbox` deliberately does not re-derive that check locally. So
    the refusal has to arrive — and it has to arrive readable.
    """
    client = FakeClient()
    with pytest.raises(service.LabelError) as caught:
        await service.nest_label(client, "m-work", "m-work")
    message = str(caught.value)
    assert message == "A label can't be nested inside itself."
    for leak in ("invalidProperties", "Mailbox/set", "{", "parentId"):
        assert leak not in message


async def test_a_server_side_cycle_refusal_is_translated_too():
    """The local guard only catches "its own id". A `parentId` deeper inside
    the subtree reaches the server, and its refusal must read the same way.
    """
    client = FakeClient()
    client.mailboxes.append(_mailbox("m-design", "Design", parent_id="m-work"))

    async def refuse(mailbox_id, patch):
        raise JmapError(
            "Mailbox/set update failed: {'type': 'invalidProperties', "
            "'description': 'Mailbox cannot be a parent of itself.', "
            "'properties': ['parentId']}"
        )

    client.update_mailbox = refuse  # type: ignore[method-assign]
    with pytest.raises(service.LabelError) as caught:
        await service.nest_label(client, "m-work", "m-design")
    assert str(caught.value) == "A label can't be nested inside itself."


async def test_a_duplicate_name_is_explained_with_the_name_in_it():
    client = FakeClient()
    with pytest.raises(service.LabelError) as caught:
        await service.create_label(client, "Work")
    assert str(caught.value) == "There's already a label called “Work” here."


async def test_an_unrecognised_refusal_never_leaks_the_jmap_error():
    error = JmapError(
        "Mailbox/set update failed: {'type': 'somethingNew', 'description': 'internals'}"
    )
    explained = service.explain(error, name="Work")
    assert str(explained) == "Couldn't update “Work”."
    assert "internals" not in str(explained)
    assert "somethingNew" not in str(explained)


async def test_creating_a_nested_label_keeps_the_label_when_the_nest_fails():
    """Two `Mailbox/set` calls (the client's `create_mailbox` sends no
    `parentId`), so the half-states are real. The better half is chosen: a
    usable label at the top level, and a message saying where it went.
    """
    client = FakeClient()

    async def refuse(mailbox_id, patch):
        raise JmapError("Mailbox/set update failed: {'type': 'forbidden'}")

    client.update_mailbox = refuse  # type: ignore[method-assign]
    with pytest.raises(service.LabelError) as caught:
        await service.create_label(client, "Receipts 2026", parent_id="m-work")
    assert "top level" in str(caught.value)
    assert "Receipts 2026" in {m.name for m in client.mailboxes}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Work  ", "Work"),
        ("Work   stuff", "Work stuff"),
        ("Legal/Finance", "Legal/Finance"),
    ],
)
def test_clean_name_normalises_without_changing_meaning(raw, expected):
    assert service.clean_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "Work\nstuff", "x" * (service.MAX_NAME + 1)])
def test_clean_name_refuses_what_it_cannot_normalise(raw):
    with pytest.raises(service.LabelError):
        service.clean_name(raw)


def test_clean_name_folds_two_spellings_of_the_same_accented_name():
    """NFC first, so a decomposed "Café" and a composed one are one name and
    the server's own duplicate check can see them as such.
    """
    assert service.clean_name("Café") == service.clean_name("Café")


# ---------------------------------------------------------------------------
# 5. "Show if unread" is computed, and costs nothing extra
# ---------------------------------------------------------------------------


async def test_the_nav_reads_every_unread_count_from_one_mailbox_get():
    """ "Show if unread" is a predicate over a count the account's single
    `Mailbox/get` already carried. A hundred such labels must still be one
    request — the moment this needs a count *per label*, the nav has become
    an N+1 on the hot path every page render pays for.
    """
    mailboxes = _default_mailboxes()
    for n in range(100):
        mailboxes.append(_mailbox(f"m{n}", f"Label {n}", unread=n % 2))
    client = FakeClient(mailboxes=mailboxes)
    meta = {f"m{n}": LabelMeta(visibility="show_if_unread") for n in range(100)}

    nav = await _nav(client, meta)

    assert len(client.named("get_mailboxes")) == 1
    assert client.named("query_search") == []
    assert client.named("get_email_states") == []
    # ...and the predicate is a pure function of the node it is handed.
    quiet = [n for n in nav.labels if n.visibility == "show_if_unread" and n.count == 0]
    assert quiet and all(hidden_in_nav(node) for node in quiet)
    loud = [n for n in nav.labels if n.visibility == "show_if_unread" and n.count > 0]
    assert loud and not any(hidden_in_nav(node) for node in loud)


def test_selection_state_is_tri_state():
    """Gmail's own answer, and the reason it matters: with three
    conversations selected and one filed under Work, a plain checked box
    would claim something untrue.
    """
    from mailosh.jmap.client import EmailState

    def state(mailboxes):
        return EmailState(
            id="e", thread_id="t", mailbox_ids=frozenset(mailboxes), keywords=frozenset()
        )

    all_on = [state({"m-work"}), state({"m-work", "mb-inbox"})]
    some = [state({"m-work"}), state({"mb-inbox"})]
    none = [state({"mb-inbox"})]
    assert service.selection_state(all_on, "m-work") == "on"
    assert service.selection_state(some, "m-work") == "mixed"
    assert service.selection_state(none, "m-work") == "off"
    assert service.selection_state([], "m-work") == "off"
