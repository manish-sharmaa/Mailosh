#!/usr/bin/env bash
set -euo pipefail

# Mailosh restore -- the half nobody practises.
#
# A backup nobody has restored is not a backup, it is a hope. This script is
# the other end of scripts/backup.sh, and it is deliberately awkward to run
# by accident: it destroys whatever is in the target stack before it puts the
# backup there, and there is no undo.
#
# ---------------------------------------------------------------------------
# What it destroys, precisely
# ---------------------------------------------------------------------------
#   * the target project's stalwart-data volume  -- ALL MAIL in that stack
#   * the target project's stalwart-etc volume   -- its store pointer
#   * the target project's Postgres database     -- dropped and recreated
#
# Not touched: .env, the images, anything outside the target compose project.
#
# ---------------------------------------------------------------------------
# Restoring somewhere other than the stack you are standing in
# ---------------------------------------------------------------------------
# Use Compose's own environment variables. This is how you rehearse a restore
# without endangering the live stack, and how you bring a backup up on a new
# host:
#
#   COMPOSE_PROJECT_NAME=mailosh-drill COMPOSE_FILE=/tmp/drill/docker-compose.yml \
#     scripts/restore.sh backups/mailosh-20260905T042034Z
#
# The script prints a loud warning when the target project is not the project
# the backup was taken from, because that is either exactly what you meant or
# a very bad mistake, and only you know which.
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   scripts/restore.sh --check BACKUP        verify the archive only; changes nothing
#   scripts/restore.sh BACKUP                restore (asks for confirmation)
#   scripts/restore.sh --yes BACKUP          restore without asking (scripts/cron)
#   scripts/restore.sh --identity KEYFILE [--check] BACKUP.tar.age
#                                            an encrypted backup (backup.sh
#                                            --encrypt-to); KEYFILE is the age
#                                            identity, or set MAILOSH_AGE_IDENTITY
#
#   BACKUP is a mailosh-<stamp> directory, or the mailosh-<stamp>.tar.age file
#   backup.sh --encrypt-to produces. The .tar.age is decrypted into a private
#   temporary directory (removed on exit) and then treated exactly like the
#   directory -- so `--check` on an encrypted backup also proves that the
#   identity you hold actually decrypts it, which is the one thing a checksum
#   cannot tell you.
#
# See docs/operations.md.

usage() {
	sed -n '/^# Usage$/,/^$/p' "$0" | sed 's/^# \{0,1\}//' | grep -v '^-\{10,\}$'
	exit "${1:-0}"
}

log()  { printf '%s  %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
die()  { printf 'restore.sh: ERROR: %s\n' "$*" >&2; exit 1; }

need_cmd() {
	command -v "$1" >/dev/null 2>&1 || die "$1 is required but not on PATH. $2"
}

CHECK_ONLY=no
ASSUME_YES=no
BACKUP=""
IDENTITY="${MAILOSH_AGE_IDENTITY:-}"
while [ $# -gt 0 ]; do
	case "$1" in
		-h|--help) usage 0 ;;
		--check) CHECK_ONLY=yes; shift ;;
		-y|--yes) ASSUME_YES=yes; shift ;;
		--identity|-i)
			[ $# -ge 2 ] || die "--identity needs a file (the age identity that decrypts the backup)"
			IDENTITY="$2"; shift 2 ;;
		--identity=*) IDENTITY="${1#--identity=}"; shift ;;
		-*) die "unknown option '$1' (try --help)" ;;
		*)
			[ -z "$BACKUP" ] || die "more than one backup given ('$BACKUP' and '$1')"
			BACKUP="$1"; shift
			;;
	esac
done
[ -n "$BACKUP" ] || usage 1

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_ARG="$BACKUP"
case "$BACKUP_ARG" in
	*.partial) die "'$BACKUP_ARG' is an interrupted backup (.partial). It is incomplete by definition; do not restore it." ;;
esac

# An encrypted backup is decrypted into a temporary directory first, and the
# rest of this script never knows the difference. The directory is private
# (mktemp -d gives 0700) and removed on exit -- including on `die` -- so a
# decrypted copy of everyone's mail does not outlive the run.
WORK=""
case "$BACKUP_ARG" in
	*.tar.age)
		need_cmd age "It decrypts a backup made with backup.sh --encrypt-to. https://age-encryption.org"
		[ -f "$BACKUP_ARG" ] || die "encrypted backup '$BACKUP_ARG' does not exist."
		[ -n "$IDENTITY" ] || die "'$BACKUP_ARG' is encrypted; pass --identity KEYFILE (the age identity whose public key it was encrypted to), or set MAILOSH_AGE_IDENTITY."
		[ -f "$IDENTITY" ] || die "identity file '$IDENTITY' does not exist."
		WORK="$(mktemp -d "${TMPDIR:-/tmp}/mailosh-restore.XXXXXX")"
		trap 'rm -rf "$WORK"' EXIT
		log "decrypting $BACKUP_ARG"
		age -d -i "$IDENTITY" "$BACKUP_ARG" | tar -xf - -C "$WORK" \
			|| die "could not decrypt '$BACKUP_ARG' with '$IDENTITY'. Wrong identity, or a damaged archive -- nothing was changed."
		BACKUP="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d -name 'mailosh-*' | head -1)"
		[ -n "$BACKUP" ] || die "'$BACKUP_ARG' decrypted, but holds no mailosh-<stamp> directory -- not an archive made by scripts/backup.sh."
		;;
	*)
		[ -z "$IDENTITY" ] || [ -d "$BACKUP_ARG" ] || die "--identity was given but '$BACKUP_ARG' is not a .tar.age archive."
		BACKUP="$(cd "$BACKUP_ARG" 2>/dev/null && pwd)" || die "backup directory '$BACKUP_ARG' does not exist."
		;;
esac
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Verify the archive. This runs for --check and for a real restore alike:
# there is no path through this script that touches a live stack before the
# backup has been proven readable.
# ---------------------------------------------------------------------------
need_cmd gzip "It is part of every base system; check your PATH."
if command -v sha256sum >/dev/null 2>&1; then
	SHA256_CHECK() { sha256sum -c "$@"; }
elif command -v shasum >/dev/null 2>&1; then
	SHA256_CHECK() { shasum -a 256 -c "$@"; }
else
	die "neither sha256sum nor shasum found; cannot verify the backup."
fi

for f in MANIFEST.txt SHA256SUMS postgres.sql.gz stalwart-data.tar.gz stalwart-etc.tar.gz; do
	[ -f "$BACKUP/$f" ] || die "'$BACKUP' is missing $f -- that is not a backup made by scripts/backup.sh."
done

log "verifying $BACKUP"
( cd "$BACKUP" && SHA256_CHECK SHA256SUMS >/dev/null ) \
	|| die "CHECKSUM MISMATCH. This backup is corrupt -- do not restore it. Find another copy."
for f in postgres.sql.gz stalwart-data.tar.gz stalwart-etc.tar.gz; do
	gzip -t "$BACKUP/$f" || die "$f is not a valid gzip stream."
done
PG_TAIL="$(gzip -dc "$BACKUP/postgres.sql.gz" | tail -20)"
printf '%s\n' "$PG_TAIL" | grep -q 'PostgreSQL database dump complete' \
	|| die "postgres.sql.gz has no completion marker -- the dump is truncated."
PG_TABLES="$(gzip -dc "$BACKUP/postgres.sql.gz" | grep -c '^CREATE TABLE ' || true)"

need_cmd docker "Install Docker Desktop or the docker engine."
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is required."
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable. Start Docker and retry."
HELPER_IMAGE="${MAILOSH_BACKUP_HELPER_IMAGE:-postgres:16-alpine}"
docker image inspect "$HELPER_IMAGE" >/dev/null 2>&1 \
	|| die "helper image '$HELPER_IMAGE' is not present locally. Run: docker pull $HELPER_IMAGE"

STALWART_ENTRIES="$(docker run --rm -i "$HELPER_IMAGE" tar -tzf - < "$BACKUP/stalwart-data.tar.gz" | grep -vc '^\./$' || true)"
HAS_CURRENT="$(docker run --rm -i "$HELPER_IMAGE" tar -tzf - < "$BACKUP/stalwart-data.tar.gz" | grep -c '^\./CURRENT$' || true)"
if [ "${STALWART_ENTRIES:-0}" -eq 0 ]; then
	log "WARNING: this backup's stalwart-data is EMPTY -- it contains NO MAIL."
	log "         (That is what a backup of a never-bootstrapped Stalwart looks like.)"
elif [ "${HAS_CURRENT:-0}" -lt 1 ]; then
	die "stalwart-data.tar.gz has $STALWART_ENTRIES entries but no ./CURRENT -- it is not a RocksDB store that will open."
fi

BACKUP_PROJECT="$(sed -n 's/^compose_project *: *//p' "$BACKUP/MANIFEST.txt" | head -1)"
BACKUP_CREATED="$(sed -n 's/^created_at *: *//p' "$BACKUP/MANIFEST.txt" | head -1)"
BACKUP_STALWART_IMAGE="$(sed -n 's/^  stalwart_image *: *//p' "$BACKUP/MANIFEST.txt" | head -1)"

log "archive OK: $PG_TABLES tables in the SQL dump, $STALWART_ENTRIES files in the mail store"
if [ "$CHECK_ONLY" = yes ]; then
	printf '\n%s\n' "$BACKUP_ARG is intact and restorable-looking."
	[ -z "$WORK" ] || printf '%s\n' "  decrypted : yes, with $IDENTITY"
	printf '%s\n' "  taken     : ${BACKUP_CREATED:-unknown} from project '${BACKUP_PROJECT:-unknown}'"
	printf '%s\n' "  stalwart  : ${BACKUP_STALWART_IMAGE:-unknown}"
	printf '%s\n' "  mail store: $STALWART_ENTRIES files"
	printf '%s\n' "  app state : $PG_TABLES tables"
	printf '\n%s\n' "--check proves the bytes are readable and structurally right. It does NOT"
	printf '%s\n'   "prove the data comes back. Only an actual restore into a throwaway stack"
	printf '%s\n'   "does that -- see docs/operations.md, 'Restore drill'."
	exit 0
fi

# ---------------------------------------------------------------------------
# Resolve the target.
# ---------------------------------------------------------------------------
docker compose config -q 2>/dev/null \
	|| die "'docker compose config' failed here. Wrong directory, or COMPOSE_FILE points somewhere unreadable?"
PROJECT="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
[ -n "$PROJECT" ] || die "could not determine the target compose project name."
SERVICES="$(docker compose config --services)"
for svc in postgres stalwart; do
	printf '%s\n' "$SERVICES" | grep -qx "$svc" || die "target project '$PROJECT' has no '$svc' service."
done
PG_USER="${MAILOSH_PG_USER:-mailosh}"
PG_DB="${MAILOSH_PG_DB:-mailosh}"

# Volume names: prefer what Docker says (compose labels every volume it
# creates); fall back to Compose's own "<project>_<name>" convention for a
# target that has never been started.
volume_name() {
	local logical="$1" found
	found="$(docker volume ls -q \
		--filter "label=com.docker.compose.project=$PROJECT" \
		--filter "label=com.docker.compose.volume=$logical" | head -1)"
	printf '%s\n' "${found:-${PROJECT}_${logical}}"
}
VOL_STALWART_DATA="$(volume_name stalwart-data)"
VOL_STALWART_ETC="$(volume_name stalwart-etc)"

human_vol() {
	docker volume inspect "$1" >/dev/null 2>&1 || { echo "does not exist yet"; return; }
	docker run --rm -v "$1":/src:ro "$HELPER_IMAGE" du -sh /src 2>/dev/null | awk '{print $1}'
}
current_db_summary() {
	local cid
	cid="$(docker compose ps -q postgres 2>/dev/null || true)"
	[ -n "$cid" ] || { echo "postgres not running -- contents unknown"; return; }
	docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -At \
		-c "select pg_size_pretty(pg_database_size('$PG_DB')) || ', ' || count(*) || ' tables' from information_schema.tables where table_schema='public'" 2>/dev/null \
		| tr -d '\r' || echo "unreadable"
}

# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------
cat >&2 <<BANNER

============================================================
  DESTRUCTIVE RESTORE
============================================================
  Backup   : $BACKUP
             taken ${BACKUP_CREATED:-?} from project '${BACKUP_PROJECT:-?}'
             $STALWART_ENTRIES files of mail, $PG_TABLES tables of app state

  Target   : compose project '$PROJECT'
             files: ${COMPOSE_FILE:-docker-compose.yml (default)}

  About to PERMANENTLY DESTROY in that target and replace from the backup:
    * volume $VOL_STALWART_DATA  (now: $(human_vol "$VOL_STALWART_DATA"))   <-- ALL MAIL
    * volume $VOL_STALWART_ETC  (now: $(human_vol "$VOL_STALWART_ETC"))
    * database '$PG_DB'  (now: $(current_db_summary))

  There is no undo. If what is there now matters, stop and run
  scripts/backup.sh first.
============================================================
BANNER

if [ "${BACKUP_PROJECT:-}" != "$PROJECT" ]; then
	cat >&2 <<CROSS

  !! CROSS-STACK RESTORE: this backup came from project
  !! '${BACKUP_PROJECT:-unknown}' and you are restoring it into '$PROJECT'.
  !! That is correct for a restore drill or a new host, and catastrophic
  !! if you meant to name a different target.

CROSS
fi

if [ "$ASSUME_YES" = yes ]; then
	log "--yes given; proceeding without confirmation"
else
	[ -t 0 ] || die "refusing to restore non-interactively without --yes (stdin is not a terminal)."
	printf 'Type the target project name (%s) to proceed: ' "$PROJECT" >&2
	IFS= read -r answer
	[ "$answer" = "$PROJECT" ] || die "you typed '$answer', not '$PROJECT'. Nothing was changed."
fi

# ---------------------------------------------------------------------------
# 1. Quiesce. The app must not be writing to Postgres while the database is
#    dropped, and Stalwart must not hold the RocksDB lock while its volume is
#    replaced under it.
# ---------------------------------------------------------------------------
log "stopping the target stack"
docker compose stop >/dev/null 2>&1 || true

# `create stalwart` (not `create`, and not `up`): it materialises the two
# Stalwart volumes if this target has never run, without building the
# `mailosh` service's image, which a plain `docker compose create` would.
docker compose create stalwart >/dev/null 2>&1 \
	|| die "could not create the stalwart container in project '$PROJECT'."
VOL_STALWART_DATA="$(volume_name stalwart-data)"
VOL_STALWART_ETC="$(volume_name stalwart-etc)"

# ---------------------------------------------------------------------------
# 2. The mail.
# ---------------------------------------------------------------------------
restore_volume() {
	local vol="$1" archive="$2"
	docker volume inspect "$vol" >/dev/null 2>&1 || die "volume '$vol' does not exist and could not be created."
	# Wipe the contents rather than deleting the volume: the volume keeps the
	# compose labels that make Compose treat it as its own, and a half-deleted
	# volume is a worse state to be interrupted in than an empty one.
	docker run --rm -v "$vol":/dst "$HELPER_IMAGE" \
		find /dst -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
		|| die "could not empty volume '$vol'."
	docker run --rm -i -v "$vol":/dst "$HELPER_IMAGE" \
		tar -xzf - -C /dst < "$archive" \
		|| die "could not extract '$archive' into '$vol'."
}

log "restoring the mail store into $VOL_STALWART_DATA"
restore_volume "$VOL_STALWART_DATA" "$BACKUP/stalwart-data.tar.gz"
log "restoring the store pointer into $VOL_STALWART_ETC"
restore_volume "$VOL_STALWART_ETC" "$BACKUP/stalwart-etc.tar.gz"

# Stalwart runs as uid 2000 and will not start if it cannot write its own
# store. tar preserves the numeric ownership, but assert it rather than
# trusting it -- a wrong-ownership restore fails in a way that looks like
# data loss.
OWNER="$(docker run --rm -v "$VOL_STALWART_DATA":/dst:ro "$HELPER_IMAGE" stat -c '%u:%g' /dst/CURRENT 2>/dev/null || echo "")"
if [ "${STALWART_ENTRIES:-0}" -gt 0 ]; then
	[ "$OWNER" = "2000:2000" ] \
		|| die "restored store files are owned by '${OWNER:-?}', expected 2000:2000 (the stalwart user). Stalwart would fail to start."
fi

# ---------------------------------------------------------------------------
# 3. The app state.
# ---------------------------------------------------------------------------
log "starting postgres"
docker compose up -d postgres >/dev/null || die "could not start postgres."
ready=no
for _ in $(seq 1 60); do
	if docker compose exec -T postgres pg_isready -U "$PG_USER" >/dev/null 2>&1; then ready=yes; break; fi
	sleep 2
done
[ "$ready" = yes ] || die "postgres did not accept connections within 120s."

log "dropping and recreating database '$PG_DB'"
docker compose exec -T postgres psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q \
	-c "select pg_terminate_backend(pid) from pg_stat_activity where datname = '$PG_DB' and pid <> pg_backend_pid();" \
	-c "drop database if exists \"$PG_DB\";" \
	-c "create database \"$PG_DB\" owner \"$PG_USER\";" >/dev/null \
	|| die "could not recreate database '$PG_DB'."

log "loading the SQL dump"
gzip -dc "$BACKUP/postgres.sql.gz" \
	| docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -q -o /dev/null \
	|| die "psql failed while loading the dump. The database is now in an unknown state -- fix the cause and run this script again."

# ---------------------------------------------------------------------------
# 4. Back up (as in: up again).
# ---------------------------------------------------------------------------
log "starting the rest of the stack"
docker compose up -d >/dev/null || die "the stack did not come up. Check 'docker compose ps' and 'docker compose logs'."

for svc in stalwart postgres; do
	cid="$(docker compose ps -aq "$svc" 2>/dev/null || true)"
	[ -n "$cid" ] || continue
	state=unknown
	for _ in $(seq 1 60); do
		state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
		case "$state" in healthy|running) break ;; esac
		sleep 2
	done
	log "$svc: $state"
	case "$state" in
		healthy|running) ;;
		*) log "WARNING: $svc is '$state' after the restore. Check: docker compose logs $svc" ;;
	esac
done

# The app is the slowest thing to come back: `docker compose up -d` returns as
# soon as the container is running, but docker/entrypoint.sh still has to run
# `alembic upgrade head` before uvicorn listens. Without this wait, "Restore
# finished" prints while the web UI is still refusing connections, which reads
# exactly like a failed restore. Same probe healthcheck.sh uses: the app's own
# liveness route, GET /healthz -> 204 (no session, no template, no database
# query -- it replaced a GET /login probe that rendered a whole page to learn
# one bit).
APP_STATE=unreachable
if printf '%s\n' "$SERVICES" | grep -qx mailosh; then
	for _ in $(seq 1 30); do
		code="$(docker compose exec -T mailosh python3 -c '
import urllib.error, urllib.request
try:
    with urllib.request.urlopen("http://localhost:8000/healthz", timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
' 2>/dev/null | tr -d '\r')"
		if [ "$code" = "204" ]; then APP_STATE="serving (GET /healthz 204)"; break; fi
		sleep 2
	done
	log "mailosh: $APP_STATE"
	[ "$APP_STATE" != unreachable ] \
		|| log "WARNING: the app is not serving after the restore. Check: docker compose logs mailosh"
fi

ROWS="$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -At \
	-c "select 'app_user='||(select count(*) from app_user)||' session='||(select count(*) from session)||' audit_log='||(select count(*) from audit_log)" 2>/dev/null | tr -d '\r' || echo unavailable)"
ALEMBIC="$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -At -c 'select version_num from alembic_version' 2>/dev/null | tr -d '\r' || echo unavailable)"

cat >&2 <<DONE

Restore finished into project '$PROJECT'.
  app state : $ROWS
  schema    : alembic revision $ALEMBIC

Now prove it, because the script cannot:
  1. docker compose ps                       -- everything up and healthy
  2. open the web UI and sign in             -- Stalwart accounts came back
  3. check a mailbox you know had messages   -- the mail came back

Sessions from before the restore are gone from the browser's point of view
only if MAILOSH_SECRET_KEY changed; otherwise old cookies keep working.
DONE
