"""Unit tests for `mailosh.stalwart_admin.StalwartAdmin` (Task 10, SPK-3/SPK-5).

Every respx fixture below is shaped from what the live `stalwartlabs/stalwart:v0.16.20`
container actually returned while writing this client (see
`docs/spikes/p0-findings.md` SPK-5's close-out and SPK-3 section) --
not guessed from the brief. In particular:

- `x:Domain/set create`'s minimal `{"name": ...}` payload and its duplicate-name
  error shape (`primaryKeyViolation`, `properties: ["name"]`, an `objectId`
  pointing at the existing domain) were captured verbatim against a live
  container -- this was SPK-5's one open question ("never exercised") and this
  task is its first real exercise.
- `x:Account/set create`'s duplicate-email error shape mirrors the domain one
  exactly (`primaryKeyViolation`, `properties: ["email"]`).
- `x:DkimSignature/get`'s two-algorithm response (RSA + Ed25519, the same pair
  Stalwart's `dkimManagement: Automatic` default always generates) is reduced
  from the real live response for `mailosh.test`.
- `x:ApiKey/set create`'s response (a server-generated `secret` returned
  directly in the `created` map) is the SPK-3 probe's key finding: minting a
  per-user token is a real, working admin operation, not a dead end.

`StalwartAdmin` never does JMAP session discovery (`.well-known/jmap`) the way
`JmapClient` does -- every admin/management call goes straight to
`POST {base_url}/jmap` with HTTP Basic auth, matching
`scripts/stalwart-init.sh`'s already-proven approach (SPK-5's "no separate REST
management API" finding) -- so the only two routes ever mocked here are
`GET {base}/jmap/session` (resolving the admin's own JMAP account id, done once
per client and cached -- see `test_admin_account_id_is_resolved_once`) and
`POST {base}/jmap` (every actual method call).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import respx
import typer.testing

import mailosh.stalwart_admin as stalwart_admin_module
from mailosh.jmap.errors import JmapError
from mailosh.stalwart_admin import ApiKey, DkimRecord, StalwartAdmin

BASE = "http://s"

#: Minimal admin JMAP session (`GET /jmap/session`) -- only `accounts` matters,
#: since `StalwartAdmin._admin_account_id` does `next(iter(session["accounts"]))`
#: exactly like `scripts/stalwart-init.sh`'s `current_account_id()` (SPK-5 notes
#: this must be resolved dynamically, never hardcoded, even though it was
#: observed as `"d333333"` on every fresh install tried in Task 2).
ADMIN_SESSION = {"accounts": {"admin-acct": {"name": "admin", "isPersonal": True}}}


def _route_by_first_method(responses: dict[str, dict]):
    """respx side_effect: reply based on the posted batch's first call name.

    `StalwartAdmin`'s multi-step methods (e.g. `create_account` chains a
    `x:Domain/query` lookup before its `x:Account/set` create) POST more than
    once per public call; a plain `.respond(json=...)` route can't return a
    different body per call. Dispatching on the first method name (rather than
    positional call order) keeps each test's fixture readable as "when you ask
    X, you get Y" instead of a fragile ordered list.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["methodCalls"][0][0]
        return httpx.Response(200, json=responses[method])

    return handler


@pytest.fixture
async def admin():
    """A `StalwartAdmin` with its admin-session lookup pre-mocked.

    Mirrors `tests/conftest.py`'s own `client` fixture shape (open the respx
    mock context here, yield the connected object, close it on teardown) --
    each test adds its own `respx.post(f"{BASE}/jmap")` route on top of this
    same active router, the same way that file's `api_mock`/`upload_mock`
    fixtures layer onto its `client` fixture.
    """
    with respx.mock:
        respx.get(f"{BASE}/jmap/session").respond(json=ADMIN_SESSION)
        a = StalwartAdmin(BASE, "admin", "s3cret")
        yield a
        await a.close()


# ---------------------------------------------------------------------------
# create_domain
# ---------------------------------------------------------------------------


async def test_create_domain_sends_minimal_payload(admin):
    """SPK-5 close-out: `x:Domain/set create` needs no `@type` discriminator
    (unlike `x:Account`, `x:Domain` is a plain object, not a tagged union) --
    a bare `{"name": ...}` is both necessary and sufficient, confirmed live.
    """
    route = respx.post(f"{BASE}/jmap").respond(
        json={
            "methodResponses": [
                [
                    "x:Domain/set",
                    {"accountId": "admin-acct", "created": {"d0": {"id": "dom1"}}},
                    "c0",
                ]
            ],
            "sessionState": "s1",
        }
    )
    await admin.create_domain("spike.test")
    body = json.loads(route.calls[0].request.content)
    assert body["methodCalls"][0][0] == "x:Domain/set"
    assert body["methodCalls"][0][1]["create"] == {"d0": {"name": "spike.test"}}
    assert "urn:stalwart:jmap" in body["using"]


async def test_create_domain_is_idempotent_on_existing_domain(admin):
    """Live duplicate-create response, captured verbatim (SPK-5): a second
    `x:Domain/set create` for the same name comes back as a normal 200 with
    `notCreated: {"d0": {"type": "primaryKeyViolation", "properties": ["name"],
    "objectId": {"object": "Domain", "id": "<existing id>"}}}` -- not an
    `error`-named batch response, and not a 4xx. `create_domain` must treat
    this specific shape as success, not raise.
    """
    respx.post(f"{BASE}/jmap").respond(
        json={
            "methodResponses": [
                [
                    "x:Domain/set",
                    {
                        "accountId": "admin-acct",
                        "notCreated": {
                            "d0": {
                                "type": "primaryKeyViolation",
                                "properties": ["name"],
                                "objectId": {"object": "Domain", "id": "dom1"},
                            }
                        },
                    },
                    "c0",
                ]
            ],
            "sessionState": "s1",
        }
    )
    await admin.create_domain("spike.test")  # must not raise


async def test_create_domain_raises_on_other_set_error(admin):
    """A `notCreated` error that ISN'T a duplicate-name conflict (e.g. a
    rejected domain name) must still raise -- idempotency tolerance is
    specifically for `primaryKeyViolation`, not a blanket swallow-all-errors.
    """
    respx.post(f"{BASE}/jmap").respond(
        json={
            "methodResponses": [
                [
                    "x:Domain/set",
                    {
                        "accountId": "admin-acct",
                        "notCreated": {
                            "d0": {
                                "type": "invalidArguments",
                                "description": "not a valid hostname",
                            }
                        },
                    },
                    "c0",
                ]
            ],
            "sessionState": "s1",
        }
    )
    with pytest.raises(JmapError):
        await admin.create_domain("not a domain")


# ---------------------------------------------------------------------------
# create_account
# ---------------------------------------------------------------------------

#: `_find_domain_id`'s lookup response for "spike.test" -- two domains listed
#: (not just the one being searched for) so the id-by-name filter has to
#: actually discriminate, not just return whatever's list[0].
_DOMAIN_QUERY_RESPONSE = {
    "methodResponses": [
        ["x:Domain/query", {"accountId": "admin-acct", "ids": ["dom0", "dom1"]}, "q0"],
        [
            "x:Domain/get",
            {
                "accountId": "admin-acct",
                "list": [
                    {"id": "dom0", "name": "mailosh.test"},
                    {"id": "dom1", "name": "spike.test"},
                ],
                "notFound": [],
            },
            "g0",
        ],
    ],
    "sessionState": "s1",
}


async def test_create_account_sends_index_keyed_credentials_map(admin):
    """SPK-5's two non-obvious `x:Account/set create` encoding rules,
    exercised end to end: the `@type: "User"` discriminator, and
    `credentials` as an index-STRING-keyed map (`{"0": {...}}`) -- a plain
    list or an arbitrarily-keyed map both fail live with `invalidPatch`
    (see SPK-5 in the findings doc). `domainId` must be the id resolved from
    the `x:Domain/query`+`get` lookup (`"dom1"`), not the domain name string.
    """
    account_create_response = {
        "methodResponses": [
            ["x:Account/set", {"accountId": "admin-acct", "created": {"a0": {"id": "acct1"}}}, "c0"]
        ],
        "sessionState": "s1",
    }
    route = respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Domain/query": _DOMAIN_QUERY_RESPONSE, "x:Account/set": account_create_response}
        )
    )
    created_result = await admin.create_account(
        "wizard@spike.test", "Wizard Admin", "hunter2-secret"
    )
    assert created_result is True  # freshly created -- password WAS applied

    create_call = next(
        c
        for c in route.calls
        if json.loads(c.request.content)["methodCalls"][0][0] == "x:Account/set"
    )
    create_args = json.loads(create_call.request.content)["methodCalls"][0][1]
    created = create_args["create"]["a0"]
    assert created["@type"] == "User"
    assert created["name"] == "wizard"
    assert created["domainId"] == "dom1"
    assert created["credentials"] == {"0": {"@type": "Password", "secret": "hunter2-secret"}}


async def test_create_account_is_idempotent_on_existing_email(admin):
    """Live duplicate-create response (SPK-5): same shape as the domain case,
    but `properties: ["email"]` -- account uniqueness is enforced on the
    computed `name@domain` address, not the raw `name` field.

    Also the return-value contract `mailosh/cli.py`'s `setup` command
    depends on: `False` here means the `password` argument this call was
    given was NOT applied -- the pre-existing account's own password is
    untouched (FINDING 2 fix: `create_account` used to return `None`
    either way, so a caller had no way to distinguish "freshly created,
    my password is now live" from "already existed, my password did
    nothing").
    """
    account_conflict_response = {
        "methodResponses": [
            [
                "x:Account/set",
                {
                    "accountId": "admin-acct",
                    "notCreated": {
                        "a0": {
                            "type": "primaryKeyViolation",
                            "properties": ["email"],
                            "objectId": {"object": "Account", "id": "acct1"},
                        }
                    },
                },
                "c0",
            ]
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Domain/query": _DOMAIN_QUERY_RESPONSE, "x:Account/set": account_conflict_response}
        )
    )
    created_result = await admin.create_account(
        "wizard@spike.test", "Wizard Admin", "hunter2-secret"
    )
    assert created_result is False  # already existed -- password NOT applied, must not raise


async def test_create_account_raises_when_domain_missing(admin):
    """`create_account` depends on `create_domain` having already run (the
    CLI always calls them in that order) -- calling it for a domain the
    lookup can't find must fail loudly, not silently create an orphaned
    account with a garbage `domainId`.
    """
    empty_domain_list = {
        "methodResponses": [
            ["x:Domain/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:Domain/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").respond(json=empty_domain_list)
    with pytest.raises(JmapError):
        await admin.create_account("wizard@nosuchdomain.test", "Wizard", "pw")


# ---------------------------------------------------------------------------
# get_dkim_record
# ---------------------------------------------------------------------------

#: Reduced from the live two-algorithm response `x:DkimSignature/query`+`get`
#: actually returned for `mailosh.test` (SPK-5): Stalwart's default
#: `dkimManagement: Automatic` generates both an RSA and an Ed25519 key per
#: domain. `publicKey` here is a shortened stand-in for the real ~380-char
#: base64 RSA key -- the exact bytes don't matter to this client, only that
#: they're threaded through unmodified into the TXT value.
_DKIM_GET_RESPONSE = {
    "methodResponses": [
        [
            "x:DkimSignature/query",
            {"accountId": "admin-acct", "ids": ["dk-rsa", "dk-ed25519"]},
            "q0",
        ],
        [
            "x:DkimSignature/get",
            {
                "accountId": "admin-acct",
                "list": [
                    {
                        "id": "dk-rsa",
                        "domainId": "dom1",
                        "selector": "v1-rsa-20260901",
                        "@type": "Dkim1RsaSha256",
                        "publicKey": "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCg...RSAKEY...IDAQAB",
                    },
                    {
                        "id": "dk-ed25519",
                        "domainId": "dom1",
                        "selector": "v1-ed25519-20260901",
                        "@type": "Dkim1Ed25519Sha256",
                        "publicKey": "Kw17WZ0LB4WdimjrMQ2aKjwXIYBZHypFPsKmRPg3Cos=",
                    },
                ],
                "notFound": [],
            },
            "g0",
        ],
    ],
    "sessionState": "s1",
}

#: A domain whose only DKIM key generated so far is Ed25519 -- the shape a
#: still-in-progress key generation leaves behind (see the async-generation
#: finding documented on `_DKIM_POLL_ATTEMPTS`), and also just a domain
#: that will never get an RSA key (e.g. some future non-default
#: `dkimManagement` configuration). Shared by both the "keeps polling, RSA
#: eventually shows up" test and the "RSA never shows up, falls back with a
#: warning" test below -- the same fixture, different response sequences.
_DKIM_ED25519_ONLY_RESPONSE = {
    "methodResponses": [
        ["x:DkimSignature/query", {"accountId": "admin-acct", "ids": ["dk-ed25519"]}, "q0"],
        [
            "x:DkimSignature/get",
            {
                "accountId": "admin-acct",
                "list": [
                    {
                        "id": "dk-ed25519",
                        "domainId": "dom1",
                        "selector": "v1-ed25519-20260901",
                        "@type": "Dkim1Ed25519Sha256",
                        "publicKey": "Kw17WZ0LB4WdimjrMQ2aKjwXIYBZHypFPsKmRPg3Cos=",
                    }
                ],
                "notFound": [],
            },
            "g0",
        ],
    ],
    "sessionState": "s1",
}


async def test_get_dkim_record_prefers_rsa_and_formats_txt_value(admin):
    """When a domain has both algorithms (Stalwart's default), RSA is
    returned -- the algorithm every major mailbox provider's DKIM verifier
    supports, unlike Ed25519 (RFC 8463), whose verifier support is still
    inconsistent (see this module's `_DKIM_PREFERENCE` docstring). The host
    is `<selector>._domainkey.<domain>`; the value is a hand-assembled
    `v=DKIM1; k=rsa; h=sha256; p=<publicKey>` -- `h=sha256` matches every
    live zone-file DKIM line Task 2/10 observed (see SPK-5), not a guess.
    """
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Domain/query": _DOMAIN_QUERY_RESPONSE, "x:DkimSignature/query": _DKIM_GET_RESPONSE}
        )
    )
    record = await admin.get_dkim_record("spike.test")
    assert isinstance(record, DkimRecord)
    assert record.host == "v1-rsa-20260901._domainkey.spike.test"
    assert record.value == (
        "v=DKIM1; k=rsa; h=sha256; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCg...RSAKEY...IDAQAB"
    )


async def test_get_dkim_record_raises_when_domain_missing(admin):
    empty_domain_list = {
        "methodResponses": [
            ["x:Domain/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:Domain/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").respond(json=empty_domain_list)
    with pytest.raises(JmapError):
        await admin.get_dkim_record("nosuchdomain.test")


async def test_get_dkim_record_raises_when_no_keys_exist(admin, monkeypatch):
    """A domain with DKIM management set to Manual (or not yet generated)
    has no `x:DkimSignature` entries at all -- must fail loudly rather than
    returning a nonsensical empty/None record.

    `_DKIM_POLL_ATTEMPTS` is monkeypatched down to 1 (a single attempt, no
    retries) purely so this test doesn't burn several real seconds sleeping
    through the retry budget `test_get_dkim_record_retries_until_...` below
    exists to prove out -- this test's own concern (still-empty -> raise)
    doesn't depend on how many attempts happen first.
    """
    monkeypatch.setattr(stalwart_admin_module, "_DKIM_POLL_ATTEMPTS", 1)
    no_dkim_response = {
        "methodResponses": [
            ["x:DkimSignature/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:DkimSignature/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Domain/query": _DOMAIN_QUERY_RESPONSE, "x:DkimSignature/query": no_dkim_response}
        )
    )
    with pytest.raises(JmapError):
        await admin.get_dkim_record("spike.test")


async def test_get_dkim_record_retries_until_preferred_algorithm_appears(admin, monkeypatch):
    """Regression test for a live finding (SPK-5 close-out): Stalwart
    generates a freshly created domain's DKIM keys ASYNCHRONOUSLY, not as
    part of `x:Domain/set create` itself -- confirmed live by creating a
    domain and querying `x:DkimSignature` in the very same batched request,
    which showed zero keys for it yet. A query moments later can catch a
    partial state too: the faster-to-generate Ed25519 key already present,
    the slower RSA key not yet. `get_dkim_record` must poll (briefly,
    boundedly) for its preferred algorithm specifically, rather than
    raising on the first empty response or silently settling for whichever
    algorithm happens to exist on the first non-empty one.

    Simulates exactly that three-step sequence: empty, then Ed25519-only,
    then both -- and asserts the RSA record wins once it's actually there.
    `_DKIM_POLL_DELAY_SECONDS` is monkeypatched to 0 so this test doesn't
    spend real wall-clock time on the (real, but here pointless) sleep
    between attempts.
    """
    monkeypatch.setattr(stalwart_admin_module, "_DKIM_POLL_DELAY_SECONDS", 0)
    empty_response = {
        "methodResponses": [
            ["x:DkimSignature/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:DkimSignature/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    dkim_attempts = iter([empty_response, _DKIM_ED25519_ONLY_RESPONSE, _DKIM_GET_RESPONSE])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["methodCalls"][0][0] == "x:Domain/query":
            return httpx.Response(200, json=_DOMAIN_QUERY_RESPONSE)
        return httpx.Response(200, json=next(dkim_attempts))

    respx.post(f"{BASE}/jmap").mock(side_effect=handler)
    record = await admin.get_dkim_record("spike.test")
    assert record.host == "v1-rsa-20260901._domainkey.spike.test"
    with pytest.raises(StopIteration):
        next(dkim_attempts)  # exactly 3 DKIM query attempts, not more


async def test_get_dkim_record_warns_and_returns_fallback_when_preferred_never_appears(
    admin, monkeypatch, caplog
):
    """FINDING 1 fix (code review): if RSA never shows up within the poll
    budget but Ed25519 did (a domain that will genuinely never get an RSA
    key -- not just "hasn't yet"), `get_dkim_record` must not raise (a
    working Ed25519 record beats a blocked wizard) but must not silently
    substitute it either. Every one of the (monkeypatched-small) poll
    attempts returns the Ed25519-only fixture -- RSA never appears, so the
    poll loop runs its full budget rather than breaking early.

    Asserts both halves: the Ed25519 record is still returned (not an
    exception), AND exactly one `logger.warning` fired, with a message
    that names both algorithms (the one that never appeared, and the one
    actually returned) -- not just a generic "something's off" line.
    """
    monkeypatch.setattr(stalwart_admin_module, "_DKIM_POLL_DELAY_SECONDS", 0)
    monkeypatch.setattr(stalwart_admin_module, "_DKIM_POLL_ATTEMPTS", 3)
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {
                "x:Domain/query": _DOMAIN_QUERY_RESPONSE,
                "x:DkimSignature/query": _DKIM_ED25519_ONLY_RESPONSE,
            }
        )
    )
    with caplog.at_level(logging.WARNING, logger="mailosh.stalwart_admin"):
        record = await admin.get_dkim_record("spike.test")

    assert record.host == "v1-ed25519-20260901._domainkey.spike.test"
    assert (
        record.value
        == "v=DKIM1; k=ed25519; h=sha256; p=Kw17WZ0LB4WdimjrMQ2aKjwXIYBZHypFPsKmRPg3Cos="
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}: {warnings}"
    message = warnings[0].getMessage()
    assert "Dkim1RsaSha256" in message  # the preferred algorithm that never appeared
    assert "Dkim1Ed25519Sha256" in message  # the algorithm actually returned instead


# ---------------------------------------------------------------------------
# try_mint_user_token (SPK-3 probe)
# ---------------------------------------------------------------------------

#: `_find_account_id`'s lookup response -- two accounts listed, same
#: "must actually discriminate" shape as `_DOMAIN_QUERY_RESPONSE`.
_ACCOUNT_QUERY_RESPONSE = {
    "methodResponses": [
        ["x:Account/query", {"accountId": "admin-acct", "ids": ["acct0", "acct1"]}, "q0"],
        [
            "x:Account/get",
            {
                "accountId": "admin-acct",
                "list": [
                    {"id": "acct0", "emailAddress": "someone-else@spike.test"},
                    {"id": "acct1", "emailAddress": "wizard@spike.test"},
                ],
                "notFound": [],
            },
            "g0",
        ],
    ],
    "sessionState": "s1",
}


async def test_try_mint_user_token_returns_server_generated_secret(admin):
    """SPK-3's key finding: `x:ApiKey/set create`, called with `accountId`
    set to the TARGET user's own JMAP account id (resolved by
    `_find_account_id`, NOT the admin's account id used everywhere else in
    this client), returns a server-generated `secret` directly in the
    `created` map -- confirmed live to work as a Bearer token for that
    user's own JMAP session (see SPK-3 in the findings doc). The `"API_"`
    prefix below matches the real live secret's shape.
    """
    apikey_create_response = {
        "methodResponses": [
            [
                "x:ApiKey/set",
                {
                    "accountId": "acct1",
                    "created": {"k0": {"id": "key1", "secret": "API_faketoken123"}},
                },
                "c0",
            ]
        ],
        "sessionState": "s1",
    }
    route = respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": apikey_create_response}
        )
    )
    token = await admin.try_mint_user_token("wizard@spike.test")
    assert token == "API_faketoken123"

    mint_call = next(
        c
        for c in route.calls
        if json.loads(c.request.content)["methodCalls"][0][0] == "x:ApiKey/set"
    )
    mint_args = json.loads(mint_call.request.content)["methodCalls"][0][1]
    assert mint_args["accountId"] == "acct1"  # the TARGET user's account, not admin's


async def test_try_mint_user_token_returns_none_for_unknown_account(admin):
    empty_accounts = {
        "methodResponses": [
            ["x:Account/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:Account/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").respond(json=empty_accounts)
    assert await admin.try_mint_user_token("nobody@spike.test") is None


async def test_try_mint_user_token_returns_none_when_secret_field_missing(admin, caplog):
    """FIX 5 (phase0 final review): `try_mint_user_token`'s docstring
    promises it never raises, but the code used to do `created["secret"]`
    directly -- a bare `KeyError` on any `created` response shaped without
    a `secret` field would have broken that contract. `.get("secret")`
    plus this same warn-and-return-`None` path (mirroring the "not
    created" branch just above it) closes the gap; this proves the
    missing-secret shape degrades exactly like a rejected mint does.
    """
    apikey_create_response = {
        "methodResponses": [
            ["x:ApiKey/set", {"accountId": "acct1", "created": {"k0": {"id": "key1"}}}, "c0"]
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": apikey_create_response}
        )
    )
    with caplog.at_level(logging.WARNING, logger="mailosh.stalwart_admin"):
        token = await admin.try_mint_user_token("wizard@spike.test")
    assert token is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}: {warnings}"
    assert "wizard@spike.test" in warnings[0].getMessage()


async def test_try_mint_user_token_returns_none_when_mint_rejected(admin):
    """A server-side rejection of the mint itself (permissions, quota, ...)
    must degrade to `None`, not raise -- this is a best-effort probe/feature,
    not a required step in account setup (the CLI's `setup` command never
    calls it; see `mailosh/cli.py`)."""
    rejected_response = {
        "methodResponses": [
            [
                "x:ApiKey/set",
                {"accountId": "acct1", "notCreated": {"k0": {"type": "forbidden"}}},
                "c0",
            ]
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": rejected_response}
        )
    )
    assert await admin.try_mint_user_token("wizard@spike.test") is None


# ---------------------------------------------------------------------------
# create_api_key / destroy_api_key (Task 4)
#
# `x:ApiKey/set create`'s request/response shape below is copied exactly
# from the live probe recorded in docs/spikes/p0-findings.md
# SPK-3: a bare `{"description": ...}` create payload (no other fields),
# `accountId` set to the TARGET user's own account id (resolved via
# `_find_account_id`). `try_mint_user_token`'s own existing tests (above)
# now exercise this same code path end to end (Task 4 controller decision
# #1: it delegates to `create_api_key` instead of duplicating the call) --
# unchanged by this refactor, since neither test asserts on the
# `description` field's exact contents.
#
# `destroy_api_key` ALSO resolves and uses the target account's id, not the
# admin's -- a correction made after live testing (`docs/
# spikes/p1a-findings.md`, "Auth exchange"): unlike every other admin-scoped
# call in this module, `x:ApiKey` turned out to be a genuinely per-account
# resource, so a destroy scoped to the admin's own account id returns
# `notFound` for a key that demonstrably exists. `destroy_api_key`'s own
# signature gained a `username` parameter for exactly this reason -- a
# departure from this task's plan-time Interfaces block, recorded there and
# in this task's own report.
# ---------------------------------------------------------------------------


def test_api_key_repr_redacts_the_secret():
    """Task 5 controller ruling #4: `ApiKey` starts getting logged around
    mint/destroy (per-user key reuse/reaper bookkeeping) — a redacting
    `__repr__` means a stray `logger.debug("... %r", key)` can never leak a
    live Stalwart Bearer credential into the logs, whether or not the
    caller remembered to log `key.id` specifically instead of `key` itself.
    """
    key = ApiKey(id="k1", secret="API_totally-live-bearer-credential")
    text = repr(key)
    assert text == "ApiKey(id='k1', secret='API_***')"
    assert "totally-live-bearer-credential" not in text


async def test_create_api_key_sends_bare_description_scoped_to_target_account(admin):
    """SPK-3's exact recorded shape: a bare `{"description": ...}` create
    payload, `accountId` = the TARGET user's account id. `name` (controller
    decision #3: `mailosh-session-<8 hex>`) becomes that `description`
    value with an 8-hex-character random suffix `create_api_key` itself
    appends -- callers pass a short base name (e.g. `"mailosh-session"`),
    not a pre-formatted unique string.
    """
    apikey_create_response = {
        "methodResponses": [
            [
                "x:ApiKey/set",
                {"accountId": "acct1", "created": {"k0": {"id": "key-1", "secret": "API_abc"}}},
                "c0",
            ]
        ],
        "sessionState": "s1",
    }
    route = respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": apikey_create_response}
        )
    )
    key = await admin.create_api_key("wizard@spike.test", "mailosh-session")
    assert isinstance(key, ApiKey)
    assert key.id == "key-1"
    assert key.secret == "API_abc"

    mint_call = next(
        c
        for c in route.calls
        if json.loads(c.request.content)["methodCalls"][0][0] == "x:ApiKey/set"
    )
    mint_args = json.loads(mint_call.request.content)["methodCalls"][0][1]
    assert mint_args["accountId"] == "acct1"  # TARGET user's account, not admin's
    create = mint_args["create"]["k0"]
    assert set(create) == {"description"}  # bare payload, nothing else — matches SPK-3
    assert create["description"].startswith("mailosh-session-")
    suffix = create["description"].removeprefix("mailosh-session-")
    assert len(suffix) == 8
    int(suffix, 16)  # 8 hex characters


async def test_create_api_key_raises_when_account_unknown(admin):
    empty_accounts = {
        "methodResponses": [
            ["x:Account/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:Account/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").respond(json=empty_accounts)
    with pytest.raises(JmapError):
        await admin.create_api_key("nobody@spike.test", "mailosh-session")


async def test_create_api_key_raises_when_mint_rejected(admin):
    rejected_response = {
        "methodResponses": [
            [
                "x:ApiKey/set",
                {"accountId": "acct1", "notCreated": {"k0": {"type": "forbidden"}}},
                "c0",
            ]
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": rejected_response}
        )
    )
    with pytest.raises(JmapError):
        await admin.create_api_key("wizard@spike.test", "mailosh-session")


async def test_create_api_key_raises_on_over_quota_matching_live_shape(admin):
    """Regression test for a real live finding (Task 4, not anticipated by
    SPK-3): Stalwart caps `x:ApiKey` objects at **5 per account**. A 6th
    `x:ApiKey/set create` for the same account was confirmed live to come
    back exactly as `notCreated: {"type": "overQuota", "description": "You
    have exceeded your quota of 5 API keys."}` -- reproduced verbatim here
    (see docs/spikes/p1a-findings.md, "Auth exchange", for the
    live transcript and why this matters for Task 5's session design).
    `create_api_key` has no special handling for this specific `type` --
    it's just another `notCreated` reason and raises `JmapError` the same
    way `test_create_api_key_raises_when_mint_rejected` above already
    proves for a generic rejection -- this test exists to pin the exact
    live shape down as a named regression, not to prove new code behavior.
    """
    over_quota_response = {
        "methodResponses": [
            [
                "x:ApiKey/set",
                {
                    "accountId": "acct1",
                    "notCreated": {
                        "k0": {
                            "type": "overQuota",
                            "description": "You have exceeded your quota of 5 API keys.",
                        }
                    },
                },
                "c0",
            ]
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": over_quota_response}
        )
    )
    with pytest.raises(JmapError, match="overQuota"):
        await admin.create_api_key("wizard@spike.test", "mailosh-session")


async def test_create_api_key_raises_when_secret_field_missing(admin):
    """Unlike `try_mint_user_token` (which degrades to `None`),
    `create_api_key` is the raising primitive -- a `created` response
    without a usable `secret` is a hard failure here, not a caller-visible
    `None` (see the Interfaces block: `create_api_key(...) -> ApiKey`, no
    `| None`).
    """
    apikey_create_response = {
        "methodResponses": [
            ["x:ApiKey/set", {"accountId": "acct1", "created": {"k0": {"id": "key-1"}}}, "c0"]
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Account/query": _ACCOUNT_QUERY_RESPONSE, "x:ApiKey/set": apikey_create_response}
        )
    )
    with pytest.raises(JmapError):
        await admin.create_api_key("wizard@spike.test", "mailosh-session")


async def test_destroy_api_key_sends_destroy_list_scoped_to_target_account(admin):
    """`destroy_api_key` takes `username` as well as `key_id` -- a corrected
    departure from this task's plan-time Interfaces block
    (`destroy_api_key(self, key_id: str) -> None`), which assumed (like
    every OTHER admin-scoped call in this module) that the admin's own
    account id would work here too. Run live, that assumption failed
    outright: `x:ApiKey/set destroy` scoped to the admin's own account
    returned `notFound` for a key that unquestionably existed. `x:ApiKey`,
    unlike `x:Domain`/`x:Account`, is a genuinely per-account resource --
    the destroy call must be scoped to the key's OWNING account id
    (resolved via `_find_account_id`, same as `create_api_key`'s own create
    call), confirmed live end to end (secret authenticates before, HTTP 401
    after) -- see docs/spikes/p1a-findings.md, "Auth exchange".
    """
    route = respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {
                "x:Account/query": _ACCOUNT_QUERY_RESPONSE,
                "x:ApiKey/set": {
                    "methodResponses": [
                        ["x:ApiKey/set", {"accountId": "acct1", "destroyed": ["key-1"]}, "c0"]
                    ],
                    "sessionState": "s1",
                },
            }
        )
    )
    await admin.destroy_api_key("wizard@spike.test", "key-1")
    destroy_call = next(
        c
        for c in route.calls
        if json.loads(c.request.content)["methodCalls"][0][0] == "x:ApiKey/set"
    )
    body = json.loads(destroy_call.request.content)
    assert body["methodCalls"][0][1] == {"accountId": "acct1", "destroy": ["key-1"]}


async def test_destroy_api_key_raises_when_account_unknown(admin):
    empty_accounts = {
        "methodResponses": [
            ["x:Account/query", {"accountId": "admin-acct", "ids": []}, "q0"],
            ["x:Account/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
        ],
        "sessionState": "s1",
    }
    respx.post(f"{BASE}/jmap").respond(json=empty_accounts)
    with pytest.raises(JmapError):
        await admin.destroy_api_key("nobody@spike.test", "key-1")


async def test_destroy_api_key_raises_on_not_destroyed(admin):
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {
                "x:Account/query": _ACCOUNT_QUERY_RESPONSE,
                "x:ApiKey/set": {
                    "methodResponses": [
                        [
                            "x:ApiKey/set",
                            {"accountId": "acct1", "notDestroyed": {"key-1": {"type": "notFound"}}},
                            "c0",
                        ]
                    ],
                    "sessionState": "s1",
                },
            }
        )
    )
    with pytest.raises(JmapError):
        await admin.destroy_api_key("wizard@spike.test", "key-1")


async def test_destroy_api_key_raises_when_id_not_reported_at_all(admin):
    """A server that says nothing about the id in either `destroyed` or
    `notDestroyed` isn't evidence it worked -- same "fail loudly" stance
    `mailosh.jmap.client._check_updated` already takes for `Email/set`.
    """
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {
                "x:Account/query": _ACCOUNT_QUERY_RESPONSE,
                "x:ApiKey/set": {
                    "methodResponses": [["x:ApiKey/set", {"accountId": "acct1"}, "c0"]],
                    "sessionState": "s1",
                },
            }
        )
    )
    with pytest.raises(JmapError):
        await admin.destroy_api_key("wizard@spike.test", "key-1")


# ---------------------------------------------------------------------------
# Admin account id resolution -- resolved once, cached, never hardcoded
# ---------------------------------------------------------------------------


async def test_admin_account_id_is_resolved_once_and_cached(admin):
    """SPK-5 note: the recovery-admin's JMAP account id looked stable
    (`"d333333"` on every fresh install Task 2 tried) but must still be
    resolved dynamically via `GET /jmap/session`, never hardcoded. This
    proves both halves: it's actually fetched from the session (not a
    hardcoded guess -- `ADMIN_SESSION` here uses a deliberately made-up id,
    `"admin-acct"`, that doesn't match Task 2's `"d333333"`), and it's only
    fetched once even across two separate public method calls on the same
    client, not once per call.
    """
    # Re-registering the same route (respx matches most-recently-registered
    # first) so this test can read its own `.call_count` -- must still
    # `.respond(...)` with the same session body the `admin` fixture's own
    # route would have, or this shadows it with an empty-body response.
    session_route = respx.get(f"{BASE}/jmap/session").respond(json=ADMIN_SESSION)
    respx.post(f"{BASE}/jmap").respond(
        json={
            "methodResponses": [
                [
                    "x:Domain/set",
                    {"accountId": "admin-acct", "created": {"d0": {"id": "dom1"}}},
                    "c0",
                ]
            ],
            "sessionState": "s1",
        }
    )
    await admin.create_domain("one.test")
    await admin.create_domain("two.test")
    assert session_route.call_count == 1


# ---------------------------------------------------------------------------
# CLI: `mailosh setup --domain X --email Y [--password P]`
# ---------------------------------------------------------------------------


def test_setup_cli_prints_all_four_dns_record_types(monkeypatch):
    """Drives the real Typer `setup` command end to end with a fake admin
    client, asserting the printed DNS block contains all four record types
    the design spec calls for (MX/SPF/DKIM/DMARC) plus the rDNS reminder.

    Dependency seam: `StalwartAdmin`'s three network-calling methods
    (`create_domain`/`create_account`/`get_dkim_record`) are monkeypatched
    directly on the class, the same technique already established by
    `tests/unit/test_sse_hub.py`'s `monkeypatch.setattr(JmapClient, "connect",
    fake_connect)` -- no special test-only factory/hook was added to
    `mailosh/cli.py` for this. `StalwartAdmin.__init__`/`close` are left
    real: `__init__` only builds an `httpx.AsyncClient` (no I/O), and
    `close()`'s `aclose()` is always safe on a client that made no requests.
    `Settings()` is satisfied the same way `test_config.py`/`test_sse_hub.py`
    already do it: monkeypatched env vars, no dependence on a real `.env`.
    """
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "test-admin-secret-0123456789")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "y")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)

    calls = []

    async def fake_create_domain(self, name):
        calls.append(("create_domain", name))

    async def fake_create_account(self, email, display_name, password):
        calls.append(("create_account", email, display_name, password))
        return True  # freshly created -- the fresh-account path, unchanged by FINDING 2's fix

    async def fake_get_dkim_records(self, domain):
        calls.append(("get_dkim_records", domain))
        return [
            DkimRecord(
                host=f"v1-rsa-20260901._domainkey.{domain}",
                value="v=DKIM1; k=rsa; h=sha256; p=FAKEKEYDATA",
            )
        ]

    async def fake_outbound_relay_host(self):
        calls.append(("outbound_relay_host",))
        return None

    monkeypatch.setattr(StalwartAdmin, "create_domain", fake_create_domain)
    monkeypatch.setattr(StalwartAdmin, "create_account", fake_create_account)
    monkeypatch.setattr(StalwartAdmin, "get_dkim_records", fake_get_dkim_records)
    monkeypatch.setattr(StalwartAdmin, "outbound_relay_host", fake_outbound_relay_host)

    from mailosh.cli import app

    runner = typer.testing.CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "--domain",
            "spike.test",
            "--email",
            "admin@spike.test",
            "--password",
            "test-password-123",
        ],
    )

    assert result.exit_code == 0, result.output
    out = result.output
    assert "MX" in out
    assert "v=spf1 mx ~all" in out
    assert "v=DKIM1; k=rsa; h=sha256; p=FAKEKEYDATA" in out
    assert "v1-rsa-20260901._domainkey.spike.test" in out
    assert "v=DMARC1; p=none; rua=mailto:dmarc@spike.test" in out
    assert "PTR" in out or "rDNS" in out or "reverse DNS" in out
    # Fresh creation (fake_create_account returns True) with an explicit
    # --password: neither a "Generated password" echo (nothing was
    # generated) nor the FINDING 2 "already existed" warning belongs here.
    assert "Generated password" not in out
    assert "already existed" not in out

    assert calls == [
        ("create_domain", "spike.test"),
        ("create_account", "admin@spike.test", "Admin", "test-password-123"),
        ("get_dkim_records", "spike.test"),
        ("outbound_relay_host",),
    ]
    # setup never mints a per-user token -- try_mint_user_token is a
    # standalone SPK-3 probe, not part of the account-setup flow.
    assert all(c[0] != "try_mint_user_token" for c in calls)


def test_setup_cli_generates_and_prints_a_password_when_omitted(monkeypatch):
    """Fresh-account path, unchanged by FINDING 2's fix: `fake_create_account`
    returns `True` (freshly created), so the generated password really was
    applied and printing it is accurate.
    """
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "test-admin-secret-0123456789")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "y")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)

    seen_passwords = []

    async def fake_create_domain(self, name):
        pass

    async def fake_create_account(self, email, display_name, password):
        seen_passwords.append(password)
        return True  # freshly created

    async def fake_get_dkim_records(self, domain):
        return [DkimRecord(host="sel._domainkey.spike.test", value="v=DKIM1; k=rsa; h=sha256; p=X")]

    async def fake_outbound_relay_host(self):
        return None

    monkeypatch.setattr(StalwartAdmin, "create_domain", fake_create_domain)
    monkeypatch.setattr(StalwartAdmin, "create_account", fake_create_account)
    monkeypatch.setattr(StalwartAdmin, "get_dkim_records", fake_get_dkim_records)
    monkeypatch.setattr(StalwartAdmin, "outbound_relay_host", fake_outbound_relay_host)

    from mailosh.cli import app

    runner = typer.testing.CliRunner()
    result = runner.invoke(app, ["setup", "--domain", "spike.test", "--email", "admin@spike.test"])

    assert result.exit_code == 0, result.output
    assert len(seen_passwords) == 1
    generated = seen_passwords[0]
    assert len(generated) >= 16  # secrets.token_urlsafe(18) is well over this
    assert generated in result.output  # printed so the operator can capture it
    assert "already existed" not in result.output


def test_setup_cli_warns_and_omits_password_when_account_already_existed(monkeypatch):
    """FINDING 2 fix (code review): `create_account` returning `False`
    (idempotent no-op -- the account already existed, so `password` here
    was never applied) must make `setup` print an explicit warning instead
    of a "Generated password" line that would misrepresent an unapplied
    password as the account's real, current one.
    """
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "test-admin-secret-0123456789")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "y")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)

    async def fake_create_domain(self, name):
        pass

    async def fake_create_account(self, email, display_name, password):
        return False  # already existed

    async def fake_get_dkim_records(self, domain):
        return [DkimRecord(host="sel._domainkey.spike.test", value="v=DKIM1; k=rsa; h=sha256; p=X")]

    async def fake_outbound_relay_host(self):
        return None

    monkeypatch.setattr(StalwartAdmin, "create_domain", fake_create_domain)
    monkeypatch.setattr(StalwartAdmin, "create_account", fake_create_account)
    monkeypatch.setattr(StalwartAdmin, "get_dkim_records", fake_get_dkim_records)
    monkeypatch.setattr(StalwartAdmin, "outbound_relay_host", fake_outbound_relay_host)

    from mailosh.cli import app

    runner = typer.testing.CliRunner()
    result = runner.invoke(app, ["setup", "--domain", "spike.test", "--email", "admin@spike.test"])

    assert result.exit_code == 0, result.output
    assert "already existed" in result.output
    assert "NOT changed" in result.output
    assert "Generated password" not in result.output


def test_setup_cli_warns_when_explicit_password_given_but_account_already_existed(monkeypatch):
    """Same FINDING 2 warning, the other sub-case the ruling calls out
    explicitly: `--password` was supplied (not generated) but still
    silently ignored because the account already existed -- must warn the
    same way, not stay silent just because nothing was "generated".
    """
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "test-admin-secret-0123456789")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "y")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)

    async def fake_create_domain(self, name):
        pass

    async def fake_create_account(self, email, display_name, password):
        return False  # already existed -- the given --password was ignored

    async def fake_get_dkim_records(self, domain):
        return [DkimRecord(host="sel._domainkey.spike.test", value="v=DKIM1; k=rsa; h=sha256; p=X")]

    async def fake_outbound_relay_host(self):
        return None

    monkeypatch.setattr(StalwartAdmin, "create_domain", fake_create_domain)
    monkeypatch.setattr(StalwartAdmin, "create_account", fake_create_account)
    monkeypatch.setattr(StalwartAdmin, "get_dkim_records", fake_get_dkim_records)
    monkeypatch.setattr(StalwartAdmin, "outbound_relay_host", fake_outbound_relay_host)

    from mailosh.cli import app

    runner = typer.testing.CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "--domain",
            "spike.test",
            "--email",
            "admin@spike.test",
            "--password",
            "my-explicit-password",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "already existed" in result.output
    assert "NOT changed" in result.output
    assert "my-explicit-password" not in result.output  # never echoed as if it were now live


# ---------------------------------------------------------------------------
# `_format_dns_block` -- the records an operator actually pastes
# ---------------------------------------------------------------------------


def test_dns_block_includes_an_a_record_for_the_mx_target():
    """The MX stanza points at `mail.<domain>`; without an A/AAAA record for
    that name the MX target does not resolve and no mail arrives at all.

    This is the failure mode the stanza exists to prevent, and it is the
    quietest one available: a sending MTA that cannot resolve the MX target
    never connects, so nothing is logged on this side either. The block used
    to print the MX without ever mentioning the record it depends on.
    """
    from mailosh.cli import _format_dns_block

    out = _format_dns_block(
        "mailosh.com",
        DkimRecord(host="sel._domainkey.mailosh.com", value="v=DKIM1; k=rsa; p=X"),
    )

    assert "A / AAAA" in out
    assert "AAAA" in out
    assert "Host:  mail.mailosh.com" in out
    # The A stanza has to come before the MX stanza that depends on it: the
    # block is read top to bottom and acted on in that order.
    assert out.index("A / AAAA") < out.index("MX record")
    # No invented address -- the CLI cannot know the box's public IP, and a
    # plausible-looking placeholder would get published verbatim.
    assert "<this server's public IPv4 address>" in out


def test_dns_block_gives_the_relay_spf_variant_next_to_the_direct_send_one():
    """`v=spf1 mx ~all` authorises the MX host's own addresses, which is
    correct only for direct send on port 25. `docs/hosting.md` recommends
    relay mode for most deployments (most budget VPS providers block
    outbound 25), and under a relay the sending IP is the relay's -- not
    covered by `mx`, so outbound mail fails SPF at the receiver. Both must
    appear, with which is which stated.
    """
    from mailosh.cli import _format_dns_block

    out = _format_dns_block(
        "mailosh.com",
        DkimRecord(host="sel._domainkey.mailosh.com", value="v=DKIM1; k=rsa; p=X"),
    )

    assert "v=spf1 mx ~all" in out
    assert "DIRECT SEND" in out
    assert "RELAY" in out
    assert "include:amazonses.com" in out
    assert "include:spf.smtp2go.com" in out
    assert "hosting.md" in out


def test_dns_block_still_carries_mx_dkim_dmarc_and_the_ptr_reminder():
    """Regression guard for the records that were already there, so the
    A/AAAA and SPF-relay additions cannot quietly displace one.
    """
    from mailosh.cli import _format_dns_block

    out = _format_dns_block(
        "mailosh.com",
        DkimRecord(host="v1-rsa-20260901._domainkey.mailosh.com", value="v=DKIM1; k=rsa; p=ABC"),
    )

    assert "  Value:    mail.mailosh.com" in out  # MX target
    assert "  Priority: 10" in out
    assert "v1-rsa-20260901._domainkey.mailosh.com" in out
    assert "v=DKIM1; k=rsa; p=ABC" in out
    assert "_dmarc.mailosh.com" in out
    assert "v=DMARC1; p=none; rua=mailto:dmarc@mailosh.com" in out
    assert "reverse DNS (PTR)" in out


# ---------------------------------------------------------------------------
# Published ports vs. listening ports
# ---------------------------------------------------------------------------
# Not a test of `StalwartAdmin`, but of the same server's setup, and it lives
# here because it is the assertion that closes a real bug: both compose files
# published `587` while Stalwart `v0.16.20` ran no listener there, and the dev
# one also published `1143 -> 143`. A published port with nothing behind it is
# the worst shape that failure can take -- Docker's proxy accepts the
# connection on the host and then drops it, so the client sees a hang or a
# reset, the packet never reaches Stalwart, and nothing appears in any log.
#
# The compose files are parsed with a regex rather than a YAML parser on
# purpose: `docker-compose.prod.yml` uses Compose's `!override` / `!reset`
# tags, which PyYAML rejects, and PyYAML is not a declared dependency of this
# project in any case.

#: Stalwart v0.16.20's default post-bootstrap listener set, read from its own
#: `x:NetworkListener` objects on a freshly bootstrapped server and confirmed
#: by opening a TCP connection to each *from inside the container*.
STALWART_DEFAULT_LISTENER_PORTS = frozenset({25, 443, 993, 995, 4190, 8080, 465})

#: Plus the one `scripts/stalwart-bootstrap.sh` creates, because clients need
#: it and Stalwart does not ship it. Guarded by
#: `test_bootstrap_script_creates_the_587_submission_listener` below: these
#: two facts are only allowed to move together.
STALWART_LISTENER_PORTS = STALWART_DEFAULT_LISTENER_PORTS | {587}

_PORT_MAPPING = re.compile(r'"(?:(?P<ip>[0-9.]+):)?(?P<host>\d+):(?P<container>\d+)(?:/\w+)?"')


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _published_ports(compose_file: str, service: str) -> set[int]:
    """The *container-side* ports a compose file publishes for one service.

    Container-side, not host-side: the question this answers is "is something
    listening behind it", and that is a property of the container.
    """
    path = _repo_root() / compose_file
    lines = path.read_text(encoding="utf-8").splitlines()

    in_service = False
    in_ports = False
    ports: set[int] = set()
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        # A service header: two-space indent, no deeper.
        if raw.startswith("  ") and not raw.startswith("   ") and stripped.endswith(":"):
            in_service = stripped[:-1] == service
            in_ports = False
            continue
        if not in_service:
            continue
        if stripped.startswith("ports:"):
            in_ports = True
            ports.update(int(m.group("container")) for m in _PORT_MAPPING.finditer(stripped))
            continue
        if in_ports:
            if stripped.startswith("- "):
                ports.update(int(m.group("container")) for m in _PORT_MAPPING.finditer(stripped))
                continue
            in_ports = False
    return ports


def test_every_published_stalwart_port_has_a_listener_behind_it():
    """The bug this file's last section exists for, in one assertion.

    If someone publishes a port Stalwart does not listen on, a mail client
    pointed at it gets a refused-or-dropped connection that no log explains.
    """
    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        published = _published_ports(compose_file, "stalwart")
        assert published, f"{compose_file} publishes no stalwart ports -- parser broke?"
        unlistened = published - STALWART_LISTENER_PORTS
        assert not unlistened, (
            f"{compose_file} publishes {sorted(unlistened)}, which nothing listens on. "
            "Either add the listener in scripts/stalwart-bootstrap.sh or stop publishing it."
        )


def test_the_compose_files_publish_the_submission_port_clients_default_to():
    """587 is what Thunderbird, Apple Mail, iOS Mail and Outlook default to.

    The fix for "587 is published and nothing listens" was to add the
    listener, not to unpublish the port; this keeps it that way round.
    """
    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        assert 587 in _published_ports(compose_file, "stalwart"), (
            f"{compose_file} no longer publishes 587. "
            "scripts/stalwart-bootstrap.sh configures a listener there precisely so it can be."
        )


def test_plaintext_imap_is_neither_published_nor_implied():
    """143 went the other way: the published port was removed.

    993 covers every modern client, and the deployment spec records 143 as
    opt-in rather than default. Re-adding it means adding a
    plaintext-capable listener too -- which is a decision, not an oversight.
    """
    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        assert 143 not in _published_ports(compose_file, "stalwart")
    assert 143 not in STALWART_LISTENER_PORTS


def test_bootstrap_script_creates_the_587_submission_listener():
    """Ties the compose assertion above to the thing that makes it true.

    `useTls: true` with `tlsImplicit: false` is STARTTLS rather than implicit
    TLS -- 465 is the implicit-TLS port and already exists. If this creation
    is ever removed, 587 becomes a published port with nothing behind it
    again, so the two must move together.
    """
    script = (_repo_root() / "scripts" / "stalwart-bootstrap.sh").read_text(encoding="utf-8")

    assert "x:NetworkListener/set" in script
    assert "SUBMISSION_PORT=587" in script
    assert r"\"bind\":{\"[::]:$SUBMISSION_PORT\":true}" in script
    assert r"\"useTls\":true" in script
    assert r"\"tlsImplicit\":false" in script
    # And that it checks the result rather than trusting HTTP 200 -- the
    # failure mode the whole script is built around.
    assert "extract listener-set" in script


def test_bootstrap_script_makes_stalwart_log_to_stdout():
    """A configured Stalwart logs to a file under /var/log/stalwart/, a
    directory the container image does not have and the compose file does
    not mount -- so nothing it wrote after first boot went anywhere. Found
    on the first public deployment. The script creates a stdout tracer
    (what `docker compose logs stalwart` shows) and disables a file tracer
    whose directory is missing, checking each result rather than the status.
    """
    script = (_repo_root() / "scripts" / "stalwart-bootstrap.sh").read_text(encoding="utf-8")

    assert "x:Tracer/query" in script and "x:Tracer/get" in script
    assert r"\"@type\":\"Stdout\",\"enable\":true,\"level\":\"info\"" in script
    assert r"\"update\":{\"$tid\":{\"enable\":false}}" in script
    assert 'sw_exec test -d "$tpath"' in script
    assert "extract tracers" in script and "extract tracer-set" in script
    # Wired into the bootstrap path and covered by the restart that applies it.
    assert "ensure_stdout_tracer\n" in script
    assert '[ "$TRACER_CHANGED" = yes ]' in script


# ---------------------------------------------------------------------------
# get_dkim_records / outbound_relay_host, and the DNS block they feed
# (ops-hardening: `mailosh setup` prints BOTH DKIM records, and the SPF
# record that matches the server's actual outbound mode)
# ---------------------------------------------------------------------------


async def test_get_dkim_records_returns_both_algorithms_rsa_first(admin):
    """Stalwart signs with both of the keys it generates; publishing only
    the RSA record (what `get_dkim_record` alone gave the CLI) leaves the
    Ed25519 signature failing at every verifier. Same live-shaped fixture
    as the singular test above; the order is `_DKIM_PREFERENCE`."""
    respx.post(f"{BASE}/jmap").mock(
        side_effect=_route_by_first_method(
            {"x:Domain/query": _DOMAIN_QUERY_RESPONSE, "x:DkimSignature/query": _DKIM_GET_RESPONSE}
        )
    )
    records = await admin.get_dkim_records("spike.test")
    assert [r.host for r in records] == [
        "v1-rsa-20260901._domainkey.spike.test",
        "v1-ed25519-20260901._domainkey.spike.test",
    ]
    assert records[0].value.startswith("v=DKIM1; k=rsa; h=sha256; p=MIIBIjAN")
    assert (
        records[1].value
        == "v=DKIM1; k=ed25519; h=sha256; p=Kw17WZ0LB4WdimjrMQ2aKjwXIYBZHypFPsKmRPg3Cos="
    )


async def test_get_dkim_records_raises_when_domain_missing(admin):
    respx.post(f"{BASE}/jmap").respond(
        json={
            "methodResponses": [
                ["x:Domain/query", {"accountId": "admin-acct", "ids": []}, "q0"],
                ["x:Domain/get", {"accountId": "admin-acct", "list": [], "notFound": []}, "g0"],
            ],
            "sessionState": "s1",
        }
    )
    with pytest.raises(JmapError, match="does not exist"):
        await admin.get_dkim_records("nope.test")


#: The outbound singleton and route list exactly as the live server
#: returned them after `scripts/stalwart-bootstrap.sh --relay-host
#: relay.mailosh.test:587` (docs/operations.md, "Relay mode") -- the
#: `match` map is index-keyed, the literal is single-quoted.
_OUTBOUND_RELAY_RESPONSE = {
    "methodResponses": [
        [
            "x:MtaOutboundStrategy/get",
            {
                "accountId": "admin-acct",
                "list": [
                    {
                        "id": "singleton",
                        "route": {
                            "match": {
                                "0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}
                            },
                            "else": "'relay'",
                        },
                    }
                ],
            },
            "s0",
        ],
        ["x:MtaRoute/query", {"accountId": "admin-acct", "ids": ["r1", "r2", "r3"]}, "q0"],
        [
            "x:MtaRoute/get",
            {
                "accountId": "admin-acct",
                "list": [
                    {
                        "id": "r1",
                        "name": "relay",
                        "@type": "Relay",
                        "address": "relay.mailosh.test",
                        "port": 587,
                    },
                    {"id": "r2", "name": "local", "@type": "Local"},
                    {"id": "r3", "name": "mx", "@type": "Mx"},
                ],
            },
            "g0",
        ],
    ],
    "sessionState": "s1",
}


async def test_outbound_relay_host_reads_the_relay_route_the_strategy_points_at(admin):
    respx.post(f"{BASE}/jmap").respond(json=_OUTBOUND_RELAY_RESPONSE)
    assert await admin.outbound_relay_host() == "relay.mailosh.test:587"


async def test_outbound_relay_host_is_none_for_stock_direct_delivery(admin):
    """A stock server's `else` is `'mx'`, a route of `@type: Mx` -- not a
    relay, so `None`, and the CLI prints the direct-send SPF."""
    stock = json.loads(json.dumps(_OUTBOUND_RELAY_RESPONSE))
    stock["methodResponses"][0][1]["list"][0]["route"]["else"] = "'mx'"
    respx.post(f"{BASE}/jmap").respond(json=stock)
    assert await admin.outbound_relay_host() is None


async def test_outbound_relay_host_is_none_when_nothing_is_recognisable(admin):
    """No singleton at all (an older or oddly-configured server) must not
    break `setup` -- the DNS hint degrades to the direct-send default."""
    respx.post(f"{BASE}/jmap").respond(
        json={
            "methodResponses": [
                ["x:MtaOutboundStrategy/get", {"accountId": "admin-acct", "list": []}, "s0"],
                ["x:MtaRoute/query", {"accountId": "admin-acct", "ids": []}, "q0"],
                ["x:MtaRoute/get", {"accountId": "admin-acct", "list": []}, "g0"],
            ],
            "sessionState": "s1",
        }
    )
    assert await admin.outbound_relay_host() is None


def test_dns_block_prints_every_dkim_record_and_says_both_are_needed():
    from mailosh.cli import _format_dns_block

    out = _format_dns_block(
        "mailosh.com",
        [
            DkimRecord(host="v1-rsa-2026._domainkey.mailosh.com", value="v=DKIM1; k=rsa; p=R"),
            DkimRecord(
                host="v1-ed25519-2026._domainkey.mailosh.com", value="v=DKIM1; k=ed25519; p=E"
            ),
        ],
    )
    assert "v1-rsa-2026._domainkey.mailosh.com" in out
    assert "v=DKIM1; k=rsa; p=R" in out
    assert "v1-ed25519-2026._domainkey.mailosh.com" in out
    assert "v=DKIM1; k=ed25519; p=E" in out
    assert "2 of them" in out
    assert "signs every message with BOTH keys" in out
    # RSA first: the record every verifier supports is the one an operator
    # pastes first if they only paste one.
    assert out.index("k=rsa") < out.index("k=ed25519")


def test_dns_block_switches_spf_to_the_relay_include_when_relaying():
    """With a relay configured, `v=spf1 mx ~all` is simply wrong for this
    server, so it is not offered as the value at all; the include form is,
    naming the relay the server actually routes through."""
    from mailosh.cli import _format_dns_block

    out = _format_dns_block(
        "mailosh.com",
        DkimRecord(host="sel._domainkey.mailosh.com", value="v=DKIM1; k=rsa; p=X"),
        relay_host="email-smtp.eu-west-1.amazonaws.com:587",
    )
    assert "Value: v=spf1 mx ~all" not in out
    assert "Value: v=spf1 include:<your relay's SPF include> ~all" in out
    assert "RELAYS outbound mail through email-smtp.eu-west-1.amazonaws.com:587" in out
    assert "include:amazonses.com" in out
    # The direct-send paragraph (and its "if you use a relay" caveat) is
    # not relevant to a server that already relays.
    assert "DIRECT SEND" not in out


def test_setup_cli_prints_both_dkim_records_and_the_relay_spf(monkeypatch):
    """End to end through the Typer command: two records from
    `get_dkim_records`, a relay from `outbound_relay_host`, both reflected
    in the printed block."""
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "test-admin-secret-0123456789")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "y")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)

    async def fake_create_domain(self, name):
        pass

    async def fake_create_account(self, email, display_name, password):
        return True

    async def fake_get_dkim_records(self, domain):
        return [
            DkimRecord(host=f"v1-rsa-1._domainkey.{domain}", value="v=DKIM1; k=rsa; p=R"),
            DkimRecord(host=f"v1-ed25519-1._domainkey.{domain}", value="v=DKIM1; k=ed25519; p=E"),
        ]

    async def fake_outbound_relay_host(self):
        return "smtp.smtp2go.com:587"

    monkeypatch.setattr(StalwartAdmin, "create_domain", fake_create_domain)
    monkeypatch.setattr(StalwartAdmin, "create_account", fake_create_account)
    monkeypatch.setattr(StalwartAdmin, "get_dkim_records", fake_get_dkim_records)
    monkeypatch.setattr(StalwartAdmin, "outbound_relay_host", fake_outbound_relay_host)

    from mailosh.cli import app

    runner = typer.testing.CliRunner()
    result = runner.invoke(
        app, ["setup", "--domain", "spike.test", "--email", "a@spike.test", "--password", "p"]
    )
    assert result.exit_code == 0, result.output
    assert "v1-rsa-1._domainkey.spike.test" in result.output
    assert "v1-ed25519-1._domainkey.spike.test" in result.output
    assert "RELAYS outbound mail through smtp.smtp2go.com:587" in result.output
    assert "Value: v=spf1 mx ~all" not in result.output
