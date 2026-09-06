#!/usr/bin/env bash
set -euo pipefail

# Schedule scripts/backup.sh with a systemd timer.
#
# ---------------------------------------------------------------------------
# Why a systemd timer on the host, and not a compose sidecar
# ---------------------------------------------------------------------------
# A backup container would need the Docker socket mounted into it to stop
# and start Stalwart and to `exec` pg_dump -- and a container holding
# /var/run/docker.sock is root on the host, wearing a costume. It would be the
# single most privileged thing in the stack, running around the clock, so
# that it can do ten seconds of work a day. It would also need the compose
# project's own files and .env inside it to resolve the project, which is a
# second copy of state that drifts.
#
# The host already has everything the backup needs: docker access, the
# checkout, the .env, and a scheduler with logging (journald), a catch-up
# mode for a machine that was off at 03:15 (`Persistent=true`), and a
# one-line way to see when it last ran and whether it worked
# (`systemctl list-timers`, `journalctl -u mailosh-backup`). So: a timer.
#
# What this script writes -- and prints first, so you can read it before it
# lands under /etc:
#
#   /etc/systemd/system/mailosh-backup.service   Type=oneshot, runs backup.sh
#   /etc/systemd/system/mailosh-backup.timer     OnCalendar=..., Persistent
#
# Then `systemctl daemon-reload` and `systemctl enable --now` the timer.
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   sudo scripts/backup-timer.sh [--dest DIR] [--keep N] [--encrypt-to RECIPIENT]...
#                                [--rclone-remote REMOTE:PATH] [--calendar SPEC]
#                                [--user USER]
#   scripts/backup-timer.sh --print [same options]     show the units, install nothing
#   sudo scripts/backup-timer.sh --uninstall           stop, disable and remove both units
#
#   --dest DIR         where backups go (default /srv/mailosh-backups). Created
#                      if missing, owned by --user.
#   --keep N           passed through to backup.sh (its default is 14).
#   --encrypt-to R     passed through; repeatable.
#   --rclone-remote R  passed through.
#   --calendar SPEC    systemd OnCalendar= (default '*-*-* 03:15:00', daily at
#                      03:15 local time; see `man systemd.time`).
#   --user USER        the account the backup runs as. It must be able to run
#                      `docker compose` for this stack. Default: the user who
#                      invoked sudo, else root.
#
# COMPOSE_FILE and COMPOSE_PROJECT_NAME, if set when you run this, are baked
# into the unit as Environment= lines, so the timer targets the same stack
# you would from this shell (the production overlay, typically:
# COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml).
#
# After installing:
#   systemctl list-timers mailosh-backup.timer      next and last run
#   systemctl start mailosh-backup.service          run one now, to prove it
#   journalctl -u mailosh-backup.service -n 50      what the last run said
#
# See docs/operations.md, "Running it on a schedule".

usage() {
	sed -n '/^# Usage$/,/^$/p' "$0" | sed 's/^# \{0,1\}//' | grep -v '^-\{10,\}$'
	exit "${1:-0}"
}

log() { printf '%s  %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
die() { printf 'backup-timer.sh: ERROR: %s\n' "$*" >&2; exit 1; }

DEST=/srv/mailosh-backups
KEEP=""
ENCRYPT_ARGS=""
RCLONE_REMOTE=""
CALENDAR="*-*-* 03:15:00"
RUN_AS="${SUDO_USER:-$(id -un)}"
PRINT_ONLY=no
UNINSTALL=no
while [ $# -gt 0 ]; do
	case "$1" in
		-h|--help) usage 0 ;;
		--print) PRINT_ONLY=yes; shift ;;
		--uninstall) UNINSTALL=yes; shift ;;
		--dest)
			[ $# -ge 2 ] || die "--dest needs a directory"
			DEST="$2"; shift 2 ;;
		--dest=*) DEST="${1#--dest=}"; shift ;;
		--keep)
			[ $# -ge 2 ] || die "--keep needs a number"
			KEEP="$2"; shift 2
			case "$KEEP" in (''|*[!0-9]*) die "--keep must be a non-negative integer" ;; esac ;;
		--keep=*)
			KEEP="${1#--keep=}"; shift
			case "$KEEP" in (''|*[!0-9]*) die "--keep must be a non-negative integer" ;; esac ;;
		--encrypt-to)
			[ $# -ge 2 ] || die "--encrypt-to needs an age recipient"
			ENCRYPT_ARGS="$ENCRYPT_ARGS --encrypt-to $2"; shift 2 ;;
		--encrypt-to=*) ENCRYPT_ARGS="$ENCRYPT_ARGS --encrypt-to ${1#--encrypt-to=}"; shift ;;
		--rclone-remote)
			[ $# -ge 2 ] || die "--rclone-remote needs a value"
			RCLONE_REMOTE="$2"; shift 2 ;;
		--rclone-remote=*) RCLONE_REMOTE="${1#--rclone-remote=}"; shift ;;
		--calendar)
			[ $# -ge 2 ] || die "--calendar needs a systemd OnCalendar spec"
			CALENDAR="$2"; shift 2 ;;
		--calendar=*) CALENDAR="${1#--calendar=}"; shift ;;
		--user)
			[ $# -ge 2 ] || die "--user needs a name"
			RUN_AS="$2"; shift 2 ;;
		--user=*) RUN_AS="${1#--user=}"; shift ;;
		-*) die "unknown option '$1' (try --help)" ;;
		*) die "unexpected argument '$1' -- this script takes options only (try --help)" ;;
	esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR=/etc/systemd/system
SERVICE="$UNIT_DIR/mailosh-backup.service"
TIMER="$UNIT_DIR/mailosh-backup.timer"

have_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }

if [ "$UNINSTALL" = yes ]; then
	have_systemd || die "systemd is not running here; nothing to uninstall."
	[ "$(id -u)" -eq 0 ] || die "run with sudo: it removes units under $UNIT_DIR."
	systemctl disable --now mailosh-backup.timer 2>/dev/null || true
	rm -f "$SERVICE" "$TIMER"
	systemctl daemon-reload
	log "removed mailosh-backup.service and mailosh-backup.timer"
	exit 0
fi

case "$DEST" in
	/*) ;;
	*) die "--dest must be an absolute path (a timer has no working directory of yours to be relative to), got '$DEST'." ;;
esac
[ -x "$REPO_ROOT/scripts/backup.sh" ] || die "$REPO_ROOT/scripts/backup.sh is missing or not executable."
# --print may well be run on a laptop to review units meant for a server, so
# the user is only required to exist where the units will actually run.
[ "$PRINT_ONLY" = yes ] || id "$RUN_AS" >/dev/null 2>&1 || die "user '$RUN_AS' does not exist."

EXEC="$REPO_ROOT/scripts/backup.sh $DEST"
[ -z "$KEEP" ] || EXEC="$EXEC --keep $KEEP"
EXEC="$EXEC$ENCRYPT_ARGS"
[ -z "$RCLONE_REMOTE" ] || EXEC="$EXEC --rclone-remote $RCLONE_REMOTE"

ENV_LINES=""
[ -z "${COMPOSE_FILE:-}" ] || ENV_LINES="${ENV_LINES}Environment=COMPOSE_FILE=$COMPOSE_FILE
"
[ -z "${COMPOSE_PROJECT_NAME:-}" ] || ENV_LINES="${ENV_LINES}Environment=COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME
"

# Kept in variables and printed before anything is written, so `--print`
# shows byte-for-byte what an install would put under /etc.
SERVICE_UNIT="[Unit]
Description=Mailosh backup (scripts/backup.sh)
Documentation=file://$REPO_ROOT/docs/operations.md
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$REPO_ROOT
${ENV_LINES}ExecStart=$EXEC
# backup.sh restarts Stalwart itself; killing it mid-run would leave the mail
# server stopped. Give it time, then let the EXIT trap do its job.
TimeoutStartSec=2h
KillMode=mixed
"

TIMER_UNIT="[Unit]
Description=Run the Mailosh backup on a schedule

[Timer]
OnCalendar=$CALENDAR
# A machine that was off at the scheduled time runs the backup at boot
# instead of skipping the day.
Persistent=true
# Spread by up to 10 minutes so two timers on one box do not stop Stalwart
# at the same instant.
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
"

printf '# %s\n%s\n# %s\n%s' "$SERVICE" "$SERVICE_UNIT" "$TIMER" "$TIMER_UNIT"

if [ "$PRINT_ONLY" = yes ]; then
	exit 0
fi
have_systemd || die "systemd is not running here (no /run/systemd/system). The units above are what would be installed; on this machine use cron or launchd, or re-run with --print to just see them."
[ "$(id -u)" -eq 0 ] || die "run with sudo: it writes units under $UNIT_DIR."

install -d -m 0750 -o "$RUN_AS" "$DEST"
printf '%s' "$SERVICE_UNIT" > "$SERVICE"
printf '%s' "$TIMER_UNIT" > "$TIMER"
chmod 0644 "$SERVICE" "$TIMER"
systemctl daemon-reload
systemctl enable --now mailosh-backup.timer
log "installed; next run:"
systemctl list-timers mailosh-backup.timer --no-pager >&2 || true
cat >&2 <<DONE

Prove it before trusting it:
  sudo systemctl start mailosh-backup.service      # one run, now
  journalctl -u mailosh-backup.service -n 50       # what it said
  scripts/restore.sh --check $DEST/mailosh-<stamp>   # the routine verification
DONE
