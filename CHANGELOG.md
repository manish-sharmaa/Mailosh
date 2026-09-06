# Changelog

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
