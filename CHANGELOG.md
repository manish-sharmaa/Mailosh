# Changelog

## 0.1.2 — 2026-09-06

One fix, found by the first public deployment.

- **The production image builds its own static assets.** The vendored
  JavaScript, the icons, the subsetted Inter font and the compiled stylesheet
  are not tracked in git; they are produced by `make vendor icons fonts css`.
  The Dockerfile assumed they were already on disk, which is true on a
  developer's machine after `make test` and false on a server that just ran
  `git clone` — so every page answered 500 from `static()` hashing a font that
  was never there. The image now has a build stage that runs those Makefile
  targets and a final stage that refuses to finish without the results.
  `docker compose up -d --build` from a bare clone works as the README says.

## 0.1.1 — 2026-09-05

Bug fixes from a review of the client code. No new features; nothing in the
deployment story changes.

### Reading and triage

- `z` after auto-advance un-archives the last conversation again, instead of
  un-reading the next one: the passive mark-read-on-open no longer takes the
  undo slot, and no longer toasts "Marked as read" on every open.
- `s` in an open conversation can unstar as well as star.
- Archived rows are no longer left as invisible zero-height ghosts — still
  focusable, still counted — when an unrelated swap lands during the collapse.
- Returning from a conversation whose row has left the list keeps the cursor
  near where the reader was, rather than on row 1.

### Compose

- Sending inside the ten-second undo window no longer leaves a copy of the
  sent message in Drafts when an autosave landed in between.
- Save-and-close, discard and pop-out abort an in-flight autosave first, so a
  draft is never duplicated or resurrected.
- A mail-server failure during attachment upload is reported instead of
  silently dropping the file.
- A failed load of the compose module is reported and retried on the next
  attempt, rather than leaving compose dead until reload.

### Labels

- Type-to-create from the picker also applies the new label to the selection,
  with an undo token. `POST /labels` accepts `ids`.
- The picker's search field stays available in label mode, so an account with
  fewer than seven labels can create one from there.
- One list refresh per label change instead of two.

### Live updates, palette, sign-in

- A session signed out in another tab reaches the login page immediately,
  instead of showing "connection lost" for up to two minutes.
- Catch-up refetches carry the real last event id.
- The ⌘K palette keeps its cursor on the same command when mail arrives while
  it is open.
- The sign-in form no longer stays busy after a Back to a cached page.

### Server

- Only a CSRF failure is translated into the "signed out elsewhere" toast;
  other 403s keep their real status.

## 0.1.0 — 2026-09-05

The first tagged release. Mailosh is a self-hosted webmail client for
[Stalwart](https://github.com/stalwartlabs/stalwart): FastAPI and htmx on the
server, no Node toolchain, AGPL-3.0.

**This is an early release, and the section below on what has not been proven
is the most important part of these notes.** Read it before pointing a domain
you care about at this.

### What works

**Reading.** Conversation view with safe HTML rendering. Hostile mail passes
through three independent layers — `nh3` strips the HTML server-side,
`tinycss2` re-serialises CSS through an allow-list, and the result is served
into a sandboxed iframe under its own restrictive CSP. Each layer is written
assuming the other two may fail. `allow-same-origin` appears in no sandbox
anywhere, which is what keeps a hostile message away from the reader's
session. Remote images are blocked until asked for, and the frame's `img-src`
is byte-identical whether they are on or off, so a sanitiser miss still cannot
leak the reader's IP.

**Compose and send.** Rich text via Squire, drafts with autosave, attachments,
reply / reply all / forward with correct `In-Reply-To` and `References`
threading. Quoted mail is sanitised on the way out as well as in — replying to
a hostile message must not put its markup in someone else's inbox.

**Search.** Gmail-style operators — `from: to: subject: body: has: is: in:
label: before: after: older_than: newer_than: larger: smaller:`, quoted
phrases, `-` negation, `OR`, parentheses. The parser emits only allow-listed
JMAP filter keys and never raises: unknown operators and malformed input
produce an inline hint, never an error page.

**Labels.** Create, rename, nest and delete over JMAP mailboxes, with colour,
visibility and ordering. Deleting a label never deletes mail.

**The rest.** Keyboard-first throughout with a command palette, live updates
over SSE, optimistic actions with undo, light and dark themes, three
densities, responsive to 390px with a nav drawer, and a WCAG 2.2 AA pass with
measured contrast ratios in both themes.

**Operations.** A production compose file that publishes only the four mail
ports and terminates TLS; backup and restore scripts, exercised by a real
restore drill into a throwaway target; a liveness endpoint; and scripted
first-boot configuration for a new Stalwart.

### What has *not* been proven

None of the following has ever been done, and none of it is a small remaining
step:

- **Mailosh has never received a message from the internet.** No MX record has
  ever pointed at a running instance.
- **Mailosh has never delivered a message to an outside recipient.** Sending
  works as far as your own mail server; delivery beyond it is untested.
- **It has never been deployed.** The production compose file is verified by
  rendering and by unit tests, not by a stack running on a public host.
- **ACME certificates were issued against Let's Encrypt staging only.**
  DNS-01 with a real provider works end to end, including wildcards;
  production issuance is not exercised.
- There is **no queue visibility**: if outbound delivery fails, the interface
  says "Sent" and then nothing until the server bounces the message.
- Tested in Chromium and Safari. **Firefox is unverified.** The
  `forced-colors` rules are written from the specification and have not been
  run in a real Windows High Contrast environment, and no screen reader has
  been used.
- **No external security review.** See `SECURITY.md`, which names the parts
  most worth attacking.

### Requirements

Docker and Docker Compose, a domain, and a host that can receive on port 25 —
or a relay, which is what most residential and budget-VPS connections need.
See `docs/hosting.md`. `README.md` has the quick start; `docs/operations.md`
covers backup, restore and first boot.
