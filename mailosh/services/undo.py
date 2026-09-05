"""The undo half of design spec §6.3's action model: actions commit
immediately, and the *reverse* operation is handed to the browser as a
short-lived signed token instead of being parked in any server-side store.

Why a token and not a row: undo has a 10 s window in the UI, has to survive a
page swap, and must cost nothing when (as usual) it is never used. A stateless
HMAC token gives all three — no table, no cleanup job, no per-user quota — at
the price of the two properties this module has to enforce itself:

- **Unforgeable.** The payload is signed with HMAC-SHA256 under a key derived
  from `Settings.secret_key` with the HKDF purpose `b"undo"` — a *different*
  purpose from `mailosh.security.crypto.encrypt`/`decrypt`'s `b"sessions"`,
  so a leaked session-decryption key cannot mint undo tokens, and vice versa
  (`crypto.derive_key`'s own docstring, and
  `test_undo_key_is_domain_separated_from_the_session_key`). Signature
  comparison is `secrets.compare_digest`, and an expired, tampered, wrongly
  keyed, or simply malformed token all raise the same plain `ValueError`.

- **Scoped.** The payload is *signed, not encrypted*: anyone holding a token
  can read the email ids inside it (they are always the holder's own — a token
  is only ever returned to the session that performed the action). What must
  not happen is one account replaying another's token, so `sign`/`verify` take
  an optional `scope`, which `mailosh.web.actions` fills with the session's
  JMAP account id and requires to match. That parameter is additive to the
  interface the Phase 1A plan names for this module; a caller that omits it
  gets an unscoped token, and an unscoped token never verifies against a
  scoped expectation (or the reverse).

Expiry is 60 s — long enough for the UI's 10 s window plus a slow round trip,
short enough that a token leaking out of a browser history/log is dead by the
time anyone looks at it.

**Size is a correctness property here, not a nicety.** The token travels in an
`HX-Trigger` response header, and `prev` carries one entry per message, so a
naive `{id: [mailbox ids]}` map made undo quietly vanish on selections far
smaller than the bulk-confirm line (measured: 30 messages with 32-character
ids). The payload is therefore stored densely — the ids once, a table of the
distinct mailbox ids, a table of the distinct *placements* as index lists into
that table, and one small integer per message pointing into it — and then
deflated before signing. Measured together that is ~3.8x the old capacity:
100 messages with 32-character ids and two mailboxes each now cost a
3.3 KB header where the old shape needed 12.4 KB. `mailosh.web.actions` owns
the budget the result is checked against.

Even the dense shape is not an id-length-independent guarantee, so "undo
survives every selection at or below the confirm line" only holds for short
ids — it is not a property of the encoding on its own. Measured points at
which `mailosh.web.actions._done` has to shed the token entirely: 219
messages for 16-character ids, 115 for 32-character ids, but only 59 for
64-character ones. That headroom comfortably covers Stalwart's short ids
against `BULK_CONFIRM_OVER`'s 100-message line, but a server issuing
64-character email ids would lose undo on a 60-100 message selection
without the confirm dialog ever having appeared.

Deflate is applied *before* the MAC and undone strictly *after* it verifies,
so no attacker-supplied bytes are ever handed to `zlib`; the decompressor is
additionally bounded (`_MAX_PAYLOAD_BYTES`) so that stays true even if some
future edit reorders the checks. The compact wire shape is private to this
module and carries no version marker: a token lives 60 s, so no token signed
by an older deploy can outlive the deploy that replaced it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
import zlib
from dataclasses import dataclass

from mailosh.jmap.client import JmapClient
from mailosh.security.crypto import derive_key

__all__ = ["UndoSpec", "apply", "sign", "verify"]

#: HKDF `info` for the undo signing key. MUST differ from every other purpose
#: derived from the same `Settings.secret_key` (see module docstring).
_PURPOSE = b"undo"

#: Seconds a token stays valid after `sign`.
TTL_SECONDS = 60

#: Wire keys, one character each (see the module docstring on size):
#: kind, email ids, mailbox-id table, placement table, per-message placement,
#: keyword, on, toast, expiry, scope.
_KIND, _IDS, _MAILBOXES, _GROUPS, _PLACEMENT, _KEYWORD, _ON, _TOAST, _EXPIRES, _SCOPE = (
    "k",
    "e",
    "m",
    "g",
    "p",
    "w",
    "o",
    "t",
    "x",
    "s",
)

#: `_PLACEMENT` entry for a message the action changed but did not *move*
#: (spam's `$junk` on a message already sitting in Junk): it belongs in
#: `email_ids` but has no `prev` to restore.
_NO_PLACEMENT = -1

#: Ceiling on a decompressed payload. The MAC is checked first, so this can
#: only ever fire on something this app itself signed — it exists so that
#: stays harmless even if that order is ever disturbed.
_MAX_PAYLOAD_BYTES = 256 * 1024


@dataclass(frozen=True)
class UndoSpec:
    """Everything needed to reverse one action, and nothing else.

    `prev` maps each message id to the mailbox ids it was in *before* the
    action, which is what makes undo exact rather than approximate: archive is
    reversed by restoring the mailboxes that message actually had, not by
    "remove Archive, add Inbox" (which would resurrect a message into the
    Inbox it was never in, and lose the label it was filed under). It
    supersedes the plan's earlier `add`/`remove` field pair for exactly that
    reason.

    `keyword`/`on` carry the flag half of an action (`$flagged`, `$seen`, and
    spam's `$junk` rider); undo applies `not on`. Both are `None` for an
    action that touched no keyword. `email_ids` lists only the messages the
    action *actually changed* — starring an already-starred message is not
    something undo should unstar.

    `toast` is the past-tense label the UI shows ("Archived"), kept in the
    signed payload so the toast and its undo button can never disagree about
    what happened.
    """

    kind: str
    email_ids: list[str]
    prev: dict[str, list[str]]
    keyword: str | None
    on: bool | None
    toast: str


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(part: str) -> bytes:
    """Strict inverse of `_b64u_encode`, raising `binascii.Error` on anything
    that is not canonical base64url.

    `validate=True` (and hence the manual translation, since
    `base64.urlsafe_b64decode` gives no way to ask for it) matters: the default
    *silently discards* every character outside the alphabet, which would make
    a token with punctuation sprinkled through it decode — and therefore
    verify — exactly like the clean one. That is not a forgery (the MAC still
    covers the decoded bytes, so the payload itself can't be altered), but a
    token should have one spelling, not an infinite family of them.
    """
    padded = part.replace("-", "+").replace("_", "/") + "=" * (-len(part) % 4)
    return base64.b64decode(padded, validate=True)


def _mac(secret_key: str, payload: bytes) -> bytes:
    return hmac.new(derive_key(secret_key, _PURPOSE), payload, hashlib.sha256).digest()


def _same_scope(token_scope: object, scope: str | None) -> bool:
    """Constant-time scope comparison that cannot raise.

    Both sides are encoded to bytes first: `secrets.compare_digest` refuses
    two `str`s unless both are pure ASCII (`TypeError`), and a scope arriving
    from a request has no obligation to be — a 500 on a non-ASCII scope would
    be a worse answer than the 400 every other rejection gets.
    """
    if (token_scope is None) != (scope is None):
        return False
    if scope is None:
        return True
    return secrets.compare_digest(str(token_scope).encode("utf-8"), scope.encode("utf-8"))


def _encode(spec: UndoSpec, expires: float, scope: str | None) -> dict:
    """`spec` as the compact wire payload described in the module docstring.

    Every mailbox id is written once into `_MAILBOXES`, every *distinct*
    placement once into `_GROUPS` as a list of indices into it, and each
    message carries only its index into `_GROUPS` — which is what collapses a
    bulk archive (where every message shares one or two placements) from one
    id-and-mailbox-list per message down to one small integer per message.

    Trusts `prev` to name only ids that are also in `email_ids` — an
    invariant of everything in `mailosh.services.actions` that builds a spec
    (both come from the same list of changes), asserted there at
    `_result`, the one place it is actually established, rather than
    re-checked here. `sign` runs on the response path *after* the write it
    is undoing has already committed, so raising on a violation here would
    turn a future bug into a 500 (plus a misleading "revert" toast) for an
    action the server had already performed successfully. The loop below is
    driven by `email_ids`, not `prev`, so if the invariant were ever broken
    anyway, a stray `prev` entry is simply never encoded rather than
    crashing the response.
    """
    mailbox_index: dict[str, int] = {}
    group_index: dict[tuple[int, ...], int] = {}
    groups: list[list[int]] = []
    placement: list[int] = []
    for email_id in spec.email_ids:
        previous = spec.prev.get(email_id)
        if previous is None:
            placement.append(_NO_PLACEMENT)
            continue
        indices = tuple(
            mailbox_index.setdefault(mailbox_id, len(mailbox_index)) for mailbox_id in previous
        )
        slot = group_index.get(indices)
        if slot is None:
            slot = group_index[indices] = len(groups)
            groups.append(list(indices))
        placement.append(slot)

    return {
        _KIND: spec.kind,
        _IDS: spec.email_ids,
        _MAILBOXES: list(mailbox_index),
        _GROUPS: groups,
        _PLACEMENT: placement if spec.prev else None,
        _KEYWORD: spec.keyword,
        _ON: spec.on,
        _TOAST: spec.toast,
        _EXPIRES: expires,
        _SCOPE: scope,
    }


def _decode(payload: dict) -> tuple[UndoSpec, float, object]:
    """Inverse of `_encode`: the spec, its expiry, and the scope it was signed
    with. Raises (`KeyError`/`TypeError`/`ValueError`/`IndexError`) on anything
    that is not a payload this module wrote — `verify` maps all of those to one
    `ValueError`.
    """
    email_ids = [str(email_id) for email_id in payload[_IDS]]
    mailboxes = [str(mailbox_id) for mailbox_id in payload[_MAILBOXES]]
    groups = [[mailboxes[index] for index in group] for group in payload[_GROUPS]]

    prev: dict[str, list[str]] = {}
    placement = payload[_PLACEMENT]
    if placement is not None:
        for email_id, slot in zip(email_ids, placement, strict=True):
            if slot != _NO_PLACEMENT:
                prev[email_id] = list(groups[slot])

    spec = UndoSpec(
        kind=str(payload[_KIND]),
        email_ids=email_ids,
        prev=prev,
        keyword=payload[_KEYWORD],
        on=payload[_ON],
        toast=str(payload[_TOAST]),
    )
    return spec, float(payload[_EXPIRES]), payload[_SCOPE]


def _pack(payload: dict) -> bytes:
    """Payload -> deflated JSON bytes (what actually gets signed)."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return zlib.compress(raw, 9)


def _unpack(raw: bytes) -> dict:
    """Inverse of `_pack`, bounded at `_MAX_PAYLOAD_BYTES`. Only ever called
    on bytes whose MAC has already verified.
    """
    stream = zlib.decompressobj()
    body = stream.decompress(raw, _MAX_PAYLOAD_BYTES)
    if not stream.eof or stream.unconsumed_tail:
        raise ValueError("undo payload is larger than expected")
    return json.loads(body)


def sign(
    spec: UndoSpec, secret_key: str, now: float | None = None, *, scope: str | None = None
) -> str:
    """`spec` as a `<payload>.<signature>` token, both halves urlsafe-base64
    without padding (so it is safe in a header, a form field and a URL alike).

    `scope`, when given, is signed into the payload and must be presented
    again at `verify` time — see the module docstring.

    Trusts `spec.prev` to name only ids that are also in `spec.email_ids` —
    see `_encode`'s docstring for why that invariant is asserted where it is
    established (`mailosh.services.actions._result`) instead of re-checked
    on this response path.
    """
    now = time.time() if now is None else now
    raw = _pack(_encode(spec, now + TTL_SECONDS, scope))
    return f"{_b64u_encode(raw)}.{_b64u_encode(_mac(secret_key, raw))}"


def verify(
    token: str, secret_key: str, now: float | None = None, *, scope: str | None = None
) -> UndoSpec:
    """The `UndoSpec` `token` carries, or `ValueError`.

    One error type and one message for every rejection — bad signature,
    tampered payload, wrong key, expired, wrong scope, not even a token — so
    nothing about *why* a guess failed leaks back to whoever is guessing.
    """
    now = time.time() if now is None else now
    try:
        payload_part, signature_part = token.split(".")
        raw = _b64u_decode(payload_part)
        signature = _b64u_decode(signature_part)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid undo token") from exc

    if not secrets.compare_digest(signature, _mac(secret_key, raw)):
        raise ValueError("invalid undo token")

    # Everything below runs only on bytes this app itself signed — hence the
    # deflate is undone here and not a line earlier. A payload that still fails
    # to parse means some older/other shape of this app's own making, which is
    # rejected rather than trusted; `_same_scope` is inside the guard too, so a
    # non-ASCII scope is a 400 like every other rejection, never a `TypeError`.
    try:
        spec, expires, token_scope = _decode(_unpack(raw))
        if now > expires or not _same_scope(token_scope, scope):
            raise ValueError("invalid undo token")
    except (KeyError, IndexError, TypeError, ValueError, zlib.error) as exc:
        raise ValueError("invalid undo token") from exc
    return spec


async def apply(client: JmapClient, spec: UndoSpec) -> None:
    """Perform the reverse operation `spec` describes.

    Mailboxes are restored to exactly `prev` — computed against a *fresh*
    snapshot rather than against whatever the action assumed, so a message
    somebody moved again in between is still put back where it was rather than
    having a stale patch applied to it. Keywords are reversed (`not on`).

    "Exactly" is a deliberate trade-off, not an oversight: a change made
    *inside* the undo window is overwritten by it. Archive a message, file it
    under Work, then press `z`, and the message goes back to the Inbox
    *without* Work, because Work is not in the placement the action recorded.
    Restoring the pre-action state is what "Undo" means to a reader watching a
    toast count down, and the alternative — merging the two — would silently
    resurrect labels for the far more common case of undoing a move the user
    simply did not mean to make. The window is 10 s in the UI and the token
    dies at 60 s, which is what keeps the overwritten interval small.

    Costs one `Email/get` plus one `Email/set` for a mailbox action, and a
    single `Email/set` for a keyword-only one (no snapshot is needed when
    there is nothing to restore). Spam's mailbox restore and its `$junk`
    removal ride in the same `Email/set` whenever the two cover the same ids.

    A message with no `prev` entry (or an empty one) is never given a mailbox
    patch at all: "restore to nothing" would mean removing every mailbox it is
    in, which is both invalid JMAP and the one outcome undo must never
    produce.
    """
    keyword_patch = (
        {spec.keyword: not spec.on} if spec.keyword is not None and spec.on is not None else None
    )

    mailbox_patches: dict[str, dict[str, bool | None]] = {}
    restorable = [email_id for email_id in spec.email_ids if spec.prev.get(email_id)]
    if restorable:
        for state in await client.get_email_states(restorable):
            previous = set(spec.prev[state.id])
            patch: dict[str, bool | None] = {
                mid: True for mid in sorted(previous - state.mailbox_ids)
            }
            patch.update({mid: None for mid in sorted(state.mailbox_ids - previous)})
            if patch:
                mailbox_patches[state.id] = patch

    # The keyword reversal can ride along inside the mailbox `Email/set` only
    # when that call already covers every message the keyword applies to.
    rides_along = keyword_patch is not None and set(mailbox_patches) == set(spec.email_ids)
    if mailbox_patches:
        await client.set_mailboxes_patch(
            mailbox_patches, keywords=keyword_patch if rides_along else None
        )
    if keyword_patch is not None and not rides_along and spec.email_ids:
        keyword, on = next(iter(keyword_patch.items()))
        await client.set_keywords(spec.email_ids, keyword, on)
