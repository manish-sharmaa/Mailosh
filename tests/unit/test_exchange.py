"""Unit tests for `mailosh.security.exchange.verify_password` (Task 4).

`SESSION` (`tests/fixtures/session.json`) is a real captured JMAP session for
the demo account, the same fixture `tests/conftest.py`/`tests/unit/
test_jmap_request.py` already use for `JmapClient.connect`. Design spec §9 /
`docs/spikes/p0-findings.md` SPK-3/SPK-5: Stalwart's `GET
/.well-known/jmap` answers Basic-auth'd requests directly (no redirect
indirection) and returns HTTP 200 even for bad credentials — an *anonymous*
session body (`username`/`accounts`/`primaryAccounts` all empty) rather than
a 401. `verify_password` must tell that apart from a genuinely verified
session using the response body alone, not the status code, and must
distinguish that "wrong password" case (→ `None`) from a transport-level
failure (→ raises `TransportError`, so Task 5's login route can show "can't
reach the mail server" instead of "wrong password" — controller decision #2).
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest
import respx

from mailosh.jmap.errors import TransportError
from mailosh.security.exchange import VerifiedAccount, verify_password

SESSION = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())


@respx.mock
async def test_valid_credentials_return_account():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    acct = await verify_password("http://s", "demo@mailosh.test", "pw")
    assert acct is not None
    assert acct.username == SESSION["username"]
    assert acct.account_id == SESSION["primaryAccounts"]["urn:ietf:params:jmap:mail"]
    assert acct.email == SESSION["username"]


@respx.mock
async def test_valid_credentials_send_basic_auth_not_bearer():
    """`verify_password` authenticates with the caller's own username/password
    (HTTP Basic) — this is the credential *check* itself, not a request made
    with an already-minted API key (that's `JmapClient.connect_bearer`).
    """
    route = respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    await verify_password("http://s", "demo@mailosh.test", "correct horse battery staple")
    request = route.calls[0].request
    assert "Authorization" in request.headers
    assert request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_anonymous_session_is_rejected():
    """Stalwart returns 200 for anonymous sessions (SPK-3/SPK-5) — a body
    with no `username` and no mail `primaryAccounts` entry must read as
    "invalid credentials", not be parsed as a successful, empty account.
    """
    anon = dict(SESSION, username="", accounts={}, primaryAccounts={})
    respx.get("http://s/.well-known/jmap").respond(json=anon)
    assert await verify_password("http://s", "demo@mailosh.test", "wrong") is None


@respx.mock
async def test_missing_mail_primary_account_is_rejected():
    """A body with a non-empty `username` but no mail-capability entry in
    `primaryAccounts` is still not a usable verified account — both halves
    of the "non-empty username AND a mail primary account" check must hold,
    not just one.
    """
    no_mail_account = dict(SESSION, primaryAccounts={})
    respx.get("http://s/.well-known/jmap").respond(json=no_mail_account)
    assert await verify_password("http://s", "demo@mailosh.test", "pw") is None


@respx.mock
async def test_401_is_rejected_not_raised():
    respx.get("http://s/.well-known/jmap").respond(401)
    assert await verify_password("http://s", "u", "p") is None


@respx.mock
async def test_403_is_rejected_not_raised():
    respx.get("http://s/.well-known/jmap").respond(403)
    assert await verify_password("http://s", "u", "p") is None


@respx.mock
async def test_server_error_raises_transport_error():
    """A 5xx (mail server broken/unreachable) must NOT read as "wrong
    password" — controller decision #2: this needs to surface as a distinct
    failure so Task 5's login page can say "can't reach the mail server."
    """
    respx.get("http://s/.well-known/jmap").respond(500)
    with pytest.raises(TransportError) as exc_info:
        await verify_password("http://s", "u", "p")
    assert exc_info.value.status_code == 500


@respx.mock
async def test_connection_failure_raises_transport_error():
    respx.get("http://s/.well-known/jmap").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TransportError):
        await verify_password("http://s", "u", "p")


def test_verified_account_is_a_frozen_dataclass_shape():
    """Interfaces block contract: `VerifiedAccount(username, account_id, email)`."""
    acct = VerifiedAccount(username="demo@mailosh.test", account_id="c", email="demo@mailosh.test")
    assert acct.username == "demo@mailosh.test"
    assert acct.account_id == "c"
    assert acct.email == "demo@mailosh.test"
