"""Task 14's end-to-end integration flow: the *containerised app*, driven
over HTTP exactly the way a browser drives it.

Everything else under `tests/integration/` talks to Stalwart directly
(`test_live_stalwart.py`, SPK-2) or exercises one seam of the credential
exchange with no app process involved at all (`test_live_auth_flow.py`,
Task 4). This file is the only one that needs **both**: real mail imported
through the P0 JMAP client, then read and mutated through
`http://localhost:8000` — `POST /login`, `GET /mail/inbox`,
`POST /a/{archive,delete,spam}`, `POST /a/undo`, `POST /logout` — so it
covers the seams *between* tasks, which is where this branch's two worst
bugs lived.

Kept as its own module rather than appended to `test_live_auth_flow.py`
(which the plan's Task 14 wording suggested) for one concrete reason: the
two files have genuinely different preconditions. `test_live_auth_flow.py`
needs Stalwart alone and skips on missing demo credentials;
this one *additionally* needs the `mailosh` container to be up and
serving, and skips separately when it is not (`_app_is_up`). Folding them
together would mean one module-level guard that is right for neither half,
and a reader could no longer tell which service a skip was actually about.

**Hermetic and self-cleaning.** Every run stamps a fresh `uuid4` into both
the `Message-ID` and the `Subject` of the three messages it imports, so two
runs never collide in Stalwart's References-based threading (SPK-2: a
repeated `Message-ID` folds a re-import into the *existing* thread, which is
exactly how a leaked SPK-2 fixture once broke `make itest` permanently —
see `docs/spikes/p0-findings.md` SPK-6 and `scripts/measure.py`'s
`_resolve_thread_id` docstring). Cleanup destroys precisely what the run
created, in a `finally`, so a failed assertion still leaves the account as
it found it and the next run behaves identically.

Run with `make itest` (only; `pyproject.toml`'s default `addopts` deselects
`integration`), against `make up`/`make dev` plus
`bash scripts/stalwart-init.sh`.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
import pytest

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient, find_inbox
from mailosh.jmap.errors import JmapError
from mailosh.security.sessions import cookie_name

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

#: The app under test. Both compose stacks (`make up`, `make dev`) publish
#: this on loopback; nothing here starts or stops it.
APP_URL = "http://localhost:8000"

#: `<meta name="csrf-token" content="...">` from `layouts/app.html`. Every
#: mutating route wants this back as `X-CSRF-Token`
#: (`mailosh.security.csrf`), which is what htmx's inherited `hx-headers`
#: does in a browser and what `_csrf_token` below does here.
_CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')


def _app_is_up() -> bool:
    """Is the `mailosh` container actually serving? Checked once per test
    so a stack with Stalwart up but the app down skips with an honest
    reason instead of failing with a bare `ConnectError`.
    """
    try:
        return httpx.get(f"{APP_URL}/login", timeout=5.0).status_code == 200
    except httpx.HTTPError:
        return False


def _message(run_id: str, index: int, subject: str) -> bytes:
    """One RFC 5322 message whose `Message-ID` is unique to this run.

    The per-run id is the hermeticity guarantee (module docstring): a fixed
    `Message-ID`, re-imported, threads into the previous run's copy instead
    of standing alone, and every subsequent count assertion would then be
    measuring the wrong thing.
    """
    msg = EmailMessage()
    msg["From"] = "itest-sender@example.com"
    msg["To"] = "demo@mailosh.test"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<itest-{run_id}-{index}@mailosh.test>"
    msg["Date"] = "Tue, 02 Sep 2026 09:00:00 +0000"
    msg.set_content(
        f"Imported by tests/integration/test_live_app_flow.py, run {run_id}.\n\n{subject}\n"
    )
    return bytes(msg)


async def _destroy_emails(client: JmapClient, ids: list[str]) -> None:
    """Best-effort `Email/set destroy` for exactly the ids this run created.

    `client._call` directly, rather than a public client method: `destroy`
    is deliberately not part of the `JmapClient` contract (the same call
    `tests/integration/test_live_stalwart.py`'s own `_destroy` helper makes,
    for the same reason). Logs and swallows a `JmapError` so a cleanup
    hiccup can never clobber a real assertion failure propagating through
    the same `finally`.
    """
    if not ids:
        return
    try:
        await client._call(
            [("Email/set", {"accountId": client.account_id, "destroy": list(ids)}, "d0")]
        )
    except JmapError:
        _log.warning("cleanup: Email/set destroy failed for %r", ids, exc_info=True)


def _csrf_token(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "no <meta name=csrf-token> in the rendered app shell"
    token = match.group(1)
    assert token, "app shell rendered an empty CSRF token for a logged-in session"
    return token


def _done_trigger(response: httpx.Response) -> dict:
    """The `om:done` payload an action answers with.

    Asserted rather than tolerated: `mailosh.web.actions` answers `204` and
    puts the whole canonical delta in an `HX-Trigger` header, so an action
    that "worked" but shipped no trigger has told the browser nothing and is
    a failure, not a pass.
    """
    assert response.status_code == 204, response.text
    raw = response.headers.get("hx-trigger")
    assert raw, "action answered 204 with no HX-Trigger"
    payload = json.loads(raw)
    assert "om:done" in payload, payload
    return payload["om:done"]


async def _mailbox_ids_of(client: JmapClient, email_id: str) -> set[str]:
    states = await client.get_email_states([email_id])
    assert states, f"Email/get returned nothing for {email_id!r}"
    return set(states[0].mailbox_ids)


async def _keywords_of(client: JmapClient, email_id: str) -> set[str]:
    states = await client.get_email_states([email_id])
    assert states, f"Email/get returned nothing for {email_id!r}"
    return set(states[0].keywords)


async def test_login_list_triage_undo_logout():
    """The whole flow the plan's Step 1 asks for, plus `delete` and `spam`.

    `archive` was the only triage action ever fired at real mail before this
    task; `delete` and `spam` share its shape but not its code, and had
    never once run against the live server (`actions.delete`/`actions.spam`
    replace `mailboxIds` outright, where `archive` subtracts — a genuinely
    different write, and `spam` additionally sets `$junk`). Each is
    exercised here against a real message and then undone, so the account is
    left exactly as it was found.
    """
    settings = Settings()
    if settings.demo_user is None or settings.demo_password is None:
        pytest.skip("MAILOSH_DEMO_USER/MAILOSH_DEMO_PASSWORD not set")
    if not _app_is_up():
        pytest.skip(f"the mailosh container is not serving {APP_URL}")

    run_id = uuid.uuid4().hex[:8]
    subjects = [f"Mailosh itest {run_id} #{i}" for i in range(3)]

    jmap = await JmapClient.connect(
        settings.stalwart_url, settings.demo_user, settings.demo_password
    )
    email_ids: list[str] = []
    http = httpx.Client(base_url=APP_URL, timeout=30.0, follow_redirects=False)
    try:
        mailboxes = await jmap.get_mailboxes()
        inbox = find_inbox(mailboxes)
        trash_id = next(m.id for m in mailboxes if m.role == "trash")
        junk_id = next(m.id for m in mailboxes if m.role == "junk")

        # --- three per-run messages, Inbox-only, newest-first at the top ---
        # `received_at=now` on purpose: `query_inbox` sorts newest-first, so
        # this puts all three on the first page regardless of how much other
        # mail the dev account has accumulated.
        for index, subject in enumerate(subjects):
            blob = await jmap.upload(_message(run_id, index, subject), "message/rfc822")
            email_ids.append(await jmap.import_email(blob, {inbox.id}, set(), datetime.now(UTC)))
        assert len(set(email_ids)) == 3, "each import must be its own message, not a re-thread"

        # --- POST /login ---
        login = http.post(
            "/login",
            data={
                "username": settings.demo_user,
                "password": settings.demo_password,
                "next": "/mail/inbox",
            },
        )
        assert login.status_code == 303, login.text
        assert login.headers["location"] == "/mail/inbox"
        assert http.cookies.get(cookie_name(settings)), "login set no session cookie"

        # --- GET /mail/inbox shows all three ---
        listing = http.get("/mail/inbox")
        assert listing.status_code == 200
        for subject in subjects:
            assert subject in listing.text, f"{subject!r} missing from the inbox render"
        csrf = _csrf_token(listing.text)
        headers = {"X-CSRF-Token": csrf}

        # --- archive #0, and the row leaves the inbox ---
        done = _done_trigger(http.post("/a/archive", data={"ids": [email_ids[0]]}, headers=headers))
        assert done["toast"] == "Archived"
        assert done["undo"], "archive handed back no undo token"
        undo_token = done["undo"]
        assert inbox.id not in await _mailbox_ids_of(jmap, email_ids[0])

        after_archive = http.get("/mail/inbox")
        assert after_archive.status_code == 200
        assert subjects[0] not in after_archive.text, "archived row is still in the inbox"
        assert subjects[1] in after_archive.text
        assert subjects[2] in after_archive.text

        # An Inbox-only message has nowhere to live once the Inbox is
        # subtracted, so `actions.archive` files it under the Archive-role
        # mailbox — and must land it in exactly one place, not several.
        archived_in = await _mailbox_ids_of(jmap, email_ids[0])
        archive_ids = {m.id for m in await jmap.get_mailboxes() if m.role == "archive"}
        assert len(archive_ids) == 1, (
            f"expected exactly one archive-role mailbox, got {archive_ids}"
        )
        assert archived_in == archive_ids, (
            f"an Inbox-only archive must land in the Archive mailbox alone, got {archived_in}"
        )

        # --- undo puts it back ---
        undone = _done_trigger(http.post("/a/undo", data={"token": undo_token}, headers=headers))
        assert undone["toast"] == "Undone"
        assert undone.get("refresh") is True
        assert inbox.id in await _mailbox_ids_of(jmap, email_ids[0])

        restored = http.get("/mail/inbox")
        assert subjects[0] in restored.text, "undo did not restore the row to the inbox"

        # --- delete #1 -> Trash and nowhere else, then undo ---
        done = _done_trigger(http.post("/a/delete", data={"ids": [email_ids[1]]}, headers=headers))
        assert done["toast"] == "Deleted"
        assert await _mailbox_ids_of(jmap, email_ids[1]) == {trash_id}
        after_delete = http.get("/mail/inbox")
        assert subjects[1] not in after_delete.text, "deleted row is still in the inbox"
        _done_trigger(http.post("/a/undo", data={"token": done["undo"]}, headers=headers))
        assert await _mailbox_ids_of(jmap, email_ids[1]) == {inbox.id}

        # --- spam #2 -> Junk + `$junk`, then undo (which must clear the
        # keyword again, not just move the message back) ---
        done = _done_trigger(http.post("/a/spam", data={"ids": [email_ids[2]]}, headers=headers))
        assert done["toast"] == "Reported spam"
        assert await _mailbox_ids_of(jmap, email_ids[2]) == {junk_id}
        assert "$junk" in await _keywords_of(jmap, email_ids[2])
        after_spam = http.get("/mail/inbox")
        assert subjects[2] not in after_spam.text, "spammed row is still in the inbox"
        _done_trigger(http.post("/a/undo", data={"token": done["undo"]}, headers=headers))
        assert await _mailbox_ids_of(jmap, email_ids[2]) == {inbox.id}
        assert "$junk" not in await _keywords_of(jmap, email_ids[2])

        # --- all three back in the inbox, unchanged ---
        final = http.get("/mail/inbox")
        for subject in subjects:
            assert subject in final.text, f"{subject!r} did not survive the triage/undo round trip"

        # --- POST /logout, and the inbox is closed again ---
        logout = http.post("/logout", headers=headers)
        assert logout.status_code == 303
        assert logout.headers["location"] == "/login"

        closed = http.get("/mail/inbox")
        assert closed.status_code == 303
        assert closed.headers["location"].startswith("/login?next=")
    finally:
        http.close()
        try:
            await _destroy_emails(jmap, email_ids)
            # Self-verify the cleanup: a run that leaves its own fixtures
            # live is exactly the failure mode that broke `make itest` for
            # SPK-2, and it is silent unless something looks.
            if email_ids:
                leftover = await jmap._call(
                    [
                        (
                            "Email/query",
                            {
                                "accountId": jmap.account_id,
                                "filter": {"subject": f"Mailosh itest {run_id}"},
                                "calculateTotal": True,
                                "limit": 0,
                            },
                            "q0",
                        )
                    ]
                )
                assert leftover["q0"]["total"] == 0, (
                    f"cleanup left {leftover['q0']['total']} message(s) of run {run_id} live"
                )
        finally:
            await jmap.close()


async def test_archive_mailbox_is_created_exactly_once():
    """Archiving Inbox-only mail on an account with **no** Archive folder
    creates one, and a second archive reuses it rather than creating a
    second.

    Stalwart provisions Inbox / Deleted Items / Junk Mail / Drafts / Sent
    Items and *not* Archive, so this is the ordinary state of a fresh
    self-hosted account — but this dev account has had an Archive mailbox
    (with real mail in it) since the first time anything archived, so the
    creation path cannot be reached again without taking the folder away
    first.

    Rather than destroying it (that would take five real messages with it),
    the existing Archive is temporarily stripped of its `role` **and**
    renamed: RFC 8621 §2 makes a role unique per account, and a sibling
    name unique too, so `ensure_role_mailbox`'s create would otherwise be
    rejected for a reason that has nothing to do with what is being tested.
    Both are restored in `finally`, and the restore is asserted, so a failed
    assertion inside the test cannot leave the account mis-shaped. No
    message is moved or destroyed by the swap itself — only the folder's own
    `name`/`role` properties change.
    """
    settings = Settings()
    if settings.demo_user is None or settings.demo_password is None:
        pytest.skip("MAILOSH_DEMO_USER/MAILOSH_DEMO_PASSWORD not set")
    if not _app_is_up():
        pytest.skip(f"the mailosh container is not serving {APP_URL}")

    run_id = uuid.uuid4().hex[:8]
    subjects = [f"Mailosh itest-arch {run_id} #{i}" for i in range(2)]

    jmap = await JmapClient.connect(
        settings.stalwart_url, settings.demo_user, settings.demo_password
    )
    http = httpx.Client(base_url=APP_URL, timeout=30.0, follow_redirects=False)
    email_ids: list[str] = []
    parked: tuple[str, str] | None = None  # (mailbox id, original name)
    created_archive_id: str | None = None
    try:
        mailboxes = await jmap.get_mailboxes()
        inbox = find_inbox(mailboxes)
        existing = [m for m in mailboxes if m.role == "archive"]
        assert len(existing) <= 1, f"account already has {len(existing)} archive-role mailboxes"

        if existing:
            original = existing[0]
            parked = (original.id, original.name)
            await jmap._call(
                [
                    (
                        "Mailbox/set",
                        {
                            "accountId": jmap.account_id,
                            "update": {
                                original.id: {
                                    "role": None,
                                    "name": f"Archive (parked by itest {run_id})",
                                }
                            },
                        },
                        "u0",
                    )
                ]
            )
        assert not [m for m in await jmap.get_mailboxes() if m.role == "archive"], (
            "the account still has an archive-role mailbox after parking"
        )

        for index, subject in enumerate(subjects):
            blob = await jmap.upload(_message(run_id, index, subject), "message/rfc822")
            email_ids.append(await jmap.import_email(blob, {inbox.id}, set(), datetime.now(UTC)))

        login = http.post(
            "/login",
            data={
                "username": settings.demo_user,
                "password": settings.demo_password,
                "next": "/mail/inbox",
            },
        )
        assert login.status_code == 303, login.text
        headers = {"X-CSRF-Token": _csrf_token(http.get("/mail/inbox").text)}

        # Two *separate* requests, deliberately: one archive proves the
        # folder gets created at all, the second proves the second archive
        # finds it rather than creating a rival (the failure mode that
        # splits an account's mail across two Archives).
        for email_id in email_ids:
            _done_trigger(http.post("/a/archive", data={"ids": [email_id]}, headers=headers))
            found = [m for m in await jmap.get_mailboxes() if m.role == "archive"]
            assert len(found) == 1, f"expected exactly 1 archive-role mailbox, got {found}"
            created_archive_id = found[0].id

        assert created_archive_id is not None
        assert parked is None or created_archive_id != parked[0], (
            "the parked mailbox was re-used instead of a new Archive being created"
        )
        for email_id in email_ids:
            assert await _mailbox_ids_of(jmap, email_id) == {created_archive_id}

        http.post("/logout", headers=headers)
    finally:
        http.close()
        try:
            await _destroy_emails(jmap, email_ids)
            if created_archive_id is not None:
                try:
                    await jmap._call(
                        [
                            (
                                "Mailbox/set",
                                {
                                    "accountId": jmap.account_id,
                                    "destroy": [created_archive_id],
                                },
                                "d0",
                            )
                        ]
                    )
                except JmapError:
                    _log.warning(
                        "cleanup: could not destroy the Archive mailbox this test created (%r)",
                        created_archive_id,
                        exc_info=True,
                    )
            if parked is not None:
                mailbox_id, name = parked
                await jmap._call(
                    [
                        (
                            "Mailbox/set",
                            {
                                "accountId": jmap.account_id,
                                "update": {mailbox_id: {"role": "archive", "name": name}},
                            },
                            "u0",
                        )
                    ]
                )
                restored = [m for m in await jmap.get_mailboxes() if m.role == "archive"]
                assert [m.id for m in restored] == [mailbox_id], (
                    f"failed to restore the account's original Archive mailbox: {restored}"
                )
        finally:
            await jmap.close()
