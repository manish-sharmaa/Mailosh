"""Guards on the operations scripts (`scripts/*.sh`) -- the shell is not
importable, so these pin the load-bearing strings the way
`tests/unit/test_stalwart_admin.py`'s bootstrap-script tests already do:
each assertion ties a documented behaviour to the exact text that
implements it, so a refactor that drops one has to touch this file too.

Every object shape asserted here was read from the running Stalwart
v0.16.20 server's own `GET /api/schema` and then written and read back
live (docs/operations.md, "Relay mode"); nothing is inferred from the
Stalwart documentation alone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"


def _read(name: str) -> str:
    return (_SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    ["healthcheck.sh", "backup.sh", "restore.sh", "backup-timer.sh", "stalwart-bootstrap.sh"],
)
def test_script_parses(name: str) -> None:
    """`bash -n` -- the cheapest check there is, and the one that catches a
    heredoc left open by an edit."""
    subprocess.run(["bash", "-n", str(_SCRIPTS / name)], check=True)


# ---------------------------------------------------------------------------
# stalwart-bootstrap.sh: relay mode
# ---------------------------------------------------------------------------


def test_bootstrap_relay_creates_the_three_objects_the_schema_names() -> None:
    """`x:MtaRoute` @type Relay, an `x:MtaTlsStrategy` with TLS *required*,
    and the `x:MtaOutboundStrategy` singleton's `route`/`tls` leaves pointed
    at it -- with the `match` list written as the index-keyed map the
    server returns it as. Then a ReloadSettings action, since none of
    these needs a rebind."""
    script = _read("stalwart-bootstrap.sh")
    assert '"@type": "Relay"' in script
    assert 'obj["authSecret"] = {"@type": "Value", "secret": secret}' in script
    assert '"startTls":"require","dane":"disable","mtaSts":"disable"' in script
    assert '"mtaSts":"disable","allowInvalidCerts":false' in script
    assert r"\"else\":\"'$RELAY_ROUTE'\"" in script
    assert r"\"match\":{\"0\":{\"if\":\"is_local_domain(rcpt_domain)\"" in script
    assert '\'"create":{"reload":{"@type":"ReloadSettings"}}\'' in script
    # Every write is checked from the response body, never from HTTP 200.
    assert script.count("extract set-any") >= 4


def test_bootstrap_relay_password_never_reaches_argv() -> None:
    """The password is read from a file inside the python that builds the
    JSON, and the whole request body travels in curl's stdin config
    (`data-binary = "..."`), not as a `--data-binary` argument -- `docker
    exec`'s argv is visible in `ps` on the host."""
    script = _read("stalwart-bootstrap.sh")
    assert "--relay-password-file" in script
    # No `--relay-password VALUE` option exists; asking for one is refused.
    assert "--relay-password|--relay-password=*)" in script
    assert '--data-binary "$1"' not in script
    assert 'printf \'data-binary = "%s"\\n\' "$(cfg_escape "$1")"' in script
    # The python reads the secret from the path in argv, never the value.
    assert 'with open(pwfile, encoding="utf-8") as f:' in script


def test_bootstrap_relay_is_idempotent_and_reported_by_verify() -> None:
    script = _read("stalwart-bootstrap.sh")
    # create-or-update on the route and the strategy
    assert '{\\"$rid\\":{$props}}' in script
    assert "outbound strategy already routes remote mail via" in script
    # verify() item 9 reads it back and fails a mismatch against the flags
    assert 'ok "outbound delivery: $rmode"' in script
    assert 'bad "outbound delivery is $rmode' in script
    # --verify-only never calls ensure_relay
    assert re.search(r'\[ "\$VERIFY_ONLY" = no \]; then\n(.*\n){0,8}.*ensure_relay', script)


# ---------------------------------------------------------------------------
# backup.sh / restore.sh
# ---------------------------------------------------------------------------


def test_backup_defaults_to_keep_14_and_prunes_both_shapes() -> None:
    script = _read("backup.sh")
    assert "KEEP=14" in script
    assert "-name 'mailosh-????????T??????Z.tar.age'" in script
    assert "printf '\\n%s\\n' \"Next: verify it.  $VERIFY_CMD\"" in script
    assert 'VERIFY_CMD="scripts/restore.sh --check $OUT"' in script
    assert 'VERIFY_CMD="scripts/restore.sh --check --identity KEYFILE $ARCHIVE"' in script


def test_backup_refuses_a_secret_key_as_recipient() -> None:
    assert "-r AGE-SECRET-KEY-" in _read("backup.sh")


def test_restore_decrypts_into_a_private_tempdir_removed_on_exit() -> None:
    script = _read("restore.sh")
    assert 'mktemp -d "${TMPDIR:-/tmp}/mailosh-restore.XXXXXX"' in script
    assert "trap 'rm -rf \"$WORK\"' EXIT" in script
    assert 'age -d -i "$IDENTITY" "$BACKUP_ARG" | tar -xf - -C "$WORK"' in script


# ---------------------------------------------------------------------------
# healthcheck.sh
# ---------------------------------------------------------------------------


def test_healthcheck_thresholds_and_secret_handling() -> None:
    script = _read("healthcheck.sh")
    assert "TLS_WARN_DAYS=21" in script and "TLS_FAIL_DAYS=7" in script
    assert "DISK_WARN_FREE=20" in script and "DISK_FAIL_FREE=10" in script
    assert "ACME_LATE_MINUTES=15" in script
    assert "rcgen self signed" in script
    # The admin credential is read from the mailosh container's own
    # environment inside the probe, and the probe program goes on stdin.
    assert 'secret = os.environ.get("MAILOSH_STALWART_ADMIN_SECRET")' in script
    assert "docker compose exec -T mailosh python3 - " in script
    assert "MAILOSH_STALWART_ADMIN_SECRET=" not in script
