#!/usr/bin/env bash
set -euo pipefail

# Mailosh backup: both stores, in a state that restores.
#
# ---------------------------------------------------------------------------
# The one thing to understand before reading any further
# ---------------------------------------------------------------------------
# Mailosh keeps its data in two completely different places, and they are not
# equally precious:
#
#   * Stalwart's volumes hold THE ACTUAL MAIL. Every message, every mailbox,
#     every account and password, and Stalwart's own configuration (it stores
#     its settings inside the same store, not in a config file -- see
#     /etc/stalwart/config.json, which is only a 128-byte pointer at the
#     store). Lose these and the mail is gone permanently. There is nothing
#     to re-fetch it from.
#
#   * Postgres holds APPLICATION STATE ONLY: app_user, session, label_meta,
#     ui_pref, contact, image_sender_allow, sender_pref, login_attempt,
#     audit_log (mailosh/db/models.py is the complete list). No message
#     bodies, no headers, no attachments -- not one byte of mail. Lose it and
#     everyone signs in again and their UI preferences reset. Annoying. Not
#     a disaster.
#
# An operator who backs up only Postgres has backed up the preferences and
# lost the mail. This script always does both.
#
# ---------------------------------------------------------------------------
# Why Stalwart is stopped for a few seconds and Postgres is not
# ---------------------------------------------------------------------------
# Postgres: `pg_dump` takes its dump inside a single repeatable-read
# transaction, so a dump taken while the app is writing is internally
# coherent by construction. No downtime, and no reason for any.
#
# Stalwart: the store is RocksDB (an LSM tree -- SST files, a write-ahead
# LOG, a MANIFEST and a CURRENT pointer that must agree with each other).
# Copying that directory out from under a live writer captures those files at
# different instants and can produce a store that will not open. There is no
# online-snapshot switch exposed for it, and Stalwart's own logical exporter
# cannot help either, because it wants the same exclusive lock the running
# server is holding. Verified directly against this stack rather than
# assumed -- `stalwart --export` inside the running container answers:
#
#     Startup failed: Failed to open database: Error { message: "IO error:
#     While lock file: /var/lib/stalwart//LOCK: Resource temporarily
#     unavailable" }
#
# So the honest options are "stop it briefly" or "produce an archive that
# may silently not restore". This script stops it briefly. The window covers
# only the tar of the volumes (a few seconds on a small store), the Postgres
# dump having already been taken while everything was still up, and an EXIT
# trap starts Stalwart again even if the backup fails halfway or you Ctrl-C
# it. If Stalwart was already stopped when you invoked this, it is left
# stopped.
#
# One consequence, stated plainly rather than glossed over: the two halves
# are NOT a single atomic point in time. Postgres is captured first, Stalwart
# a few seconds later. The only value that crosses the two stores is the
# per-session Stalwart API key (session.api_key_secret_enc in Postgres, the
# key object itself in Stalwart), and both skew directions are harmless: the
# worst case is a session that has to sign in again. No mail is affected.
#
# ---------------------------------------------------------------------------
# What is deliberately NOT in the backup
# ---------------------------------------------------------------------------
# `.env`. It holds MAILOSH_SECRET_KEY and MAILOSH_STALWART_ADMIN_SECRET, and
# a backup archive that carries live credentials is a much bigger problem
# than the one it solves -- backups get copied to laptops, object stores and
# other people's machines. Keep .env somewhere a backup does not reach (a
# password manager). Nothing in it is needed to recover mail: losing it costs
# everyone one re-login (the Fernet key derived from MAILOSH_SECRET_KEY is
# what decrypts stored session keys) and nothing else.
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   scripts/backup.sh [DEST_DIR] [--keep N]
#
#   DEST_DIR    where to write (default: ./backups, or $MAILOSH_BACKUP_DIR).
#   --keep N    after a successful backup, delete all but the N newest
#               backups in DEST_DIR. Default 0 = never delete anything.
#
# Which stack it acts on is chosen with Compose's own environment variables,
# not with flags of our own:
#
#   COMPOSE_PROJECT_NAME=other COMPOSE_FILE=/path/other.yml scripts/backup.sh
#
# Restore with scripts/restore.sh. See docs/operations.md.

usage() {
	sed -n '/^# Usage$/,/^$/p' "$0" | sed 's/^# \{0,1\}//' | grep -v '^-\{10,\}$'
	exit "${1:-0}"
}

log()  { printf '%s  %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
die()  { printf 'backup.sh: ERROR: %s\n' "$*" >&2; exit 1; }

need_cmd() {
	command -v "$1" >/dev/null 2>&1 || die "$1 is required but not on PATH. $2"
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
DEST="${MAILOSH_BACKUP_DIR:-}"
KEEP=0
while [ $# -gt 0 ]; do
	case "$1" in
		-h|--help) usage 0 ;;
		--keep)
			[ $# -ge 2 ] || die "--keep needs a number"
			KEEP="$2"; shift 2
			case "$KEEP" in (''|*[!0-9]*) die "--keep must be a non-negative integer, got '$KEEP'" ;; esac
			;;
		--keep=*)
			KEEP="${1#--keep=}"; shift
			case "$KEEP" in (''|*[!0-9]*) die "--keep must be a non-negative integer" ;; esac
			;;
		-*) die "unknown option '$1' (try --help)" ;;
		*)
			[ -z "$DEST" ] || die "more than one destination given ('$DEST' and '$1')"
			DEST="$1"; shift
			;;
	esac
done

# Run from the repo root so a relative COMPOSE_FILE (the default
# docker-compose.yml) resolves no matter where the script was invoked from.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
DEST="${DEST:-$REPO_ROOT/backups}"

# ---------------------------------------------------------------------------
# Preflight. Everything that can be checked before writing a byte is checked
# here, so a missing prerequisite fails immediately instead of leaving a
# half-written backup directory that looks plausible.
# ---------------------------------------------------------------------------
need_cmd docker "Install Docker Desktop or the docker engine."
need_cmd gzip   "It is part of every base system; check your PATH."
need_cmd python3 "It reads the project name out of 'docker compose config --format json'."

docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is required; 'docker-compose' v1 is not supported."
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable. Start Docker and retry."

# sha256: coreutils on Linux, shasum on macOS. One of the two must exist --
# a backup with no checksums is a backup you cannot tell has rotted.
if command -v sha256sum >/dev/null 2>&1; then
	SHA256() { sha256sum "$@"; }
elif command -v shasum >/dev/null 2>&1; then
	SHA256() { shasum -a 256 "$@"; }
else
	die "neither sha256sum nor shasum found; cannot checksum the backup."
fi

docker compose config -q 2>/dev/null \
	|| die "'docker compose config' failed here. Wrong directory, or COMPOSE_FILE points somewhere unreadable?"

PROJECT="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
[ -n "$PROJECT" ] || die "could not determine the compose project name."

SERVICES="$(docker compose config --services)"
for svc in postgres stalwart; do
	printf '%s\n' "$SERVICES" | grep -qx "$svc" \
		|| die "compose project '$PROJECT' has no '$svc' service. This script backs up Mailosh's own stack."
done

# The helper image is used for `tar`/`gzip` against the raw volumes. Default
# to the Postgres image the stack already pulls, so a backup never needs to
# reach the network for an image that is not here yet -- which is exactly the
# moment (disk dying, network flaky) you least want a pull.
HELPER_IMAGE="${MAILOSH_BACKUP_HELPER_IMAGE:-postgres:16-alpine}"
docker image inspect "$HELPER_IMAGE" >/dev/null 2>&1 \
	|| die "helper image '$HELPER_IMAGE' is not present locally. Run: docker pull $HELPER_IMAGE  (or set MAILOSH_BACKUP_HELPER_IMAGE)"

PG_USER="${MAILOSH_PG_USER:-mailosh}"
PG_DB="${MAILOSH_PG_DB:-mailosh}"

# Postgres must be up: pg_dump is the only coherent way to read it, and it
# needs a running server. Backing up a stopped stack is not supported -- say
# so now rather than producing something misleading.
PG_CID="$(docker compose ps -q postgres || true)"
[ -n "$PG_CID" ] || die "the 'postgres' service is not running in project '$PROJECT'. Start the stack (make up) and retry."
docker compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
	|| die "postgres is running but not accepting connections as user '$PG_USER' on database '$PG_DB'."

# Volume names. Compose labels every volume it creates, so ask Docker what
# the names really are rather than assuming "${PROJECT}_${name}" -- that
# assumption breaks the moment a volume is declared external or renamed.
volume_name() {
	local logical="$1" found
	found="$(docker volume ls -q \
		--filter "label=com.docker.compose.project=$PROJECT" \
		--filter "label=com.docker.compose.volume=$logical")"
	[ -n "$found" ] || die "volume '$logical' of project '$PROJECT' does not exist. Has this stack ever been started?"
	printf '%s\n' "$found" | head -1
}

VOL_STALWART_DATA="$(volume_name stalwart-data)"
VOL_STALWART_ETC="$(volume_name stalwart-etc)"

# Free-space check, deliberately pessimistic: require room for the
# *uncompressed* source. gzip will use much less, and running out of disk
# midway through a backup is a failure mode worth spending a few seconds to
# rule out.
vol_kb() {
	local kb
	kb="$(docker run --rm -v "$1":/src:ro "$HELPER_IMAGE" du -sk /src 2>/dev/null | awk '{print $1}')"
	case "$kb" in (''|*[!0-9]*) kb=0 ;; esac
	printf '%s\n' "$kb"
}
mkdir -p "$DEST" || die "cannot create destination directory '$DEST'"
[ -w "$DEST" ] || die "destination '$DEST' is not writable"

# If the destination lives inside a git checkout (the default ./backups
# does), drop a self-ignoring .gitignore in it the first time. Backups are
# large, contain a copy of everyone's mail, and are exactly the sort of
# thing that gets committed by accident on a hurried `git add -A`. A
# `.gitignore` containing `*` also ignores itself, so this leaves `git
# status` clean rather than trading one stray file for another.
if [ ! -e "$DEST/.gitignore" ] && git -C "$DEST" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	printf '*\n' > "$DEST/.gitignore"
	log "wrote $DEST/.gitignore (backups must never be committed)"
fi

PG_BYTES="$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -At -c "select pg_database_size('$PG_DB')" 2>/dev/null | tr -d '\r')"
case "$PG_BYTES" in (''|*[!0-9]*) PG_BYTES=0 ;; esac
NEED_KB=$(( $(vol_kb "$VOL_STALWART_DATA") + $(vol_kb "$VOL_STALWART_ETC") + PG_BYTES / 1024 + 1024 ))
AVAIL_KB="$(df -Pk "$DEST" | awk 'NR==2 {print $4}')"
[ "$AVAIL_KB" -gt "$NEED_KB" ] \
	|| die "not enough free space at '$DEST': need ~${NEED_KB}KB (uncompressed source), have ${AVAIL_KB}KB."

# ---------------------------------------------------------------------------
# Where this backup goes. Written into <name>.partial and renamed only once
# every component is present and checksummed, so an interrupted run can never
# be mistaken for a complete backup by restore.sh or by a human.
# ---------------------------------------------------------------------------
STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
NAME="mailosh-$STAMP"
OUT="$DEST/$NAME"
PARTIAL="$OUT.partial"
[ ! -e "$OUT" ] || die "'$OUT' already exists."
rm -rf "$PARTIAL"
mkdir -p "$PARTIAL"

cleanup_failed() {
	local rc=$?
	if [ $rc -ne 0 ] && [ -d "$PARTIAL" ]; then
		log "backup FAILED (exit $rc); leaving the incomplete directory at $PARTIAL for inspection"
	fi
}

# Stalwart must come back up whatever happens below -- a crashed backup that
# leaves the mail server down is worse than no backup.
STALWART_WAS_RUNNING=no
STALWART_STOPPED_BY_US=no
restart_stalwart() {
	if [ "$STALWART_STOPPED_BY_US" = yes ]; then
		log "restarting stalwart"
		docker compose start stalwart >/dev/null 2>&1 || log "WARNING: could not restart stalwart -- start it by hand: docker compose start stalwart"
		STALWART_STOPPED_BY_US=no
	fi
}
trap 'restart_stalwart; cleanup_failed' EXIT
trap 'exit 130' INT TERM

# ---------------------------------------------------------------------------
# 1. Postgres, online.
# ---------------------------------------------------------------------------
# Plain SQL rather than pg_dump's custom format on purpose: it restores with
# psql alone (no pg_restore, no version-matching dance), it is greppable when
# something looks wrong, and at this size the difference in speed is
# invisible. --no-owner/--no-privileges so the dump restores cleanly into a
# database owned by whatever role the target happens to use. The dump
# includes alembic_version, so a restored database comes back at exactly the
# migration revision it was taken at.
log "dumping postgres database '$PG_DB' (online, no downtime)"
docker compose exec -T postgres \
	pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner --no-privileges --clean --if-exists \
	| gzip -9 > "$PARTIAL/postgres.sql.gz" \
	|| die "pg_dump failed."

# pg_dump writes its trailer last, so its presence is a real end-to-end
# check that the dump is complete and the gzip stream is intact -- not just
# that some bytes arrived.
# `tail -20`, not `tail -3`: pg_dump 16.15 (and every build carrying the
# CVE-2025-8714 fix) wraps its output in psql restricted mode, so the very
# last lines of the file are a `\unrestrict <token>` and a blank -- the
# completion marker sits several lines above them, not last. Found by this
# check failing on a dump that was in fact perfectly complete.
#
# Capturing into a variable rather than piping straight into `grep -q`:
# with `set -o pipefail` a `-q` that exits on its first match SIGPIPEs the
# upstream gzip, and the pipeline then reports failure even though the
# marker was found.
PG_TAIL="$(gzip -dc "$PARTIAL/postgres.sql.gz" | tail -20)"
printf '%s\n' "$PG_TAIL" | grep -q 'PostgreSQL database dump complete' \
	|| die "the Postgres dump has no completion marker -- it is truncated. Refusing to call this a backup."
PG_TABLES="$(gzip -dc "$PARTIAL/postgres.sql.gz" | grep -c '^CREATE TABLE ' || true)"
[ "${PG_TABLES:-0}" -ge 1 ] || die "the Postgres dump contains no tables. Refusing to call this a backup."
log "postgres dump ok ($PG_TABLES tables, $(du -h "$PARTIAL/postgres.sql.gz" | awk '{print $1}'))"

# ---------------------------------------------------------------------------
# 2. Stalwart, stopped. This is the mail.
# ---------------------------------------------------------------------------
STALWART_CID="$(docker compose ps -aq stalwart || true)"
if [ -n "$STALWART_CID" ] && [ "$(docker inspect -f '{{.State.Running}}' "$STALWART_CID" 2>/dev/null)" = "true" ]; then
	STALWART_WAS_RUNNING=yes
fi

if [ "$STALWART_WAS_RUNNING" = yes ]; then
	log "stopping stalwart (RocksDB cannot be copied safely from under a live writer)"
	STALWART_STOPPED_BY_US=yes
	docker compose stop stalwart >/dev/null || die "could not stop stalwart."
else
	log "stalwart is already stopped; backing up its volumes as they are and leaving it stopped"
fi

tar_volume() {
	local vol="$1" out="$2"
	docker run --rm -v "$vol":/src:ro "$HELPER_IMAGE" \
		tar -czf - -C /src . > "$out" \
		|| die "could not archive volume '$vol'."
	gzip -t "$out" || die "archive '$out' is not a valid gzip stream."
}

log "archiving $VOL_STALWART_DATA (the mail store)"
tar_volume "$VOL_STALWART_DATA" "$PARTIAL/stalwart-data.tar.gz"
log "archiving $VOL_STALWART_ETC (the store pointer)"
tar_volume "$VOL_STALWART_ETC" "$PARTIAL/stalwart-etc.tar.gz"

# A RocksDB store that opens has to contain CURRENT (the pointer at the live
# MANIFEST). An archive of a non-empty store that lacks it is a torn capture,
# and better to find that out here than at 3am.
#
# The one legitimate exception is a Stalwart that has never been through
# initial setup: its volumes are genuinely, completely empty (verified --
# a fresh container logs "Server started in bootstrap mode ... No
# configuration file was found" and writes nothing to either volume until
# scripts/stalwart-init.sh completes x:Bootstrap/set). That is a warning,
# not an error: there is no mail to lose yet, but an operator running
# backups against a stack they believe is in service should be told.
STALWART_ENTRIES="$(docker run --rm -i "$HELPER_IMAGE" tar -tzf - < "$PARTIAL/stalwart-data.tar.gz" | grep -vc '^\./$' || true)"
HAS_CURRENT="$(docker run --rm -i "$HELPER_IMAGE" tar -tzf - < "$PARTIAL/stalwart-data.tar.gz" | grep -c '^\./CURRENT$' || true)"
if [ "${STALWART_ENTRIES:-0}" -eq 0 ]; then
	log "WARNING: $VOL_STALWART_DATA is EMPTY -- this Stalwart has never completed initial setup,"
	log "         so this backup contains no mail. If that is a surprise, run scripts/stalwart-init.sh."
elif [ "${HAS_CURRENT:-0}" -lt 1 ]; then
	die "stalwart-data archive has $STALWART_ENTRIES entries but no ./CURRENT -- that is not a RocksDB store that will open."
fi

restart_stalwart

# Wait for the mail server to be serving again before declaring success, so a
# backup run never quietly leaves mail delivery down.
if [ "$STALWART_WAS_RUNNING" = yes ]; then
	CID="$(docker compose ps -aq stalwart || true)"
	state=unknown
	if [ -n "$CID" ]; then
		for _ in $(seq 1 60); do
			state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CID" 2>/dev/null || echo unknown)"
			case "$state" in
				healthy|running) log "stalwart is back ($state)"; break ;;
			esac
			sleep 2
		done
		[ "$state" = healthy ] || [ "$state" = running ] \
			|| log "WARNING: stalwart did not report healthy within 120s (last state: $state). The backup itself is fine; check 'docker compose ps'."
	fi
fi

# ---------------------------------------------------------------------------
# 3. Manifest + checksums, then publish.
# ---------------------------------------------------------------------------
STALWART_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$STALWART_CID" 2>/dev/null || docker compose config --images | grep -i stalwart | head -1)"
PG_SERVER_VERSION="$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -At -c 'show server_version' 2>/dev/null | tr -d '\r')"

{
	echo "Mailosh backup"
	echo "=============="
	echo
	echo "created_at        : $(date -u '+%Y-%m-%dT%H:%M:%SZ') (UTC)"
	echo "created_by        : $(id -un)@$(hostname) via scripts/backup.sh"
	echo "compose_project   : $PROJECT"
	echo "compose_files     : ${COMPOSE_FILE:-docker-compose.yml (default)}"
	echo "repo_root         : $REPO_ROOT"
	echo "git_commit        : $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
	echo "docker            : $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
	echo "compose           : $(docker compose version --short 2>/dev/null || echo unknown)"
	echo
	echo "MAIL  (losing this loses messages permanently)"
	echo "  stalwart_image  : $STALWART_IMAGE"
	echo "  stalwart_stopped_for_backup : $STALWART_WAS_RUNNING"
	echo "  volume data     : $VOL_STALWART_DATA -> stalwart-data.tar.gz"
	echo "  volume etc      : $VOL_STALWART_ETC -> stalwart-etc.tar.gz"
	echo
	echo "APP STATE  (losing this costs a re-login and reset preferences; no mail)"
	echo "  postgres_server : ${PG_SERVER_VERSION:-unknown}"
	echo "  database        : $PG_DB (user $PG_USER) -> postgres.sql.gz"
	echo "  tables_in_dump  : $PG_TABLES"
	echo
	echo "NOT INCLUDED"
	echo "  .env / MAILOSH_SECRET_KEY / MAILOSH_STALWART_ADMIN_SECRET -- keep these"
	echo "  separately (password manager). Losing them costs one re-login for every"
	echo "  user; it does not lose mail."
	echo
	echo "CONSISTENCY"
	echo "  Postgres was dumped online first; Stalwart was archived a few seconds"
	echo "  later with its container stopped. The halves are not one atomic instant."
	echo "  The only cross-store value is the per-session Stalwart API key, and both"
	echo "  skew directions cost at most a re-login."
	echo
	echo "RESTORE"
	echo "  scripts/restore.sh $OUT"
	echo "  Restoring into a *different* stack (a drill, or a new host):"
	echo "    COMPOSE_PROJECT_NAME=mailosh-drill COMPOSE_FILE=/path/to/compose.yml \\"
	echo "      scripts/restore.sh $OUT"
	echo "  See docs/operations.md."
	echo
	echo "CONTENTS"
	( cd "$PARTIAL" && ls -l ./*.gz | awk '{printf "  %-24s %10s bytes\n", $9, $5}' )
} > "$PARTIAL/MANIFEST.txt"

( cd "$PARTIAL" && SHA256 ./*.gz > SHA256SUMS ) || die "could not write checksums."

mv "$PARTIAL" "$OUT"
trap - EXIT
restart_stalwart

log "backup complete: $OUT ($(du -sh "$OUT" | awk '{print $1}'))"

# ---------------------------------------------------------------------------
# 4. Retention.
# ---------------------------------------------------------------------------
# Only ever deletes directories that match this script's own naming pattern,
# so pointing --keep at a directory that also holds something else cannot
# eat it.
if [ "$KEEP" -gt 0 ]; then
	# No `mapfile`/process substitution: this has to keep working under the
	# bash 3.2 that ships with macOS, where neither exists.
	find "$DEST" -maxdepth 1 -type d -name 'mailosh-????????T??????Z' \
		| sort -r | tail -n +$((KEEP + 1)) > "$DEST/.retention.$$" || true
	while IFS= read -r old; do
		[ -n "$old" ] || continue
		log "retention: removing $old"
		rm -rf "$old"
	done < "$DEST/.retention.$$"
	rm -f "$DEST/.retention.$$"
	log "retention: kept the $KEEP most recent backup(s) in $DEST"
fi

printf '\n%s\n' "Next: verify it.  scripts/restore.sh --check $OUT"
