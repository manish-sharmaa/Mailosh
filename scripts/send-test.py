#!/usr/bin/env python3
"""Send one uniquely-subjected test email to the Mailosh demo mailbox, for
exercising the SSE live-refresh pipeline (Task 8) by hand: run this while
`/inbox` is open (or while watching `curl -N http://localhost:8000/events`)
and confirm the new row shows up without a page reload/poll.

Stdlib-only (`smtplib` + `email.message`) — no project dependencies, so it
runs from any Python 3 interpreter, not just this repo's venv:

    python3 scripts/send-test.py

Talks straight to Stalwart's SMTP port (`localhost:2525`, docker-compose's
`2525:25` mapping — the plain MTA/receive port, not the authenticated
submission port on `1587:587`), exactly the way a real external sender
would deliver mail to `demo@mailosh.test`: no auth, no TLS. That's a
deliberate match to this port's job (accepting mail for a domain Stalwart
is configured to own — see `scripts/stalwart-init.sh`'s bootstrap of
`mailosh.test` — the same as any real-world inbound MX, not an
authenticated submission client), not an oversight.
"""

from __future__ import annotations

import smtplib
import sys
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage

HOST = "localhost"
PORT = 2525
SENDER = "sender@example.com"
RECIPIENT = "demo@mailosh.test"


def main() -> int:
    sent_at = datetime.now(UTC)
    subject = f"Mailosh send-test {sent_at.strftime('%Y%m%dT%H%M%SZ')} {uuid.uuid4().hex[:8]}"

    msg = EmailMessage()
    msg["From"] = SENDER
    msg["To"] = RECIPIENT
    msg["Subject"] = subject
    msg.set_content(f"Sent by scripts/send-test.py at {sent_at.isoformat()}.\n")

    try:
        with smtplib.SMTP(HOST, PORT, timeout=10) as smtp:
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1

    print(f"subject: {subject}")
    print(f"sent_at: {sent_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
