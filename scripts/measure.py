#!/usr/bin/env python3
"""Task 11 (SPK-6) + Task 14: four live-stack latency measurements against
the real `docker compose` stack (stalwart + postgres + mailosh, all
already running -- this script starts/stops nothing).

Run from the repo root, with the stack up and `scripts/stalwart-init.sh`
already applied:

    .venv/bin/python scripts/measure.py [--keep]

Prints progress to stderr and a copy-pasteable Markdown report (dataset
size, results table, raw per-run numbers) to stdout -- redirect stdout
alone to capture just the report, e.g. `... > report.md`.

Four measurements, run once each (in order). All four share one JMAP
connection to Stalwart; (c) and (d) additionally share one logged-in HTTP
session against the app itself (`_app_session`), because both of the
endpoints they hit are session-authenticated:

(a) **Inbox query** -- `JmapClient.query_inbox` called ``QUERY_RUNS`` times
    in a loop, one HTTP round trip per call (`Email/query` + `Email/get`
    chained via a JMAP result reference -- see `JmapClient.query_inbox`'s
    own docstring). Wall-clock per call, client-side (includes network +
    server time; this machine's client and server are both localhost, so
    network is near-zero).
(b) **Thread fetch** -- `JmapClient.get_thread` called ``THREAD_RUNS``
    times against one real, already-live multi-message thread (see
    `_resolve_thread_id`'s docstring for exactly which one and why).
(c) **SMTP -> SSE** -- a raw `httpx` SSE `GET` against this app's own
    `/events` endpoint (not Stalwart's -- this measures the full pipeline
    a browser tab actually experiences: SMTP accept -> Stalwart indexes
    -> `stalwart_listener` -> `SseHub` -> this stream), then ``SMTP_RUNS``
    times: send one uniquely-subjected message via `smtplib` straight to
    Stalwart's SMTP port, and measure wall-clock from just before that
    send to the next `mail` frame this script's own SSE subscription
    receives. **Every message this creates is destroyed before the script
    exits** -- see "Cleanup" below.
(d) **Rows fragment** (Task 14) -- ``ROWS_RUNS`` `GET /mail/inbox/rows`
    with `HX-Request: true`, on the same logged-in session. This is the one
    measurement that exercises the app's own render path end to end, and
    the only one spec §11's "partial TTFB < 200 ms" is actually about. The
    report ends with a budget comparison table for it. See
    `_measure_rows_fragment` for what "server time" does and does not mean
    at this measuring point.

Task 14 also **fixed** (c): `/events` gained a session dependency after
Task 11 wrote this script, so the anonymous SSE reader it used to open had
been failing with `401 Unauthorized` -- aborting the whole run -- for as
long as nothing re-ran it. See `_app_session`.

KNOWN QUIRK (found live in Task 8, recorded in
`docs/spikes/p0-findings.md` SPK-6): Stalwart's own spam
heuristic routes anonymous, unauthenticated local SMTP delivery (exactly
what this script and `scripts/send-test.py` both do, port 2525, no auth)
into **Junk Mail**, not Inbox. This does NOT affect measurement (c)'s
validity: `is_mail_change` (`mailosh/sse.py`) fires the `mail` event
on *any* Email/Mailbox state change for the account, regardless of which
mailbox the message lands in -- so the push still fires, and the
send-to-frame latency is unaffected. It only means: don't expect these
test messages to visibly appear in `/inbox` without an extra move -- moot
anyway once cleanup (below) removes them.

**Attribution approach (spacing/draining):** the brief allows either
spacing sends >=1s apart or a drain-then-send pattern to keep each send
correctly matched to the frame it caused, and asks that the choice be
noted. This script does **both**: each iteration drains any frame(s)
already sitting in its local queue immediately before sending (so a late
straggler from a previous iteration -- or, this store having exactly one
account, literally nothing else in this idle dev stack can produce an
unrelated Email/Mailbox change -- can never be misattributed to the next
send), *and* waits ``SEND_SPACING_SECONDS`` (comfortably >1s) after each
observed frame before sending the next message, so two real deliveries are
never in flight against Stalwart at the same time either.

**Cleanup:** an earlier version of this script sent its 10 SMTP test
messages per run and never removed them -- found in review, exactly the
class of stale-live-data problem this same task's own SPK-2 fix (a leaked
3-message thread from earlier work, breaking `make itest`) had to clean up
elsewhere; see task-11-report.md's fix-report addendum. Fixed: after each
send, this script resolves the message's JMAP id back from its own unique
subject (`Email/query`'s `subject` filter, a substring match per RFC 8621
SS4.4.1 -- safe here since every subject carries a uuid4 suffix, so it can
only ever match the one message being resolved), collects every id it
finds, and destroys all of them via `Email/set destroy` (mirroring
`tests/integration/test_live_stalwart.py`'s own `_destroy` helper --
`destroy` is deliberately not added to the public `JmapClient` contract,
same reasoning that test file gives) in a `finally` block, so an
interrupted or failed run still cleans up whatever it already created.
Any subject that can't be resolved to an id is logged (not raised) by
name, so a human can find and remove it by hand. Pass **`--keep`** to skip
this cleanup for debugging (e.g. to inspect a sent message by hand) --
this deliberately leaves real data live in the account; the default, with
no flags, is cleanup **on**. The stdout report's "Dataset size" section
prints the account's message totals both before this run's sends and
after this run's cleanup, so every run self-verifies it left no residue
without needing a separate manual check.

**`sys.path` note:** this repo's editable install
(`pip install -e '.[dev]'`, per the Makefile's `venv` target) was found,
while writing this script, to not reliably put the repo root on
`sys.path` for a plain `python scripts/measure.py` invocation in this dev
environment (`pytest`/the console-script entry point both happen to work
via other mechanisms -- see task-11-report.md's "concerns" section for the
full diagnosis). The explicit `sys.path.insert` below sidesteps that
rather than depending on it, the same reasoning `scripts/send-test.py`'s
docstring gives for staying stdlib-only (this script can't avoid the
import, since the brief asks it to exercise the real `JmapClient`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import smtplib
import statistics
import sys
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mailosh.config import Settings  # noqa: E402 -- see this module's own "sys.path note" above
from mailosh.jmap.client import JmapClient, find_inbox, parse_sse_stream  # noqa: E402
from mailosh.jmap.errors import JmapError  # noqa: E402

QUERY_RUNS = 20
THREAD_RUNS = 20
SMTP_RUNS = 10
ROWS_RUNS = 20

SMTP_HOST = "localhost"
SMTP_SENDER = "measure@example.com"
APP_URL = "http://localhost:8000"
SSE_PATH = "/events"

#: The list fragment every live update and every pager click re-fetches
#: (`mailosh.web.mail.mail_rows`) -- spec §11's "partial TTFB < 200 ms" is
#: about exactly this response, so it is the one measurement (d) takes.
ROWS_PATH = "/mail/inbox/rows"

#: The SSE event the app actually publishes for new mail
#: (`mailosh.sse.stalwart_listener` -> `hub.publish("mail", ...)`, which
#: `static/js/sse.js` listens for as `source.addEventListener("mail", ...)`).
#:
#: **Task 14 fix.** This script was written against an earlier name,
#: `"new-mail"`, and kept matching on it after the event was renamed -- so
#: every run reported "0/10 runs got a frame" and looked exactly like a dead
#: live-update pipeline, while a real browser tab open at the same moment
#: was visibly refetching its rows on every one of those same sends. Two
#: independent stale assumptions in one measurement (this and the
#: `/events` 401 -- see `_app_session`), both from the same cause: a
#: measurement script that nothing re-ran between the tasks that changed the
#: thing it measures.
MAIL_EVENT = "mail"

#: >1s per the brief; extra margin so two sends are never in flight together.
SEND_SPACING_SECONDS = 1.5
#: How long to wait for a `mail` frame after one send before giving up
#: on that run. Generous relative to Task 8's own observed worst case
#: (~4s, cold listener straight after a container restart; this stack has
#: been up and warm throughout this task, so the listener's upstream
#: connection to Stalwart is already established).
FRAME_WAIT_TIMEOUT_SECONDS = 15.0

#: Bounded poll for resolving a just-sent message's id back from its own
#: subject (see `_resolve_email_id_by_subject`) -- same shape as
#: `StalwartAdmin.get_dkim_record`'s `_DKIM_POLL_ATTEMPTS`/
#: `_DKIM_POLL_DELAY_SECONDS`, for a different asynchronous-indexing race.
#: ~1.2s worst case; costs nothing on the common path (found first try).
_RESOLVE_POLL_ATTEMPTS = 5
_RESOLVE_POLL_DELAY_SECONDS = 0.3


def _pctl(values: list[float], p: float) -> float:
    """The p-th percentile of `values` (linear interpolation between the
    two nearest ranks, via `statistics.quantiles(..., method="inclusive")`
    -- the standard library's own percentile primitive, not a hand-rolled
    one). Adequate for a 10-20 sample spike measurement; not a claim of
    statistical rigor at that sample size, which is exactly why this
    script also prints every raw run (see the module docstring/output).
    """
    if len(values) == 1:
        return values[0]
    cuts = statistics.quantiles(sorted(values), n=100, method="inclusive")
    idx = min(max(round(p) - 1, 0), len(cuts) - 1)
    return cuts[idx]


def _fmt(ms: float) -> str:
    return f"{ms:.1f}"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _measure_inbox_query(client: JmapClient, inbox_id: str) -> list[float]:
    timings: list[float] = []
    for i in range(QUERY_RUNS):
        t0 = time.perf_counter()
        await client.query_inbox(inbox_id, limit=50, position=0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings.append(elapsed_ms)
        _log(f"  inbox query run {i + 1}/{QUERY_RUNS}: {_fmt(elapsed_ms)} ms")
    return timings


async def _resolve_thread_id(client: JmapClient, inbox_id: str) -> tuple[str, str]:
    """Pick a real, already-live multi-message thread to benchmark
    `get_thread` against.

    Prefers a row whose subject contains "spike thread" (case-insensitive)
    -- the same discovery idiom
    `tests/integration/test_live_stalwart.py::test_import_thread_and_multilabel`
    already uses for SPK-2's own 3-message fixture -- so this script keeps
    working unmodified if that fixture is ever (re-)imported and left live.

    Falls back to the newest inbox row otherwise (query_inbox already
    sorts newest-first, so `rows[0]` is "the thread a user opening their
    inbox right now would click first" -- a realistic benchmark target,
    not an arbitrary one). This fallback is what actually runs as of this
    task: a 3-message "Spike thread kickoff" fixture (Message-IDs
    `t1`/`t2`/`t3@example.org`) *was* found leaked live in a mailbox
    literally named "Spike" at the start of this task -- not cleaned up by
    its own test run, left over from earlier SPK-2 work -- and was
    confirmed to permanently break `make itest` (a fresh `Email/import` of
    the identical fixture shares those Message-IDs, so Stalwart's
    References-based threading, per SPK-2's own findings, folds the new
    import and the leaked copy into one 6-message thread instead of the
    test's expected 3). It was destroyed as part of getting `make itest`
    green again for this task's own final gate (`Email/set destroy` +
    `Mailbox/set destroy`, live `/jmap` calls -- see task-11-report.md for
    the full before/after evidence). Every thread left in this store as a
    result is a genuine 2-message Sent+Inbox pair from Task 9's own SPK-1
    compose verification -- real JMAP data, not synthetic filler.
    """
    rows = await client.query_inbox(inbox_id, limit=50, position=0)
    for row in rows:
        if "spike thread" in (row.subject or "").lower():
            return row.thread_id, "subject match: 'spike thread'"
    if not rows:
        raise RuntimeError("inbox is empty -- no thread available to benchmark get_thread against")
    return rows[0].thread_id, "fallback: newest inbox row (no 'spike thread'-subject row found)"


async def _measure_thread_fetch(client: JmapClient, thread_id: str) -> list[float]:
    timings: list[float] = []
    for i in range(THREAD_RUNS):
        t0 = time.perf_counter()
        await client.get_thread(thread_id)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings.append(elapsed_ms)
        _log(f"  thread fetch run {i + 1}/{THREAD_RUNS}: {_fmt(elapsed_ms)} ms")
    return timings


def _build_message(subject: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = SMTP_SENDER
    msg["To"] = "demo@mailosh.test"
    msg["Subject"] = subject
    msg.set_content(f"Sent by scripts/measure.py for SPK-6 latency measurement.\n\n{subject}\n")
    return msg


def _send_smtp_message(smtp_port: int, subject: str) -> None:
    """Blocking SMTP send (stdlib `smtplib`) -- always called via
    `asyncio.to_thread` from the async measurement loop below, so this
    synchronous call never blocks the event loop that the concurrent SSE
    reader task needs to keep draining the network in the background.
    """
    with smtplib.SMTP(SMTP_HOST, smtp_port, timeout=10) as smtp:
        smtp.send_message(_build_message(subject))


async def _resolve_email_id_by_subject(client: JmapClient, subject: str) -> str | None:
    """Find the JMAP id of the message this run's SMTP send just created,
    by its own unique subject -- so it can be destroyed afterward (see
    `_destroy_created_emails`). `Email/query`'s `subject` filter condition
    is a substring match (RFC 8621 SS4.4.1), which is exactly safe here:
    every subject this script sends carries its own uuid4 suffix, so a
    substring match can only ever hit the one message being resolved, not
    some unrelated one.

    Small bounded poll (`_RESOLVE_POLL_ATTEMPTS`/`_RESOLVE_POLL_DELAY_SECONDS`,
    same shape as `StalwartAdmin.get_dkim_record`'s own poll for a
    different asynchronous-indexing race) rather than one attempt, in case
    Stalwart's subject-search index lags a beat behind the raw Email
    object's creation. Returns `None` (never raises) on a query failure or
    an exhausted poll -- the caller logs and reports unresolved subjects
    for manual cleanup rather than letting one lookup failure derail the
    whole run.
    """
    for attempt in range(_RESOLVE_POLL_ATTEMPTS):
        try:
            out = await client._call(
                [
                    (
                        "Email/query",
                        {
                            "accountId": client.account_id,
                            "filter": {"subject": subject},
                            "limit": 1,
                        },
                        "q0",
                    )
                ]
            )
        except JmapError:
            _log(f"  cleanup: Email/query for subject {subject!r} failed to resolve an id")
            return None
        ids = out["q0"].get("ids") or []
        if ids:
            return ids[0]
        if attempt + 1 < _RESOLVE_POLL_ATTEMPTS:
            await asyncio.sleep(_RESOLVE_POLL_DELAY_SECONDS)
    return None


async def _destroy_created_emails(
    client: JmapClient, ids: list[str], unresolved_subjects: list[str]
) -> None:
    """Best-effort cleanup for every message this run's SMTP sends created.

    Mirrors `tests/integration/test_live_stalwart.py`'s own `_destroy`
    helper: `Email/set` with `destroy`, via `client._call` directly --
    `destroy` is deliberately not part of the public `JmapClient` contract
    (same reasoning that test file's own docstring gives), so this stays a
    tiny helper local to this script rather than a client method. Called
    from `_measure_smtp_to_sse`'s own `finally` block, so this runs even if
    the measurement loop above was interrupted partway through. Never
    raises: a cleanup hiccup here must not mask whatever real error is
    already propagating through that `finally`.
    """
    if unresolved_subjects:
        _log(
            f"  cleanup: {len(unresolved_subjects)} sent message(s) could not be resolved "
            f"to an id (Email/query by subject found nothing after "
            f"{_RESOLVE_POLL_ATTEMPTS} attempts) -- NOT destroyed, find and remove by hand: "
            f"{unresolved_subjects}"
        )
    if not ids:
        _log("  cleanup: nothing to destroy")
        return
    try:
        out = await client._call(
            [("Email/set", {"accountId": client.account_id, "destroy": ids}, "d0")]
        )
        destroyed = out["d0"].get("destroyed") or []
        not_destroyed = out["d0"].get("notDestroyed") or {}
        _log(f"  cleanup: destroyed {len(destroyed)}/{len(ids)} sent test message(s)")
        if not_destroyed:
            _log(f"  cleanup: FAILED to destroy {list(not_destroyed)}: {not_destroyed}")
    except JmapError:
        _log(f"  cleanup: Email/set destroy call failed entirely -- ids left live: {ids}")


@contextlib.asynccontextmanager
async def _app_session(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """A logged-in HTTP session against the app: `(client, csrf_token)`.

    Two measurements need one. `/events` (measurement (c)) is
    session-authenticated -- `mailosh.web.events.stream_session` -- and so
    is `/mail/inbox/rows` (measurement (d)).

    **This is a Task 14 fix, not a new nicety.** Measurement (c) used to
    open an anonymous `httpx.AsyncClient` straight at `/events`, which was
    correct when Task 11 wrote it and silently stopped being correct the
    moment `/events` grew a session dependency: re-run on this branch, the
    SSE reader task died on `401 Unauthorized` before the first SMTP send,
    and the whole script aborted. Nothing caught it in between because
    nothing re-ran the script. Recorded in `p1a-findings.md` ("Budgets") as
    a real seam regression, since it is exactly the class of breakage this
    task exists to find.

    Signs out on the way out (`POST /logout`, CSRF token included, which is
    why the token is fetched up front and yielded alongside the client): a
    leaked session row keeps a pooled JMAP client and a Stalwart listener
    alive, and counts against this account's api-key quota of 5.
    """
    async with httpx.AsyncClient(base_url=APP_URL, timeout=30.0, follow_redirects=False) as http:
        login = await http.post(
            "/login",
            data={
                "username": settings.demo_user,
                "password": settings.demo_password,
                "next": "/mail/inbox",
            },
        )
        if login.status_code != 303:
            raise RuntimeError(
                f"POST /login answered {login.status_code}, expected 303 -- cannot measure "
                "the session-authenticated endpoints logged out"
            )
        shell = await http.get("/mail/inbox")
        shell.raise_for_status()
        match = re.search(r'<meta name="csrf-token" content="([^"]*)">', shell.text)
        csrf = match.group(1) if match else ""
        if not csrf:
            _log("  WARNING: no CSRF token in the app shell -- sign-out will fail with 403")
        try:
            yield http, csrf
        finally:
            with contextlib.suppress(httpx.HTTPError):
                out = await http.post("/logout", headers={"X-CSRF-Token": csrf})
                if out.status_code != 303:
                    _log(f"  sign-out: POST /logout answered {out.status_code}, not 303")


async def _measure_smtp_to_sse(
    client: JmapClient, http: httpx.AsyncClient, smtp_port: int, *, keep: bool
) -> list[float]:
    frame_times: asyncio.Queue[float] = asyncio.Queue()
    created_ids: list[str] = []
    unresolved_subjects: list[str] = []

    async def reader() -> None:
        # `read=None`: this is a long-lived stream that's expected to sit
        # idle between real events (ping comments aside, already filtered
        # by `parse_sse_stream`'s comment-line handling) -- detecting "no
        # frame showed up" is this function's own `asyncio.wait_for` job
        # below, not httpx's. Overridden per request rather than on the
        # shared client, whose ordinary 30s read timeout is right for every
        # other call made through it.
        seen_other: set[str] = set()
        async with http.stream("GET", SSE_PATH, timeout=httpx.Timeout(10.0, read=None)) as resp:
            resp.raise_for_status()
            async for frame in parse_sse_stream(resp.aiter_lines()):
                if frame.event == MAIL_EVENT:
                    await frame_times.put(time.monotonic())
                elif frame.event not in seen_other:
                    # Loud, once per name: the last time this script and the
                    # app disagreed about an event name, every run reported
                    # "0/10 frames" and looked like a broken pipeline
                    # (see MAIL_EVENT).
                    seen_other.add(frame.event)
                    _log(f"  smtp->sse: ignoring unexpected SSE event {frame.event!r}")

    reader_task = asyncio.create_task(reader())
    await asyncio.sleep(1.0)  # let the SSE connection establish before the first send

    timings: list[float] = []
    try:
        for i in range(SMTP_RUNS):
            # Drain-then-send: discard anything already queued so it can't be
            # misattributed to this iteration's send (see module docstring).
            drained = 0
            while not frame_times.empty():
                frame_times.get_nowait()
                drained += 1
            if drained:
                _log(f"  smtp->sse run {i + 1}/{SMTP_RUNS}: drained {drained} stale frame(s) first")

            subject = f"Mailosh measure-smtp-sse {i} {uuid.uuid4().hex[:8]}"
            t0 = time.monotonic()
            await asyncio.to_thread(_send_smtp_message, smtp_port, subject)
            try:
                frame_t = await asyncio.wait_for(
                    frame_times.get(), timeout=FRAME_WAIT_TIMEOUT_SECONDS
                )
                elapsed_ms = (frame_t - t0) * 1000
                timings.append(elapsed_ms)
                _log(
                    f"  smtp->sse run {i + 1}/{SMTP_RUNS}: {_fmt(elapsed_ms)} ms "
                    f"(subject={subject!r})"
                )
            except TimeoutError:
                _log(
                    f"  smtp->sse run {i + 1}/{SMTP_RUNS}: NO FRAME within "
                    f"{FRAME_WAIT_TIMEOUT_SECONDS}s (subject={subject!r}) -- excluded from results"
                )

            # Resolve+track for cleanup regardless of whether a frame
            # arrived: the SMTP send itself already created a real,
            # live message either way (see module docstring's "Cleanup").
            email_id = await _resolve_email_id_by_subject(client, subject)
            if email_id is not None:
                created_ids.append(email_id)
            else:
                unresolved_subjects.append(subject)

            await asyncio.sleep(SEND_SPACING_SECONDS)
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        if keep:
            _log(
                f"  --keep set: leaving {len(created_ids)} sent test message(s) live "
                f"({len(unresolved_subjects)} unresolved subject(s)) -- not destroyed"
            )
        else:
            await _destroy_created_emails(client, created_ids, unresolved_subjects)

    return timings


async def _measure_rows_fragment(http: httpx.AsyncClient) -> tuple[list[float], float, int]:
    """(d) `GET /mail/inbox/rows` as a logged-in browser makes it.

    Returns `(timings, warmup_ms, bytes)` -- the `ROWS_RUNS` measured runs,
    the discarded first request, and the size of the last response body.

    This is the only measurement in this script that goes through the *app*
    rather than straight to Stalwart, and it is the one spec §11's "partial
    TTFB < 200 ms" budget is actually about: `/mail/{key}/rows` is what the
    endless-scroll sentinel appends from and what every `mail:changed` live
    update re-fetches and morphs in (`mailosh.web.mail.mail_rows`). Its
    server time contains a full `Email/query`+`Email/get` round trip to
    Stalwart *plus* nav, prefs (a Postgres read) and Jinja rendering, so it
    is strictly larger than measurement (a) and cannot be inferred from it.

    **Logged in for real**: `http` is the shared `_app_session` client, so
    the session cookie a browser would carry is on every request here.
    `HX-Request: true` is set on the measured GETs because that is what htmx
    sends and what the app `Vary`s on -- measuring the header a browser
    never sends would measure a response no browser ever receives.

    **What "server time" means here, precisely**: client-side wall clock
    around the request, from a client on the same machine as the container,
    so it is server time plus a loopback round trip and httpx's own
    overhead -- the same basis measurements (a) and (b) use, and an
    over-estimate of pure server time rather than an under-estimate. There
    is no `Server-Timing` header on this app to read a truer number from
    (checked -- `mailosh.web.app` has one middleware and it only sets
    security headers), and adding one purely to flatter a measurement would
    be worse than reporting the honest upper bound.

    **The first request is discarded** and reported separately: it pays TCP
    connect, this session's first pooled-JMAP-client construction, and
    Jinja's compile of every template in the fragment. The budget is about
    the steady-state interaction, and hiding a cold outlier inside a p95
    over 20 would misrepresent both.
    """
    headers = {"HX-Request": "true"}
    t0 = time.perf_counter()
    warm = await http.get(ROWS_PATH, headers=headers)
    warmup_ms = (time.perf_counter() - t0) * 1000
    warm.raise_for_status()
    _log(f"  rows fragment warm-up (discarded): {_fmt(warmup_ms)} ms")

    timings: list[float] = []
    size = len(warm.content)
    for i in range(ROWS_RUNS):
        t0 = time.perf_counter()
        resp = await http.get(ROWS_PATH, headers=headers)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        resp.raise_for_status()
        size = len(resp.content)
        timings.append(elapsed_ms)
        _log(f"  rows fragment run {i + 1}/{ROWS_RUNS}: {_fmt(elapsed_ms)} ms")

    return timings, warmup_ms, size


async def _dataset_size(client: JmapClient, inbox_id: str) -> tuple[int, int]:
    """`(inbox_total, account_total)` via `Email/query`'s `calculateTotal`
    -- `limit: 0` so this asks only for the count, not any rows. Uses
    `_call` directly (same "tiny thing outside the public client contract,
    fine for a spike script/test" precedent
    `tests/integration/test_live_stalwart.py`'s own cleanup helper already
    set) rather than adding a one-off `count()` method to `JmapClient`
    for a single measurement script.
    """
    out = await client._call(
        [
            (
                "Email/query",
                {
                    "accountId": client.account_id,
                    "filter": {"inMailbox": inbox_id},
                    "calculateTotal": True,
                    "limit": 0,
                },
                "q0",
            ),
            (
                "Email/query",
                {"accountId": client.account_id, "calculateTotal": True, "limit": 0},
                "q1",
            ),
        ]
    )
    return out["q0"]["total"], out["q1"]["total"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mailosh P0 (SPK-6) live-stack latency measurements."
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help=(
            "skip cleanup of the SMTP test messages measurement (c) creates "
            "(debugging only -- leaves real data live in the account; default "
            "is to destroy every message this run created)"
        ),
    )
    return parser.parse_args()


async def main(*, keep: bool) -> int:
    settings = Settings()
    if settings.demo_user is None or settings.demo_password is None:
        _log(
            "MAILOSH_DEMO_USER and MAILOSH_DEMO_PASSWORD must both be set (e.g. in .env) "
            "to run this script."
        )
        return 1
    _log(f"connecting to {settings.stalwart_url} as {settings.demo_user} ...")
    client = await JmapClient.connect(
        settings.stalwart_url, settings.demo_user, settings.demo_password
    )
    try:
        inbox = find_inbox(await client.get_mailboxes())

        inbox_total, account_total = await _dataset_size(client, inbox.id)

        _log(f"running inbox query x{QUERY_RUNS} ...")
        query_timings = await _measure_inbox_query(client, inbox.id)

        thread_id, thread_choice = await _resolve_thread_id(client, inbox.id)
        _log(f"running thread fetch x{THREAD_RUNS} (thread_id={thread_id!r}, {thread_choice}) ...")
        thread_timings = await _measure_thread_fetch(client, thread_id)

        # One logged-in app session for both app-facing measurements:
        # `/events` and `/mail/inbox/rows` are equally session-authenticated,
        # and signing in twice would mint/reuse the api key twice for no gain.
        _log(f"signing in to {APP_URL} as {settings.demo_user} ...")
        async with _app_session(settings) as (http, _csrf):
            _log(
                f"running SMTP->SSE x{SMTP_RUNS} (spacing={SEND_SPACING_SECONDS}s, "
                f"drain-then-send, frame timeout={FRAME_WAIT_TIMEOUT_SECONDS}s, "
                f"cleanup={'OFF (--keep)' if keep else 'on'}) ..."
            )
            smtp_timings = await _measure_smtp_to_sse(client, http, settings.smtp_port, keep=keep)

            _log(f"running rows fragment GET {ROWS_PATH} x{ROWS_RUNS} (logged in) ...")
            rows_timings, rows_warmup_ms, rows_bytes = await _measure_rows_fragment(http)

        post_inbox_total, post_account_total = await _dataset_size(client, inbox.id)
        _log(
            f"post-cleanup dataset size: inbox={post_inbox_total} (was {inbox_total}), "
            f"account={post_account_total} (was {account_total})"
        )
    finally:
        await client.close()

    # ---- Markdown report (stdout) ----
    print(f"# Mailosh P0 measurements -- {datetime.now(UTC).isoformat()}")
    print()
    print("## Dataset size (context for the spec §6 budget comparison)")
    print()
    print(
        f"- Inbox (`{inbox.id}`) total: **{inbox_total}** messages before this run -> "
        f"**{post_inbox_total}** after cleanup"
    )
    print(
        f"- Account-wide total (all mailboxes): **{account_total}** messages before this run -> "
        f"**{post_account_total}** after cleanup"
    )
    if keep:
        print(
            "- `--keep` was set: measurement (c)'s SMTP test messages were **not** destroyed "
            "this run; the totals above will not match."
        )
    print(
        "- Spec §6's 400 ms inbox-render budget is stated **at 100k messages**; this store "
        "holds only spike/test messages, three orders of magnitude below that. The comparison "
        "below is directional (small-N sanity check that nothing is pathologically slow), "
        "**not** a conclusive verification of the 100k-message budget."
    )
    print()
    print("## Results")
    print()
    print("| Measurement | n | p50 (ms) | p95 (ms) | min (ms) | max (ms) | mean (ms) |")
    print("|---|---|---|---|---|---|---|")
    rows = [
        ("inbox query (`query_inbox`, limit=50)", query_timings),
        (f"thread fetch (`get_thread`, thread_id=`{thread_id}`)", thread_timings),
        ("SMTP -> SSE (`/events` `mail` frame)", smtp_timings),
        (f"rows fragment (`GET {ROWS_PATH}`, logged in)", rows_timings),
    ]
    for name, data in rows:
        if not data:
            print(f"| {name} | 0 | -- | -- | -- | -- | -- |")
            continue
        print(
            f"| {name} | {len(data)} | {_fmt(_pctl(data, 50))} | {_fmt(_pctl(data, 95))} | "
            f"{_fmt(min(data))} | {_fmt(max(data))} | {_fmt(statistics.fmean(data))} |"
        )
    print()
    print("### Raw runs (ms)")
    print()
    print(f"**inbox query:** {[round(v, 1) for v in query_timings]}")
    print()
    print(f"**thread fetch:** {[round(v, 1) for v in thread_timings]}")
    print()
    smtp_note = (
        ""
        if len(smtp_timings) == SMTP_RUNS
        else f"  ({len(smtp_timings)}/{SMTP_RUNS} runs got a frame)"
    )
    print(f"**SMTP -> SSE:** {[round(v, 1) for v in smtp_timings]}{smtp_note}")
    print()
    print(f"**rows fragment:** {[round(v, 1) for v in rows_timings]}")
    print()
    print(
        f"Rows-fragment notes: cold first request **{_fmt(rows_warmup_ms)} ms**, discarded from "
        f"the table above (TCP connect + this session's first pooled JMAP client + Jinja's "
        f"first compile of the fragment's templates -- see `_measure_rows_fragment`). Response "
        f"body **{rows_bytes} bytes** uncompressed, over {inbox_total} inbox message(s). "
        f"Measured client-side on loopback, so the figure is server time **plus** a loopback "
        f"round trip and httpx overhead -- an upper bound on server time, not an under-estimate."
    )
    print()
    print("## Budget comparison (design spec §11)")
    print()
    print("| Budget | Target | Measured | Verdict |")
    print("|---|---|---|---|")
    if rows_timings:
        rows_p50 = _pctl(rows_timings, 50)
        rows_p95 = _pctl(rows_timings, 95)
        verdict = "PASS" if rows_p95 < 200 else "MISS"
        print(
            f"| Partial TTFB (`GET {ROWS_PATH}`) | < 200 ms | p50 {_fmt(rows_p50)} ms / "
            f"p95 {_fmt(rows_p95)} ms | {verdict} |"
        )
    else:
        print(f"| Partial TTFB (`GET {ROWS_PATH}`) | < 200 ms | -- | NOT MEASURED |")
    print()
    print(
        "Spec §11's other budgets are browser-side (swap paint < 100 ms, INP < 200 ms, "
        "first-load LCP < 1.5 s) or static-asset sizes; this script measures neither. See "
        '`docs/spikes/p1a-findings.md` ("Budgets") for those, measured '
        "separately."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(keep=_parse_args().keep)))
