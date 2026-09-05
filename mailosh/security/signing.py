"""Generic HMAC payload signing — the primitive behind a *capability URL*: a
link this app hands to a browser and later has to trust back without keeping
any server-side record of having issued it.

The image proxy (`mailosh.render.image_policy`) is the first caller. `GET
/img?u=…` fetches a URL that *the sender of an email chose*, from inside the
server, so the question "did we issue this?" has to be answerable from the
token alone — a table of outstanding image URLs would cost a row per remote
image per open message, and a cleanup job, to say something HMAC already
says for free.

Three properties, each enforced here rather than left to callers:

- **Unforgeable.** HMAC-SHA256 under a key derived from `Settings.secret_key`
  by `mailosh.security.crypto.derive_key`, compared with
  `secrets.compare_digest`. The payload is *signed, not encrypted*: whoever
  holds a token can read what is inside it, so callers must not put a secret
  in one.

- **Domain-separated.** `purpose` is HKDF's `info`, so two purposes derive two
  unrelated keys from the same `secret_key`. A token minted for `"img"` can
  never verify as anything else, and neither can ever verify as
  `mailosh.services.undo`'s `b"undo"` token — which keeps its own signer
  deliberately (its payload is deflated and shaped for header size; this
  module's is plain JSON). Two signers, two purposes, no shared abstraction
  retrofitted onto working security code.

- **Expiring.** `ttl` is folded into the payload before the MAC covers it, so
  the expiry cannot be edited by the holder. `_EXPIRES` is reserved: a caller
  cannot supply its own `"e"` and cannot read one back, which means no caller
  can accidentally treat the expiry as its own data.

Every rejection — bad base64, wrong key, wrong purpose, tampered payload,
expired, not a token at all — raises a plain `ValueError` with one fixed
message, so nothing about *why* a guess failed leaks back to whoever is
guessing. Callers turn that into one status code (403 for the image proxy)
without branching on the reason.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

from mailosh.security.crypto import derive_key

__all__ = ["sign_payload", "verify_payload"]

#: Payload key holding the absolute expiry, in `time.time()` seconds. Reserved:
#: `sign_payload` refuses a payload that already carries it, and
#: `verify_payload` strips it before returning, so a round trip through this
#: module gives a caller back exactly the dict it signed.
_EXPIRES = "e"

#: Ceiling on the token a `verify_payload` caller will even look at. A token
#: arrives in a query string chosen by whoever is calling, and base64-decoding
#: then JSON-parsing megabytes to discover the MAC is wrong is work this
#: process should not do on an unauthenticated guess. Two orders of magnitude
#: above any URL this app signs.
_MAX_TOKEN_CHARS = 8192

_ERROR = "invalid signed token"


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(part: str) -> bytes:
    """Strict inverse of `_b64u_encode`, raising `binascii.Error` on anything
    that is not *the* canonical base64url spelling of its own bytes.

    A token must have one spelling, not an infinite family of them — otherwise
    anything that compares two token strings (a log line, a cache key, a rate
    limiter, a revocation list) can be slipped past with a token that decodes
    identically and reads differently. Base64 offers three ways to respell:
    characters outside the alphabet, which the default decoder *silently
    discards*; surplus `=` padding, which it happily ignores; and the unused
    low bits of the final character, four spellings of which decode to the
    same bytes. `validate=True` covers only the first, so the decoded bytes
    are re-encoded and compared — one line that closes all three.
    """
    padded = part.replace("-", "+").replace("_", "/") + "=" * (-len(part) % 4)
    raw = base64.b64decode(padded, validate=True)
    if _b64u_encode(raw) != part:
        raise binascii.Error("non-canonical base64url encoding")
    return raw


def _mac(secret_key: str, purpose: str, payload: bytes) -> bytes:
    if not purpose:
        raise ValueError("purpose must be a non-empty string")
    key = derive_key(secret_key, purpose.encode("utf-8"))
    return hmac.new(key, payload, hashlib.sha256).digest()


def sign_payload(
    payload: dict,
    *,
    secret_key: str,
    purpose: str,
    ttl: int,
    now: float | None = None,
) -> str:
    """`payload` plus an expiry `ttl` seconds out, as a `<payload>.<signature>`
    token — both halves urlsafe-base64 without padding, so it is safe unescaped
    in a query string, a form field and a header alike.

    `payload` must be JSON-serialisable and must not contain the reserved key
    `"e"`. `purpose` domain-separates the signing key; see the module
    docstring. Raises `ValueError` for a reserved key or an empty purpose —
    both caller bugs, surfaced loudly rather than silently producing a token
    that verifies as something else.
    """
    if _EXPIRES in payload:
        raise ValueError(f"{_EXPIRES!r} is reserved for the expiry")
    now = time.time() if now is None else now
    body = {**payload, _EXPIRES: now + ttl}
    # sort_keys so a payload has one spelling; allow_nan=False so no caller can
    # produce a token whose JSON is not JSON to anything but Python.
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return f"{_b64u_encode(raw)}.{_b64u_encode(_mac(secret_key, purpose, raw))}"


def verify_payload(
    token: str,
    *,
    secret_key: str,
    purpose: str,
    now: float | None = None,
) -> dict:
    """The payload `token` carries, with the expiry stripped, or `ValueError`.

    The MAC is checked *before* the payload is JSON-parsed, so no
    attacker-chosen bytes are ever handed to the parser: everything below the
    `compare_digest` runs only on bytes this app itself signed under this
    exact purpose.
    """
    now = time.time() if now is None else now
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
        raise ValueError(_ERROR)
    try:
        payload_part, signature_part = token.split(".")
        raw = _b64u_decode(payload_part)
        signature = _b64u_decode(signature_part)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(_ERROR) from exc

    if not secrets.compare_digest(signature, _mac(secret_key, purpose, raw)):
        raise ValueError(_ERROR)

    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(_ERROR) from exc
    if not isinstance(body, dict):
        raise ValueError(_ERROR)

    expires = body.pop(_EXPIRES, None)
    # `type(...) is` and not `isinstance`: `True` is an `int`, and a bool
    # expiry compares `<` against a float perfectly happily.
    if type(expires) not in (int, float) or now > expires:
        raise ValueError(_ERROR)
    return body
