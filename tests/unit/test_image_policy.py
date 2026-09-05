"""The generic HMAC signer under the image proxy (Task 6).

`mailosh.security.signing` is the primitive that makes a *capability URL*
possible: a link handed to a browser and trusted back with no server-side
record of having issued it. `mailosh.render.image_policy` is its first caller
and, for now, its only one — so its wiring onto the signer (purpose `"img"`,
`IMAGE_URL_TTL`) is asserted here too, while the payload-level attacks on the
image token itself live in `tests/unit/test_img_proxy.py`.

The forgeries below reach for the module's private `_mac` on purpose. A test
that can only sign through `sign_payload` can only produce payloads
`sign_payload` is willing to make, and the shapes worth defending against —
an expiry that is a string, a payload that is a JSON list, a `"s"` that is
`true` — are exactly the ones it will not make. Reaching for `_mac` is how
this file asks "and if a *validly signed* token said that?", which is the
question a verifier has to be able to answer.

The other half of `image_policy` — `ImageDecision`, `decide`, `allow_sender`,
the per-sender preference — is exercised at the bottom of this file, against a
real (aiosqlite) session rather than a mocked one: every branch of `decide`
turns on whether a row is there, so a fake that answers "yes there is" would
be asserting the fake. The HTTP shape of the two controls the decision drives
lives in `tests/unit/test_frame_routes.py`.
"""

from __future__ import annotations

import dataclasses
import json
import string

import pytest
import pytest_asyncio

from mailosh.db import models
from mailosh.render.image_policy import (
    CID_URL_TTL,
    IMAGE_URL_TTL,
    ImageDecision,
    allow_sender,
    decide,
    sign_cid_url,
    sign_remote_url,
    verify_cid_token,
    verify_image_token,
)
from mailosh.security.crypto import derive_key
from mailosh.security.signing import (
    _b64u_decode,
    _b64u_encode,
    _mac,
    sign_payload,
    verify_payload,
)

KEY = "k" * 40
OTHER_KEY = "j" * 40
PURPOSE = "img"


def _forge(body: dict | list | str | int, *, purpose: str = PURPOSE, key: str = KEY) -> str:
    """A token this app would never mint, signed with a key it really holds."""
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64u_encode(raw)}.{_b64u_encode(_mac(key, purpose, raw))}"


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


def test_a_payload_comes_back_exactly_as_it_went_in():
    payload = {"u": "https://cdn.test/a.png", "s": 7, "nested": {"a": [1, 2]}, "uni": "café"}
    tok = sign_payload(payload, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    assert verify_payload(tok, secret_key=KEY, purpose=PURPOSE, now=1000.0) == payload


def test_the_expiry_is_stripped_so_no_caller_mistakes_it_for_its_own_data():
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    assert "e" not in verify_payload(tok, secret_key=KEY, purpose=PURPOSE, now=1000.0)
    assert json.loads(_b64u_decode(tok.split(".")[0]))["e"] == 1060.0


def test_the_expiry_key_is_reserved_at_signing_time():
    """A caller who supplied their own `"e"` would silently overwrite the
    expiry — or have it silently overwrite them. Loud instead.
    """
    with pytest.raises(ValueError):
        sign_payload({"e": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)


def test_a_token_is_signed_not_encrypted_and_says_so():
    """Anyone holding a token can read what is in it, so no caller may put a
    secret in one. Asserted rather than left as folklore.
    """
    tok = sign_payload({"u": "https://cdn.test/a.png"}, secret_key=KEY, purpose=PURPOSE, ttl=60)
    assert json.loads(_b64u_decode(tok.split(".")[0]))["u"] == "https://cdn.test/a.png"


def test_the_token_is_url_and_header_safe():
    tok = sign_payload(
        {"u": "https://cdn.test/a.png?a=b&c=d"}, secret_key=KEY, purpose="img", ttl=60
    )
    assert set(tok) <= set(string.ascii_letters + string.digits + "-_.")


def test_the_signature_is_a_full_sha256():
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60)
    assert len(_b64u_decode(tok.split(".")[1])) == 32


# --------------------------------------------------------------------------
# Domain separation
# --------------------------------------------------------------------------


def test_two_purposes_derive_two_unrelated_keys():
    keys = {derive_key(KEY, p) for p in (b"img", b"undo", b"sessions", b"att")}
    assert len(keys) == 4


def test_a_token_never_verifies_under_another_purpose():
    """Domain separation is the whole reason `purpose` exists: one feature's
    leaked token must not be spendable as another's.
    """
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose="img", ttl=60, now=1000.0)
    for other in ("undo", "att", "sessions", "IMG", "im", "imgx", " img"):
        with pytest.raises(ValueError):
            verify_payload(tok, secret_key=KEY, purpose=other, now=1000.0)


def test_an_undo_token_is_not_a_signed_payload():
    """`mailosh.services.undo` keeps its own signer (deflated payload, shaped
    for header size). The two must be mutually unreadable, in both directions.
    """
    from mailosh.services.undo import UndoSpec, sign, verify

    spec = UndoSpec(kind="archive", email_ids=["e1"], prev={}, keyword=None, on=None, toast="x")
    undo_token = sign(spec, KEY, now=1000.0)
    for purpose in ("img", "undo"):
        with pytest.raises(ValueError):
            verify_payload(undo_token, secret_key=KEY, purpose=purpose, now=1000.0)
    with pytest.raises(ValueError):
        verify(sign_payload({"a": 1}, secret_key=KEY, purpose="undo", ttl=60, now=1000.0), KEY)


def test_an_empty_purpose_is_a_caller_bug_not_a_default():
    with pytest.raises(ValueError):
        sign_payload({"a": 1}, secret_key=KEY, purpose="", ttl=60)
    with pytest.raises(ValueError):
        verify_payload("a.b", secret_key=KEY, purpose="")


def test_the_secret_key_is_load_bearing():
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    with pytest.raises(ValueError):
        verify_payload(tok, secret_key=OTHER_KEY, purpose=PURPOSE, now=1000.0)


# --------------------------------------------------------------------------
# Forgery and tampering
# --------------------------------------------------------------------------


def test_flipping_any_character_of_the_payload_breaks_the_mac():
    tok = sign_payload(
        {"u": "https://cdn.test/a.png", "s": 7}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0
    )
    payload, signature = tok.split(".")
    for index in range(len(payload)):
        original = payload[index]
        swap = "B" if original != "B" else "C"
        with pytest.raises(ValueError):
            verify_payload(
                f"{payload[:index]}{swap}{payload[index + 1 :]}.{signature}",
                secret_key=KEY,
                purpose=PURPOSE,
                now=1000.0,
            )


def test_flipping_any_character_of_the_signature_breaks_it():
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    payload, signature = tok.split(".")
    for index in range(len(signature)):
        original = signature[index]
        swap = "B" if original != "B" else "C"
        with pytest.raises(ValueError):
            verify_payload(
                f"{payload}.{signature[:index]}{swap}{signature[index + 1 :]}",
                secret_key=KEY,
                purpose=PURPOSE,
                now=1000.0,
            )


def test_the_final_characters_spare_bits_are_not_a_second_spelling():
    """The subtle one. A base64 string's last character carries bits the
    decoder discards, so up to four characters decode to identical bytes and
    a MAC over those bytes verifies for all of them. A token must have exactly
    one spelling, so the decoder re-encodes and compares.
    """
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    payload, signature = tok.split(".")
    # A 32-byte MAC is 43 base64 characters -- 258 bits carrying 256, so its
    # last character always has two bits nobody reads and exactly four
    # spellings. (The payload half only sometimes does, so it is the wrong
    # place to assert this from.)
    respellings = _respellings_of(signature)
    assert len(respellings) == 3, "a 32-byte MAC has four base64 spellings, not one"
    for respelled in respellings:
        with pytest.raises(ValueError):
            verify_payload(f"{payload}.{respelled}", secret_key=KEY, purpose=PURPOSE, now=1000.0)


def _respellings_of(part: str) -> list[str]:
    """Every string but `part` whose last character decodes to the same bytes
    -- i.e. differs only in the bits base64 discards.
    """
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
    expected = _b64u_decode(part)
    return [
        part[:-1] + char
        for char in alphabet
        if char != part[-1] and _decodes_the_same(part[:-1] + char, expected)
    ]


def _decodes_the_same(candidate: str, expected: bytes) -> bool:
    import base64
    import binascii

    padded = candidate.replace("-", "+").replace("_", "/") + "=" * (-len(candidate) % 4)
    try:
        return base64.b64decode(padded, validate=True) == expected
    except binascii.Error:
        return False


@pytest.mark.parametrize(
    "token",
    ["", "a", ".", "..", "a.b.c", "a.", ".b", "!!.??", "eyJhIjoxfQ", "eyJhIjoxfQ.", "x" * 9000],
)
def test_a_malformed_token_is_a_plain_value_error(token):
    with pytest.raises(ValueError):
        verify_payload(token, secret_key=KEY, purpose=PURPOSE, now=1000.0)


@pytest.mark.parametrize("token", [None, 7, b"a.b", ["a", "b"]])
def test_a_token_that_is_not_a_string_is_refused_rather_than_crashing(token):
    with pytest.raises(ValueError):
        verify_payload(token, secret_key=KEY, purpose=PURPOSE, now=1000.0)


def test_an_oversized_token_is_refused_before_it_is_decoded():
    """An unauthenticated caller must not be able to make this process
    base64-decode and JSON-parse megabytes to learn that the MAC is wrong.
    """
    tok = sign_payload({"u": "x" * 7000}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    assert len(tok) > 8192
    with pytest.raises(ValueError):
        verify_payload(tok, secret_key=KEY, purpose=PURPOSE, now=1000.0)


@pytest.mark.parametrize("body", [[1, 2, 3], "a string", 7, None, True])
def test_a_validly_signed_payload_that_is_not_an_object_is_refused(body):
    with pytest.raises(ValueError):
        verify_payload(_forge(body), secret_key=KEY, purpose=PURPOSE, now=1000.0)


@pytest.mark.parametrize("expires", ["9999999999", None, True, [1], {"e": 1}])
def test_a_validly_signed_expiry_of_the_wrong_type_is_refused(expires):
    """`True` is an `int` in Python and compares happily against a float — a
    payload carrying `{"e": true}` must not read as "expires at 1".
    """
    with pytest.raises(ValueError):
        verify_payload(_forge({"a": 1, "e": expires}), secret_key=KEY, purpose=PURPOSE, now=0.5)


def test_a_validly_signed_payload_with_no_expiry_at_all_is_refused():
    with pytest.raises(ValueError):
        verify_payload(_forge({"a": 1}), secret_key=KEY, purpose=PURPOSE, now=1000.0)


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_expiry_is_inclusive_at_the_boundary_and_closed_after_it():
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=60, now=1000.0)
    assert verify_payload(tok, secret_key=KEY, purpose=PURPOSE, now=1060.0) == {"a": 1}
    with pytest.raises(ValueError):
        verify_payload(tok, secret_key=KEY, purpose=PURPOSE, now=1060.001)


@pytest.mark.parametrize("ttl", [0, -1, -3600])
def test_a_zero_or_negative_ttl_yields_a_token_that_is_already_dead(ttl):
    tok = sign_payload({"a": 1}, secret_key=KEY, purpose=PURPOSE, ttl=ttl, now=1000.0)
    with pytest.raises(ValueError):
        verify_payload(tok, secret_key=KEY, purpose=PURPOSE, now=1000.1)


def test_a_payload_that_is_not_json_is_a_caller_bug_at_signing_time():
    with pytest.raises(ValueError):
        sign_payload({"a": float("inf")}, secret_key=KEY, purpose=PURPOSE, ttl=60)
    with pytest.raises(TypeError):
        sign_payload({"a": object()}, secret_key=KEY, purpose=PURPOSE, ttl=60)


# --------------------------------------------------------------------------
# image_policy's wiring onto the signer
# --------------------------------------------------------------------------


def test_the_image_token_is_this_signer_under_the_img_purpose():
    tok = sign_remote_url("https://cdn.test/a.png", secret_key=KEY, user_id=7, now=1000.0)
    assert verify_payload(tok, secret_key=KEY, purpose="img", now=1000.0) == {
        "u": "https://cdn.test/a.png",
        "s": 7,
    }
    assert IMAGE_URL_TTL == 3600


def test_the_image_token_reads_back_its_url_and_its_reader_with_no_reader_supplied():
    """`GET /img` has no session to compare against — the request comes from
    an opaque-origin document that sends no cookie — so the token has to be
    able to say who it was minted for rather than only agree with a caller
    who already knows.
    """
    tok = sign_remote_url("https://cdn.test/a.png", secret_key=KEY, user_id=7, now=1000.0)
    assert verify_image_token(tok, secret_key=KEY, now=1500.0) == ("https://cdn.test/a.png", 7)
    with pytest.raises(ValueError):
        verify_image_token(tok, secret_key=OTHER_KEY, now=1500.0)
    with pytest.raises(ValueError):
        verify_image_token(tok, secret_key=KEY, now=1000.0 + IMAGE_URL_TTL + 1)


# --------------------------------------------------------------------------
# The inline-part capability
# --------------------------------------------------------------------------
#
# `/m/{id}/cid/{cid}?u=…` reads a part of the reader's own mail with no
# session cookie behind it, so this token is the whole admission decision.
# It must name three things — the reader, the message, the part — and every
# test below removes or changes exactly one of them.


def test_the_cid_token_is_this_signer_under_its_own_purpose():
    tok = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    assert verify_payload(tok, secret_key=KEY, purpose="cid", now=1000.0) == {
        "m": "E1",
        "c": "logo@mail",
        "s": 7,
    }
    assert CID_URL_TTL == 3600


def test_a_cid_token_roundtrips_to_its_reader_and_expires():
    tok = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    assert (
        verify_cid_token(tok, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1500.0)
        == 7
    )
    with pytest.raises(ValueError):
        verify_cid_token(
            tok,
            secret_key=KEY,
            email_id="E1",
            content_id="logo@mail",
            now=1000.0 + CID_URL_TTL + 1,
        )


def test_a_cid_token_is_bound_to_the_message_and_to_the_part():
    """The binding that turns a broken-image bug into a mailbox disclosure
    if it is missing. A signature logo keeps its Content-ID across every
    message a correspondent sends, so a token that named only the part would
    read all of them; one that named only the message would read every part
    of it.
    """
    tok = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    for email_id, content_id in [
        ("E2", "logo@mail"),
        ("E1", "secret@mail"),
        ("E1", "logo@mai"),
        ("E1", "logo@mail "),
        ("E1", "LOGO@MAIL"),
        ("e1", "logo@mail"),
        ("", "logo@mail"),
        ("E1", ""),
    ]:
        with pytest.raises(ValueError):
            verify_cid_token(
                tok, secret_key=KEY, email_id=email_id, content_id=content_id, now=1000.0
            )


def test_a_cid_token_is_bound_to_the_secret():
    tok = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    with pytest.raises(ValueError):
        verify_cid_token(tok, secret_key=OTHER_KEY, email_id="E1", content_id="logo@mail")


def test_two_readers_get_two_different_cid_tokens_for_the_same_part():
    """A token is not a function of the resource alone — otherwise every
    reader would hold every other reader's capability for a shared mailbox,
    and the id inside it would be decoration.
    """
    a = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    b = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=8, now=1000.0)
    assert a != b
    assert (
        verify_cid_token(a, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0) == 7
    )
    assert (
        verify_cid_token(b, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0) == 8
    )


@pytest.mark.parametrize("flip", [0, 1])
def test_tampering_with_either_half_of_a_cid_token_is_refused(flip):
    tok = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    halves = tok.split(".")
    halves[flip] = halves[flip][:-2] + ("AB" if halves[flip][-2:] != "AB" else "CD")
    with pytest.raises(ValueError):
        verify_cid_token(
            ".".join(halves), secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0
        )


def test_an_image_token_is_not_a_cid_token_and_the_reverse():
    """Two purposes, two HKDF-derived keys. Both payloads carry `"s"`, so
    the key separation is the only thing standing between a proxy token and
    a mailbox read.
    """
    proxy = sign_remote_url("https://cdn.test/a.png", secret_key=KEY, user_id=7, now=1000.0)
    with pytest.raises(ValueError):
        verify_cid_token(proxy, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0)

    inline = sign_cid_url("E1", "logo@mail", secret_key=KEY, user_id=7, now=1000.0)
    with pytest.raises(ValueError):
        verify_image_token(inline, secret_key=KEY, now=1000.0)


def test_a_cid_token_signed_for_another_purpose_entirely_is_refused():
    """The forgery a real key makes possible: the same payload, minted under
    a purpose whose key an attacker might separately hold or influence.
    """
    forged = _forge({"m": "E1", "c": "logo@mail", "s": 7, "e": 9e9}, purpose="undo")
    with pytest.raises(ValueError):
        verify_cid_token(forged, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0)


def test_a_true_user_id_in_a_cid_token_cannot_impersonate_user_one():
    """`True == 1` in Python and `True != 1` is `False`, so `{"s": true}`
    would come back as user 1 under a naive check — and here that is not a
    fetch of one image, it is somebody's mailbox.
    """
    forged = _forge({"m": "E1", "c": "logo@mail", "s": True, "e": 9e9}, purpose="cid")
    with pytest.raises(ValueError):
        verify_cid_token(forged, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0)


@pytest.mark.parametrize(
    "body",
    [
        {"m": "E1", "c": "logo@mail", "e": 9e9},
        {"m": "E1", "s": 7, "e": 9e9},
        {"c": "logo@mail", "s": 7, "e": 9e9},
        {"m": "E1", "c": "logo@mail", "s": "7", "e": 9e9},
        {"m": "E1", "c": "logo@mail", "s": 7.0, "e": 9e9},
        {"m": ["E1"], "c": "logo@mail", "s": 7, "e": 9e9},
        {"m": "E1", "c": None, "s": 7, "e": 9e9},
        {"m": None, "c": None, "s": None, "e": 9e9},
    ],
)
def test_a_validly_signed_cid_payload_of_the_wrong_shape_is_refused(body):
    """Signed with the real key under the real purpose, so nothing but the
    verifier's own type checks is left to refuse them.
    """
    forged = _forge(body, purpose="cid")
    with pytest.raises(ValueError):
        verify_cid_token(forged, secret_key=KEY, email_id="E1", content_id="logo@mail", now=1000.0)


@pytest.mark.parametrize("token", ["", "nonsense", "a.b", ".", "x" * 9000])
def test_a_malformed_cid_token_is_a_plain_value_error(token):
    with pytest.raises(ValueError):
        verify_cid_token(token, secret_key=KEY, email_id="E1", content_id="logo@mail")


def test_the_cid_signing_key_is_unrelated_to_the_image_signing_key():
    """The property the purpose separation rests on, asserted at the key
    rather than at the token: two `info` strings, two HKDF outputs."""
    assert derive_key(KEY, b"cid") != derive_key(KEY, b"img")
    assert derive_key(KEY, b"cid") != derive_key(OTHER_KEY, b"cid")


# ---------------------------------------------------------------------------
# The decision half: policy, per-sender allow list, contacts
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def user(db):
    """The `AppUser` every row below belongs to.

    Defined here rather than taken from `tests/conftest.py` so this module's
    DB-level assertions name their own owner: half of what `decide` promises
    is that one reader's allow list is not another's, and a test for that
    needs two users it created itself.
    """
    row = models.AppUser(stalwart_username="reader", email="reader@mailosh.test")
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def other_user(db):
    row = models.AppUser(stalwart_username="stranger", email="stranger@mailosh.test")
    db.add(row)
    await db.commit()
    return row


async def _allow_rows(db) -> list:
    return (await db.execute(models.ImageSenderAllow.__table__.select())).all()


async def test_override_beats_every_policy_in_both_directions(db, user):
    """A per-message choice a stored preference could overrule is not a
    choice. Both directions, because only asserting the permissive one would
    miss a policy that quietly re-enables images the reader just turned off.
    """
    shown = await decide(db, user_id=user.id, policy="ask", sender_email="a@x", override=1)
    hidden = await decide(db, user_id=user.id, policy="always", sender_email="a@x", override=0)
    assert (shown.show, shown.reason) == (True, "override")
    assert (hidden.show, hidden.reason) == (False, "override")


async def test_policy_always_shows_and_ask_blocks(db, user):
    allowed = await decide(db, user_id=user.id, policy="always", sender_email="a@x", override=None)
    blocked = await decide(db, user_id=user.id, policy="ask", sender_email="a@x", override=None)
    assert (allowed.show, allowed.reason) == (True, "policy_always")
    assert (blocked.show, blocked.reason) == (False, "blocked")


async def test_allow_listed_sender_shows_under_ask_whatever_the_case(db, user):
    await allow_sender(db, user_id=user.id, sender_email="  A@X.test ")
    for spelling in ("a@x.test", "A@X.TEST", " a@X.test  "):
        d = await decide(db, user_id=user.id, policy="ask", sender_email=spelling, override=None)
        assert (d.show, d.reason) == (True, "sender_allowed"), spelling


async def test_the_allow_list_survives_a_policy_the_reader_narrows_later(db, user):
    """An "always show from Priya" is a judgement about Priya, not a side effect
    of whichever default was selected the day it was clicked.
    """
    await allow_sender(db, user_id=user.id, sender_email="priya@x.test")
    for policy in ("ask", "contacts", "always", "some-later-policy"):
        d = await decide(
            db, user_id=user.id, policy=policy, sender_email="priya@x.test", override=None
        )
        assert d.show, policy


async def test_allow_sender_is_idempotent(db, user):
    await allow_sender(db, user_id=user.id, sender_email="a@x")
    await allow_sender(db, user_id=user.id, sender_email="A@X")
    assert len(await _allow_rows(db)) == 1


async def test_allow_sender_stores_the_folded_address(db, user):
    await allow_sender(db, user_id=user.id, sender_email=" News@Example.TEST ")
    rows = await _allow_rows(db)
    assert [r.sender_email for r in rows] == ["news@example.test"]


async def test_allow_sender_refuses_an_address_that_folds_away_to_nothing(db, user):
    """A row keyed on the empty string would match every message with no
    `From` at all — the mail least worth trusting.
    """
    for empty in ("", "   ", "\t\n"):
        with pytest.raises(ValueError):
            await allow_sender(db, user_id=user.id, sender_email=empty)
    assert await _allow_rows(db) == []


async def test_one_readers_allow_list_is_not_another_readers(db, user, other_user):
    await allow_sender(db, user_id=other_user.id, sender_email="a@x")
    d = await decide(db, user_id=user.id, policy="ask", sender_email="a@x", override=None)
    assert (d.show, d.reason) == (False, "blocked")


async def test_contacts_policy_reads_the_harvested_table(db, user):
    before = await decide(db, user_id=user.id, policy="contacts", sender_email="a@x", override=None)
    assert (before.show, before.reason) == (False, "blocked")

    db.add(models.Contact(user_id=user.id, email="a@x", count=1))
    await db.commit()
    after = await decide(db, user_id=user.id, policy="contacts", sender_email="a@x", override=None)
    assert (after.show, after.reason) == (True, "contact")


async def test_a_contact_matches_however_either_side_spells_the_case(db, user):
    """`contact` rows are harvested by the send path, which stores whatever
    case the header carried — so the fold has to happen in the query, not
    only on the way in.
    """
    db.add(models.Contact(user_id=user.id, email="Priya@Example.test", count=1))
    await db.commit()
    d = await decide(
        db, user_id=user.id, policy="contacts", sender_email="PRIYA@example.TEST", override=None
    )
    assert (d.show, d.reason) == (True, "contact")


async def test_a_contact_is_ignored_under_every_other_policy(db, user):
    db.add(models.Contact(user_id=user.id, email="a@x", count=1))
    await db.commit()
    d = await decide(db, user_id=user.id, policy="ask", sender_email="a@x", override=None)
    assert (d.show, d.reason) == (False, "blocked")


async def test_one_readers_contacts_are_not_another_readers(db, user, other_user):
    db.add(models.Contact(user_id=other_user.id, email="a@x", count=1))
    await db.commit()
    d = await decide(db, user_id=user.id, policy="contacts", sender_email="a@x", override=None)
    assert not d.show


@pytest.mark.parametrize("sender", [None, "", "   "])
async def test_a_missing_sender_never_shows_under_ask_or_contacts(db, user, sender):
    for policy in ("ask", "contacts"):
        d = await decide(db, user_id=user.id, policy=policy, sender_email=sender, override=None)
        assert (d.show, d.reason) == (False, "blocked"), (policy, sender)


@pytest.mark.parametrize("policy", ["", "never", "ASK", "Always", "contact", None])
async def test_an_unrecognised_policy_blocks_rather_than_defaulting_open(db, user, policy):
    """A stale row, a hand-edited database or a policy a later build adds must
    fail towards "the reader is not tracked".
    """
    d = await decide(db, user_id=user.id, policy=policy, sender_email="a@x", override=None)
    assert (d.show, d.reason) == (False, "blocked")


async def test_deciding_writes_nothing(db, user):
    """A render must not create rows. Every allow-list entry comes from a
    click on "Always show from", through the one writer.
    """
    for policy in ("ask", "always", "contacts"):
        await decide(db, user_id=user.id, policy=policy, sender_email="a@x", override=None)
    contacts = (await db.execute(models.Contact.__table__.select())).all()
    assert (await _allow_rows(db), contacts) == ([], [])


def test_a_decision_carries_nothing_but_the_answer_and_the_rule():
    """`ImageDecision` is handed to a template. Anything else on it — the
    sender, the row, the session — would be a second, divergent source for
    what the banner says.
    """
    assert ImageDecision.__dataclass_fields__.keys() == {"show", "reason"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        ImageDecision(show=True, reason="override").show = False
