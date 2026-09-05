"""Images in a message: whether one may load, and under whose URL.

Two halves, and both are here. **The decision half** — `ImageDecision`,
`decide` and `allow_sender` — answers "may this message's images load at all",
from the reader's policy, their per-sender allow list and their harvested
contacts. **The signing half** — `sign_remote_url`/`sign_cid_url` and the
`verify_*` functions beside them — mints and checks the one-reader,
one-resource, one-hour tokens that act on a yes. They live in one module
because a decision that says yes with no way to mint the URL, or a URL
mintable without the decision, is the same bug twice.

**Both kinds of image URL are capability URLs, and that is not an
optimisation — it is the only way they load at all.** The document that
carries them (`mailosh.render.frame_document`) is served under a CSP whose
`sandbox` directive has no `allow-same-origin`, so it has an *opaque origin*.
A subresource request from an opaque-origin document is not same-site for
cookie purposes, so no browser attaches the `SameSite=Lax` session cookie to
it: an `<img>` inside the frame arrives at this app with no session at all
and, before these tokens, was answered `303 -> /login` and rendered as a
broken image. The frame is deliberately a document with *no ambient
authority*; the answer is to give its subresources explicit, per-URL,
per-reader capabilities rather than to widen the cookie. (`httpx` attaches
cookies whatever `SameSite` says and has no notion of a document origin,
which is why every route test passed while every real browser failed.)

Why remote images are signed at all. A remote image in an email is a tracking
pixel until proven otherwise: fetched directly by the reader's browser it
leaks the reader's IP, their user agent, the fact that they opened the mail and
the moment they did, to whoever sent it. Mailosh therefore never lets the
browser touch the sender's host — every remote `src` is rewritten to
`/img?u=<token>` on the app's own origin, and the server fetches it behind
`mailosh.render.fetch_guard`.

That rewrite makes the app an open proxy unless the URL is unforgeable, so the
token is signed (`mailosh.security.signing`, purpose `"img"`) and carries:

- **the URL**, so nobody can point the proxy at a target of their choosing —
  without the signature, `/img?u=http://169.254.169.254/` is an SSRF handed
  out with a query string, and `fetch_guard` would be the *only* thing left
  between a stranger and the operator's network rather than the second thing;
- **the user id**, so the proxy's fetches stay attributable and a token is
  scoped to the reader it was minted for rather than being a global one. Note
  what this is *not*: `GET /img` has no session to compare it against (see
  above), so the id is read back out of the token rather than checked against
  a caller, and the token is a **bearer** capability — anyone holding it can
  spend it for the hour it lives. It is what keys the route's per-reader
  fan-out budget, and what stops one leaked URL from being every reader's;
- **an expiry**, an hour, which is long enough to read a message and to
  re-open it, and short enough that a URL copied out of a browser history or a
  server log is dead by the time anybody tries it.

**The inline (`cid:`) token carries the same three things, one of them twice.**
`/m/{id}/cid/{cid}` serves a part of a message out of the reader's own
mailbox, so a token for it must name **the reader**, **the message** and
**the content id** — all three, because any one left out is a different bug:
without the reader it is transferable, without the message it reads any
message's part with that content id, and without the content id it reads any
part of that message. It is minted under its own purpose (`"cid"`), so an
image-proxy token and an inline-part token can never be spent as each other
however alike their payloads look.

Every verifier here raises `ValueError` — never anything more specific — for a
bad signature, an expired token, a payload of the wrong shape and a resource
that is not the one asked for alike. The routes answer one status for every one
of them (`403` for the proxy, `404` for an inline part) without asking which,
so a rejection is never an oracle.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db.models import Contact, ImageSenderAllow
from mailosh.security.signing import sign_payload, verify_payload

__all__ = [
    "CID_URL_TTL",
    "IMAGE_URL_TTL",
    "ImageDecision",
    "allow_sender",
    "decide",
    "sign_cid_url",
    "sign_remote_url",
    "verify_cid_token",
    "verify_image_token",
]

#: An hour. See the module docstring.
IMAGE_URL_TTL: int = 3600

#: The same hour, for the same two reasons: long enough to read a message and
#: re-open it, short enough that a URL left in a server log is dead before
#: anybody tries it. Stated as its own constant rather than aliased to
#: `IMAGE_URL_TTL` because they are two decisions that happen to agree — one
#: about a URL on a stranger's host, one about a part of the reader's own
#: mail — and a later change to either must be made deliberately.
CID_URL_TTL: int = 3600

#: HKDF `info` for the image-token signing key. MUST differ from every other
#: purpose derived from the same `Settings.secret_key` — notably
#: `mailosh.services.undo`'s `b"undo"` and `mailosh.security.crypto`'s
#: `b"sessions"` — so that a token minted for one can never be spent as
#: another.
_PURPOSE = "img"

#: HKDF `info` for the inline-part token, and the reason the two token kinds
#: cannot be confused: `mailosh.security.signing` derives its MAC key from
#: this string, so a `"cid"` token and an `"img"` token are signed under
#: unrelated keys and neither verifies as the other. That matters more than
#: it looks — both payloads carry `"s"`, and only the key separation stops a
#: proxy token from being spent as a mailbox read.
_CID_PURPOSE = "cid"

#: Payload keys, one character each: URL, subject (the user id), message id,
#: content id. The expiry (`"e"`) is `mailosh.security.signing`'s own, and
#: reserved there.
_URL, _SUBJECT, _MESSAGE, _CONTENT_ID = "u", "s", "m", "c"


def sign_remote_url(url: str, *, secret_key: str, user_id: int, now: float | None = None) -> str:
    """A token authorising *this user* to have *this URL* fetched by the image
    proxy, for `IMAGE_URL_TTL` seconds.

    Deliberately no opinion here on whether `url` is fetchable: signing says
    "the app produced this", and `mailosh.render.fetch_guard` — which runs
    against the URL again at fetch time, and against every redirect hop after
    it — says whether it may be fetched. Putting a second, weaker copy of that
    judgement here would invite someone to trust the token instead of the
    guard.
    """
    payload = {_URL: url, _SUBJECT: user_id}
    return sign_payload(
        payload, secret_key=secret_key, purpose=_PURPOSE, ttl=IMAGE_URL_TTL, now=now
    )


def verify_image_token(token: str, *, secret_key: str, now: float | None = None) -> tuple[str, int]:
    """`(url, user id)` for a live image-proxy token, or `ValueError`.

    The form `GET /img` uses, and the reason it can: the request arrives from
    an opaque-origin document with **no session cookie**, so there is no
    reader to compare the token against — the token *is* the reader. That is
    a real change in what the token is worth (before, spending one also
    needed the minting reader's session; now it is a bearer capability), and
    the three properties that make it an acceptable one are the three the
    module docstring lists: it is unforgeable, it names exactly one URL, and
    it dies in an hour.

    The type checks are not ceremony. `type(...) is int` rather than
    `isinstance`, because `True` *is* an `int` in Python and `True != 1` is
    `False` — a payload carrying `{"s": true}` would otherwise come back as
    user 1. Nothing but this app can produce such a payload, which is exactly
    why the check belongs here: it stays true if some later caller signs a
    payload built from data it did not fully control.
    """
    payload = verify_payload(token, secret_key=secret_key, purpose=_PURPOSE, now=now)
    url = payload.get(_URL)
    subject = payload.get(_SUBJECT)
    if type(url) is not str or type(subject) is not int:
        raise ValueError("invalid signed token")
    return url, subject


def sign_cid_url(
    email_id: str,
    content_id: str,
    *,
    secret_key: str,
    user_id: int,
    now: float | None = None,
) -> str:
    """A token authorising *this reader* to read *this part* of *this
    message*, for `CID_URL_TTL` seconds.

    All three are in the payload and all three are checked back, because the
    URL they end up in — `/m/{id}/cid/{cid}?u=…` — is guessable in both of its
    path segments and is fetched with no session cookie behind it. A token
    bound to fewer of them is a different vulnerability each time: bound to
    the reader alone it reads any message they hold, bound to the message
    alone it is transferable to any account.

    The token authorises the *fetch*; it is not a substitute for the route's
    own check that the message is one this account holds and the content id
    one that message carries. `mailosh.web.frames` does both, in that order.
    """
    payload = {_MESSAGE: email_id, _CONTENT_ID: content_id, _SUBJECT: user_id}
    return sign_payload(
        payload, secret_key=secret_key, purpose=_CID_PURPOSE, ttl=CID_URL_TTL, now=now
    )


def verify_cid_token(
    token: str, *, secret_key: str, email_id: str, content_id: str, now: float | None = None
) -> int:
    """The reader `token` was minted for, having checked it names exactly
    `email_id` and `content_id` — or `ValueError`.

    The resource comparison is the whole point and it is an equality, not a
    prefix or a normalisation: the values signed in are the values the
    sanitiser wrote into the URL, and the values compared against are the
    path segments the router decoded back out of it. Anything that does not
    match — a swapped message id, a swapped content id, a token from another
    message that happens to name the same part — is refused before the
    mailbox is touched at all.

    Returns the user id rather than taking one, for the same reason as
    `verify_image_token`: the request carries no session cookie, so the token
    is the only statement of who is asking.
    """
    payload = verify_payload(token, secret_key=secret_key, purpose=_CID_PURPOSE, now=now)
    message = payload.get(_MESSAGE)
    content = payload.get(_CONTENT_ID)
    subject = payload.get(_SUBJECT)
    if type(message) is not str or type(content) is not str or type(subject) is not int:
        raise ValueError("invalid signed token")
    if message != email_id or content != content_id:
        raise ValueError("invalid signed token")
    return subject


# ---------------------------------------------------------------------------
# The decision half
# ---------------------------------------------------------------------------

#: The three values `UiPref.remote_images` may hold (design spec §10). Anything
#: else — a stale row, a hand-edited database, a later policy this build does
#: not know — falls through `decide` to `"blocked"`, because the failure mode
#: of an unrecognised policy has to be "the reader is not tracked", never "the
#: reader is tracked by default".
POLICIES: frozenset[str] = frozenset({"ask", "always", "contacts"})


@dataclass(frozen=True, slots=True)
class ImageDecision:
    """Whether this message's remote images may load, and which rule said so.

    `reason` is not decoration. Two different rules can both answer "show" —
    the reader's global `always`, and a single allow-listed sender — and only
    one of them is something the banner should offer to undo. It is also the
    only way a support question ("why did this newsletter load images?") has
    a factual answer rather than a re-derivation.

    Its values, one per branch of `decide`:

    - `"override"` — the reader clicked "Show images" (or navigated back to
      `remote=0`) *on this message*. Both directions share the reason; the
      branch that fired is the override either way.
    - `"policy_always"` — `remote_images = "always"`.
    - `"sender_allowed"` — an `ImageSenderAllow` row for this sender.
    - `"contact"` — `remote_images = "contacts"` and a harvested `Contact`
      row for this sender.
    - `"blocked"` — nothing said yes. The default.
    """

    show: bool
    reason: str


def _normalise(sender_email: str | None) -> str | None:
    """A sender address as this module keys rows on it, or `None` for one that
    cannot be keyed on at all.

    Case-folded, because `News@Example.test` and `news@example.test` are one
    mailbox and a reader who allow-listed the sender once must not be asked
    again when the same sender capitalises differently. Whitespace-stripped
    for the same reason. An address that is empty after both is `None` rather
    than `""`: an allow-list row keyed on the empty string would be matched by
    every message with no `From` at all, which is precisely the mail least
    worth trusting.
    """
    if sender_email is None:
        return None
    return sender_email.strip().lower() or None


async def decide(
    db: AsyncSession,
    *,
    user_id: int,
    policy: str,
    sender_email: str | None,
    override: bool | int | None,
) -> ImageDecision:
    """Whether `user_id`'s remote images may load for a message from
    `sender_email`.

    Precedence, highest first — and the order is the whole design:

    1. `override`, the reader's decision about *this message* (`1` show, `0`
       hide, `None` defer). It beats every stored preference in both
       directions: a reader who clicked "Show images" gets them even under
       `ask`, and a reader who backed out gets them hidden even under
       `always`. A per-message choice the stored policy could overrule is not
       a choice.
    2. `policy == "always"`.
    3. An `ImageSenderAllow` row — checked *before* the policy branch below,
       so "Always show from this sender" keeps working if the reader later
       moves from `contacts` to `ask`. The row is the reader's explicit
       judgement about one correspondent; a policy is their default for
       strangers.
    4. `policy == "contacts"` and a `Contact` row: someone this reader has
       written to has already been handed their address, so a tracking pixel
       tells them nothing they could not have learned from the reply.
    5. Blocked.

    Reads only — a decision never writes. `allow_sender` is the one writer,
    and it is called from the route the reader clicked, not from a render.
    """
    if override is not None:
        return ImageDecision(show=bool(override), reason="override")
    if policy == "always":
        return ImageDecision(show=True, reason="policy_always")

    address = _normalise(sender_email)
    if address is not None:
        # `allow_sender` is the only writer of this table and lower-cases
        # what it stores, so the primary key comparison is exact on purpose:
        # this is the lookup that runs on every framed message, and it should
        # be an index hit rather than a scan under `lower()`.
        allowed = await db.scalar(
            select(ImageSenderAllow.sender_email).where(
                ImageSenderAllow.user_id == user_id,
                ImageSenderAllow.sender_email == address,
            )
        )
        if allowed is not None:
            return ImageDecision(show=True, reason="sender_allowed")

        if policy == "contacts":
            # `contact` is harvested by the compose/send path, not by this
            # module, so its rows carry whatever case the header had. Folding
            # in SQL is what keeps "you have written to Priya" true no matter
            # how she spells her own address.
            known = await db.scalar(
                select(Contact.email).where(
                    Contact.user_id == user_id,
                    func.lower(Contact.email) == address,
                )
            )
            if known is not None:
                return ImageDecision(show=True, reason="contact")

    return ImageDecision(show=False, reason="blocked")


async def allow_sender(db: AsyncSession, *, user_id: int, sender_email: str) -> ImageSenderAllow:
    """Add `sender_email` to `user_id`'s always-show list, idempotently.

    `session.merge` rather than an `INSERT ... ON CONFLICT DO NOTHING`:
    `merge` is dialect-neutral, so the aiosqlite unit fixture and production
    Postgres take the same path, and a second click on "Always show from"
    updates the row it finds instead of raising a duplicate-key error the
    caller would have to know to swallow.

    The address is stored lower-cased — the invariant `decide`'s exact-match
    lookup rests on. An address that normalises away to nothing is a caller
    bug, not a row: the route only reaches here after matching the form's
    `sender` against the message's own `From`, so an empty one means that
    check let something through.
    """
    address = _normalise(sender_email)
    if address is None:
        raise ValueError("an allow-list entry needs a sender address")
    row = await db.merge(ImageSenderAllow(user_id=user_id, sender_email=address))
    await db.commit()
    return row
