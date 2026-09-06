"""FastAPI app factory for the Mailosh webmail UI.

``create_app`` wires up: the design-system Jinja environment
(``mailosh.ui.env.build_env``) and ``/static`` mount, security-header
middleware, the ``SessionRequired`` -> login-redirect exception handler
(``mailosh.web.deps``), the auth router (``mailosh.web.auth``: login,
logout, logout/all), the live-updates router (``mailosh.web.events``:
``GET /events``), the mail-action router (``mailosh.web.actions``:
``POST /a/*``) and the list-first mail shell (``mailosh.web.mail``: ``/``,
``/mail/{key}``, ``/mail/{key}/rows``, ``/t/{thread_id}``). The
lifespan builds every piece of shared runtime state later routes depend on
via ``app.state``: ``settings``, ``sessionmaker`` (SQLAlchemy async engine +
session factory), ``pool`` (``mailosh.jmap.pool.ClientPool``, one
``JmapClient`` per session), ``hubs`` (``mailosh.sse.HubRegistry``, one
SSE hub + upstream Stalwart listener per *user*, plus — only when
``MAILOSH_SSE_FANOUT=postgres`` — the ``LISTEN``/``NOTIFY`` bus that relays
those events between worker processes, ``mailosh.db.notify``), ``admin``
(``StalwartAdmin``), and ``templates``. A single background task, restarted
every ~5 minutes for the app's whole lifetime, does three jobs: evict idle
pooled JMAP clients (design spec §9), stop idle per-user SSE listeners
(design spec §6.5), and sweep expired sessions / stale rate-limit rows
(``mailosh.web.auth.reap_expired_sessions`` — controller ruling #2).

Task 5 (controller ruling #3) deletes every Phase 0 demo route this module
used to serve directly against a single shared demo account (``/inbox``,
``/inbox/rows``, ``/thread/{id}``, ``/compose``, ``/email/*``) along with
``mailosh.web.deps.get_client`` — a real multi-user app authenticates
first. ``html_to_text`` (below) is what controller ruling #3 explicitly
keeps of that old code: a pure function with no route/client dependency of
its own, still covered by its own unit tests, that a later task (compose)
reuses. ``split_quoted`` was the other one, and Task 8 moved it to
``mailosh.render.plain_text`` alongside the rest of the plain-text
rendering — that package is where "what does this body mean" now lives, and
the move ended ``mailosh.web.mail``'s deferred import back into this
module.

Task 13 registers the two routers that were deliberately left unmounted
while they landed (``mailosh.web.palette``: ``GET /palette/index``,
``mailosh.web.prefs``: ``POST /prefs`` — see each module's own docstring
for why "not registered here yet" was the point) and adds the app's global
error surface: a ``JmapError``/``TransportError`` handler pair (a JMAP- or
transport-level failure never reaches a caller as a bare 500 — an HTMX
request gets back a ``200`` with nothing to swap plus an ``om:error`` toast
trigger, a full page gets ``fragments/error_page.html`` at 502/500 with a
Retry link) and a ``RequestValidationError`` handler (FastAPI's own 422
body, plus the same toast trigger for an HTMX caller).

**The ``200`` is the load-bearing part, and it has a client half.** htmx
treats a 4xx/5xx as a load failure and offers a listener nothing useful,
which is why a failure answers ``200`` + ``HX-Reswap: none`` instead — but
that also means ``response.ok`` is *true* on a failure, so a client that
tests only the status reads a dead mail server as a success. Both halves
of the client now read the event instead:
``static/js/actions.js``'s ``run()``/``undo()`` check it on their own
``fetch`` responses, and one body-level listener in the same file catches
the ones htmx dispatches for its own requests. See that file's "failure
contract" header for why ``retry`` is carried but not yet acted on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from mailosh.config import Settings
from mailosh.db.base import Base
from mailosh.db.notify import NotifyBus, PostgresNotifyBus, asyncpg_dsn
from mailosh.db.session import make_engine, make_sessionmaker
from mailosh.jmap.errors import JmapError, TransportError
from mailosh.jmap.pool import ClientPool
from mailosh.security.csrf import CsrfError
from mailosh.services import outbound
from mailosh.services.mailbox_tree import hidden_in_nav
from mailosh.sse import HubRegistry
from mailosh.stalwart_admin import StalwartAdmin
from mailosh.ui.env import build_env
from mailosh.web import (
    actions,
    auth,
    compose,
    deps,
    events,
    frames,
    labels,
    mail,
    orphan_keys,
    palette,
    prefs,
    search,
)
from mailosh.web import (
    settings as settings_web,
)

logger = logging.getLogger(__name__)

#: Templates are loaded by `mailosh.ui.env.build_env` itself (a relative
#: path from the repo root — see that function's own docstring), not from
#: here; this app only needs to know where the *static* tree lives, both
#: for the `/static` mount below and as `build_env`'s `static_dir` argument
#: (`icon()`/`static()`'s real-file-reading globals).
_STATIC_DIR = Path(__file__).parent / "static"

#: How often the background maintenance task runs (design spec §9: "idle-
#: evicted" JMAP clients; controller ruling #2's reaper).
_MAINTENANCE_INTERVAL_SECONDS = 300

#: A pooled JMAP client not used within this many seconds gets closed and
#: evicted by the same sweep.
_POOL_IDLE_SECONDS = 1800

#: A per-user SSE hub with no `/events` subscriber and no activity for this
#: long has its upstream Stalwart listener cancelled and its hub forgotten
#: by the same sweep (design spec §6.5: "started on first request after
#: login, stopped after 30 min idle").
_HUB_IDLE_SECONDS = 1800

#: Design spec §9's exact security-header set for the app itself (mail
#: frames carry their own stricter header — a later task's concern, not
#: this one's — so this is applied with `setdefault`, not an unconditional
#: overwrite, letting a more specific route override it).
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

#: Task 13's exact toast copy (brief verbatim) for a `JmapError`/
#: `TransportError` reaching an HTMX caller — the same string regardless of
#: which of the two actually failed: from the reader's chair "the mail
#: server didn't answer" is one fact, not two. `retry: true` alongside it
#: says the failure is transient, unlike the validation toast below — the
#: client carries it but does not yet act on it (`static/js/actions.js`'s
#: failure-contract header says why: nothing may silently re-issue a write
#: the reader has just been told did not happen).
_JMAP_ERROR_TOAST = "Couldn't reach the mail server. Try again."

#: `RequestValidationError`'s own toast copy — no exact string is pinned by
#: the brief the way `_JMAP_ERROR_TOAST`'s is, so this is this module's own
#: choice, generic on purpose: the handler below is app-wide, not written
#: for any one route's fields.
_VALIDATION_TOAST = "That request wasn't valid"

#: A live session whose page is carrying a *different* session's CSRF token.
#: The wording has to do two things at once: say the page is stale, and stop
#: the reader reloading before they have rescued what they were writing. A
#: reload is the fix, and it is also what throws away an unsent message --
#: compose autosave is failing for the same reason, so the only copy of
#: their text is in the DOM in front of them.
_STALE_CSRF_TOAST = "This page signed out elsewhere. Copy anything unsent, then reload."


#: Block-level tags whose boundary becomes a line break in `html_to_text`'s
#: output — the common block elements a Squire-authored message body
#: actually contains (Squire's default `blockTag` config is `DIV`; `<p>`/
#: headings/list items/blockquotes/table rows cover pasted-in content too),
#: not an exhaustive list of every HTML block element.
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table"}
)

#: Elements whose content must never reach `html_to_text`'s output, even
#: though `HTMLParser` still calls `handle_data` for the raw text between
#: their tags — `<script>`/`<style>` bodies are code/CSS, not message text.
_SKIPPED_TAGS = frozenset({"script", "style"})


class _TextExtractor(HTMLParser):
    """Accumulates the visible text of an HTML fragment for `html_to_text`:
    drops tags, decodes entities (`HTMLParser(convert_charrefs=True)`
    already does this for `handle_data`'s text), skips `<script>`/`<style>`
    content entirely, and emits a newline at each block-element boundary so
    e.g. paragraphs read as separate lines rather than one run-on string.

    `_skip_depth` is a counter, not a flag, so nested same-tag markup
    (unusual, but not invalid HTML) can't have an inner close tag
    prematurely re-enable text collection while an outer one is still open.
    `handle_startendtag` is overridden (rather than left at `HTMLParser`'s
    default, which calls `handle_starttag` then `handle_endtag`) so a
    self-closed void element like `<br/>` emits exactly one newline, the
    same as the far more common un-self-closed `<br>` (which never gets a
    matching `handle_endtag` call at all) — without this override, only the
    self-closed spelling would double up.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Derive a plain-text fallback from a Squire-authored HTML body.

    A minimal, stdlib-only (`html.parser`) tag stripper — not a general
    HTML-to-text renderer — used by compose's send path (a later task) when
    the form's `html` field is the only body the browser submitted: the
    resulting string becomes `send`'s `text` argument, so the *sent*
    message still carries a text/plain alternative part the way any
    well-formed `multipart/alternative` email should (design spec §8: real
    HTML rendering/sanitization is P2 scope — this is not that, it only
    ever produces the *outgoing* text/plain part, never renders anything
    back to a user).

    Block-element boundaries (paragraphs, `<br>`, list items, ...) become
    blank-line-separated paragraphs in the output — including between two
    plain, adjacent blocks with no explicit empty block between them (e.g.
    Squire's own `<div>line one</div><div>line two</div>` for two typed
    lines) — a deliberate simplification for this ~20-line utility rather
    than trying to distinguish "just the next line" from "an intentional
    paragraph break" the way a full HTML-to-text renderer might. Runs of
    2+ blank lines collapse to exactly one, and the result is stripped of
    leading/trailing blank lines.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [line.strip() for line in parser.text().splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line == "" and (not collapsed or collapsed[-1] == ""):
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip("\n")


def _notify_bus(settings: Settings) -> NotifyBus | None:
    """The cross-process SSE fan-out transport for `settings`, or `None`
    for the single-process default.

    `None` is not a degraded mode — it is Phase 1's documented behaviour
    (design spec §12) and exactly what `mailosh.sse.HubRegistry` did before
    any of this existed: one uvicorn worker, one in-memory registry.
    `MAILOSH_SSE_FANOUT=postgres` is what a deployment sets when it wants
    more than one worker, and `asyncpg_dsn` raises here — at startup, where
    an operator will see it — rather than letting a non-Postgres
    `database_url` turn into a connection that quietly never works.
    """
    if settings.sse_fanout != "postgres":
        return None
    return PostgresNotifyBus(asyncpg_dsn(settings.database_url))


async def _maintenance_loop(app: FastAPI) -> None:
    """Runs for the app's whole lifetime (cancelled at shutdown, see
    `create_app`'s lifespan): every `_MAINTENANCE_INTERVAL_SECONDS`, evicts
    idle pooled JMAP clients, stops idle per-user SSE listeners, and sweeps
    expired sessions / stale rate-limit rows. Any one part failing is
    logged and does not cancel the loop itself — a single bad sweep (e.g. a
    transient DB hiccup) must not permanently stop future ones.
    """
    while True:
        await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)
        try:
            await app.state.pool.stop_idle(_POOL_IDLE_SECONDS)
        except Exception:
            logger.exception("idle JMAP client eviction sweep failed")
        try:
            await app.state.hubs.stop_idle(_HUB_IDLE_SECONDS)
        except Exception:
            logger.exception("idle SSE listener sweep failed")
        await outbound.sweep(app)  # never raises: see its docstring
        try:
            async with app.state.sessionmaker() as db:
                summary = await auth.reap_expired_sessions(
                    db, app.state.admin, app.state.settings, app.state.pool
                )
                await orphan_keys.retry(db, app.state.admin)
            logger.info(
                "session reap: %d session(s) reaped, %d login_attempt row(s) pruned",
                summary.sessions_reaped,
                summary.login_attempts_pruned,
            )
        except Exception:
            logger.exception("session reap sweep failed")


def _is_htmx(request: Request) -> bool:
    """Whether `request` is one of htmx's own — the same `hx-request:
    true` header check `create_app`'s `SessionRequired` handler and
    `mailosh.web.mail._is_fragment` already use. Kept as a free function
    (not a shared import from `mail.py`) since an exception handler runs
    outside any route's own dependencies and has nothing else in common
    with that module.
    """
    return request.headers.get("hx-request") == "true"


def _hx_trigger(name: str, payload: dict[str, object]) -> str:
    """One `HX-Trigger` header value naming a single event — the same
    `json.dumps(..., separators=(",", ":"))` shape `mailosh.web.actions`
    and `mailosh.web.prefs` already build theirs with, so every
    `HX-Trigger` this app ever sends is serialized the same way.
    """
    return json.dumps({name: payload}, separators=(",", ":"))


def _error_toast(*, toast: str, retry: bool) -> Response:
    """The HTMX-shaped answer to a `JmapError`/`TransportError`: `200`, not
    a 4xx/5xx — htmx's default `responseHandling` treats those as a load
    failure (`htmx:responseError`, no swap, and no help for a listener that
    wants to show a friendly toast instead), which is exactly what this
    response is built to avoid. `HX-Reswap: none` says there is nothing to
    swap into whatever target made the request (there is no body at all),
    and the `om:error` trigger carries the toast copy plus whether this
    failure is one worth automatically retrying.

    `HX-Push-Url: false` is the third header the `200` makes necessary.
    htmx pushes a boosted link's URL on any successful response, and this
    one is successful by construction — so a failed click on Starred
    rewrote the address bar to `/mail/starred` while the inbox stayed on
    screen, leaving the URL claiming a mailbox the reader was never shown
    (and a reload or a Back that would then act on it). Nothing was
    swapped, so nothing may be pushed.
    """
    return Response(
        status_code=200,
        headers={
            "HX-Reswap": "none",
            "HX-Push-Url": "false",
            "HX-Trigger": _hx_trigger("om:error", {"toast": toast, "retry": retry}),
        },
    )


def _error_page(request: Request, *, status_code: int, message: str) -> Response:
    """The full-page answer to a `JmapError`/`TransportError`:
    `fragments/error_page.html` with a Retry link back to the same URL, so
    the way out of a dead mail server is one click rather than a manual
    reload. The *status* stays here — `502` for a transport failure, `500`
    for a `JmapError` — because it is what distinguishes the two callers
    below, and the template renders identically for both.

    `app.state.templates` is built by `create_app` itself, not by the
    lifespan, so it exists for every request this handler can ever run
    for. The template is what makes `request.url` safe in an attribute:
    this used to be a hand-built HTML string with its own `html.escape`
    calls, and Jinja's autoescaping is the version of that nobody has to
    remember.
    """
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/error_page.html",
        {"message": message, "retry_url": str(request.url)},
        status_code=status_code,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the Mailosh FastAPI app: Jinja env/`/static`, security
    headers, the login-redirect/JMAP-failure/validation exception handlers,
    the auth/events/actions/palette/prefs/mail routers, and the lifespan
    that wires `app.state`.

    `settings` lets a caller (unit tests) inject a `Settings` built from a
    throwaway aiosqlite URL/secret rather than the real environment;
    `None` (every real run — `docker/entrypoint.sh`'s
    ``uvicorn mailosh.web.app:create_app --factory``) constructs one from
    the process environment/`.env`, same as any other `Settings()` call in
    this codebase.

    When `settings.database_url` is a `sqlite+...` URL, the lifespan
    creates every table via `Base.metadata.create_all` at startup — unit
    tests only; production (a `postgresql+asyncpg://...` URL) always relies
    on Alembic (`migrations/versions/`, applied by `docker/entrypoint.sh`
    before this app ever starts serving), never this call.

    A test that needs to replace `app.state.admin` (e.g. with a fake that
    never talks to a real Stalwart server) can set it directly on the
    returned `FastAPI` object before the lifespan first runs (any
    `TestClient(app)`'s first request/portal entry) — the lifespan checks
    `hasattr(app.state, "admin")` first and, if already set, leaves it
    alone (and does not close it at shutdown either — the caller that set
    it owns its lifecycle).
    """
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved_settings
        engine = make_engine(resolved_settings.database_url)
        if resolved_settings.database_url.startswith("sqlite"):
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.pool = ClientPool()
        app.state.hubs = HubRegistry(_notify_bus(resolved_settings))
        await app.state.hubs.start()

        owns_admin = not hasattr(app.state, "admin")
        if owns_admin:
            app.state.admin = StalwartAdmin(
                resolved_settings.stalwart_url,
                resolved_settings.stalwart_admin_user,
                resolved_settings.stalwart_admin_secret,
            )

        maintenance_task = asyncio.create_task(_maintenance_loop(app))
        try:
            yield
        finally:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
            # Order matters: the per-user SSE listeners are streaming *from*
            # the pooled clients, so they are cancelled and awaited first —
            # closing a client out from under a live listener would only
            # make it log a failure and try to reconnect.
            await app.state.hubs.close()
            await app.state.pool.close_all()
            if owns_admin:
                await app.state.admin.close()
            await engine.dispose()

    app = FastAPI(title="Mailosh", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    env = build_env(_STATIC_DIR)
    # The one nav-template predicate that lives in a service rather than in
    # `mailosh.ui` (it reads `LabelNode.visibility`, a domain concept the
    # design system has no business knowing). Registered here — the web
    # layer already depends on `mailosh.services`, while `build_env` sits
    # below both and must not.
    env.globals["hidden_in_nav"] = hidden_in_nav
    templates = Jinja2Templates(env=env)
    app.state.templates = templates

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(deps.SessionRequired)
    async def _session_required(request: Request, exc: deps.SessionRequired) -> Response:
        # For HX requests, `HX-Redirect` tells htmx to do a full-page
        # navigation to the login page itself — a plain 401 body swapped
        # into whatever element made the request would make no sense.
        #
        # `next` has to come from the page the reader is looking at, not
        # from the request that happened to 401. Those differ for every
        # htmx request: `deps._next_url` reads `request.url.path`, which
        # for a fragment fetch is the fragment's own route. When a session
        # expired, `sse.js`'s 120 s poll refetched `/mail/inbox/rows`,
        # inherited `next=/mail/inbox/rows?position=0&limit=50`, and
        # signing in then rendered a bare row fragment with no shell
        # around it — an app that looks broken at the exact moment the
        # user has just proved who they are. `HX-Current-URL` is the
        # browser's address bar, which is what "where I was" means.
        target = exc.next_url
        if request.headers.get("hx-request") == "true":
            current = request.headers.get("hx-current-url")
            if current:
                # Path + query only: `_safe_next` rejects anything with a
                # scheme or host, and this header carries an absolute URL.
                parsed = urlsplit(current)
                if parsed.path:
                    target = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
            location = f"/login?next={quote(target, safe='')}"
            return Response(status_code=401, headers={"HX-Redirect": location})
        return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=303)

    @app.exception_handler(TransportError)
    async def _transport_error(request: Request, exc: TransportError) -> Response:
        """The transport-level half of Task 13's error surface — a bad
        response or an unreachable Stalwart (RFC 8620 has nothing to say
        here; the request never got a JMAP-shaped answer at all). `502` for
        a full page reads this the same way a caller would read any other
        upstream gateway failure.
        """
        if _is_htmx(request):
            return _error_toast(toast=_JMAP_ERROR_TOAST, retry=True)
        return _error_page(request, status_code=502, message=_JMAP_ERROR_TOAST)

    @app.exception_handler(JmapError)
    async def _jmap_error(request: Request, exc: JmapError) -> Response:
        """Every other `JmapError` (`MethodError` and friends — a request
        that *did* reach Stalwart and *did* get an answer, just an error
        one). Registering this alongside `_transport_error` above is safe
        precisely because `TransportError` is a `JmapError` subclass:
        Starlette's own handler lookup walks `type(exc).__mro__` and picks
        the closest registered match, so a `TransportError` always reaches
        its own handler above and never falls through to this one. `500`
        for a full page, not `502` — nothing here says the upstream is
        unreachable, only that it answered with a failure.
        """
        if _is_htmx(request):
            return _error_toast(toast=_JMAP_ERROR_TOAST, retry=True)
        return _error_page(request, status_code=500, message=_JMAP_ERROR_TOAST)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """A 403 from `deps.csrf_protect` is the one HTTP error worth
        translating, and it has a specific cause worth naming.

        Signing out and back in mints a new session with a new CSRF token.
        Any tab still showing the old page keeps sending the old token, and
        every mutation from it answers 403 -- compose autosave, send,
        archive, all of it -- while the session cookie is perfectly valid,
        so nothing redirects to the login page and nothing explains itself.
        The reader sees a Send button that does nothing. That is what was
        reported, and reproducing it took signing out in one tab and
        posting from another.

        Deliberately *not* an `HX-Refresh`. Reloading fixes the token and
        destroys the message being written, and the autosave that would
        otherwise have preserved it is failing for the same reason. The
        reader is told what happened and left in control of when to reload.

        Every other `HTTPException` keeps FastAPI's own behaviour: this
        re-raises into the default handler rather than inventing a response
        for statuses it knows nothing about -- *including other 403s*. The
        match is on `CsrfError`, not on the status: `frames.py` answers 403
        when the sender posted with "always show images from this sender"
        is not the message's own, and that is not a stale token.
        """
        if isinstance(exc, CsrfError) and _is_htmx(request):
            return _error_toast(toast=_STALE_CSRF_TOAST, retry=False)
        # A wrong URL typed into the address bar used to get Starlette's bare
        # `{"detail":"Not Found"}`. A browser asking for a page gets a page;
        # every other caller (htmx, a script, a test asserting the JSON)
        # keeps FastAPI's own answer.
        if (
            exc.status_code == 404
            and not _is_htmx(request)
            and "text/html" in request.headers.get("accept", "")
        ):
            return _error_page(request, status_code=404, message="There's nothing at this address.")
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        """FastAPI's own 422 body (`{"detail": [...]}`, byte-for-byte what
        `fastapi.exception_handlers.request_validation_exception_handler`
        already answers) plus, for an HTMX caller only, an `om:error`
        toast trigger alongside it — a non-HTMX caller (a bare API client,
        or a test asserting the JSON shape directly) gets exactly what it
        already expects, unchanged.
        """
        headers = {}
        if _is_htmx(request):
            headers["HX-Trigger"] = _hx_trigger(
                "om:error", {"toast": _VALIDATION_TOAST, "retry": False}
            )
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors())},
            headers=headers,
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Response:
        """Liveness: is this process up and serving requests?

        204, no body, no session, no database, no template. The container
        healthcheck runs this every 30 seconds; the alternative was `GET
        /login` -- the only other route answering an anonymous 200 -- which
        renders an entire page to learn one bit.

        Deliberately **not** a readiness probe, and it makes no claim about
        Postgres or Stalwart. A dependency check here would mark the app
        container unhealthy for an outage in a different container, and
        restarting the app does not fix Stalwart. `scripts/healthcheck.sh`
        probes each store separately, which is where that belongs -- and
        note that Stalwart's own `/healthz/live` answers 200 even in
        bootstrap mode, so neither probe substitutes for the other.
        """
        return Response(status_code=204)

    app.include_router(auth.router)
    app.include_router(events.router)
    app.include_router(actions.router)
    # `prefs.router` already carries its own router-level
    # `dependencies=[Depends(deps.csrf_protect)]` (see that module's
    # docstring) — nothing is passed here, so `POST /prefs` is CSRF-checked
    # exactly once, not twice.
    app.include_router(palette.router)
    app.include_router(prefs.router)
    # Compose (Phase 1C): the dock, the inline reply card, autosave, send,
    # discard and the attachment upload. Like `prefs.router` above it
    # carries its own router-level `dependencies=[Depends(deps.
    # csrf_protect)]`, so nothing is passed here and its four mutating
    # routes are CSRF-checked exactly once (`csrf.validate` exempts the
    # GETs on method, so they pay only for the session lookup they need
    # anyway). Registered before `mail.router` for no reason beyond
    # reading order: none of its paths collide with any of mail's.
    app.include_router(compose.router)
    # Message rendering (Phase 1B): `GET /m/{id}/html` (the sandboxed
    # frame document under its own, stricter CSP -- which is why
    # `_security_headers` above applies the app's with `setdefault`),
    # `GET /m/{id}/frame`, and the signed remote-image proxy `GET /img`.
    # Phase 1D. Registered from stub routers so that search and labels could
    # be authored in parallel without both needing this file.
    app.include_router(search.router)
    app.include_router(labels.router)
    app.include_router(settings_web.router)
    app.include_router(frames.router)
    app.include_router(mail.router)

    return app
