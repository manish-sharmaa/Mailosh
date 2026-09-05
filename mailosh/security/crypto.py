"""Fernet symmetric encryption for small application secrets — right now,
the per-session Stalwart API key (`mailosh.security.sessions`). Task 9's
undo tokens are HMAC-signed, not Fernet-encrypted, but they derive their
own signing key the same way, via `derive_key(secret_key, b"undo")`.

Every key handed to `Fernet` is derived from `Settings.secret_key` with
HKDF-SHA256 rather than used directly, so:

- the same `secret_key` never appears in a form usable as a Fernet/HMAC key
  (HKDF's "extract" step spreads any structure/bias in an
  operator-generated secret across a full-entropy 256-bit key), and
- two different purposes derive two *unrelated* keys from that one
  secret (`info=purpose` is HKDF's domain-separation input) — compromising
  or rotating one derived key/purpose says nothing about the other, so a
  leaked session-decryption key can never be used to forge or read an undo
  token, and vice versa (`test_roundtrip_and_key_isolation`).
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: `encrypt`/`decrypt`'s fixed purpose — the only Fernet use this task has
#: (the per-session encrypted Stalwart API key). A caller that needs its
#: own independently-derived key (Task 9's undo tokens, `info=b"undo"`)
#: calls `derive_key` directly instead of a purpose parameter here, so
#: `encrypt`/`decrypt` stay a fixed, unambiguous two-argument pair.
_SESSION_PURPOSE = b"sessions"


def derive_key(secret_key: str, purpose: bytes) -> bytes:
    """A 32-byte, urlsafe-base64-encoded key derived from `secret_key` via
    HKDF-SHA256 — ready to hand to `Fernet(...)`, or use directly as an
    HMAC key.

    `purpose` is HKDF's `info` parameter: two different purposes always
    derive two different, unrelated keys from the same `secret_key` (see
    module docstring). Callers must use a distinct `purpose` per use case
    and never reuse one across them.
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose)
    raw = hkdf.derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def encrypt(secret_key: str, plaintext: str) -> bytes:
    """Encrypt `plaintext` with a Fernet key derived from `secret_key`
    (purpose: sessions). Returns the opaque Fernet token, safe to store as
    `SessionRow.api_key_secret_enc`.
    """
    key = derive_key(secret_key, _SESSION_PURPOSE)
    return Fernet(key).encrypt(plaintext.encode("utf-8"))


def decrypt(secret_key: str, token: bytes) -> str:
    """Reverse of `encrypt`. Raises `ValueError` — never the `cryptography`
    library's own `InvalidToken` — when `token` doesn't decrypt under
    `secret_key`, whether because the key is wrong, `token` was corrupted,
    or it wasn't produced by `encrypt` at all; callers should treat all of
    those the same way (reject) without needing to import
    `cryptography.fernet` themselves. The error message carries neither
    `token` nor the derived key, only a fixed, generic description.
    """
    key = derive_key(secret_key, _SESSION_PURPOSE)
    try:
        raw = Fernet(key).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("could not decrypt: wrong key, or corrupted/invalid token") from exc
    return raw.decode("utf-8")
