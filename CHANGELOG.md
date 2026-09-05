# Changelog

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
