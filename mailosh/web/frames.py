"""Every route that serves part of a message to a browser.

    GET  /m/{id}/html            the sandboxed document itself
    GET  /m/{id}/frame           the banner and the `<iframe>` that embeds it
    GET  /img?u=<token>          the remote-image proxy those images point at
    GET  /m/{id}/cid/{cid}?u=…   one inline part, for an `<img src="cid:…">`
    GET  /m/{id}/att/{blob}    one attachment, downloaded or previewed
    GET  /m/{id}/source        the raw RFC 5322 message, capped
    POST /m/{id}/images/allow  "Always show from", remembered for this sender
    POST /m/{id}/restyle       "Original colours", remembered for this sender

They are one module because they are one trust boundary. `/m/{id}/html`
renders markup a stranger wrote; `/m/{id}/frame` is the only element that
may embed it, and its `sandbox` attribute is half of the containment
(`mailosh.render.frame_document`'s CSP is the other half — a document under
both gets the *intersection*, so the two lists must agree); `/img` exists
so that the browser never opens a socket to the sender's host; and the
three blob routes serve bytes the frame's own `img-src 'self'` points at.
Splitting them across modules would let one drift.

**A part id is not a capability.** `/cid`, `/att` and `/source` all take
an id straight out of a URL, and every one of them resolves it *inside one
message this reader's own account holds* — `_load_message` runs against
that reader's own `JmapClient`, so another account's message id is simply
not found, and the part is then looked up in that message's own
`attachments`/`blobId` rather than fetched by id. `html_sanitize` already
refuses a `cid:` that is not one of this message's parts, but these URLs
are guessable and reachable without going through a rendered document at
all, so each route repeats the check rather than trusting the sanitiser to
have been the only way in.

**The two routes the frame itself fetches are authenticated by a signed
token, not by the session cookie**, and this is the one thing about this
module that is not obvious from any single route. The framed document is
served under a CSP whose `sandbox` directive omits `allow-same-origin`, so
it has an **opaque origin**; a subresource request from such a document is
not same-site for cookie purposes, and no browser attaches a
`SameSite=Lax` cookie to it. `GET /img?u=…` and `GET /m/{id}/cid/{cid}?u=…`
therefore arrive with **no session at all** — before they took tokens, both
answered `303 -> /login` and every inline logo and proxied image in the app
rendered as a broken-image icon in every browser, while every test passed
(`httpx` sends cookies whatever `SameSite` says, and has no notion of a
document origin).

The fix is the posture the frame already has, carried one step further:
the document has no ambient authority, so its subresources carry explicit,
per-URL, per-reader capabilities. `mailosh.render.image_policy` mints both
tokens and `mailosh.render.html_sanitize` writes them into the `src` it
emits. What that changes, stated plainly: a token is now a **bearer**
capability — whoever holds one can spend it without being logged in — so
each one names exactly one resource, names the reader it was minted for,
and dies in an hour. `/cid`'s names the message *and* the content id,
because it reads the reader's own mail and a token that named only one of
them would read a different message or a different part.

The token authorises the fetch. It does **not** replace the scoping above:
`/cid` verifies the token, resolves that reader's own client, and then
still requires that the message is one *that account* holds and the
Content-ID one *that message* carries. The two other blob routes (`/att`,
`/source`) are followed from the top document, not from inside the frame,
so they keep the session cookie and are unchanged.

**`/cid` answers `404` for everything it refuses** — a missing, forged,
expired or foreign token, a message this account does not hold, a
Content-ID it does not carry, a type outside `INLINE_IMAGE_TYPES`, an
oversized part. One status for every refusal is what keeps the route from
being an oracle: a `403` for "bad token" and a `404` for "no such part"
would together answer "does this message carry this Content-ID?" for
anybody willing to ask twice.

**Nothing here is ever served as something a browser will run.** `/cid` is
a positive allow-list of six raster types (no `image/svg+xml`: an SVG is a
scripting context, and served under an `<img>` the frame's CSP already
permits it would be the one XSS the sandbox exists to prevent). `/att`
serves the real type only for `PREVIEW_TYPES`, and `application/octet-
stream` with `Content-Disposition: attachment` for everything else,
whatever `?inline=1` asks for. `/source` is `text/plain` — the raw source
*is* the sender's HTML, and this app's own origin is exactly where it must
not render. All three carry `nosniff` so a browser cannot second-guess
those declarations from the bytes, and `default-src 'none'; sandbox` so
that even if one did, there would be nothing left to do.

**Why `/img` exists at all.** A remote `<img>` in an email is a tracking
pixel until proven otherwise: fetched by the reader's browser it hands the
sender their IP, their user agent, and the exact moment they opened the
mail. So the browser never fetches one. `html_sanitize` rewrites every
permitted remote image to `/img?u=<signed token>` on this app's own origin,
this route verifies the token, and `fetch_guard` does the fetching from the
server behind a full SSRF check. The frame's CSP `img-src 'self' data:` is
byte-identical with `?remote=0` and `?remote=1` for that reason — "show
images" widens *what the sanitiser emits*, never what the browser is
allowed to reach.

**`BlockedUrl` is a `ValueError`, and the two must not be caught together.**
A bad or forged token is `403`; a URL the guard refused is `502`. They are
handled in two separate `try` blocks rather than one, because a single
`except ValueError` would answer `403` for a blocked target — telling
whoever sent the email that their token was fine and their *target* was the
problem. Both bodies are empty for the same reason: the response to
`/img?u=…` is read by an `<img>` in the sender's own document, and a
reason string there is a probe result. `502` says only "no image".

**Fan-out.** One message can carry two hundred remote images and the
browser will ask for all of them at once. A per-user `asyncio.Semaphore(6)`
in `app.state` caps how many outbound fetches one reader can have in flight,
so a single hostile newsletter cannot exhaust the process's sockets or turn
this server into someone else's load generator. Per user, not global: one
reader's newsletter must not stall another reader's inbox.

**The remote-image gate is decided here and nowhere else, for the same
reason.** What a message's blocked images *are* is knowable only where the
sanitiser runs, because the count and the host list are what that pass
produces — and `/m/{id}/html`'s answer is an opaque sandboxed document the
conversation view cannot read a number out of. So `/m/{id}/frame` is where
the count is taken: it sanitises once, renders `thread/banner.html` with
that result and the `<iframe>` under it, and `thread/message.html` loads
the pair. Counting a second time in `mailosh.web.mail` would do the same
expensive work twice and give the number two places to drift.

`image_policy.decide` is what the `?remote=` on this route feeds: it is the
reader's override *for this message*, not the whole answer. Their stored
`remote_images` policy and their per-sender allow list are the rest of it,
and this module reads them because it is the layer holding a database
session — the same reason the restyle folds together here.

**The dark restyle is decided here and nowhere else.** `mailosh.render.dark`
answers the three factual questions (does the mail declare a colour scheme,
does its background read light, and given those what mode applies);
`mailosh.render.frame_document` emits the CSS each mode means. This module
is what folds a reader's Appearance setting, their per-sender "Show
original" override and the `?restyle=0` in the URL into the one `enabled`
flag `restyle_mode` takes, because it is the only layer that has a database
session and a `UiPref` row to read.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.db.models import AppUser, UiPref
from mailosh.jmap.client import _EMAIL_BODY_PROPS, _MAX_BODY_VALUE_BYTES, JmapClient
from mailosh.jmap.models import BodyPart, EmailBody
from mailosh.jmap.pool import ClientPool
from mailosh.render import quote_trim
from mailosh.render.dark import background_is_light, declares_color_scheme, restyle_mode
from mailosh.render.fetch_guard import BlockedUrl, fetch_image
from mailosh.render.frame_document import csp_header, render_frame
from mailosh.render.html_sanitize import (
    BodyTooDeep,
    SanitizeContext,
    extract_styles,
    sanitize_email_html,
)
from mailosh.render.image_policy import (
    allow_sender,
    decide,
    sign_cid_url,
    sign_remote_url,
    verify_cid_token,
    verify_image_token,
)
from mailosh.security import sessions
from mailosh.web import deps

__all__ = [
    "FANOUT_LIMIT",
    "INLINE_IMAGE_TYPES",
    "MAX_INLINE_BYTES",
    "MAX_SOURCE_BYTES",
    "PREVIEW_TYPES",
    "router",
]

router = APIRouter(tags=["frames"])

UserDep = Annotated[AppUser, Depends(deps.current_user)]
ClientDep = Annotated[JmapClient, Depends(deps.client_for)]
DbDep = Annotated[AsyncSession, Depends(deps.get_db)]
PrefsDep = Annotated[UiPref, Depends(deps.prefs_for)]

#: Concurrent outbound image fetches allowed to one reader. Six is the
#: per-host connection budget a browser itself works to, so a message whose
#: images all live on one CDN is not slowed by this, while one carrying two
#: hundred is bounded.
FANOUT_LIMIT = 6

#: The largest inline part `/m/{id}/cid/{cid}` will serve. A logo, a
#: signature image or a header banner is kilobytes; five megabytes is
#: already far past anything a mail client should be inlining, and a part
#: over it is a `404` rather than a truncated stream, because half a PNG is
#: a broken image the reader cannot tell from a bug.
MAX_INLINE_BYTES = 5 * 1024 * 1024

#: The largest slice of a raw message `/m/{id}/source` will hand back.
#: Unlike `MAX_INLINE_BYTES` this one *truncates* rather than refusing: the
#: single reader who reaches this route is the one looking at a body
#: Stalwart already cut short, so "too big to show at all" would be a dead
#: end at exactly the moment the route exists for. The first two megabytes
#: carry every header and the start of the body, which is what "show
#: original" is actually for.
MAX_SOURCE_BYTES = 2 * 1024 * 1024

#: The only types `/m/{id}/cid/{cid}` will serve — a positive allow-list of
#: raster images, checked against the *normalised* declared type.
#:
#: `image/svg+xml` is deliberately absent and must stay absent: an SVG is a
#: scripting context, and this route's URL is what the frame's
#: `img-src 'self'` exists to permit. One served from here would be a
#: script running on this app's own origin — the exact thing the sandbox,
#: the CSP and the whole proxy design are built to prevent.
INLINE_IMAGE_TYPES = frozenset(
    {"image/png", "image/gif", "image/jpeg", "image/webp", "image/bmp", "image/x-icon"}
)

#: The only types `/m/{id}/att/{blob}?inline=1` will serve with their real
#: `Content-Type`. Everything else — `text/html` and `image/svg+xml` most
#: of all — falls back to the download shape no matter what the query
#: string asks for, because `inline=1` is a request from a link the reader
#: clicked, not a permission the reader granted.
PREVIEW_TYPES = frozenset(
    {
        "image/png",
        "image/gif",
        "image/jpeg",
        "image/webp",
        "image/bmp",
        "application/pdf",
        "text/plain",
    }
)

#: What a browser is told to do with bytes it must never render: nothing.
#: `application/octet-stream` plus `Content-Disposition: attachment` is the
#: pair that makes a `.html` attachment a download instead of a page on
#: this app's own origin.
_DOWNLOAD_TYPE = "application/octet-stream"

#: Where the per-user semaphores live on `app.state`. Created lazily by
#: `_fanout_slot` rather than in `create_app`'s lifespan, so this router
#: stays a `include_router` call away from being mounted and nothing in the
#: app's startup has to know the proxy exists.
_FANOUT_ATTR = "image_fanout"

#: Response headers on every `/img` hit that returns bytes. Not one upstream
#: header is forwarded — the body is the only thing that crosses.
#:
#: `nosniff` is the load-bearing one: `fetch_guard` already refuses
#: everything but the raster types in `ALLOWED_IMAGE_TYPES` (notably not
#: `image/svg+xml`, which is a scripting context), and `nosniff` stops a
#: browser from second-guessing that declaration by looking at the bytes.
#: The CSP is belt and braces on the same point — even if something
#: script-shaped were served from this origin, `default-src 'none'; sandbox`
#: leaves it nothing to do. `private` on the cache: an image URL is bound to
#: one user's token, so a shared cache must never serve it to anyone else.
_IMAGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "inline",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, max-age=86400",
}

#: Response headers on every blob this app serves out of a message —
#: `/cid`, `/att` and `/source` alike. The same reasoning as `_IMAGE_HEADERS`
#: above, with a shorter cache life: an inline logo is worth keeping for a
#: reading session, not for a day, and `private` is what keeps a shared
#: cache from handing one reader's attachment to another.
#:
#: `nosniff` is restated here even though `create_app`'s middleware already
#: applies it app-wide: these three routes are the only ones that hand a
#: browser bytes a stranger wrote, and their safety rests on a declared type
#: being believed. Inheriting that from a middleware nobody would think to
#: check before editing is not a guarantee — it is a coincidence that has
#: so far held. The CSP is the opposite case: the app-wide one permits
#: scripts and styles from `'self'`, and it is *this* dictionary, applied
#: over it, that reduces a blob response to `default-src 'none'; sandbox`.
#:
#: `Content-Disposition` is deliberately *not* here: it is the one header
#: that differs between them (`inline` for a cid part and for source, a
#: filename-carrying `attachment` for a download), and a default here would
#: be a default that a route could forget to override.
_BLOB_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, max-age=3600",
}

#: Stripped from an attachment's filename before it is put in a header.
#: A `\r\n` in a name is a header-injection attempt, not a formatting
#: quirk — the name arrives from the wire, written by whoever sent the mail.
_HEADER_BREAKS = re.compile(r"[\r\n\x00]")

#: Response headers on the framed document. The CSP is the whole containment
#: story (see `mailosh.render.frame_document`); `no-referrer` keeps the URL
#: of the message being read out of every request the frame makes; `no-store`
#: because a message body is not something to leave in a shared browser's
#: disk cache after the reader has logged out.
_FRAME_DOC_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _load_message(client: JmapClient, email_id: str) -> EmailBody | None:
    """One `Email/get` for one message, bodies fetched — or `None` when the
    account has no such message.

    Deliberately not `get_thread`: a frame is asked for by id, and making
    the whole conversation a prerequisite for rendering one body would turn
    every lazy-loaded `<iframe>` in a twenty-message thread into a
    twenty-message fetch.

    Reaches into `JmapClient._call` and the two module-private constants
    beside it rather than duplicating the property list. `_EMAIL_BODY_PROPS`
    is the definition of "everything a rendered message needs", and a second
    copy here would be one `git log` away from disagreeing with the
    conversation view about which headers exist.
    """
    out = await client._call(
        [
            (
                "Email/get",
                {
                    "accountId": client.account_id,
                    "ids": [email_id],
                    "properties": _EMAIL_BODY_PROPS,
                    "fetchTextBodyValues": True,
                    "fetchHTMLBodyValues": True,
                    "maxBodyValueBytes": _MAX_BODY_VALUE_BYTES,
                },
                "e0",
            )
        ]
    )
    rows = out["e0"]["list"]
    return EmailBody.model_validate(rows[0]) if rows else None


async def _client_for_reader(request: Request, db: AsyncSession, user_id: int) -> JmapClient | None:
    """A connected `JmapClient` acting as `user_id`, or `None` when that
    reader has no live session left.

    The counterpart to `deps.client_for` for a route authenticated by a
    capability rather than a cookie. `deps.client_for` resolves the session
    the *request* carries; this one is reached only after an unexpired HMAC
    has named a reader, and then has to find a credential to act as them —
    the per-session Stalwart API key is the only credential this app holds
    for a user, so `sessions.live_session_for_user` picks the freshest
    still-valid one and the pool connects (or reuses) a client for it.

    `None` is not an error to log, it is an answer: a reader who has signed
    out everywhere has no sessions, so their outstanding image URLs stop
    working at that moment rather than at their expiry. The caller turns it
    into the same `404` every other refusal produces.

    Nothing here re-decides *whether* the fetch is allowed. The token said
    which reader; this says how to be them; the route still checks that the
    message is one that account holds.
    """
    settings = _settings(request)
    session = await sessions.live_session_for_user(db, user_id, settings)
    if session is None:
        return None
    pool: ClientPool = request.app.state.pool
    return await pool.get(session, settings)


def _bare_cid(part: BodyPart) -> str | None:
    """`part`'s Content-ID as a `cid:` URL spells it, or `None`.

    The angle brackets RFC 2392 puts around a Content-ID in the header are
    stripped, because a `cid:` URL never carries them. Case is *not*
    touched: only the `cid:` scheme is case-insensitive, the id itself is
    not, and folding it here would let one part answer for another.

    The one place this normalisation lives. `_cid_parts` below builds the
    membership set the sanitiser rewrites against, and `_cid_part` resolves
    the URL that rewrite produces; if those two spelled the id differently
    every inline image in the app would 404 — or worse, a part the
    sanitiser refused would be reachable anyway.
    """
    return part.cid.strip().strip("<>") if part.cid else None


def _cid_parts(message: EmailBody) -> dict[str, str]:
    """`{content-id: blob id}` for this message's inline parts.

    Membership in this map is what `html_sanitize` uses to decide an inline
    image is real: an `<img src="cid:…">` naming an id this message does
    not have gets no `src` at all, so a mail cannot address another
    message's attachments by guessing.
    """
    return {
        cid: part.blob_id
        for part in message.attachments
        if (cid := _bare_cid(part)) and part.blob_id
    }


def _cid_part(message: EmailBody, content_id: str) -> BodyPart | None:
    """The part of `message` whose Content-ID is exactly `content_id`.

    Scoped to one message on purpose — see the module docstring: the id in
    the URL is guessable, so this is a lookup within what the reader is
    already entitled to see, never a fetch by id.
    """
    for part in message.attachments:
        if _bare_cid(part) == content_id and part.blob_id:
            return part
    return None


def _attachment_part(message: EmailBody, blob_id: str) -> BodyPart | None:
    """The attachment of `message` carrying `blob_id`.

    `message.blob_id` — the whole raw message — is deliberately not
    reachable this way: it has its own route, with its own cap and its own
    `text/plain`, and a second uncapped path to the same bytes would make
    that cap decorative.
    """
    for part in message.attachments:
        if part.blob_id == blob_id:
            return part
    return None


def _mime_of(part: BodyPart) -> str:
    """`part`'s declared type, normalised for comparison against an
    allow-list: parameters dropped, case folded, whitespace trimmed.

    `Content-Type: IMAGE/PNG; name=x` and `image/png` are the same type, and
    an allow-list matched against the raw string would refuse the first —
    or, matched loosely, would accept `image/png; charset=…/../` shapes
    nobody intended.
    """
    return part.type.split(";")[0].strip().lower()


def _content_disposition(name: str | None, *, inline: bool) -> str:
    """A `Content-Disposition` value carrying `name` in both RFC 5987 forms.

    Both, not one: the ASCII-folded `filename=` is what an old client
    reads, `filename*=UTF-8''` is what everything else reads, and emitting
    only one of them loses either the name or its accents.

    CR, LF and NUL come out of the name *before* either form is built. That
    is a header-injection guard, not tidiness — the name is written by
    whoever sent the mail, and a `\\r\\n` in it would otherwise end this
    header and begin one of the sender's choosing.
    """
    clean = _HEADER_BREAKS.sub("", name or "").strip() or "attachment"
    folded = clean.encode("ascii", "replace").decode("ascii").replace('"', "").replace("\\", "")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{folded}\"; filename*=UTF-8''{quote(clean, safe='')}"


async def _blob_chunks(
    client: JmapClient, blob_id: str, *, mime_type: str, name: str, limit: int | None = None
) -> AsyncIterator[bytes]:
    """Stream one blob out of Stalwart, stopping after `limit` bytes.

    Streamed rather than buffered so that a forty-megabyte attachment is
    never held in this process at once, and capped so that what a route
    forwards is bounded by a constant rather than by whatever `size` the
    sender declared — `size` is metadata, and a part that claims three
    bytes may serve six megabytes.

    `limit=None` is the deliberate exception, and `/m/{id}/att/{blob}` is
    the one route that takes it: an attachment is a file the reader is
    asking to save, whole, and truncating one silently would corrupt it.
    Nothing is buffered either way, so the absent cap costs memory nothing
    — and the two routes that render bytes *into a page* keep theirs.
    """
    sent = 0
    async with client.stream_blob(blob_id, mime_type=mime_type, name=name) as response:
        async for chunk in response.aiter_bytes():
            if limit is not None:
                remaining = limit - sent
                if remaining <= 0:
                    return
                chunk = chunk[:remaining]
            sent += len(chunk)
            yield chunk


def _sanitize_context(
    request: Request, message: EmailBody, user: AppUser, *, remote: bool
) -> SanitizeContext:
    """The `SanitizeContext` for one message, built in one place so that
    `/m/{id}/html` and every later route that renders a body cannot end up
    signing images differently.

    `origin` is this deployment's own absolute origin, which every URL the
    sanitiser emits is built from — nh3 deletes a filter result that is
    relative (see `html_sanitize`'s module docstring), and the frame's CSP
    only permits images from `'self'` anyway.

    `sign_image` closes over *this* user's id, so the token it mints is not
    spendable by any other account. It is passed even when `remote` is
    false: `html_sanitize` requires both before it will rewrite anything, so
    the decision lives in exactly one place rather than two.

    `sign_cid` closes over this user's id *and this message's id*, so the
    capability it mints reads one part of one message for one reader and
    nothing else. It is unconditional — an inline image is not gated on the
    remote-image decision, and a frame with images off still renders the
    sender's own logo — which is also why `_frame_partial`'s counting pass,
    which sanitises with `remote=False` purely to count what it blocked,
    mints these too. The tokens it mints are thrown away with the result.

    Both closures take `message.id`, the same value that becomes
    `SanitizeContext.email_id` and therefore the `{email_id}` in the URL the
    sanitiser writes. Signing one id and serving another would 404 every
    inline image in the app; there is one id here so that cannot happen.
    """
    settings = _settings(request)
    origin = str(request.base_url).rstrip("/")

    def sign_image(url: str) -> str:
        return sign_remote_url(url, secret_key=settings.secret_key, user_id=user.id)

    def sign_cid(content_id: str) -> str:
        return sign_cid_url(message.id, content_id, secret_key=settings.secret_key, user_id=user.id)

    return SanitizeContext(
        email_id=message.id,
        origin=origin,
        remote=remote,
        cid_parts=_cid_parts(message),
        sign_image=sign_image,
        sign_cid=sign_cid,
    )


def _sender_of(message: EmailBody) -> str | None:
    """The address in `message`'s `From`, normalised, or `None`.

    Lower-cased and stripped on both the read and the write side
    (`message_restyle` below), so one row answers for a sender whose
    envelope spells itself `News@T.test` today and `news@t.test` tomorrow.
    Local-parts are case-sensitive in the RFC and case-insensitive in every
    deployment anyone has met; for a per-sender display preference, folding
    is what a reader means.
    """
    for address in message.from_:
        if address.email:
            return address.email.strip().lower()
    return None


async def _restyle_suppressed(db: AsyncSession, user_id: int, message: EmailBody) -> bool:
    """True when this reader has clicked "Show original" for this sender.

    A missing row is not a suppression, and neither is a row whose
    `dark_restyle` is `None` or `True` — only an explicit `False` is the
    override, which is what keeps a future "always restyle this sender"
    value from reading as its opposite here.
    """
    sender = _sender_of(message)
    if sender is None:
        return False
    pref = await repo.sender_pref(db, user_id, sender)
    return pref is not None and pref.dark_restyle is False


async def _restyle_for(
    db: AsyncSession,
    user: AppUser,
    prefs: UiPref,
    message: EmailBody,
    *,
    raw_html: str,
    mail_css: str,
    theme: str,
    restyle: bool,
) -> str:
    """`"none"` / `"invert"` / `"color-scheme"` for this message and reader.

    Three independent switches fold into `restyle_mode`'s one `enabled`
    flag, and any of them off is off: the reader's Appearance setting
    (`prefs.dark_restyle`), the `?restyle=0` this request carries, and the
    per-sender "Show original" override. `restyle_mode` itself is still the
    only thing that decides *which* mode applies.

    `declares_color_scheme` is asked of the **raw** body and the **raw**
    `<style>` blocks, never the sanitised ones: `<meta>` is in nh3's
    `CLEAN_CONTENT_TAGS` and `color-scheme` is not in
    `css_sanitize.ALLOWED_PROPERTIES`, so both forms of evidence are gone
    by the time the sanitiser has run, and a mail that already knows how to
    be dark would be inverted into a mess. `background_is_light` is asked
    of the sanitised CSS instead, because a `background-color` *does*
    survive it — the colour it reads is the colour the reader will see.

    The two body scans are skipped when the answer cannot matter.
    `restyle_mode`'s stated contract is that a light theme never restyles
    and a disabled reader never restyles, whatever `declares`/`light` say,
    so this is a cost decision only: `theme` and `enabled` reach it
    unchanged either way, and it remains the single decision point.
    """
    enabled = bool(prefs.dark_restyle) and restyle
    if enabled:
        enabled = not await _restyle_suppressed(db, user.id, message)

    declares = light = False
    if enabled and theme != "light":
        declares = declares_color_scheme(raw_html, "\n".join(extract_styles(raw_html)))
        light = background_is_light(raw_html, mail_css)

    return restyle_mode(theme=theme, enabled=enabled, declares=declares, light=light)


def _frame_src(email_id: str, *, remote: bool, restyle: bool) -> str:
    """The `src` of the `<iframe>` that embeds one message.

    Built here rather than in each caller so that `GET /m/{id}/frame` and
    `POST /m/{id}/restyle` — the two routes that render `thread/frame.html`
    — cannot spell the URL differently. `restyle=0` is appended only when
    it means something; a reader with no override gets the same bare
    `?remote=…` this route has always produced.
    """
    query = f"remote={1 if remote else 0}"
    return f"/m/{email_id}/html?{query}" + ("" if restyle else "&restyle=0")


def _fanout_slot(request: Request, user_id: int) -> asyncio.Semaphore:
    """This user's outbound-fetch semaphore, created on first use.

    Both lookups below run without an `await` between them, so two requests
    on the same event loop cannot each create a semaphore and have one of
    them silently discarded — which would be a cap that quietly doubles
    under exactly the concurrency it exists to bound.
    """
    registry: dict[int, asyncio.Semaphore] | None = getattr(request.app.state, _FANOUT_ATTR, None)
    if registry is None:
        registry = {}
        setattr(request.app.state, _FANOUT_ATTR, registry)
    slot = registry.get(user_id)
    if slot is None:
        slot = asyncio.Semaphore(FANOUT_LIMIT)
        registry[user_id] = slot
    return slot


@router.get("/m/{email_id}/html", response_class=HTMLResponse)
async def message_html(
    request: Request,
    email_id: str,
    user: UserDep,
    client: ClientDep,
    db: DbDep,
    prefs: PrefsDep,
    remote: Annotated[bool, Query()] = False,
    theme: Annotated[str | None, Query()] = None,
    restyle: Annotated[bool, Query()] = True,
    expand: Annotated[bool, Query()] = False,
) -> Response:
    """The sandboxed document for one message body.

    404 when the message has no HTML part at all: the conversation view
    renders a text/plain body inline, escaped and linkified
    (`mailosh.render.plain_text`), and never asks for a frame — so a request
    for one is a stale URL, not a message to render empty.

    `theme` absent means "the reader's own", read from their `UiPref` row;
    passing it overrides, which is how a print view (Task 13) renders a
    message at a theme of its choosing without changing anyone's setting.

    `restyle` defaults to **on**, because the conversation view frames a
    message with a bare `/m/{id}/html` and a dark reader should see a dark
    message without every template having to remember to ask. `?restyle=0`
    is the opt-out the "Show original" swap uses; the per-sender memory
    behind it is honoured here too, so a reader who has clicked it once
    does not need the parameter on every later message from that sender.

    `expand` is the print page's (Task 13): the quote is rendered open,
    with no toggle.
    """
    message = await _load_message(client, email_id)
    if message is None or not message.html_body:
        raise HTTPException(status_code=404, detail="no HTML body")

    raw_html = message.html_body
    try:
        result = sanitize_email_html(
            raw_html, _sanitize_context(request, message, user, remote=remote)
        )
    except BodyTooDeep as exc:
        # 422, not 500: the message is well-formed HTTP and the server is
        # fine -- this body is simply nested past what can be sanitised
        # without stalling the worker for everybody (see MAX_NESTING_DEPTH).
        # The reader gets a frame that says so; "Show original" still
        # reaches /source, which does not parse anything.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    visible, quoted = quote_trim.split_html(result.html)
    resolved_theme = theme if theme is not None else prefs.theme
    document = render_frame(
        visible_html=visible,
        quoted_html=quoted,
        mail_css=result.css,
        theme=resolved_theme,
        restyle=await _restyle_for(
            db,
            user,
            prefs,
            message,
            raw_html=raw_html,
            mail_css=result.css,
            theme=resolved_theme,
            restyle=restyle,
        ),
        expand=expand,
    )
    return HTMLResponse(
        document, headers={"Content-Security-Policy": csp_header(), **_FRAME_DOC_HEADERS}
    )


async def _frame_partial(
    request: Request,
    *,
    email_id: str,
    thread_id: str,
    message: EmailBody | None,
    user: AppUser,
    db: AsyncSession,
    policy: str,
    override: bool | None,
) -> Response:
    """`thread/frame.html` for one message: the blocked-images banner and
    the `<iframe>` under it, from **one** sanitise.

    The three routes that render a frame partial all come through here, so
    a swap lands on markup identical to what first rendered — and, more to
    the point, so the banner's count has one producer. It is a property of
    the sanitising pass (`SanitizeResult.blocked_remote`/`remote_hosts`)
    and of nothing else; a second count taken anywhere would be a second
    opinion about what the reader is being protected from.

    The sanitise is skipped outright when the decision is "show". Not an
    optimisation dressed up as a rule: with remote images permitted nothing
    is blocked, so the count is zero by construction and the banner is
    absent for the honest reason rather than by a template's judgement.

    A message this account does not have still renders a frame — the `src`
    it points at is what 404s. Answering differently here would turn this
    route into a way to ask whether a message id exists.
    """
    sender = _sender_of(message) if message is not None else None
    decision = await decide(
        db, user_id=user.id, policy=policy, sender_email=sender, override=override
    )

    blocked_remote = 0
    remote_hosts: tuple[str, ...] = ()
    if message is not None and message.html_body and not decision.show:
        try:
            result = sanitize_email_html(
                message.html_body, _sanitize_context(request, message, user, remote=False)
            )
        except BodyTooDeep:
            # The body will not render either, so there is nothing to count
            # and no banner to draw. Falling through with zeroes keeps the
            # fragment itself renderable -- the placeholder inside the frame
            # is what tells the reader.
            pass
        else:
            blocked_remote, remote_hosts = result.blocked_remote, result.remote_hosts

    restyle = message is None or not await _restyle_suppressed(db, user.id, message)
    return _templates(request).TemplateResponse(
        request,
        "thread/frame.html",
        {
            "email_id": email_id,
            "thread_id": thread_id,
            "frame_src": _frame_src(email_id, remote=decision.show, restyle=restyle),
            "blocked_remote": blocked_remote,
            "remote_hosts": remote_hosts,
            "sender": sender,
        },
    )


@router.get("/m/{email_id}/frame", response_class=HTMLResponse)
async def message_frame(
    request: Request,
    email_id: str,
    user: UserDep,
    client: ClientDep,
    db: DbDep,
    prefs: PrefsDep,
    thread: Annotated[str, Query()],
    remote: Annotated[bool | None, Query()] = None,
) -> Response:
    """One message's banner and frame, as an htmx-swappable partial.

    It exists as its own route rather than only as an include because the
    banner cannot be rendered anywhere else: its count comes out of the
    sanitiser, and the conversation view has no sanitised body to count.
    `thread/message.html` therefore loads this fragment, "Show images" is a
    plain `hx-get` back to it with `remote=1`, and there is one place that
    knows how to spell the sandbox attribute.

    `remote` is the reader's override **for this message** and is
    three-valued: absent means "no answer yet, ask the policy". `1` and `0`
    are the two directions the banner's own controls move in, and both beat
    the stored policy — `image_policy.decide` documents why.

    The message is loaded for two reasons beyond the count: to learn who
    sent it, so a reader who has already allow-listed that sender (or
    chosen "Original colours" for them) gets the right frame first time
    rather than one that has to be swapped again.
    """
    message = await _load_message(client, email_id)
    return await _frame_partial(
        request,
        email_id=email_id,
        thread_id=thread,
        message=message,
        user=user,
        db=db,
        policy=prefs.remote_images,
        override=remote,
    )


@router.post(
    "/m/{email_id}/images/allow",
    response_class=HTMLResponse,
    dependencies=[Depends(deps.csrf_protect)],
)
async def message_images_allow(
    request: Request,
    email_id: str,
    user: UserDep,
    client: ClientDep,
    db: DbDep,
    prefs: PrefsDep,
    sender: Annotated[str, Form()],
) -> Response:
    """Add this message's sender to the always-show list, and re-render the
    frame with the images in it.

    The same shape, and the same reasoning, as `POST /m/{id}/restyle`
    below: `sender` is posted so the reader's own page says which sender
    the click was about, is then checked against the message's `From` and
    refused with `403` if it does not match, and the row is written under
    the address the *server* read. Without that check one message would be
    a write primitive for every address in this reader's mail — and this
    one grants remote loads, so the row it writes is a privacy decision
    rather than a display one.

    No `remote` override is passed to the re-render: the row just written
    is what `decide` will find, so the frame comes back showing images
    because the reader's stored judgement now says so, not because this
    response asserted it.
    """
    message = await _load_message(client, email_id)
    if message is None:
        raise HTTPException(status_code=404, detail="no such message")

    actual = _sender_of(message)
    if actual is None or actual != sender.strip().lower():
        raise HTTPException(status_code=403, detail="sender is not this message's own")

    await allow_sender(db, user_id=user.id, sender_email=actual)
    return await _frame_partial(
        request,
        email_id=email_id,
        thread_id=message.thread_id,
        message=message,
        user=user,
        db=db,
        policy=prefs.remote_images,
        override=None,
    )


@router.get("/img")
async def image_proxy(request: Request, u: Annotated[str, Query()]) -> Response:
    """Fetch one signed remote image server-side and hand back the bytes.

    Three outcomes and nothing else: `200` with an allow-listed image type,
    `403` for a token this app did not mint, `502` for a URL the guard
    refused or an upstream that failed. No redirect, no error page, no
    reason — see the module docstring on why both failure bodies are empty
    and why the two exception types are caught separately.

    **No session dependency, deliberately.** This URL is fetched by an
    `<img>` inside an opaque-origin document, which carries no cookie; a
    `UserDep` here is not a second check, it is a `303 -> /login` that no
    reader can do anything about (see the module docstring). The token is
    the authority, and it carries the reader it was minted for — which is
    the id the fan-out semaphore is keyed on, so one reader's newsletter
    still cannot spend another reader's socket budget.

    The token still names exactly one URL, so dropping the cookie widens
    nothing about *where* this route will fetch from; `fetch_guard` runs
    against that URL, and every redirect hop after it, exactly as before.
    """
    try:
        url, reader_id = verify_image_token(u, secret_key=_settings(request).secret_key)
    except ValueError:
        return Response(status_code=403)

    # Held across the fetch only: the semaphore bounds sockets, not the time
    # spent writing a response that is already in memory.
    async with _fanout_slot(request, reader_id):
        try:
            content_type, body = await fetch_image(url)
        except BlockedUrl:
            return Response(status_code=502)

    return Response(body, media_type=content_type, headers=dict(_IMAGE_HEADERS))


#: Every refusal `/m/{id}/cid/{cid}` can make says exactly this, whatever
#: went wrong. See the module docstring: two distinguishable refusals would
#: together answer "does this message carry this Content-ID?" for anyone
#: willing to ask twice, and this route is reachable with no session.
_NO_INLINE_PART = "no such inline part"


@router.get("/m/{email_id}/cid/{content_id}")
async def message_cid_part(
    request: Request,
    email_id: str,
    content_id: str,
    db: DbDep,
    u: Annotated[str, Query()] = "",
) -> Response:
    """One inline part, for the `<img src="cid:…">` the sanitiser rewrote.

    **Authorised by `?u=`, not by the session cookie**, because the `<img>`
    that fetches this lives in an opaque-origin document and sends no cookie
    (module docstring). The token names the reader, the message and the
    Content-ID; all three are checked, and the two ids are checked against
    the ones in *this* path, so a token minted for one part of one message
    cannot be re-pointed at another part, another message, or another
    account's mail.

    Six ways to a `404`, and only one way to a `200`: the token must verify
    for exactly this message and Content-ID, its reader must still have a
    live session to act as, the message must be one that account holds, the
    Content-ID must be one *that message* carries, the part's declared type
    must be in `INLINE_IMAGE_TYPES`, and its declared size must be under
    `MAX_INLINE_BYTES`. Every one of the six answers the same `404` with the
    same body. Refusing rather than truncating an oversized part is
    deliberate (see the constant); refusing rather than sniffing an
    undeclared one is what keeps `image/svg+xml` out.

    The token is checked *first*, before the mailbox is touched at all, so
    an unauthorised request costs one HMAC and no JMAP round trip — and so
    that no branch below can leak through a timing difference what the
    status code refuses to say.

    The stream is capped anyway, at the same ceiling the declared size was
    checked against — `size` is written by the sender, and a part that
    claims three bytes must not be able to serve six megabytes just because
    it lied about it.
    """
    try:
        reader_id = verify_cid_token(
            u,
            secret_key=_settings(request).secret_key,
            email_id=email_id,
            content_id=content_id,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail=_NO_INLINE_PART) from None

    client = await _client_for_reader(request, db, reader_id)
    if client is None:
        raise HTTPException(status_code=404, detail=_NO_INLINE_PART)

    message = await _load_message(client, email_id)
    if message is None:
        raise HTTPException(status_code=404, detail=_NO_INLINE_PART)

    part = _cid_part(message, content_id)
    if part is None or part.blob_id is None:
        raise HTTPException(status_code=404, detail=_NO_INLINE_PART)

    mime = _mime_of(part)
    if mime not in INLINE_IMAGE_TYPES or part.size > MAX_INLINE_BYTES:
        raise HTTPException(status_code=404, detail=_NO_INLINE_PART)

    return StreamingResponse(
        _blob_chunks(
            client,
            part.blob_id,
            mime_type=mime,
            name=part.name or "inline",
            limit=MAX_INLINE_BYTES,
        ),
        media_type=mime,
        headers={"Content-Disposition": "inline", **_BLOB_HEADERS},
    )


@router.get("/m/{email_id}/att/{blob_id}")
async def message_attachment(
    email_id: str,
    blob_id: str,
    user: UserDep,
    client: ClientDep,
    inline: Annotated[bool, Query()] = False,
) -> Response:
    """One attachment, downloaded by default or previewed on request.

    `inline=1` is honoured only for `PREVIEW_TYPES`; every other type — a
    `.html` attachment above all — falls back to
    `application/octet-stream` plus `Content-Disposition: attachment`, so
    the flag can never turn a document a stranger sent into a page on this
    app's own origin. The fallback is silent on purpose: there is nothing
    for the reader to do about it, and the chip that offers "Open" only
    offers it for a type this list already contains.

    The blob id is resolved inside this message's own `attachments`, which
    is what makes it a lookup rather than a fetch — including for
    `message.blob_id`, the raw source, which is not an attachment and so is
    not reachable here at all.
    """
    message = await _load_message(client, email_id)
    if message is None:
        raise HTTPException(status_code=404, detail="no such message")

    part = _attachment_part(message, blob_id)
    if part is None:
        raise HTTPException(status_code=404, detail="no such attachment")

    mime = _mime_of(part)
    previewable = inline and mime in PREVIEW_TYPES
    if not previewable:
        media_type = _DOWNLOAD_TYPE
    elif mime == "text/plain":
        # The one preview type whose bytes need an encoding named, or a
        # browser falls back to a locale guess and mangles every accent.
        media_type = f"{mime}; charset=utf-8"
    else:
        media_type = mime

    return StreamingResponse(
        _blob_chunks(client, blob_id, mime_type=mime, name=part.name or "attachment"),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(part.name, inline=previewable),
            **_BLOB_HEADERS,
        },
    )


@router.get("/m/{email_id}/source")
async def message_source(email_id: str, user: UserDep, client: ClientDep) -> Response:
    """The raw RFC 5322 message, as `text/plain`, capped.

    Two things in the conversation view link here: the ⋮ menu's "Show
    original", and the notice under a body Stalwart truncated. The second
    is the reason the cap truncates instead of refusing — a reader told
    "this message was too large to show in full" must not then be told the
    same thing by the link offering to show it.

    `text/plain` plus `nosniff` is the whole safety story, and it is not
    incidental: the bytes being served *are* the sender's HTML, and this
    app's own origin is precisely where they must not render.
    """
    message = await _load_message(client, email_id)
    if message is None or not message.blob_id:
        raise HTTPException(status_code=404, detail="no raw source for this message")

    return StreamingResponse(
        _blob_chunks(
            client,
            message.blob_id,
            mime_type="text/plain",
            name=f"{email_id}.eml",
            limit=MAX_SOURCE_BYTES,
        ),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "inline", **_BLOB_HEADERS},
    )


@router.post(
    "/m/{email_id}/restyle",
    response_class=HTMLResponse,
    dependencies=[Depends(deps.csrf_protect)],
)
async def message_restyle(
    request: Request,
    email_id: str,
    user: UserDep,
    client: ClientDep,
    db: DbDep,
    prefs: PrefsDep,
    sender: Annotated[str, Form()],
    remote: Annotated[bool | None, Form()] = None,
) -> Response:
    """Stop restyling this sender ("Original colours"), and re-render the
    frame.

    `sender` is posted rather than derived so the reader's own page says
    which sender the click was about — and is then checked against the
    message's `From` and rejected with `403` if it does not match. Without
    that check one message would be a write primitive for every address in
    this reader's mail: the field is a form value, and what it writes is a
    durable per-sender row.

    The row is written under the *message's* address, not the posted one,
    even though they have just been proved equal — the authoritative copy
    is the one the server read.

    The thread comes from the message too, for the same reason: nothing the
    client posts decides what this response says it belongs to.

    The row this writes is what `_frame_partial`'s own `_restyle_suppressed`
    then reads, so the frame comes back asking for `restyle=0` because the
    stored override says so rather than because this response asserted it —
    the same shape as `images/allow` above. `remote` stays three-valued for
    the same reason it is on the frame route: the strip posts no such field,
    and "no answer yet" must not arrive at `decide` as "hide them".
    """
    message = await _load_message(client, email_id)
    if message is None:
        raise HTTPException(status_code=404, detail="no such message")

    actual = _sender_of(message)
    if actual is None or actual != sender.strip().lower():
        raise HTTPException(status_code=403, detail="sender is not this message's own")

    await repo.set_sender_restyle(db, user.id, actual, False)
    return await _frame_partial(
        request,
        email_id=email_id,
        thread_id=message.thread_id,
        message=message,
        user=user,
        db=db,
        policy=prefs.remote_images,
        override=remote,
    )
