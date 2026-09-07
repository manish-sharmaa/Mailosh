# Changelog

## Unreleased

- Menus close when you click away from them. The account menu, advanced
  search, the search chips and a message's ⋮ all used to stay open until
  you clicked the button again.
- `mailosh setup` no longer suggests publishing an `AAAA` record without
  saying what has to be true first. Docker only manages the IPv4 firewall,
  so on a stock setup the box answers `ping6` with every port closed —
  and an `AAAA` pointing there makes senders wait out a timeout before
  falling back to IPv4, delaying inbound mail with nothing in the logs.
  The DNS block now prints the two `nc -6` checks to run before publishing.

## 0.2.0 — 2026-09-06

### Settings

A real settings area at `/settings`, from the account menu or ⌘K:
Appearance, Reading, Compose, Labels, Account and Security.

- **Signatures**, one per identity, added to new messages and above the
  quote in replies.
- **Change your password** and display name without leaving Mailosh.
- **Active sessions** with where and when each signed in, and a button to
  sign any of them out.
- Font size, undo-send window (5–30 s), and whether `r` means reply or
  reply all.
- Manage labels — rename, colour, nest, hide, delete — on one page.

### Mail

- **Trash and Spam behave properly**: Restore and Delete forever in Trash,
  Not spam in Spam, and Empty now for both. `e`, `#` and `!` follow the
  mailbox you are in.
- **Delivery status on sent mail.** A message that is still queued says so,
  and one that bounced says that with the reason the receiving server gave,
  instead of "Sent" and silence.
- Reply, Reply all, Forward and Print on individual messages.
- Printing a conversation gets its own clean page.
- Attachments preview in place — images, PDFs and text — instead of only
  downloading.
- Full timestamps when you hover a row.

### Running it

- The health check now looks at certificate expiry, disk space, Caddy, and
  Stalwart renewal tasks that have stalled.
- Backups keep 14 by default, can be encrypted with `age`, and can be
  copied off the machine with rclone; there is a systemd timer for them.
- `stalwart-bootstrap.sh` can configure an outbound relay.
- `mailosh setup` prints both DKIM records, not just the RSA one.

## 0.1.3 — 2026-09-06

- Dates and times are shown in your timezone. They were rendered in UTC,
  so "today" changed at 05:30 for readers in India.
- The conversation toolbar has the same actions as the list: spam, star,
  label and move, with their shortcuts.
- Undo toasts pause while you hover or focus them; new mail is announced
  to screen readers; list and search pages have a heading.
- Printing a conversation prints the conversation, not the app shell.
- Unknown URLs get a page instead of raw JSON.
- The Inter font was fetched twice per load and the login page shipped
  40 KB of JavaScript it never used; both fixed. The first live-update
  connection no longer re-fetches the list the server just rendered.
- Avatar initials are readable on every label colour.
- Removed three permanently disabled placeholder buttons.
- `MAILOSH_STALWART_ADMIN_SECRET` now refuses its placeholder, like
  `MAILOSH_SECRET_KEY` already did.

## 0.1.2 — 2026-09-06

- The Docker image now builds its own static assets (vendored JS, icons,
  font, stylesheet), so `docker compose up -d --build` works from a bare
  clone. Previously they had to exist on disk from a `make` run.

## 0.1.1 — 2026-09-06

Bug fixes from a review of the client code.

- `z` after auto-advance undoes the last action again instead of un-reading
  the next conversation; opening a message no longer shows a "Marked as
  read" toast.
- `s` in a conversation can unstar as well as star.
- Archived rows are no longer left as invisible ghosts when another update
  lands mid-animation.
- Returning from a conversation keeps the cursor where you were.
- Sending shortly after typing no longer leaves a stray copy in Drafts;
  save-and-close, discard and pop-out no longer duplicate drafts.
- Attachment upload failures are reported instead of silently dropped.
- Type-to-create in the label picker also applies the new label, and works
  for accounts with fewer than seven labels.
- Signing out in another tab redirects to login immediately.
- The palette keeps its cursor when mail arrives while it is open.
- Only a stale CSRF token shows the "signed out elsewhere" message.

## 0.1.0 — 2026-09-05

First release: reading with sanitised HTML mail, compose with drafts and
attachments, search with Gmail operators, labels, triage with undo, keyboard
shortcuts and a command palette, live updates, light and dark themes, a
production Compose overlay with Caddy, and backup/restore scripts.
