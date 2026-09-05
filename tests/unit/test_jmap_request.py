import json
import pathlib

import pytest
import respx

from mailosh.jmap.client import JmapClient
from mailosh.jmap.errors import JmapError, MethodError, TransportError

SESSION = json.loads(pathlib.Path("tests/fixtures/session.json").read_text())

# session.json's apiUrl is "https://mail.mailosh.test/jmap/" (trailing slash);
# Session.rebase("http://s") swaps scheme+host only, so the rebased apiUrl is
# "http://s/jmap/" — mock that exact path, not a guessed "http://s/jmap".


@respx.mock
async def test_connect_and_batched_call():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    api = respx.post("http://s/jmap/").respond(
        json={"methodResponses": [["Mailbox/get", {"list": []}, "c0"]], "sessionState": "x"}
    )
    c = await JmapClient.connect("http://s", "u", "p")
    out = await c._call([("Mailbox/get", {"accountId": c.account_id}, "c0")])
    assert out["c0"] == {"list": []}
    body = json.loads(api.calls[0].request.content)
    assert body["using"] == [
        "urn:ietf:params:jmap:core",
        "urn:ietf:params:jmap:mail",
        "urn:ietf:params:jmap:submission",
    ]
    assert body["methodCalls"][0][0] == "Mailbox/get"


@respx.mock
async def test_method_error_raises():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    respx.post("http://s/jmap/").respond(
        json={"methodResponses": [["error", {"type": "unknownMethod"}, "c0"]], "sessionState": "x"}
    )
    c = await JmapClient.connect("http://s", "u", "p")
    with pytest.raises(MethodError):
        await c._call([("Nope/get", {}, "c0")])


@respx.mock
async def test_call_batch_error_carries_correct_call_id():
    # Finding 1: a batch with an earlier success (c0) and a later error (c1)
    # must raise MethodError attributed to c1, not misattributed to c0 or to
    # "whichever tuple happens to be first".
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    respx.post("http://s/jmap/").respond(
        json={
            "methodResponses": [
                ["Mailbox/get", {"list": []}, "c0"],
                ["error", {"type": "invalidArguments"}, "c1"],
            ],
            "sessionState": "x",
        }
    )
    c = await JmapClient.connect("http://s", "u", "p")
    with pytest.raises(MethodError) as exc_info:
        await c._call(
            [
                ("Mailbox/get", {"accountId": c.account_id}, "c0"),
                ("Nope/get", {}, "c1"),
            ]
        )
    assert exc_info.value.call_id == "c1"
    assert exc_info.value.type == "invalidArguments"


@respx.mock
async def test_connect_http_error_raises_transport_error():
    # Finding 3, site 1: GET /.well-known/jmap's raise_for_status().
    respx.get("http://s/.well-known/jmap").respond(status_code=500)
    with pytest.raises(TransportError) as exc_info:
        await JmapClient.connect("http://s", "u", "p")
    assert exc_info.value.status_code == 500


@respx.mock
async def test_call_http_error_raises_transport_error():
    # Finding 3, site 2: POST {apiUrl}'s raise_for_status().
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    respx.post("http://s/jmap/").respond(status_code=500, json={"error": "boom"})
    c = await JmapClient.connect("http://s", "u", "p")
    with pytest.raises(TransportError) as exc_info:
        await c._call([("Mailbox/get", {"accountId": c.account_id}, "c0")])
    assert exc_info.value.status_code == 500


@respx.mock
async def test_call_keeps_first_response_for_a_duplicate_call_id():
    # Found live against Stalwart (Task 9): an RFC 8620 §5.3 implicit method
    # call (e.g. the Email/set update send()'s onSuccessUpdateEmail
    # triggers) is appended to methodResponses *after* the explicit call's
    # own response, but the RFC never requires its id to differ — Stalwart
    # reuses the triggering explicit call's own id. _call must keep the
    # *first* (explicit) response for a given call id, not silently let a
    # later same-id (implicit) one clobber it.
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    respx.post("http://s/jmap/").respond(
        json={
            "methodResponses": [
                ["EmailSubmission/set", {"created": {"s0": {"id": "sub-1"}}}, "s0"],
                ["Email/set", {"updated": {"e1": None}}, "s0"],
            ],
            "sessionState": "x",
        }
    )
    c = await JmapClient.connect("http://s", "u", "p")
    out = await c._call(
        [
            ("EmailSubmission/set", {"accountId": c.account_id}, "s0"),
        ]
    )
    assert out["s0"] == {"created": {"s0": {"id": "sub-1"}}}


@respx.mock
async def test_call_missing_method_responses_raises_jmap_error():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    respx.post("http://s/jmap/").respond(json={"sessionState": "x"})
    c = await JmapClient.connect("http://s", "u", "p")
    with pytest.raises(JmapError):
        await c._call([("Mailbox/get", {"accountId": c.account_id}, "c0")])


# ---------------------------------------------------------------------------
# connect_bearer (Task 4) -- same session discovery as connect, but
# Authorization: Bearer <token> instead of HTTP Basic. The per-session
# Stalwart API key minted by StalwartAdmin.create_api_key authenticates
# this way (design spec §9).
# ---------------------------------------------------------------------------


@respx.mock
async def test_connect_bearer_sends_bearer_header_not_basic_auth():
    route = respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    c = await JmapClient.connect_bearer("http://s", "API_faketoken123")
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer API_faketoken123"
    assert c.account_id == SESSION["primaryAccounts"]["urn:ietf:params:jmap:mail"]
    await c.close()


@respx.mock
async def test_connect_bearer_can_then_make_a_batched_call():
    respx.get("http://s/.well-known/jmap").respond(json=SESSION)
    api = respx.post("http://s/jmap/").respond(
        json={"methodResponses": [["Mailbox/get", {"list": []}, "c0"]], "sessionState": "x"}
    )
    c = await JmapClient.connect_bearer("http://s", "API_faketoken123")
    out = await c._call([("Mailbox/get", {"accountId": c.account_id}, "c0")])
    assert out["c0"] == {"list": []}
    # the batched call carries the same Bearer header connect_bearer used —
    # this client never switches back to Basic auth for later requests.
    assert api.calls[0].request.headers["Authorization"] == "Bearer API_faketoken123"
    await c.close()


@respx.mock
async def test_connect_bearer_http_error_raises_transport_error():
    respx.get("http://s/.well-known/jmap").respond(401)
    with pytest.raises(TransportError) as exc_info:
        await JmapClient.connect_bearer("http://s", "API_revoked")
    assert exc_info.value.status_code == 401
