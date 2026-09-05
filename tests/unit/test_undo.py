"""Unit tests for the signed undo token (Task 9, design spec §6.3).

`mailosh.services.undo` is pure crypto plus a dataclass: no JMAP client, no
app, no database. The token has to survive being handed to a browser and
posted back, so every test here is about what an attacker can and can't do
with the string in between — tamper with it, replay it after the window, sign
one with the wrong key, or replay someone else's under their own session.

`apply` (the reverse operation itself) is exercised in
`tests/unit/test_actions.py` instead, where the fake JMAP client lives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import string
import zlib

import pytest

from mailosh.security.crypto import derive_key
from mailosh.services.undo import UndoSpec, sign, verify

KEY = "k" * 40
OTHER_KEY = "j" * 40


def _spec() -> UndoSpec:
    return UndoSpec(
        kind="archive",
        email_ids=["e1"],
        prev={"e1": ["inbox", "work"]},
        keyword=None,
        on=None,
        toast="Archived",
    )


def _b64u_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _long_ids(count: int, length: int = 32) -> list[str]:
    """`count` distinct, high-entropy ids of exactly `length` characters —
    deterministic (fixed seed) so a size assertion over them is stable.
    """
    rnd = random.Random(7)
    alphabet = string.ascii_lowercase + string.digits
    return ["".join(rnd.choices(alphabet, k=length)) for _ in range(count)]


def _payload_of(token: str) -> dict:
    """The token's decoded payload — deflated JSON (see `undo`'s module
    docstring on size), so this mirrors `_unpack`.
    """
    return json.loads(zlib.decompress(_b64u_decode(token.split(".")[0])))


def _repack(token: str, mutate) -> str:
    """Rebuild `token` with its payload passed through `mutate`, keeping the
    original signature — i.e. exactly what a tamperer can do.
    """
    _, sig = token.split(".")
    body = _payload_of(token)
    mutate(body)
    raw = zlib.compress(json.dumps(body, separators=(",", ":"), sort_keys=True).encode(), 9)
    return f"{_b64u_encode(raw)}.{sig}"


def test_sign_verify_roundtrip_and_expiry():
    spec = _spec()
    token = sign(spec, KEY, now=1000.0)
    assert verify(token, KEY, now=1030.0) == spec
    with pytest.raises(ValueError):
        verify(token, KEY, now=1000.0 + 61)
    with pytest.raises(ValueError):
        verify(token + "x", KEY, now=1001.0)


def test_token_carries_no_padding_and_two_parts():
    token = sign(_spec(), KEY, now=1000.0)
    assert "=" not in token and token.count(".") == 1


def test_tampered_payload_is_rejected():
    token = sign(_spec(), KEY, now=1000.0)

    def escalate(body):
        # Point the undo at somebody else's message and a different verb.
        body["e"] = ["victim-email"]
        body["k"] = "delete"

    with pytest.raises(ValueError):
        verify(_repack(token, escalate), KEY, now=1001.0)


def test_extended_expiry_is_rejected():
    token = sign(_spec(), KEY, now=1000.0)
    with pytest.raises(ValueError):
        verify(_repack(token, lambda body: body.update(x=1_000_000.0)), KEY, now=999_999.0)


def test_wrong_key_is_rejected():
    token = sign(_spec(), KEY, now=1000.0)
    with pytest.raises(ValueError):
        verify(token, OTHER_KEY, now=1001.0)


def test_garbage_tokens_raise_value_error_not_binascii_or_index_error():
    for junk in ("", ".", "not-a-token", "a.b.c", "!!!.???", "eyJhIjoxfQ"):
        with pytest.raises(ValueError):
            verify(junk, KEY, now=1001.0)


def test_a_non_ascii_scope_is_rejected_not_raised_on():
    # `secrets.compare_digest` refuses two non-ASCII `str`s with TypeError,
    # which would surface as a 500 instead of the 400 every other rejection
    # gets. Both directions, plus a matching pair that must still round-trip.
    token = sign(_spec(), KEY, now=1000.0, scope="acct-ünï")
    assert verify(token, KEY, now=1001.0, scope="acct-ünï") == _spec()
    with pytest.raises(ValueError):
        verify(token, KEY, now=1001.0, scope="acct-a")
    with pytest.raises(ValueError):
        verify(sign(_spec(), KEY, now=1000.0, scope="acct-a"), KEY, now=1001.0, scope="ünï")


def test_sign_silently_drops_a_prev_entry_outside_email_ids():
    # `sign` used to raise here, but it runs on the response path *after* the
    # write it is undoing has already committed. The invariant (`prev` names
    # only ids in `email_ids`) is now established, and asserted, where it is
    # actually built — `mailosh.services.actions._result`, covered in
    # test_actions.py — rather than re-checked on this response path: doing
    # it here would turn a future bug into a 500 for an action that had
    # already succeeded. The encoding loop is driven by `email_ids`, not
    # `prev`, so a stray entry is simply never encoded.
    spec = UndoSpec(
        kind="archive",
        email_ids=["e1"],
        prev={"e1": ["mb-inbox"], "e2": ["mb-inbox"]},
        keyword=None,
        on=None,
        toast="Archived",
    )
    token = sign(spec, KEY, now=1000.0)
    restored = verify(token, KEY, now=1000.0)
    assert restored.email_ids == ["e1"]
    assert restored.prev == {"e1": ["mb-inbox"]}  # "e2" quietly dropped, not raised on


def test_prev_may_cover_only_some_of_the_ids():
    # Spam's shape: a message already in Junk gains `$junk` without moving, so
    # it is in `email_ids` with no `prev` entry of its own.
    spec = UndoSpec(
        kind="spam",
        email_ids=["moved", "flagged-only"],
        prev={"moved": ["mb-inbox"]},
        keyword="$junk",
        on=True,
        toast="Reported spam",
    )
    assert verify(sign(spec, KEY, now=1000.0), KEY, now=1000.0) == spec


def test_distinct_placements_round_trip_through_the_placement_table():
    spec = UndoSpec(
        kind="delete",
        email_ids=["e1", "e2", "e3"],
        prev={
            "e1": ["m-work", "mb-inbox"],
            "e2": ["m-work", "mb-inbox"],  # shares e1's placement: one table row
            "e3": ["m-receipts"],
        },
        keyword=None,
        on=None,
        toast="Deleted",
    )
    token = sign(spec, KEY, now=1000.0)
    assert verify(token, KEY, now=1000.0) == spec
    body = _payload_of(token)
    assert body["g"] == [[0, 1], [2]]  # two distinct placements, not three
    assert sorted(body["m"]) == ["m-receipts", "m-work", "mb-inbox"]


def test_a_hundred_long_ids_stay_far_smaller_than_a_header_budget():
    # The regression this encoding exists for: 100 messages with 32-character
    # ids and two mailboxes each used to need a ~12.4 KB token. The ids are
    # pseudo-random rather than sequential on purpose — a shared prefix would
    # deflate away and make this pass for the wrong reason.
    ids = _long_ids(100)
    spec = UndoSpec(
        kind="archive",
        email_ids=ids,
        prev={email_id: ["m-work", "mb-inbox"] for email_id in ids},
        keyword=None,
        on=None,
        toast="Archived",
    )
    token = sign(spec, KEY, now=1000.0, scope="acct-a")
    assert len(token) < 3500
    assert verify(token, KEY, now=1000.0, scope="acct-a") == spec


def test_undo_key_is_domain_separated_from_the_session_key():
    # The HKDF purpose must differ from `crypto.encrypt`/`decrypt`'s
    # ("sessions"): a leaked session-decryption key must not forge undo
    # tokens, and vice versa.
    undo_key = derive_key(KEY, b"undo")
    assert undo_key != derive_key(KEY, b"sessions")

    payload, sig = sign(_spec(), KEY, now=1000.0).split(".")
    expected = hmac.new(undo_key, _b64u_decode(payload), hashlib.sha256).digest()
    assert _b64u_decode(sig) == expected


def test_scope_binds_the_token_to_one_account():
    token = sign(_spec(), KEY, now=1000.0, scope="acct-a")
    assert verify(token, KEY, now=1001.0, scope="acct-a") == _spec()
    # Another logged-in user replaying the token under their own session.
    with pytest.raises(ValueError):
        verify(token, KEY, now=1001.0, scope="acct-b")
    # And it can't be downgraded to an unscoped token.
    with pytest.raises(ValueError):
        verify(token, KEY, now=1001.0)
    with pytest.raises(ValueError):
        verify(sign(_spec(), KEY, now=1000.0), KEY, now=1001.0, scope="acct-a")


def test_roundtrip_preserves_every_field_including_keyword_specs():
    spec = UndoSpec(
        kind="spam",
        email_ids=["e1", "e2"],
        prev={"e1": ["mb-inbox"], "e2": ["mb-inbox", "m-work"]},
        keyword="$junk",
        on=True,
        toast="Reported spam",
    )
    assert verify(sign(spec, KEY, now=1000.0), KEY, now=1000.0) == spec
