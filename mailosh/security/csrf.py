"""CSRF validation for the webmail app's mutation routes (design spec §9).

Every mutating route is POST-only and must carry the session's own
`csrf_token` (`mailosh.security.sessions.SessionRow.csrf_token`) back
either as the `X-CSRF-Token` header (sent by every htmx request via an
inherited `hx-headers` reading `<meta name="csrf-token">`, Task 5/6) or as
a hidden field in a plain `<form>` post. Form bodies are parsed
asynchronously (`await request.form()`), which this function — deliberately
sync, so it can run as a plain guard clause at the top of a handler — can't
do itself; a route that already parsed its own form body passes the field
through as `form_token`.

Two independent signals are required for any unsafe request, and neither
is trusted alone:

- the token itself, compared with `secrets.compare_digest` (constant-time:
  a token guess can't be narrowed down by timing how fast the comparison
  fails); and
- `Sec-Fetch-Site` — a fetch metadata header every modern browser sets
  itself, from its own request context, which a page cannot script its way
  around. `cross-site` is rejected even when the token is correct, since a
  browser only ever sends that value for a request this session's own page
  did not initiate.

`HX-Request` is deliberately never consulted here: it is a plain request
header any cross-site page can set on a `fetch()`/form just as easily as
`X-CSRF-Token`, so on its own it proves nothing about origin (design spec
§9's "`HX-Request` alone is not trusted").
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException
from starlette.requests import Request

#: Methods CSRF validation never applies to. They must not mutate state, so
#: there is nothing here for a forged cross-site request to achieve.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def new_token() -> str:
    """A fresh, unguessable per-session CSRF token.

    `secrets.token_urlsafe(32)` — the same primitive
    `mailosh.security.sessions` uses for the session id itself, but drawn
    independently each time: a session's CSRF token is never derived from,
    or equal to, its session id, so a value that leaks through one channel
    never hands over the other.
    """
    return secrets.token_urlsafe(32)


def is_cross_site(request: Request) -> bool:
    """True when the browser's own Fetch Metadata says `request` was
    initiated by a different site than this app (module docstring) — the
    one signal `validate` rejects on regardless of token correctness.

    Exposed separately (Task 5) so a route with no session yet to validate
    a token against — `mailosh.web.auth`'s `POST /login`, which by
    definition happens before any session exists — can still apply this
    same browser-enforced check on its own, rather than either skipping it
    or reimplementing the header comparison a second time.
    """
    return request.headers.get("sec-fetch-site") == "cross-site"


def validate(request: Request, session_token: str, form_token: str | None = None) -> None:
    """Raise `HTTPException(403)` unless `request` is safe to act on.

    - `GET`/`HEAD`/`OPTIONS` are exempt outright — CSRF only guards
      state-changing requests.
    - Every other method rejects `Sec-Fetch-Site: cross-site` outright,
      even given a correct token (see module docstring).
    - Every other method must also carry a token — `X-CSRF-Token`, or
      `form_token` when the caller already parsed one out of the request
      body — that matches `session_token` under `secrets.compare_digest`.
    """
    if request.method in _SAFE_METHODS:
        return
    if is_cross_site(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    token = request.headers.get("x-csrf-token") or form_token
    if not token or not secrets.compare_digest(token, session_token):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
