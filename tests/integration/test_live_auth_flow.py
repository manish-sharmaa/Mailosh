"""Live integration test for Task 4's Stalwart credential exchange (design
spec §9, SPK-3): password verification and the per-session API-key
lifecycle, proven against the real server -- not just respx mocks.

Not collected by `make test` (`pyproject.toml`'s default `addopts` excludes
anything marked ``integration``, same as `test_live_stalwart.py`) -- only
`make itest` runs this file, and only against a real `docker compose up`
Stalwart with `bash scripts/stalwart-init.sh` already applied. Skips (rather
than failing) when `MAILOSH_DEMO_USER`/`MAILOSH_DEMO_PASSWORD` aren't set,
the same guard `test_live_stalwart.py` already uses.

Proves, in one pass, against the live `stalwartlabs/stalwart:v0.16.20` stack:

- a wrong password returns `None`, never raises -- `verify_password`'s
  invalid-credentials path, confirmed against Stalwart's real
  "200-with-anonymous-session" response for bad Basic auth (SPK-3/SPK-5),
  not a mocked stand-in for it.
- the right password returns a `VerifiedAccount` whose `account_id`
  matches what an ordinary `JmapClient.connect` with the same credentials
  independently resolves -- not just "truthy", a real cross-check against
  ground truth.
- a freshly minted `x:ApiKey` (`StalwartAdmin.create_api_key`) genuinely
  authenticates a real `JmapClient.connect_bearer` session and can list
  mailboxes -- the exact SPK-3 question this task closes out for real.
- after `destroy_api_key`, that same secret is rejected with HTTP 401 --
  proving `destroy_api_key`'s target-account-scoped destroy call (a
  correction made *during this task*, after live testing disproved the
  admin-account-scoped guess the plan's Interfaces block was written
  under -- see `docs/spikes/p1a-findings.md`, "Auth exchange")
  actually revokes the TARGET user's credential.
"""

from __future__ import annotations

import logging

import pytest

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import JmapError, TransportError
from mailosh.security.exchange import verify_password
from mailosh.stalwart_admin import StalwartAdmin

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)


async def test_password_verify_and_api_key_lifecycle():
    s = Settings()
    if s.demo_user is None or s.demo_password is None:
        pytest.skip("MAILOSH_DEMO_USER/MAILOSH_DEMO_PASSWORD not set")

    admin = StalwartAdmin(s.stalwart_url, s.stalwart_admin_user, s.stalwart_admin_secret)
    key = None
    key_destroyed = False
    try:
        # --- verify_password: wrong password -> None, never raises ---
        assert await verify_password(s.stalwart_url, s.demo_user, "definitely-wrong") is None

        # --- verify_password: right password -> VerifiedAccount ---
        acct = await verify_password(s.stalwart_url, s.demo_user, s.demo_password)
        assert acct is not None
        assert acct.username == s.demo_user
        assert acct.email == s.demo_user

        # Cross-check account_id against an independently-resolved real
        # JMAP session for the same credentials -- not just non-empty.
        reference = await JmapClient.connect(s.stalwart_url, s.demo_user, s.demo_password)
        try:
            assert acct.account_id == reference.account_id
        finally:
            await reference.close()

        # --- create_api_key: mint a per-session key, prove it's a real
        # working Bearer credential for the demo account ---
        key = await admin.create_api_key(s.demo_user, "itest")
        assert key.id
        assert key.secret

        bearer_client = await JmapClient.connect_bearer(s.stalwart_url, key.secret)
        try:
            assert bearer_client.account_id == acct.account_id
            boxes = await bearer_client.get_mailboxes()
            assert boxes
        finally:
            await bearer_client.close()

        # --- destroy_api_key: same secret must now be rejected ---
        await admin.destroy_api_key(s.demo_user, key.id)
        key_destroyed = True

        with pytest.raises(TransportError) as exc_info:
            await JmapClient.connect_bearer(s.stalwart_url, key.secret)
        assert exc_info.value.status_code == 401
    finally:
        if key is not None and not key_destroyed:
            try:
                await admin.destroy_api_key(s.demo_user, key.id)
            except JmapError:
                _log.warning("cleanup: destroy_api_key failed for %r", key.id, exc_info=True)
        await admin.close()
