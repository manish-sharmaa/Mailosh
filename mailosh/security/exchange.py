"""The Stalwart credential exchange (design spec §9): verify a user's own
password directly against Stalwart, once, at login -- the only place in
this codebase a user's plaintext password is ever handled. Everything after
that runs on a minted per-session Stalwart API key
(`mailosh.stalwart_admin.StalwartAdmin.create_api_key`/`destroy_api_key`,
`mailosh.jmap.client.JmapClient.connect_bearer`) instead; the password
itself is never stored, logged, or reused.

`verify_password` deliberately does NOT reuse `JmapClient.connect`, even
though both fetch `GET {base_url}/.well-known/jmap` with HTTP Basic auth:
`JmapClient.connect` builds a `Session` whose `primary_account_id` field is
required (non-nullable) via `Session.from_jmap`, so handing it Stalwart's
own *anonymous* response for a bad password (see below) would surface as an
unrelated `pydantic.ValidationError`, not the clean "invalid credentials"
outcome this function's contract needs. This function reads the raw
response dict directly instead, exactly the way `StalwartAdmin` reads its
own raw JMAP responses.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from mailosh.jmap.errors import TransportError

#: The mail-account entry this module looks for in a session response's
#: `primaryAccounts` map -- same capability URI `mailosh.jmap.models.
#: Session.from_jmap` keys off of for its own (differently-typed)
#: `primary_account_id` field.
_MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"


@dataclass(frozen=True)
class VerifiedAccount:
    """A password that checked out against Stalwart's own JMAP session
    endpoint: the account's login `username` and its primary mail account
    id, both read directly off that response (never off the caller-supplied
    login string) -- `username`/`accounts` are what Stalwart itself
    considers this credential to be, which is the only thing worth trusting
    here. `email` duplicates `username` (Stalwart's own `username` is
    already the account's email address, confirmed by every live session
    this codebase has captured -- e.g. `tests/fixtures/session.json`) as
    its own named field, matching the Interfaces block's
    `VerifiedAccount(username, account_id, email)` shape so a caller never
    has to know that the two happen to be the same string.
    """

    username: str
    account_id: str
    email: str


def _transport_error(exc: httpx.HTTPStatusError | httpx.TransportError) -> TransportError:
    """Translate an httpx-level failure into `TransportError`, keeping
    status/message -- the same translation `mailosh.jmap.client` and
    `mailosh.stalwart_admin` each already do (both modules' own
    `_transport_error` docstrings explain why this is deliberately
    duplicated per module rather than shared: each is a small, private
    helper for a module that talks to Stalwart as its own distinct
    principal, not a shared utility).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return TransportError(
            f"Stalwart credential check at {exc.request.url} failed: "
            f"{resp.status_code} {resp.reason_phrase}",
            status_code=resp.status_code,
        )
    return TransportError(f"Stalwart credential check failed: {exc}", status_code=None)


async def verify_password(
    stalwart_url: str, username: str, password: str
) -> VerifiedAccount | None:
    """Check `username`/`password` against Stalwart itself and return the
    verified account, or `None` if the credentials are wrong.

    `GET {stalwart_url}/.well-known/jmap` with HTTP Basic auth
    (`auth=(username, password)`), `follow_redirects=True` (Stalwart 307s
    this to `/jmap/session` -- see `JmapClient.connect`), a 10s timeout (an
    interactive login request; no reason to wait as long as this codebase's
    other 30s-timeout JMAP calls, which are typically already-authenticated
    background work).

    Three distinct outcomes, matching design spec §9 / SPK-3 / SPK-5
    exactly (controller decision #2):

    - **Right password**: Stalwart answers 200 with a real session body
      (`username` set, a `urn:ietf:params:jmap:mail` entry in
      `primaryAccounts`) -- returns a `VerifiedAccount`.
    - **Wrong password**: confirmed live (SPK-3/SPK-5) that Stalwart
      answers 200 too, but with an *anonymous* session (`username` empty,
      `primaryAccounts` empty) rather than a 401/403 -- so a 401/403
      response (handled here too, in case a future Stalwart version or a
      fronting proxy ever does reject this way) and a 200-with-anonymous-
      body both mean the same thing: **returns `None`**, never raises.
    - **Stalwart unreachable, or itself broken (5xx/malformed)**: this is
      not a verdict on the password at all -- **raises `TransportError`**,
      so a caller (Task 5's login route) can tell "the mail server is
      down" apart from "that password is wrong" instead of collapsing both
      into the same generic error copy.

    Never logs anything -- not the password (obviously), not even a bare
    "login failed for user X" line: audit logging login attempts is the
    login *route*'s job (Task 5, design spec §9's "audit log rows for
    login success/failure"), layered on top of this function's plain
    verify-or-not primitive, not duplicated here.
    """
    try:
        async with httpx.AsyncClient(
            auth=(username, password), http2=False, timeout=10, follow_redirects=True
        ) as http:
            resp = await http.get(f"{stalwart_url}/.well-known/jmap")
            if resp.status_code in (401, 403):
                return None
            resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        raise _transport_error(exc) from exc

    data = resp.json()
    verified_username = data.get("username") or ""
    account_id = (data.get("primaryAccounts") or {}).get(_MAIL_CAPABILITY)
    if not verified_username or not account_id:
        return None
    return VerifiedAccount(
        username=verified_username, account_id=account_id, email=verified_username
    )
