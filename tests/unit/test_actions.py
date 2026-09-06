"""Unit tests for mail actions — archive/delete/spam/star/read plus undo
(Task 9, design spec §6.3) — at three levels:

1. `mailosh.services.actions` against `FakeClient`, which records every
   `Email/set`-shaped call *and* applies it to its own in-memory state, so an
   undo test can genuinely restore what the forward action changed.
2. `mailosh.web.actions`'s router mounted on a bare `FastAPI()` (never
   `create_app` — another task owns `mailosh/web/app.py`), with
   `deps.require_session`/`deps.client_for` overridden. The real
   `deps.csrf_protect` is left in place so its 403 is actually exercised.
3. The two `JmapClient` methods Task 9 adds (`get_email_states`,
   `set_mailboxes_patch`), against respx via `tests/conftest.py`'s `client`
   fixture — the only place the exact JMAP wire body is asserted.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import EMPTY_SET_RESPONSE, NOT_UPDATED_RESPONSE, make_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mailosh.jmap.client import EmailState, QueryPage
from mailosh.jmap.errors import JmapError
from mailosh.jmap.models import EmailHeader, Mailbox
from mailosh.services import actions
from mailosh.services.mailbox_tree import build_nav
from mailosh.services.undo import UndoSpec, sign, verify
from mailosh.web import deps
from mailosh.web.actions import MAX_TRIGGER_BYTES
from mailosh.web.actions import router as actions_router

SECRET = "test-secret-key-not-for-production-use!!"
CSRF = "csrf-token-for-tests"
ACCOUNT = "acct-a"


def _long_ids(count: int, length: int = 32, seed: int = 7) -> list[str]:
    """`count` distinct, high-entropy ids of exactly `length` characters.

    Deterministic (fixed seed) so header-size assertions are stable, and
    pseudo-random rather than sequential because a shared prefix would deflate
    away inside the undo token and make those assertions pass for the wrong
    reason.
    """
    rnd = random.Random(seed)
    alphabet = string.ascii_lowercase + string.digits
    return ["".join(rnd.choices(alphabet, k=length)) for _ in range(count)]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _mailboxes(*, archive: bool = True) -> list[Mailbox]:
    """A realistic role set: Inbox, Archive, Spam (role "junk"), Trash,
    Drafts, plus one user label ("Work") with no role at all.
    """
    boxes = [
        Mailbox(
            id="mb-inbox",
            name="Inbox",
            role="inbox",
            sort_order=10,
            total_emails=9,
            unread_emails=3,
        ),
        Mailbox(
            id="mb-drafts",
            name="Drafts",
            role="drafts",
            sort_order=30,
            total_emails=2,
            unread_emails=0,
        ),
        Mailbox(
            id="mb-junk", name="Spam", role="junk", sort_order=60, total_emails=0, unread_emails=0
        ),
        Mailbox(
            id="mb-trash",
            name="Trash",
            role="trash",
            sort_order=70,
            total_emails=1,
            unread_emails=0,
        ),
        Mailbox(
            id="m-work", name="Work", role=None, sort_order=80, total_emails=4, unread_emails=1
        ),
    ]
    if archive:
        boxes.append(
            Mailbox(
                id="mb-archive",
                name="Archive",
                role="archive",
                sort_order=50,
                total_emails=7,
                unread_emails=0,
            )
        )
    return boxes


def _email(thread: str, mailboxes: set[str], *, read: bool = False, flagged: bool = False) -> dict:
    keywords = set()
    if read:
        keywords.add("$seen")
    if flagged:
        keywords.add("$flagged")
    return {"thread": thread, "mailboxes": set(mailboxes), "keywords": keywords}


class FakeClient:
    """Stands in for `JmapClient`. Records every call and mutates its own
    state, so a forward action followed by `undo.apply` is a real round trip.
    """

    def __init__(
        self,
        emails: dict[str, dict],
        mailboxes: list[Mailbox] | None = None,
        *,
        account: str = ACCOUNT,
    ) -> None:
        self.emails = emails
        self.mailboxes = _mailboxes() if mailboxes is None else mailboxes
        self.calls: list[str] = []
        self.patch_calls: list[tuple[dict, dict | None]] = []
        self.keyword_calls: list[tuple[list[str], str, bool]] = []
        self.creates: list[tuple[str, str | None]] = []
        #: Set to make every `Mailbox/set` create fail, for the "the folder
        #: cannot be made" path.
        self.create_error: JmapError | None = None
        self.destroyed: list[str] = []
        self.query_calls: list[tuple[str, int, int]] = []
        self._account = account

    @property
    def account_id(self) -> str:
        return self._account

    async def get_mailboxes(self) -> list[Mailbox]:
        self.calls.append("get_mailboxes")
        await asyncio.sleep(0)
        return list(self.mailboxes)

    async def create_mailbox(self, name: str, *, role: str | None = None) -> str:
        """A compliant `Mailbox/set` create: RFC 8621 §2 makes a role unique
        within an account, so a second create for a role already in use is
        rejected rather than quietly duplicated.

        `sleep(0)` here and in `get_mailboxes` are what let `asyncio.gather`
        actually interleave two archives; without a suspension point the
        first would run to completion before the second began, and a
        "concurrent" test would prove nothing.
        """
        self.calls.append("create_mailbox")
        self.creates.append((name, role))
        await asyncio.sleep(0)
        if self.create_error is not None:
            raise self.create_error
        if role is not None and any(m.role == role for m in self.mailboxes):
            raise JmapError(f"Mailbox/set create failed: role {role!r} is already in use")
        created = Mailbox(
            id=f"mb-made-{len(self.mailboxes)}",
            name=name,
            role=role,
            sort_order=0,
            total_emails=0,
            unread_emails=0,
        )
        self.mailboxes.append(created)
        return created.id

    async def get_email_states(self, email_ids: list[str]) -> list[EmailState]:
        self.calls.append("get_email_states")
        out = []
        # Mirrors the real method: one state per *distinct* id, request order
        # kept, `notFound` dropped.
        for email_id in dict.fromkeys(email_ids):
            row = self.emails.get(email_id)
            if row is None:  # the server's `notFound`
                continue
            out.append(
                EmailState(
                    id=email_id,
                    thread_id=row["thread"],
                    mailbox_ids=frozenset(row["mailboxes"]),
                    keywords=frozenset(row["keywords"]),
                )
            )
        return out

    async def set_mailboxes_patch(self, patches, *, keywords=None) -> None:
        self.calls.append("set_mailboxes_patch")
        self.patch_calls.append(
            ({k: dict(v) for k, v in patches.items()}, dict(keywords) if keywords else None)
        )
        for email_id, patch in patches.items():
            row = self.emails[email_id]
            for mailbox_id, on in patch.items():
                if on:
                    row["mailboxes"].add(mailbox_id)
                else:
                    row["mailboxes"].discard(mailbox_id)
            for keyword, on in (keywords or {}).items():
                row["keywords"].add(keyword) if on else row["keywords"].discard(keyword)

    async def set_keywords(self, email_ids: list[str], keyword: str, on: bool) -> None:
        self.calls.append("set_keywords")
        self.keyword_calls.append((list(email_ids), keyword, on))
        for email_id in email_ids:
            row = self.emails[email_id]
            row["keywords"].add(keyword) if on else row["keywords"].discard(keyword)

    async def destroy_emails(self, email_ids) -> None:
        """`Email/set` destroy: the rows really go, so a later snapshot cannot
        find them — which is what the "not undoable" tests rely on."""
        self.calls.append("destroy_emails")
        self.destroyed.extend(email_ids)
        for email_id in email_ids:
            self.emails.pop(email_id, None)

    async def query_page(self, *, mailbox_id, position, limit, **_ignored) -> QueryPage:
        """A thread-collapsed page of `mailbox_id`, shaped the way the real
        chain answers: every message of every matching thread comes back,
        including members sitting in *other* mailboxes, so `empty_mailbox`
        has to do its own membership check."""
        self.calls.append("query_page")
        self.query_calls.append((mailbox_id, position, limit))
        threads: dict[str, list[EmailHeader]] = {}
        for email_id, row in self.emails.items():
            if mailbox_id in row["mailboxes"]:
                threads.setdefault(row["thread"], [])
        for email_id, row in self.emails.items():
            if row["thread"] in threads:
                threads[row["thread"]].append(
                    EmailHeader(
                        id=email_id,
                        threadId=row["thread"],
                        mailboxIds=set(row["mailboxes"]),
                        keywords=set(row["keywords"]),
                        receivedAt=datetime(2026, 9, 1, tzinfo=UTC),
                        hasAttachment=False,
                    )
                )
        order = list(threads)[position : position + limit]
        return QueryPage(
            thread_order=order,
            total=len(threads),
            emails_by_thread={tid: threads[tid] for tid in order},
            position=position,
        )


async def _nav(fake: FakeClient):
    return await build_nav(fake, active_key="inbox", label_meta={})


# ---------------------------------------------------------------------------
# services/actions.py
# ---------------------------------------------------------------------------


async def test_archive_removes_inbox_and_falls_back_to_archive_mailbox():
    fake = FakeClient(
        {
            "e1": _email("t1", {"mb-inbox"}),  # only home is the inbox
            "e2": _email("t2", {"mb-inbox", "m-work"}),  # still has Work to live in
        }
    )
    result = await actions.archive(fake, await _nav(fake), ["e1", "e2"])

    assert fake.patch_calls == [
        ({"e1": {"mb-archive": True, "mb-inbox": None}, "e2": {"mb-inbox": None}}, None)
    ]
    assert fake.emails["e1"]["mailboxes"] == {"mb-archive"}
    assert fake.emails["e2"]["mailboxes"] == {"m-work"}
    assert result.spec.kind == "archive"
    assert result.spec.toast == "Archived"
    assert result.spec.prev == {"e1": ["mb-inbox"], "e2": ["m-work", "mb-inbox"]}
    assert result.removed == ["t1", "t2"]
    assert result.counts == {"inbox": -2}


async def test_archive_creates_the_archive_mailbox_when_the_account_has_none():
    # Stalwart provisions Inbox/Deleted Items/Junk Mail/Drafts/Sent Items --
    # no Archive -- so this is the ordinary state of a fresh self-hosted
    # account, not an exotic one. Archiving an Inbox-only message used to
    # 500 here.
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})}, mailboxes=_mailboxes(archive=False))
    result = await actions.archive(fake, await _nav(fake), ["e1"])

    assert fake.creates == [("Archive", "archive")]
    made = [m for m in fake.mailboxes if m.role == "archive"]
    assert len(made) == 1
    assert fake.patch_calls == [({"e1": {made[0].id: True, "mb-inbox": None}}, None)]
    assert fake.emails["e1"]["mailboxes"] == {made[0].id}
    assert result.spec.prev == {"e1": ["mb-inbox"]}
    assert result.removed == ["t1"]


async def test_a_second_archive_reuses_the_mailbox_the_first_one_made():
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox"})},
        mailboxes=_mailboxes(archive=False),
    )
    await actions.archive(fake, await _nav(fake), ["e1"])
    # A fresh nav, as the route builds one per request: it now resolves the
    # folder by role like any other, so nothing is created a second time.
    await actions.archive(fake, await _nav(fake), ["e2"])

    assert len(fake.creates) == 1
    assert len([m for m in fake.mailboxes if m.role == "archive"]) == 1
    assert fake.emails["e1"]["mailboxes"] == fake.emails["e2"]["mailboxes"]


async def test_archive_creates_nothing_when_every_message_has_somewhere_to_live():
    # Creation is scoped to the messages that actually need it. An account
    # whose mail is all labelled never grows an Archive folder it would
    # never use -- and no GET, and no nav render, can ever create one.
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox", "m-work"})}, mailboxes=_mailboxes(archive=False)
    )
    result = await actions.archive(fake, await _nav(fake), ["e1"])

    assert fake.creates == []
    assert [m for m in fake.mailboxes if m.role == "archive"] == []
    assert fake.emails["e1"]["mailboxes"] == {"m-work"}
    assert result.counts == {"inbox": -1}


async def test_two_concurrent_archives_share_one_new_archive_mailbox():
    # Two requests, each holding a nav built before either had run, both
    # needing the folder. The wrong outcome is a *pair* of Archive folders
    # splitting the account's archived mail between them.
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox"})},
        mailboxes=_mailboxes(archive=False),
    )
    nav = await _nav(fake)
    await asyncio.gather(actions.archive(fake, nav, ["e1"]), actions.archive(fake, nav, ["e2"]))

    made = [m.id for m in fake.mailboxes if m.role == "archive"]
    assert len(made) == 1
    assert fake.emails["e1"]["mailboxes"] == {made[0]}
    assert fake.emails["e2"]["mailboxes"] == {made[0]}


async def test_archive_never_leaves_a_message_homeless():
    # The guarantee that outlives the fallback (RFC 8621 §4.1: a message is
    # in at least one mailbox). If the Archive folder can be neither
    # resolved nor created, the whole action fails *before* the single
    # `Email/set` -- never half-applied, and never with an emptied
    # `mailboxIds`.
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox", "m-work"})},
        mailboxes=_mailboxes(archive=False),
    )
    fake.create_error = JmapError("Mailbox/set create failed: {'type': 'forbidden'}")
    with pytest.raises(JmapError, match="forbidden"):
        await actions.archive(fake, await _nav(fake), ["e1", "e2"])

    assert fake.patch_calls == []
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}
    assert fake.emails["e2"]["mailboxes"] == {"mb-inbox", "m-work"}


async def test_no_write_may_empty_a_message_s_mailboxes():
    # Enforced at the single write choke point rather than per action, so a
    # future action cannot reintroduce the hole by forgetting the rule.
    # Reached here by hand because no real action can construct it.
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    state = (await fake.get_email_states(["e1"]))[0]
    homeless = actions._Change(before=state, after=frozenset())
    with pytest.raises(JmapError, match="no mailbox at all"):
        await actions._write(fake, [homeless])
    assert fake.patch_calls == []


async def test_archive_skips_messages_already_out_of_the_inbox():
    fake = FakeClient({"e1": _email("t1", {"m-work"}), "e2": _email("t2", {"mb-inbox"})})
    result = await actions.archive(fake, await _nav(fake), ["e1", "e2"])

    # e1 needs no patch at all, and must not be added to Archive.
    assert fake.patch_calls == [({"e2": {"mb-archive": True, "mb-inbox": None}}, None)]
    assert result.spec.email_ids == ["e2"]
    # ...but its row still leaves the inbox list.
    assert result.removed == ["t1", "t2"]


async def test_archive_ignores_ids_the_server_does_not_know():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    result = await actions.archive(fake, await _nav(fake), ["e1", "gone"])
    assert list(fake.patch_calls[0][0]) == ["e1"]
    assert result.spec.email_ids == ["e1"]


async def test_delete_captures_prev_and_moves_everything_to_trash():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox", "m-work"}, read=True)})
    result = await actions.delete(fake, await _nav(fake), ["e1"])

    assert fake.patch_calls == [
        ({"e1": {"mb-trash": True, "m-work": None, "mb-inbox": None}}, None)
    ]
    assert fake.emails["e1"]["mailboxes"] == {"mb-trash"}
    assert result.spec.prev == {"e1": ["m-work", "mb-inbox"]}
    assert result.spec.toast == "Deleted"
    assert result.counts == {}  # it was already read: the Inbox badge doesn't move


async def test_delete_of_a_draft_moves_the_drafts_total_badge():
    fake = FakeClient({"e1": _email("t1", {"mb-drafts"}, read=True)})
    result = await actions.delete(fake, await _nav(fake), ["e1"])
    assert result.counts == {"drafts": -1}


async def test_spam_sets_mailbox_and_keyword_in_one_email_set():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    result = await actions.spam(fake, await _nav(fake), ["e1"])

    assert fake.patch_calls == [({"e1": {"mb-junk": True, "mb-inbox": None}}, {"$junk": True})]
    assert fake.calls.count("set_mailboxes_patch") == 1
    assert fake.calls.count("set_keywords") == 0
    assert fake.emails["e1"]["keywords"] == {"$junk"}
    assert (result.spec.keyword, result.spec.on) == ("$junk", True)
    assert result.counts == {"inbox": -1}


async def test_star_toggles_flagged_only_where_it_differs():
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox"}, flagged=True)}
    )
    result = await actions.star(fake, await _nav(fake), ["e1", "e2"], on=True)

    assert fake.keyword_calls == [(["e1"], "$flagged", True)]
    assert fake.patch_calls == []  # star never touches mailboxes
    assert result.spec.email_ids == ["e1"]
    assert result.spec.prev == {}
    assert (result.removed, result.counts) == ([], {})
    assert result.spec.toast == "Starred"


async def test_unstar_reverses_and_names_itself():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}, flagged=True)})
    result = await actions.star(fake, await _nav(fake), ["e1"], on=False)
    assert fake.keyword_calls == [(["e1"], "$flagged", False)]
    assert result.spec.toast == "Unstarred"


async def test_mark_read_moves_the_inbox_unread_badge_both_ways():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox"})})
    result = await actions.mark_read(fake, await _nav(fake), ["e1", "e2"], on=True)
    assert fake.keyword_calls == [(["e1", "e2"], "$seen", True)]
    assert result.counts == {"inbox": -2}
    assert result.spec.toast == "Marked as read"

    back = await actions.mark_read(fake, await _nav(fake), ["e1", "e2"], on=False)
    assert back.counts == {"inbox": 2}
    assert back.spec.toast == "Marked as unread"


async def test_counts_only_track_unread_messages_actually_in_the_inbox():
    fake = FakeClient(
        {
            "e1": _email("t1", {"mb-inbox"}, read=True),  # read: badge unaffected
            "e2": _email("t1", {"mb-inbox"}),  # unread, in the inbox: -1
            "e3": _email("t1", {"m-work"}),  # unread but filed under Work only
        }
    )
    result = await actions.archive(fake, await _nav(fake), ["e1", "e2", "e3"])
    assert result.counts == {"inbox": -1}
    assert result.removed == ["t1"]  # one thread, three messages, one row


async def test_no_op_action_makes_no_jmap_write():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}, read=True)})
    result = await actions.mark_read(fake, await _nav(fake), ["e1"], on=True)
    assert fake.keyword_calls == []
    assert result.spec.email_ids == []


async def test_result_asserts_that_moved_implies_changed():
    # No real `_Change` can violate this — `changed` is literally `moved or
    # flagged_change` — which is exactly why it is checked here rather than
    # trusted: this is the one place `email_ids` and `prev` are built from
    # the same list, so a future action that ever broke the invariant must
    # fail loudly in testing here, not as a 500 (plus a misleading "revert"
    # toast) on the response path — `mailosh.services.undo.sign` runs after
    # the write this spec describes has already committed, and no longer
    # re-checks this itself (see its `_encode`'s docstring).
    nav = await _nav(FakeClient({}))
    bogus = SimpleNamespace(
        before=SimpleNamespace(id="e1", mailbox_ids=frozenset(), thread_id="t1"),
        after=frozenset(),
        moved=True,
        changed=False,
        unread_before=False,
        unread_after=False,
    )
    with pytest.raises(AssertionError):
        actions._result("archive", "Archived", nav, [bogus], removes_rows=True)


# ---------------------------------------------------------------------------
# undo.apply
# ---------------------------------------------------------------------------


async def test_undo_restores_previous_mailboxes_exactly():
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox", "m-work"})}
    )
    result = await actions.archive(fake, await _nav(fake), ["e1", "e2"])
    fake.patch_calls.clear()

    await actions.apply_undo(fake, result.spec)

    assert fake.patch_calls == [
        ({"e1": {"mb-inbox": True, "mb-archive": None}, "e2": {"mb-inbox": True}}, None)
    ]
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}
    assert fake.emails["e2"]["mailboxes"] == {"mb-inbox", "m-work"}


async def test_undo_of_delete_restores_every_previous_mailbox():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox", "m-work"})})
    result = await actions.delete(fake, await _nav(fake), ["e1"])
    await actions.apply_undo(fake, result.spec)
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox", "m-work"}


async def test_undo_of_spam_restores_mailboxes_and_clears_the_keyword_in_one_call():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    result = await actions.spam(fake, await _nav(fake), ["e1"])
    fake.patch_calls.clear()
    fake.keyword_calls.clear()

    await actions.apply_undo(fake, result.spec)

    assert fake.patch_calls == [({"e1": {"mb-inbox": True, "mb-junk": None}}, {"$junk": False})]
    assert fake.keyword_calls == []
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}
    assert fake.emails["e1"]["keywords"] == set()


async def test_undo_of_a_keyword_action_never_touches_mailboxes():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    result = await actions.star(fake, await _nav(fake), ["e1"], on=True)
    fake.calls.clear()
    fake.keyword_calls.clear()

    await actions.apply_undo(fake, result.spec)

    assert fake.patch_calls == []
    assert "get_email_states" not in fake.calls  # nothing to restore: no snapshot needed
    assert fake.keyword_calls == [(["e1"], "$flagged", False)]
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}


async def test_undo_never_empties_a_mailbox_set():
    fake = FakeClient({"e1": _email("t1", {"mb-archive"})})
    spec = UndoSpec(
        kind="archive", email_ids=["e1"], prev={"e1": []}, keyword=None, on=None, toast="Archived"
    )
    await actions.apply_undo(fake, spec)
    assert fake.patch_calls == []
    assert fake.emails["e1"]["mailboxes"] == {"mb-archive"}


# ---------------------------------------------------------------------------
# web/actions.py — routes on a bare FastAPI()
# ---------------------------------------------------------------------------


def _make_app(fake: FakeClient) -> FastAPI:
    app = FastAPI()
    app.include_router(actions_router)
    app.state.settings = make_settings("sqlite+aiosqlite:///:memory:", secret_key=SECRET)
    app.dependency_overrides[deps.require_session] = lambda: SimpleNamespace(csrf_token=CSRF)
    app.dependency_overrides[deps.client_for] = lambda: fake
    return app


def _post(app: FastAPI, url: str, data: dict, *, csrf: bool = True):
    headers = {"X-CSRF-Token": CSRF} if csrf else {}
    return TestClient(app).post(url, data=data, headers=headers)


def _trigger(response, name: str = "om:done") -> dict:
    return json.loads(response.headers["HX-Trigger"])[name]


def test_archive_route_returns_204_with_the_done_trigger():
    fake = FakeClient(
        {"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox", "m-work"})}
    )
    response = _post(_make_app(fake), "/a/archive", {"ids": ["e1", "e2"]})

    assert response.status_code == 204
    assert response.content == b""
    done = _trigger(response)
    assert done["toast"] == "Archived"
    assert done["removed"] == ["t1", "t2"]
    assert done["counts"] == {"inbox": -2}
    assert verify(done["undo"], SECRET, scope=ACCOUNT).kind == "archive"


def test_star_and_read_routes_take_the_on_flag():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}, flagged=True)})
    app = _make_app(fake)
    assert _post(app, "/a/star", {"ids": ["e1"], "on": "0"}).status_code == 204
    assert fake.emails["e1"]["keywords"] == set()
    assert _post(app, "/a/read", {"ids": ["e1"], "on": "1"}).status_code == 204
    assert fake.emails["e1"]["keywords"] == {"$seen"}


def test_delete_and_spam_routes_report_their_own_toasts():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox"})})
    app = _make_app(fake)
    assert _trigger(_post(app, "/a/delete", {"ids": ["e1"]}))["toast"] == "Deleted"
    assert _trigger(_post(app, "/a/spam", {"ids": ["e2"]}))["toast"] == "Reported spam"


def test_undo_route_reverses_the_action():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    app = _make_app(fake)
    token = _trigger(_post(app, "/a/archive", {"ids": ["e1"]}))["undo"]
    assert fake.emails["e1"]["mailboxes"] == {"mb-archive"}

    response = _post(app, "/a/undo", {"token": token})

    assert response.status_code == 204
    assert _trigger(response) == {"toast": "Undone", "refresh": True}
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}


def test_undo_rejects_a_token_minted_for_another_account():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    spec = UndoSpec(
        kind="archive",
        email_ids=["e1"],
        prev={"e1": ["mb-inbox"]},
        keyword=None,
        on=None,
        toast="Archived",
    )
    foreign = sign(spec, SECRET, scope="somebody-else")
    response = _post(_make_app(fake), "/a/undo", {"token": foreign})
    assert response.status_code == 400
    assert fake.patch_calls == []


def test_actions_require_csrf():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    app = _make_app(fake)
    assert _post(app, "/a/archive", {"ids": ["e1"]}, csrf=False).status_code == 403
    assert (
        TestClient(app)
        .post("/a/archive", data={"ids": ["e1"]}, headers={"X-CSRF-Token": "wrong"})
        .status_code
        == 403
    )
    assert fake.calls == []


def test_bulk_over_100_requires_confirm():
    ids = [f"e{n}" for n in range(101)]
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in ids})
    app = _make_app(fake)

    response = _post(app, "/a/archive", {"ids": ids})

    assert response.status_code == 409
    confirm = _trigger(response, "om:confirm")
    assert confirm["kind"] == "archive" and confirm["count"] == 101
    assert "101" in confirm["message"]
    assert fake.calls == []  # nothing happened, not even a read

    ok = _post(app, "/a/archive", {"ids": ids, "confirm": "1"})
    assert ok.status_code == 204
    assert len(fake.patch_calls[0][0]) == 101


def test_exactly_100_ids_need_no_confirmation():
    ids = [f"e{n}" for n in range(100)]
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in ids})
    assert _post(_make_app(fake), "/a/archive", {"ids": ids}).status_code == 204


def test_confirmation_counts_distinct_ids_not_raw_posts():
    # 101 copies of one id is one message repeated, not 101 messages — the
    # `Email/set` this guards collapses the duplicate one layer down, so the
    # decision to ask at all must match what will actually happen.
    ids = ["e1"] * 101
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    assert _post(_make_app(fake), "/a/archive", {"ids": ids}).status_code == 204


def test_confirmation_count_reflects_distinct_ids_when_still_over_the_line():
    # 102 raw ids but only 101 distinct: still needs confirming, but for 101
    # messages — what the write will actually touch — not the 102 posted.
    ids = [f"e{n}" for n in range(101)] + ["e0"]
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in set(ids)})

    response = _post(_make_app(fake), "/a/archive", {"ids": ids})

    assert response.status_code == 409
    confirm = _trigger(response, "om:confirm")
    assert confirm["count"] == 101
    assert "101" in confirm["message"] and "102" not in confirm["message"]


def test_undo_survives_a_realistically_large_selection():
    # The regression the compact undo encoding exists for: 100 messages, each
    # with a 32-character id, two mailboxes and its own 32-character thread id.
    # Undo has to still be there, and the whole header has to fit the budget.
    ids = _long_ids(100)
    threads = _long_ids(100, seed=8)
    fake = FakeClient(
        {
            email_id: _email(thread_id, {"mb-inbox", "m-work"})
            for email_id, thread_id in zip(ids, threads, strict=True)
        }
    )

    response = _post(_make_app(fake), "/a/archive", {"ids": ids, "confirm": "1"})

    assert response.status_code == 204
    header = response.headers["HX-Trigger"]
    assert len(header) <= MAX_TRIGGER_BYTES
    done = _trigger(response)
    assert done["undo"] is not None
    # The row list is what gives way first — it is recoverable by re-fetching.
    assert (done["removed"], done["refresh"]) == ([], True)
    spec = verify(done["undo"], SECRET, scope=ACCOUNT)
    assert spec.email_ids == ids
    assert spec.prev[ids[0]] == ["m-work", "mb-inbox"]  # restored exactly, not approximately


def test_a_selection_too_big_for_any_undo_says_so():
    # Far past the point where a token can fit: the action still commits, and
    # the payload explains the missing button rather than sending a bare null.
    ids = _long_ids(600)
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in ids})

    response = _post(_make_app(fake), "/a/archive", {"ids": ids, "confirm": "1"})

    assert response.status_code == 204
    done = _trigger(response)
    assert done["undo"] is None
    assert done["undo_unavailable"] == "too_many"
    assert done["counts"] == {"inbox": -600}
    assert len(response.headers["HX-Trigger"]) <= MAX_TRIGGER_BYTES
    assert len(fake.patch_calls) == 1  # ...and still exactly one Email/set


def test_star_shedding_never_adds_a_pointless_refresh():
    # `removed` is always `[]` for star (no row it touches ever leaves the
    # list), so shedding step 1 buys nothing — it must not still tack on
    # `refresh: true` when the undo token is what actually has to give.
    ids = _long_ids(118)  # measured: the smallest count that forces a shed
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in ids})

    response = _post(_make_app(fake), "/a/star", {"ids": ids, "confirm": "1"})

    assert response.status_code == 204
    done = _trigger(response)
    assert done["undo"] is None
    assert done["undo_unavailable"] == "too_many"
    assert done["removed"] == []
    assert "refresh" not in done  # nothing to refresh: no rows ever left the list


def test_an_action_that_changed_nothing_says_why_there_is_no_undo():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}, read=True)})
    done = _trigger(_post(_make_app(fake), "/a/read", {"ids": ["e1"], "on": "1"}))
    assert (done["undo"], done["undo_unavailable"]) == (None, "no_change")


def test_a_duplicated_id_moves_the_badge_once():
    # A selection built from overlapping rows can repeat an id; the `Email/set`
    # collapses the duplicate, so the count must too or it drifts from the
    # mailbox it claims to describe.
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})

    done = _trigger(_post(_make_app(fake), "/a/archive", {"ids": ["e1", "e1"]}))

    assert done["counts"] == {"inbox": -1}
    assert done["removed"] == ["t1"]
    assert verify(done["undo"], SECRET, scope=ACCOUNT).email_ids == ["e1"]


async def test_duplicate_ids_are_collapsed_before_anything_reads_them():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}), "e2": _email("t2", {"mb-inbox"})})
    result = await actions.mark_read(fake, await _nav(fake), ["e1", "e2", "e1"], on=True)
    assert fake.keyword_calls == [(["e1", "e2"], "$seen", True)]
    assert result.spec.email_ids == ["e1", "e2"]
    assert result.counts == {"inbox": -2}


def test_jmap_failures_propagate_rather_than_becoming_a_toast():
    class Boom(FakeClient):
        async def set_mailboxes_patch(self, patches, *, keywords=None):
            raise JmapError("Email/set update failed for 'e1': notFound")

    fake = Boom({"e1": _email("t1", {"mb-inbox"})})
    with pytest.raises(JmapError):
        _post(_make_app(fake), "/a/archive", {"ids": ["e1"]})


def test_routes_are_all_post_only():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    paths = {route.path: route.methods for route in actions_router.routes}
    assert set(paths) == {
        "/a/archive",
        "/a/delete",
        "/a/spam",
        "/a/star",
        "/a/read",
        "/a/undo",
        # Trash and Spam's four: the same POST-only, CSRF-checked shape.
        "/a/restore",
        "/a/unspam",
        "/a/destroy",
        "/a/empty",
    }
    assert all(methods == {"POST"} for methods in paths.values())
    assert TestClient(_make_app(fake)).get("/a/archive").status_code == 405


# ---------------------------------------------------------------------------
# The `HX-Trigger` shapes, as the browser actually reads them
#
# The tests above assert what each field *says*. These assert which fields
# exist at all, because that is the part the client branches on and the part
# the header budget changes: shedding does not blank a value, it removes the
# key and adds others. `static/js/actions.js` reads every one of them
# defensively for exactly this reason, and these are the four (five, with
# undo's own reply) shapes it is written against.
# ---------------------------------------------------------------------------


def test_shape_undo_kept_leaves_out_undo_unavailable_entirely():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})

    done = _trigger(_post(_make_app(fake), "/a/archive", {"ids": ["e1"]}))

    assert sorted(done) == ["counts", "removed", "toast", "undo"]
    # The asymmetry a client is most likely to get wrong: `undo` is always
    # present (sometimes null), `undo_unavailable` is absent whenever there
    # is an undo. `payload.undo_unavailable === null` would never be true.
    assert "undo_unavailable" not in done


def test_shape_rows_shed_trades_removed_for_refresh_and_keeps_undo():
    ids = _long_ids(100)
    threads = _long_ids(100, seed=8)
    fake = FakeClient(
        {
            email_id: _email(thread_id, {"mb-inbox", "m-work"})
            for email_id, thread_id in zip(ids, threads, strict=True)
        }
    )

    done = _trigger(_post(_make_app(fake), "/a/archive", {"ids": ids, "confirm": "1"}))

    assert sorted(done) == ["counts", "refresh", "removed", "toast", "undo"]
    assert (done["removed"], done["refresh"]) == ([], True)
    assert done["undo"] is not None
    assert "undo_unavailable" not in done


def test_shape_undo_shed_says_why_and_still_asks_for_a_refresh():
    ids = _long_ids(600)
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in ids})

    done = _trigger(_post(_make_app(fake), "/a/archive", {"ids": ids, "confirm": "1"}))

    assert sorted(done) == ["counts", "refresh", "removed", "toast", "undo", "undo_unavailable"]
    assert (done["undo"], done["undo_unavailable"]) == (None, "too_many")


def test_shape_nothing_changed_contradicts_the_toast_beside_it():
    # The reason the client explains only `"too_many"`. Starring a message
    # that is already starred SUCCEEDS — the star is on, which is what was
    # asked for — and reports both "Starred" and "there is nothing to
    # undo". A client that surfaced `undo_unavailable` whenever it arrived
    # would tell the reader their successful action had failed at
    # something, on the one action where nothing went wrong at all.
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"}, flagged=True)})

    done = _trigger(_post(_make_app(fake), "/a/star", {"ids": ["e1"], "on": "1"}))

    assert sorted(done) == ["counts", "removed", "toast", "undo", "undo_unavailable"]
    assert done["toast"] == "Starred"
    assert (done["undo"], done["undo_unavailable"]) == (None, "no_change")


def test_shape_of_the_confirmation_is_the_question_and_the_client_re_posts_it():
    ids = [f"e{n}" for n in range(101)]
    fake = FakeClient({email_id: _email("t1", {"mb-inbox"}) for email_id in ids})
    app = _make_app(fake)

    asked = _post(app, "/a/archive", {"ids": ids})

    assert asked.status_code == 409
    assert sorted(_trigger(asked, "om:confirm")) == ["count", "kind", "message"]
    # No `om:done` rides along: a 409 is a question, not a half-result, and
    # a client that toasted whatever it found would announce an archive
    # that has not happened.
    assert "om:done" not in json.loads(asked.headers["HX-Trigger"])

    # The client keeps the selection (the ids are deliberately not echoed
    # back) and re-posts the identical request with `confirm=1`.
    agreed = _post(app, "/a/archive", {"ids": ids, "confirm": "1"})

    assert agreed.status_code == 204
    assert _trigger(agreed)["toast"] == "Archived"


def test_shape_of_undos_own_reply_is_narrower_than_every_other():
    # No `undo`, no `removed`, no `counts` — three keys the client reads on
    # every other reply are simply not here, which is why it defaults every
    # one of them instead of indexing.
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    app = _make_app(fake)
    token = _trigger(_post(app, "/a/archive", {"ids": ["e1"]}))["undo"]

    done = _trigger(_post(app, "/a/undo", {"token": token}))

    assert sorted(done) == ["refresh", "toast"]
    assert (done["toast"], done["refresh"]) == ("Undone", True)


# ---------------------------------------------------------------------------
# jmap/client.py — the exact wire bodies (respx)
# ---------------------------------------------------------------------------

_STATES_RESPONSE = {
    "methodResponses": [
        [
            "Email/get",
            {
                "accountId": "c",
                "state": "s1",
                "list": [
                    {
                        "id": "e2",
                        "threadId": "t2",
                        "mailboxIds": {"mb-inbox": True, "m-work": True},
                        "keywords": {"$seen": True},
                    },
                    {
                        "id": "e1",
                        "threadId": "t1",
                        "mailboxIds": {"mb-inbox": True},
                        "keywords": {},
                    },
                ],
                "notFound": ["gone"],
            },
            "e0",
        ]
    ],
    "sessionState": "s1",
}


async def test_get_email_states_asks_for_four_properties_and_keeps_request_order(client, api_mock):
    api_mock.respond(json=_STATES_RESPONSE)
    states = await client.get_email_states(["e1", "e2", "gone"])

    sent = json.loads(api_mock.calls.last.request.content)["methodCalls"]
    assert sent[0][0] == "Email/get"
    assert sent[0][1]["ids"] == ["e1", "e2", "gone"]
    assert sent[0][1]["properties"] == ["id", "threadId", "mailboxIds", "keywords"]
    assert [s.id for s in states] == ["e1", "e2"]  # notFound dropped, request order kept
    assert states[1] == EmailState(
        id="e2",
        thread_id="t2",
        mailbox_ids=frozenset({"mb-inbox", "m-work"}),
        keywords=frozenset({"$seen"}),
    )


async def test_get_email_states_collapses_a_repeated_id(client, api_mock):
    api_mock.respond(json=_STATES_RESPONSE)
    states = await client.get_email_states(["e1", "e2", "e1"])

    sent = json.loads(api_mock.calls.last.request.content)["methodCalls"]
    assert sent[0][1]["ids"] == ["e1", "e2"]  # asked for once, not twice
    assert [s.id for s in states] == ["e1", "e2"]


async def test_get_email_states_of_nothing_makes_no_request(client, api_mock):
    assert await client.get_email_states([]) == []
    assert not api_mock.called


async def test_set_mailboxes_patch_emits_one_email_set_with_per_id_patches(client, api_mock):
    api_mock.respond(json=EMPTY_SET_RESPONSE)
    await client.set_mailboxes_patch(
        {"e1": {"mb-archive": True, "mb-inbox": None}}, keywords={"$junk": True}
    )

    sent = json.loads(api_mock.calls.last.request.content)["methodCalls"]
    assert len(sent) == 1
    name, args, call_id = sent[0]
    assert (name, call_id) == ("Email/set", "s0")
    assert args["update"] == {
        "e1": {"mailboxIds/mb-archive": True, "mailboxIds/mb-inbox": None, "keywords/$junk": True}
    }


async def test_set_mailboxes_patch_raises_when_the_server_rejects_an_id(client, api_mock):
    api_mock.respond(json=NOT_UPDATED_RESPONSE)
    with pytest.raises(JmapError, match="notFound"):
        await client.set_mailboxes_patch({"e1": {"mb-inbox": None}})


async def test_set_mailboxes_patch_of_nothing_makes_no_request(client, api_mock):
    await client.set_mailboxes_patch({})
    await client.set_mailboxes_patch({"e1": {}})
    assert not api_mock.called


# ---------------------------------------------------------------------------
# Trash and Spam: restore, not spam, delete forever, empty
# ---------------------------------------------------------------------------


async def test_restore_moves_everything_back_to_the_inbox_and_records_trash_for_undo():
    fake = FakeClient({"e1": _email("t1", {"mb-trash"}), "e2": _email("t2", {"mb-trash"})})
    result = await actions.restore(fake, await _nav(fake), ["e1", "e2"])

    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}
    assert fake.emails["e2"]["mailboxes"] == {"mb-inbox"}
    assert result.spec.kind == "restore"
    assert result.spec.toast == "Restored"
    assert result.spec.prev == {"e1": ["mb-trash"], "e2": ["mb-trash"]}
    assert result.removed == ["t1", "t2"]
    assert result.undoable is True
    # Both were unread and now sit in the Inbox: the badge moves up.
    assert result.counts == {"inbox": 2}

    await actions.apply_undo(fake, result.spec)
    assert fake.emails["e1"]["mailboxes"] == {"mb-trash"}


async def test_not_spam_returns_to_the_inbox_and_clears_junk_in_one_email_set():
    fake = FakeClient({"e1": _email("t1", {"mb-junk"})})
    fake.emails["e1"]["keywords"].add("$junk")
    result = await actions.not_spam(fake, await _nav(fake), ["e1"])

    assert fake.calls.count("set_mailboxes_patch") == 1
    assert fake.patch_calls == [({"e1": {"mb-inbox": True, "mb-junk": None}}, {"$junk": False})]
    assert fake.emails["e1"]["mailboxes"] == {"mb-inbox"}
    assert "$junk" not in fake.emails["e1"]["keywords"]
    assert result.spec.kind == "unspam"
    assert result.spec.toast == "Not spam"
    assert (result.spec.keyword, result.spec.on) == ("$junk", False)

    # Undo is report-spam again: back to Junk, `$junk` back on.
    await actions.apply_undo(fake, result.spec)
    assert fake.emails["e1"]["mailboxes"] == {"mb-junk"}
    assert "$junk" in fake.emails["e1"]["keywords"]


async def test_destroy_deletes_permanently_and_offers_nothing_to_undo():
    fake = FakeClient({"e1": _email("t1", {"mb-trash"}), "e2": _email("t1", {"mb-trash"})})
    result = await actions.destroy(fake, await _nav(fake), ["e1", "e2", "gone"])

    assert fake.destroyed == ["e1", "e2"]
    assert "e1" not in fake.emails
    # No `Email/set` update rode along: destroy is not a move.
    assert "set_mailboxes_patch" not in fake.calls
    assert result.spec.toast == "Deleted forever"
    assert result.removed == ["t1"]
    assert result.undoable is False
    # The spec is deliberately empty: nothing may sign it into a token.
    assert result.spec.email_ids == []
    assert result.spec.prev == {}


async def test_destroy_still_moves_the_inbox_badge_for_an_unread_inbox_message():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    result = await actions.destroy(fake, await _nav(fake), ["e1"])
    assert result.counts == {"inbox": -1}


async def test_empty_trash_destroys_only_the_messages_actually_in_trash():
    """A thread with one deleted reply and three live messages loses exactly
    the one: the page comes back with the whole thread, and membership is
    checked per message."""
    fake = FakeClient(
        {
            "e1": _email("t1", {"mb-trash"}),
            "e2": _email("t1", {"mb-inbox"}),
            "e3": _email("t2", {"mb-trash"}),
            "e4": _email("t3", {"mb-junk"}),
        }
    )
    result = await actions.empty_mailbox(fake, await _nav(fake), "trash")

    assert sorted(fake.destroyed) == ["e1", "e3"]
    assert set(fake.emails) == {"e2", "e4"}
    assert result.spec.toast == "Trash emptied"
    assert result.undoable is False
    assert result.removed == []
    assert fake.query_calls == [("mb-trash", 0, actions._EMPTY_PAGE)]


async def test_empty_spam_pages_from_the_top_until_nothing_is_left(monkeypatch):
    monkeypatch.setattr(actions, "_EMPTY_PAGE", 2)
    fake = FakeClient({f"e{n}": _email(f"t{n}", {"mb-junk"}) for n in range(5)})
    result = await actions.empty_mailbox(fake, await _nav(fake), "spam")

    assert fake.emails == {}
    assert result.spec.toast == "Spam emptied"
    # Always position 0: each destroyed page slides the next into place.
    assert fake.query_calls == [("mb-junk", 0, 2)] * 3


async def test_empty_refuses_any_mailbox_but_trash_and_spam():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    with pytest.raises(ValueError):
        await actions.empty_mailbox(fake, await _nav(fake), "inbox")
    assert fake.emails == {"e1": _email("t1", {"mb-inbox"})}


async def test_empty_of_an_already_empty_mailbox_destroys_nothing():
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    await actions.empty_mailbox(fake, await _nav(fake), "trash")
    assert "destroy_emails" not in fake.calls


def test_restore_and_unspam_routes_are_undoable_and_destroy_is_not():
    fake = FakeClient({"e1": _email("t1", {"mb-trash"}), "e2": _email("t2", {"mb-junk"})})
    app = _make_app(fake)

    restored = _trigger(_post(app, "/a/restore", {"ids": ["e1"]}))
    assert restored["toast"] == "Restored"
    assert verify(restored["undo"], SECRET, scope=ACCOUNT).kind == "restore"
    assert "undo_unavailable" not in restored

    unspammed = _trigger(_post(app, "/a/unspam", {"ids": ["e2"]}))
    assert unspammed["toast"] == "Not spam"
    assert verify(unspammed["undo"], SECRET, scope=ACCOUNT).kind == "unspam"

    response = _post(app, "/a/destroy", {"ids": ["e1"]})
    assert response.status_code == 204
    destroyed = _trigger(response)
    assert destroyed["toast"] == "Deleted forever"
    assert destroyed["undo"] is None
    # `permanent`, never `no_change`: the reply must not describe a
    # destroyed message as one nothing happened to.
    assert destroyed["undo_unavailable"] == "permanent"
    assert destroyed["removed"] == ["t1"]
    assert "e1" not in fake.emails


def test_destroy_over_the_bulk_line_asks_like_every_other_action():
    ids = _long_ids(101)
    fake = FakeClient({email_id: _email(f"t-{email_id}", {"mb-trash"}) for email_id in ids})
    app = _make_app(fake)
    asked = _post(app, "/a/destroy", {"ids": ids})
    assert asked.status_code == 409
    ask = _trigger(asked, "om:confirm")
    assert ask["kind"] == "destroy"
    assert "forever" in ask["message"]
    assert fake.destroyed == []
    assert _post(app, "/a/destroy", {"ids": ids, "confirm": "1"}).status_code == 204
    assert len(fake.destroyed) == 101


def test_empty_route_always_asks_first_and_then_refreshes():
    fake = FakeClient({"e1": _email("t1", {"mb-trash"}), "e2": _email("t2", {"mb-inbox"})})
    app = _make_app(fake)

    asked = _post(app, "/a/empty", {"key": "trash"})
    assert asked.status_code == 409
    ask = _trigger(asked, "om:confirm")
    assert ask["kind"] == "empty"
    assert ask["message"] == "Delete everything in Trash forever? This can't be undone."
    assert fake.destroyed == []

    done = _post(app, "/a/empty", {"key": "trash", "confirm": "1"})
    assert done.status_code == 204
    payload = _trigger(done)
    assert payload == {
        "toast": "Trash emptied",
        "undo": None,
        "undo_unavailable": "permanent",
        "refresh": True,
    }
    assert fake.destroyed == ["e1"]
    assert "e2" in fake.emails


@pytest.mark.parametrize("key", ["inbox", "archive", "m-work", "", "../trash"])
def test_empty_route_refuses_every_other_key(key):
    fake = FakeClient({"e1": _email("t1", {"mb-inbox"})})
    response = _post(_make_app(fake), "/a/empty", {"key": key, "confirm": "1"})
    # An empty `key` never reaches the route at all (FastAPI's own 422 for
    # a missing form field); everything else is the route's 400.
    assert response.status_code == (422 if key == "" else 400)
    assert fake.destroyed == []
