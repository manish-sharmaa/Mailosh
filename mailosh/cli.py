"""Mailosh CLI (Typer entry point, `pyproject.toml`'s `mailosh = "mailosh.cli:app"`).

`import-mbox` is Task 5's SPK-2 deliverable: a thin synchronous wrapper
around the same upload -> import_email loop
`tests/integration/test_live_stalwart.py` exercises against a live server,
exposed as a manual way to load a real mbox export (e.g. a Google Takeout
archive, which is what SPK-2 itself is scoped for) into a Stalwart account.

Later tasks add more commands to this same `app` (e.g. Task 10's `setup`)
rather than each owning their own Typer instance.
"""

from __future__ import annotations

import asyncio
import mailbox
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from mailosh.config import Settings
from mailosh.jmap.client import JmapClient, find_inbox
from mailosh.stalwart_admin import DkimRecord, StalwartAdmin

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Mailosh command-line tools."""
    # A Typer app with exactly one @app.command() collapses to a bare
    # top-level command (dropping its name) unless a callback is present —
    # this empty one keeps `import-mbox` an explicit subcommand today, and
    # stays correct once Task 10 registers `setup` on this same `app`.


async def _import_mbox(settings: Settings, path: Path, label: str) -> tuple[int, str]:
    """Import every message in the mbox at ``path`` into Inbox + a ``label`` mailbox.

    Returns ``(count, label_mailbox_id)``. Same loop structure as the
    integration test: one ``create_mailbox`` call up front, then per message
    an ``upload`` followed by an ``import_email`` filing it into both Inbox
    and the new label mailbox at once. Unlike the integration test (which
    passes an explicit ``receivedAt`` to verify SPK-2's "is it honoured?"
    question), this passes ``receivedAt=None`` — Stalwart's own fallback,
    since an mbox export's per-message date isn't parsed here; see SPK-2 in
    `docs/spikes/p0-findings.md`.

    Takes an already-built ``settings`` (rather than constructing its own
    ``Settings()``) so ``import_mbox`` below can validate
    ``demo_user``/``demo_password`` are actually set — both are optional on
    ``Settings`` itself (Task 1 brief: Phase 1 replaces the shared demo
    account with real per-user login, so ``Settings()`` must not hard-require
    them) — and print a clear error before ever reaching this function,
    rather than this coroutine failing deep inside ``JmapClient.connect``
    with ``None`` standing in for a username/password.
    """
    client = await JmapClient.connect(
        settings.stalwart_url, settings.demo_user, settings.demo_password
    )
    try:
        mailboxes = await client.get_mailboxes()
        inbox = find_inbox(mailboxes)
        # Reuse the label mailbox when it already exists. Creating it
        # unconditionally made this command single-use: a second
        # `import-mbox --label Work` died on Stalwart's `alreadyExists`,
        # which is a poor way to learn that importing twice is a normal
        # thing to want (seeding a demo account, or adding a second mbox
        # under the same label).
        existing = next((m for m in mailboxes if m.name == label and m.role is None), None)
        label_id = existing.id if existing is not None else await client.create_mailbox(label)
        count = 0
        for msg in mailbox.mbox(str(path)):
            blob = await client.upload(bytes(msg), "message/rfc822")
            await client.import_email(blob, {inbox.id, label_id}, set(), None)
            count += 1
        return count, label_id
    finally:
        await client.close()


@app.command("import-mbox")
def import_mbox(
    path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Path to an mbox file."),
    ],
    label: Annotated[
        str,
        typer.Option(
            "--label", help="Mailbox/label to file every imported message under, alongside Inbox."
        ),
    ],
) -> None:
    """Import every message in PATH into Inbox and a --label mailbox."""
    settings = Settings()
    if settings.demo_user is None or settings.demo_password is None:
        typer.echo(
            "MAILOSH_DEMO_USER and MAILOSH_DEMO_PASSWORD must both be set (e.g. in .env) "
            "to run import-mbox.",
            err=True,
        )
        raise typer.Exit(code=1)
    count, label_id = asyncio.run(_import_mbox(settings, path, label))
    typer.echo(f"Imported {count} message(s) from {path} into Inbox + {label!r} ({label_id})")


def _display_name_from_email(email: str) -> str:
    """Derive a human display name from an email address's local part.

    E.g. ``"admin@x"`` -> ``"Admin"``, ``"jane.doe@x"`` -> ``"Jane Doe"``.
    The `setup` command's signature (Task 10's controller-resolved
    requirements: ``--domain X --email Y [--password P]``) has no dedicated
    ``--name``/``--display-name`` flag, so this is a best-effort default
    rather than something a caller can override — reasonable for a P0
    bootstrap wizard, where the admin/demo account's exact display name is
    cosmetic (see `StalwartAdmin.create_account`'s own docstring for where
    this ends up on the server: `x:UserAccount.description`, the only
    free-text field the schema actually offers).
    """
    local_part = email.split("@", 1)[0]
    return local_part.replace(".", " ").replace("_", " ").title()


def _format_dns_block(
    domain: str, dkim: DkimRecord | Sequence[DkimRecord], relay_host: str | None = None
) -> str:
    """Render the copy-paste DNS records block for `domain` (design spec §11):
    the MX target's own A/AAAA, MX, SPF, the live DKIM record(s), and DMARC,
    plus a one-line rDNS/PTR reminder. Plain text, no markup, one record's
    Host/Value per stanza — each Value line is meant to be pasted directly
    into a DNS provider's record-editor UI (or a zone file) without further
    editing.

    `dkim` is every record the server signs with -- Stalwart generates an
    RSA and an Ed25519 key per domain and signs with both, so both must be
    published or half of every message's signatures fail to verify
    (`StalwartAdmin.get_dkim_records`). A single `DkimRecord` is still
    accepted, for a server with one key.

    `relay_host` is the smarthost the server routes outbound mail through
    (`StalwartAdmin.outbound_relay_host`), or `None` for direct delivery.
    It decides which SPF record is printed as *the* value -- see the SPF
    paragraph below -- rather than leaving the operator to work out which of
    two applies.

    The A/AAAA stanza comes first, and it is first because it is the record
    an operator is most likely to forget: the MX below points at
    ``mail.<domain>``, and if that name does not resolve, every sending MTA
    gets an unusable MX target and the mail simply never arrives. It fails
    silently on this end — nothing connects, so nothing is logged here —
    which makes it the most expensive omission in the block. This function
    cannot know the server's public address, so the value is an explicit
    placeholder rather than a plausible-looking guess.

    That stanza spends most of its length warning *against* the AAAA half,
    which is not a hedge: the block used to say "AAAA, if you have one", and
    a cloud VPS always has one. Publishing it is only correct if IPv6 reaches
    the containers, and under the `docker compose` + ufw layout
    ``docs/hosting.md`` describes it does not — Docker writes `iptables`
    rules and not `ip6tables` ones, so IPv4 bypasses ufw to reach the
    published ports while IPv6 is dropped by ufw's INPUT policy. The result
    answers ping6 with every port black-holed, which is a worse failure than
    omitting the record: a missing AAAA makes senders use IPv4 immediately,
    while an unroutable one makes them wait out a connection timeout first,
    so inbound mail is merely *slow* and nothing anywhere reports an error.
    Found on this project's own production deployment, where it cost ~250 ms
    on every new browser connection to the webmail host (0.65 s TTFB against
    0.14 s once the record was removed) on top of whatever it was costing
    inbound mail. The `nc -6` checks are printed because "do you have IPv6"
    is the wrong question and "does IPv6 answer on port 25" is the right one.

    The SPF stanza carries the relay variant alongside the direct-send
    record. ``v=spf1 mx ~all`` authorises the MX host's own addresses, which
    is correct only when this server delivers outbound mail itself on port
    25. ``docs/hosting.md`` recommends relay mode (SES, SMTP2GO, …) for most
    deployments — most budget VPS providers block outbound 25 — and under a
    relay the sending IP is the relay's, not the MX's, so the ``mx``
    mechanism does not cover it and outbound mail fails SPF at the receiver.
    Printing only the direct-send record would hand the operator a record
    that is wrong for the deployment this project's own hosting doc
    recommends.
    """
    records = [dkim] if isinstance(dkim, DkimRecord) else list(dkim)
    mx_target = f"mail.{domain}"
    dmarc_host = f"_dmarc.{domain}"
    heading = f"DNS records for {domain}"
    ptr_reminder = (
        f"Reminder: also configure a reverse DNS (PTR) record for {mx_target} pointing at "
        "this server's public IP address — most receiving mail servers treat "
        "missing/mismatched rDNS as a strong spam signal."
    )
    if relay_host is None:
        spf = [
            "SPF (TXT record)",
            f"  Host:  {domain}",
            "  Value: v=spf1 mx ~all",
            "",
            "  That value is for DIRECT SEND — this server delivering outbound mail",
            "  itself on port 25. If you send through a RELAY (Amazon SES, SMTP2GO,",
            "  your provider's smarthost), the sending IP is the relay's and is not",
            "  covered by `mx`, so outbound mail fails SPF at the receiver. Use the",
            "  relay's own include instead, e.g.",
            "      Amazon SES   v=spf1 include:amazonses.com ~all",
            "      SMTP2GO      v=spf1 include:spf.smtp2go.com ~all",
            "  Check your relay's documentation for its exact include, and see",
            "  docs/hosting.md — most budget VPS providers block outbound port 25,",
            "  and relay mode is what it recommends for most deployments.",
        ]
    else:
        spf = [
            "SPF (TXT record)",
            f"  Host:  {domain}",
            "  Value: v=spf1 include:<your relay's SPF include> ~all",
            "",
            f"  This server RELAYS outbound mail through {relay_host}, so the",
            "  sending IP is the relay's, not this server's — `v=spf1 mx ~all`",
            "  would fail SPF at every receiver. Use the relay's own include, e.g.",
            "      Amazon SES   v=spf1 include:amazonses.com ~all",
            "      SMTP2GO      v=spf1 include:spf.smtp2go.com ~all",
            "  Check your relay's documentation for its exact include.",
        ]
    dkim_lines: list[str] = []
    if len(records) > 1:
        dkim_lines += [
            f"DKIM (TXT records — {len(records)} of them)",
            "  Stalwart signs every message with BOTH keys; publish both, or half of",
            "  each message's signatures fail to verify.",
        ]
    else:
        dkim_lines.append("DKIM (TXT record)")
    for i, rec in enumerate(records):
        if i:
            dkim_lines.append("")
        dkim_lines += [f"  Host:  {rec.host}", f"  Value: {rec.value}"]
    return "\n".join(
        [
            heading,
            "=" * len(heading),
            "",
            "A / AAAA record  (create this first — the MX below points at it,",
            "                  and mail never arrives if the name does not resolve)",
            f"  Host:  {mx_target}",
            "  Value: <this server's public IPv4 address>        (A)",
            "         <this server's public IPv6 address>        (AAAA — read below first)",
            "",
            "  Publish the AAAA only if this server actually ANSWERS on IPv6. Having",
            "  an IPv6 address is not the same thing: Docker manages `iptables` but",
            "  not `ip6tables`, so with the ufw setup docs/hosting.md describes, IPv4",
            "  reaches the containers while IPv6 stops at ufw's default-DROP INPUT",
            "  chain. The host answers ping6 and every port is dead.",
            "",
            "  An AAAA pointing at ports nothing answers on is worse than no AAAA.",
            "  Senders that prefer IPv6 — Google does — connect, wait for the",
            "  timeout, and only then retry over IPv4, so inbound mail is delayed",
            "  rather than lost, which is why this goes unnoticed for months. Browsers",
            "  pay it too, stalling on each new connection to the webmail host.",
            "",
            "  Check from another machine before publishing, and again after:",
            "      nc -6 -z -v <this server's public IPv6 address> 25",
            "      nc -6 -z -v <this server's public IPv6 address> 443",
            "  If either times out, leave the AAAA off. IPv4-only is a correct,",
            "  fully-supported deployment; a black-holed AAAA is not.",
            "",
            "MX record",
            f"  Host:     {domain}",
            "  Priority: 10",
            f"  Value:    {mx_target}",
            "",
            *spf,
            "",
            *dkim_lines,
            "",
            "DMARC (TXT record)",
            f"  Host:  {dmarc_host}",
            f"  Value: v=DMARC1; p=none; rua=mailto:dmarc@{domain}",
            "",
            ptr_reminder,
        ]
    )


#: Printed by `setup` whenever `create_account` reports the account already
#: existed (its return value is `False`) -- whether `--password` was given
#: explicitly or generated, that password was NEVER applied in this case
#: (see `StalwartAdmin.create_account`'s own docstring: a pre-existing
#: account is left exactly as it was), so a "Generated password: ..." line
#: would misrepresent it as the account's real, current password. No
#: password-reset command exists yet in this P0 wizard skeleton -- flagged
#: here rather than silently implying one.
_ACCOUNT_EXISTED_WARNING = (
    "Account already existed — the password was NOT changed. Use --password "
    "plus a future password-reset command to set one."
)


async def _setup(
    domain: str, email: str, display_name: str, password: str
) -> tuple[list[DkimRecord], bool, str | None]:
    """Drive `StalwartAdmin.create_domain` -> `create_account` ->
    `get_dkim_records` -> `outbound_relay_host`, in that order (SPK-5:
    `create_account` depends on the domain already existing to resolve its
    `domainId`), against the admin credential from `Settings()`.
    Deliberately does NOT call `try_mint_user_token` — that's a standalone
    SPK-3 probe (see `mailosh.stalwart_admin` and SPK-3 in
    `docs/spikes/p0-findings.md`), not a step this wizard needs.

    Returns `(dkim_records, account_created, relay_host)` —
    `account_created` is `create_account`'s own return value, threaded back
    out so `setup` can decide whether the `password` it's holding was
    actually applied to the account (see `_ACCOUNT_EXISTED_WARNING`) before
    deciding what to print; `relay_host` picks the SPF record.
    """
    settings = Settings()
    admin = StalwartAdmin(
        settings.stalwart_url, settings.stalwart_admin_user, settings.stalwart_admin_secret
    )
    try:
        await admin.create_domain(domain)
        account_created = await admin.create_account(email, display_name, password)
        dkim = await admin.get_dkim_records(domain)
        relay_host = await admin.outbound_relay_host()
        return dkim, account_created, relay_host
    finally:
        await admin.close()


@app.command("setup")
def setup(
    domain: Annotated[
        str, typer.Option("--domain", help="Mail domain to create, e.g. example.com.")
    ],
    email: Annotated[
        str,
        typer.Option(
            "--email", help="Admin mailbox to create on that domain, e.g. admin@example.com."
        ),
    ],
    password: Annotated[
        str | None,
        typer.Option("--password", help="Account password. Generated and printed if omitted."),
    ] = None,
) -> None:
    """Bootstrap a mail domain + admin account and print the DNS records to publish.

    Task 10 (SPK-5 close-out) wizard-skeleton command: `x:Domain/set create`
    + `x:Account/set create` + `x:DkimSignature` read, then a plain-text
    MX/SPF/DKIM/DMARC block (design spec §11) ready to copy into whatever
    DNS is authoritative for `domain`.
    """
    password_was_generated = password is None
    if password is None:
        password = secrets.token_urlsafe(18)
    display_name = _display_name_from_email(email)
    dkim, account_created, relay_host = asyncio.run(_setup(domain, email, display_name, password))

    typer.echo(f"Domain {domain!r} ready; account {email!r} ready.")
    if account_created:
        if password_was_generated:
            typer.echo(f"Generated password for {email}: {password}")
    else:
        # Covers both sub-cases the same way: --password was given but
        # ignored, or a password was generated but never applied — either
        # way `password` here is NOT this account's real password.
        typer.echo(_ACCOUNT_EXISTED_WARNING)
    typer.echo("")
    typer.echo(_format_dns_block(domain, dkim, relay_host))


if __name__ == "__main__":
    app()
