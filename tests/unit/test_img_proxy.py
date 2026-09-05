"""Admission control for the image proxy: the signed `?u=` token.

`GET /img?u=<token>` fetches a URL and hands the bytes to a browser. The
token is the only thing that says "this app produced this URL, for this
reader, recently" — without it the route is an open proxy with a query string,
and `mailosh.render.fetch_guard` becomes the *first* line of defence instead of
the second. Every test here is an attempt to get a URL past that check:
tamper with the payload, re-sign it under another key, keep one past its hour,
or confuse the payload's types.

**The verifier under test is `verify_image_token`**, which is the one the
route calls (`mailosh.web.frames.image_proxy`). It takes no user id: the
request arrives from an opaque-origin document that sends no cookie, so there
is no session to compare against and the token *is* the statement of who is
asking — a bearer capability, unforgeable, naming exactly one URL, dead in an
hour. The reader id it returns is what keys the route's per-reader fan-out
semaphore.

This file is the payload-level attack suite. The route's own behaviour — the
allow-listed content types, `403` for a token this app did not mint, `502` on
a blocked target, the fan-out cap — lives in `tests/unit/test_frame_routes.py`
alongside the `token_for` fixture, and the signer's wiring onto
`mailosh.security.signing` (purpose `"img"`, `IMAGE_URL_TTL`) is asserted in
`tests/unit/test_image_policy.py`.
"""

from __future__ import annotations

import json
import time

import pytest

from mailosh.render.image_policy import IMAGE_URL_TTL, sign_remote_url, verify_image_token
from mailosh.security.signing import _b64u_decode, _b64u_encode, sign_payload

KEY = "k" * 40
OTHER_KEY = "j" * 40
URL = "https://cdn.test/a.png"


def _payload_of(token: str) -> dict:
    return json.loads(_b64u_decode(token.split(".")[0]))


# --------------------------------------------------------------------------
# The contract the route depends on
# --------------------------------------------------------------------------


def test_signed_url_roundtrips_and_expires():
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    assert verify_image_token(tok, secret_key=KEY, now=1500.0) == (URL, 7)
    with pytest.raises(ValueError):
        verify_image_token(tok, secret_key=KEY, now=1000.0 + IMAGE_URL_TTL + 1)


def test_the_signature_is_bound_to_the_secret_and_the_payload_names_its_reader():
    """Two separate properties. The secret binding is what makes the token
    unforgeable at all; naming the reader is what the route reads back to key
    its fan-out budget, so a token that verified but came back with the wrong
    id would spend somebody else's socket allowance.
    """
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    assert verify_image_token(tok, secret_key=KEY, now=1000.0) == (URL, 7)
    with pytest.raises(ValueError):
        verify_image_token(tok, secret_key=OTHER_KEY, now=1000.0)


def test_tampering_with_the_payload_is_rejected():
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    payload, sig = tok.split(".")
    with pytest.raises(ValueError):
        verify_image_token(payload[:-2] + "AA." + sig, secret_key=KEY, now=1000.0)


def test_the_ttl_is_the_hour_the_plan_names():
    assert IMAGE_URL_TTL == 3600
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    assert _payload_of(tok)["e"] == 1000.0 + 3600


def test_signing_defaults_to_the_current_clock():
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7)
    assert verify_image_token(tok, secret_key=KEY) == (URL, 7)
    assert abs(_payload_of(tok)["e"] - (time.time() + IMAGE_URL_TTL)) < 30


# --------------------------------------------------------------------------
# The reader in the payload
# --------------------------------------------------------------------------


def test_the_same_url_signs_differently_for_two_users():
    """A token is not a function of the URL alone — otherwise every reader
    would hold every other reader's token for the same newsletter image, and
    the id the route reads back off it would say nothing about who is asking.
    """
    a = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    b = sign_remote_url(URL, secret_key=KEY, user_id=8, now=1000.0)
    assert a != b
    assert verify_image_token(a, secret_key=KEY, now=1000.0) == (URL, 7)
    assert verify_image_token(b, secret_key=KEY, now=1000.0) == (URL, 8)


def test_a_true_user_id_does_not_come_back_as_user_one():
    """`True == 1` in Python, so a payload carrying `{"s": true}` reads back
    as user 1 under a naive comparison — and the route would key its fan-out
    semaphore on a reader who never asked for anything. Nothing but this app
    can mint such a payload, which is exactly why the check belongs in the
    verifier rather than in the caller that happens to be careful today.
    """
    forged = sign_payload(
        {"u": URL, "s": True}, secret_key=KEY, purpose="img", ttl=IMAGE_URL_TTL, now=1000.0
    )
    with pytest.raises(ValueError):
        verify_image_token(forged, secret_key=KEY, now=1000.0)


@pytest.mark.parametrize("subject", ["7", 7.0, None, [7], {"id": 7}])
def test_a_user_id_of_the_wrong_type_is_refused(subject):
    forged = sign_payload(
        {"u": URL, "s": subject}, secret_key=KEY, purpose="img", ttl=IMAGE_URL_TTL, now=1000.0
    )
    with pytest.raises(ValueError):
        verify_image_token(forged, secret_key=KEY, now=1000.0)


@pytest.mark.parametrize("url", [None, 7, ["https://cdn.test/a.png"], {"u": "x"}])
def test_a_url_of_the_wrong_type_is_refused(url):
    forged = sign_payload(
        {"u": url, "s": 7}, secret_key=KEY, purpose="img", ttl=IMAGE_URL_TTL, now=1000.0
    )
    with pytest.raises(ValueError):
        verify_image_token(forged, secret_key=KEY, now=1000.0)


def test_a_payload_missing_a_field_is_refused():
    for payload in ({"u": URL}, {"s": 7}, {}):
        forged = sign_payload(payload, secret_key=KEY, purpose="img", ttl=IMAGE_URL_TTL, now=1000.0)
        with pytest.raises(ValueError):
            verify_image_token(forged, secret_key=KEY, now=1000.0)


# --------------------------------------------------------------------------
# Forgery
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "nonsense",
        ".",
        "..",
        "a.b.c",
        "eyJ1IjoiaHR0cDovLzEyNy4wLjAuMS8ifQ",  # payload with no signature at all
        "eyJ1IjoiaHR0cDovLzEyNy4wLjAuMS8ifQ.",
        ".AAAA",
        "!!!.???",
    ],
)
def test_a_string_that_is_not_a_token_is_refused(token):
    with pytest.raises(ValueError):
        verify_image_token(token, secret_key=KEY, now=1000.0)


def test_an_unsigned_payload_cannot_be_bolted_onto_a_borrowed_signature():
    """The forgery a proxy invites: keep a signature that verified once and
    swap the URL underneath it.
    """
    good = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    _, signature = good.split(".")
    hostile = _b64u_encode(
        json.dumps({"e": 4600.0, "s": 7, "u": "http://169.254.169.254/"}).encode()
    )
    with pytest.raises(ValueError):
        verify_image_token(f"{hostile}.{signature}", secret_key=KEY, now=1000.0)


def test_a_signature_from_another_token_does_not_transfer():
    a = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    b = sign_remote_url("https://cdn.test/b.png", secret_key=KEY, user_id=7, now=1000.0)
    with pytest.raises(ValueError):
        verify_image_token(f"{a.split('.')[0]}.{b.split('.')[1]}", secret_key=KEY)


def test_extending_the_expiry_invalidates_the_signature():
    """The expiry is inside the MAC, so a holder cannot renew their own token."""
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    payload = _payload_of(tok)
    payload["e"] = 10**12
    renewed = _b64u_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ValueError):
        verify_image_token(f"{renewed}.{tok.split('.')[1]}", secret_key=KEY, now=1000.0)


def test_a_token_minted_for_another_purpose_is_not_an_image_token():
    """Domain separation, end to end: a token signed under any other purpose
    — an inline-part link, an undo token — must not spend as an image URL, or
    one feature's leak becomes the proxy's.
    """
    for purpose in ("undo", "cid", "sessions", "im", "imgx"):
        alien = sign_payload(
            {"u": URL, "s": 7}, secret_key=KEY, purpose=purpose, ttl=IMAGE_URL_TTL, now=1000.0
        )
        with pytest.raises(ValueError):
            verify_image_token(alien, secret_key=KEY, now=1000.0)


def test_a_padded_or_otherwise_respelled_token_is_refused():
    """One token, one spelling. Base64 that decodes to the same bytes but is
    written differently must not verify, or anything comparing token strings
    (a log, a cache key, a rate limiter) can be slipped past.
    """
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    payload, signature = tok.split(".")
    for respelled in (f"{payload}==.{signature}", f"{payload}.{signature}=", f" {tok}"):
        with pytest.raises(ValueError):
            verify_image_token(respelled, secret_key=KEY, now=1000.0)


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_the_expiry_boundary_is_inclusive_then_closed():
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=1000.0)
    assert verify_image_token(tok, secret_key=KEY, now=1000.0 + IMAGE_URL_TTL) == (URL, 7)
    with pytest.raises(ValueError):
        verify_image_token(tok, secret_key=KEY, now=1000.0 + IMAGE_URL_TTL + 0.001)


def test_a_token_from_the_far_future_still_expires():
    """Clock skew is not a bypass: an hour is an hour from whenever it was
    minted, and a token minted "later" than now is simply still valid.

    It is the only limit the token has. Nothing revokes one early — there is
    no server-side record that it was ever issued — so the hour is the whole
    of its lifetime and this test is what pins that hour down.
    """
    tok = sign_remote_url(URL, secret_key=KEY, user_id=7, now=10**9)
    assert verify_image_token(tok, secret_key=KEY, now=10**9) == (URL, 7)
    with pytest.raises(ValueError):
        verify_image_token(tok, secret_key=KEY, now=10**9 + IMAGE_URL_TTL + 1)


# --------------------------------------------------------------------------
# The URL survives intact
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.test/a.png",
        "https://cdn.test/a.png?w=100&h=50&sig=abc%3Ddef",
        "https://cdn.test/tracking/open.gif?id=" + "x" * 512,
        "https://cdn.test/café.png",
        "https://cdn.test/a.png#frag",
    ],
)
def test_the_url_comes_back_byte_for_byte(url):
    """A signed URL that came back subtly different would be fetched from a
    different place than the one that was signed.
    """
    tok = sign_remote_url(url, secret_key=KEY, user_id=7, now=1000.0)
    assert verify_image_token(tok, secret_key=KEY, now=1000.0) == (url, 7)


def test_signing_takes_no_view_on_whether_the_url_is_fetchable():
    """Deliberate: `fetch_guard` runs against the URL again at fetch time and
    against every redirect after it. A second, weaker copy of that judgement
    here would invite someone to trust the token instead of the guard.
    """
    hostile = "http://169.254.169.254/latest/meta-data/"
    tok = sign_remote_url(hostile, secret_key=KEY, user_id=7, now=1000.0)
    assert verify_image_token(tok, secret_key=KEY, now=1000.0) == (hostile, 7)
