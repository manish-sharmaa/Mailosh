"""Live integration test against a running Stalwart stack (SPK-2).

Not collected by `make test` (`pyproject.toml`'s default `addopts` excludes
anything marked ``integration``) — only `make itest`
(`.venv/bin/python -m pytest -m integration`) runs this file, and only
against a real ``docker compose up`` Stalwart with
``bash scripts/stalwart-init.sh`` already applied (see the Makefile).

Proves the three parts of SPK-2 (``Email/import`` semantics, design spec
§14) in one pass against the real server: multi-``mailboxIds`` membership,
References-based threading, and a client-supplied ``receivedAt`` actually
being honoured. The imported messages' ``receivedAt`` is deliberately set to
a date *different* from their own ``Date:`` header (see ``_RECEIVED_AT``
below), so a passing assertion can only mean the field was truly respected,
not that Stalwart happened to parse the same value out of the header. See
``docs/spikes/p0-findings.md`` SPK-2 for the recorded answer.

This test writes to a live, shared, persistent server (creates a mailbox,
imports emails), so it cleans up after itself in a ``finally`` block —
both on success and on a failed assertion — so repeated `make itest` runs
stay green instead of accumulating "SpikeLabel-*" mailboxes and duplicate
"Spike thread" rows. Cleanup uses ``JmapClient._call`` directly rather than
a public method: the brief is explicit that a tiny destroy helper local to
this file is fine, but ``destroy`` is not part of the public client
contract other Mailosh code should rely on.
"""

from __future__ import annotations

import logging
import mailbox
import uuid
from datetime import UTC, datetime

import pytest

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient, find_inbox
from mailosh.jmap.errors import JmapError

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

#: One entry per message in tests/fixtures/sample.mbox, in file order.
#: Deliberately NOT the fixture's own Date: headers (2026-08-20 09:0x) — a
#: "migrated 5 days earlier" scenario, so the round-trip check below can only
#: pass if Email/import's receivedAt argument was actually honoured.
_RECEIVED_AT = [
    datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC),
    datetime(2026, 8, 15, 12, 5, 0, tzinfo=UTC),
    datetime(2026, 8, 15, 12, 10, 0, tzinfo=UTC),
]


async def _destroy(client: JmapClient, obj_type: str, ids: list[str]) -> None:
    """Best-effort ``<Type>/set destroy`` cleanup, local to this test file.

    Logs and swallows a ``JmapError`` rather than raising: a cleanup hiccup
    (e.g. an id already gone) must never clobber a genuine assertion failure
    propagating through the same ``finally`` block that calls this.
    """
    if not ids:
        return
    try:
        await client._call(
            [(f"{obj_type}/set", {"accountId": client.account_id, "destroy": list(ids)}, "d0")]
        )
    except JmapError:
        _log.warning("cleanup: %s/set destroy failed for %r", obj_type, ids, exc_info=True)


async def test_import_thread_and_multilabel():
    s = Settings()
    if s.demo_user is None or s.demo_password is None:
        pytest.skip("MAILOSH_DEMO_USER/MAILOSH_DEMO_PASSWORD not set")
    c = await JmapClient.connect(s.stalwart_url, s.demo_user, s.demo_password)
    label_id: str | None = None
    ids: list[str] = []
    try:
        inbox = find_inbox(await c.get_mailboxes())
        # create a second mailbox to act as a label (Mailbox/set create) —
        # unique suffix so repeated/concurrent runs never collide on name
        label_id = await c.create_mailbox(f"SpikeLabel-{uuid.uuid4().hex[:8]}")
        for i, msg in enumerate(mailbox.mbox("tests/fixtures/sample.mbox")):
            blob = await c.upload(bytes(msg), "message/rfc822")
            ids.append(await c.import_email(blob, {inbox.id, label_id}, set(), _RECEIVED_AT[i]))

        rows = await c.query_inbox(inbox.id)
        spike = [r for r in rows if "spike thread" in (r.subject or "").lower()]
        assert len(spike) == 1, "collapseThreads must fold 3 messages into one row"

        msgs = await c.get_thread(spike[0].thread_id)
        by_id = {m.id: m for m in msgs}
        # Assert on what THIS run imported, not on the thread's total size.
        # The demo account these run against is seeded from this very
        # fixture (`README`'s own quick start does it), so a hard
        # `len(msgs) == 3` fails on any mailbox that already holds a copy —
        # the property under test is that References threading put *our*
        # three into one thread, which is true whoever else is in it.
        assert set(ids) <= set(by_id), "threading via References must hold after import"
        assert {inbox.id, label_id} <= set(by_id[ids[0]].mailbox_ids), (
            "multi-mailbox membership survives import"
        )
        for email_id, expected in zip(ids, _RECEIVED_AT, strict=True):
            assert by_id[email_id].received_at == expected, (
                "Email/import's receivedAt must be honoured, not silently "
                "overridden by the message's own Date: header or import time"
            )
    finally:
        await _destroy(c, "Email", ids)
        if label_id is not None:
            await _destroy(c, "Mailbox", [label_id])
        await c.close()
