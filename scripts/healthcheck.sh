#!/usr/bin/env bash
set -uo pipefail

# Is Mailosh healthy? One command, one answer, exit code you can alert on.
#
# Every probe here runs *inside* the compose network rather than against a
# published host port, for two reasons: it works on a deployment that
# publishes nothing (the production posture -- everything behind a reverse
# proxy), and it distinguishes "the service is broken" from "the port is not
# reachable from where you are standing", which are different incidents.
#
# What it probes, and why those and not others -- these are the health
# endpoints the stack actually has, found rather than invented:
#
#   stalwart  GET /healthz/live   the mail server's own liveness endpoint.
#             GET /healthz/ready  its readiness endpoint. docker-compose.yml's
#                                 healthcheck already uses /healthz/live, so
#                                 `docker compose ps` reflects it too.
#   postgres  pg_isready          same probe as the compose healthcheck.
#   mailosh   GET /healthz -> 204 the app's own liveness route. No session, no
#                                 template, no database query -- deliberately,
#                                 because a liveness probe that consulted
#                                 Postgres would report the app down for an
#                                 outage in a different container. It therefore
#                                 does NOT prove the database is reachable; the
#                                 schema probe below is what does that, and the
#                                 two are separate on purpose.
#                                 (This replaced a `GET /login` probe, which
#                                 rendered an entire page to learn one bit.)
#   schema    alembic_version     proves Postgres is not just up but holds a
#                                 migrated Mailosh database.
#   backups   newest in ./backups warns when the most recent backup is older
#                                 than MAILOSH_BACKUP_MAX_AGE_DAYS (7).
#
# Usage:  scripts/healthcheck.sh
#         COMPOSE_PROJECT_NAME=other scripts/healthcheck.sh
# Exit:   0 all good, 1 something is down, 2 warnings only.

FAIL=0
WARN=0
ok()   { printf '  ok    %-10s %s\n' "$1" "${2:-}"; }
bad()  { printf '  FAIL  %-10s %s\n' "$1" "${2:-}"; FAIL=1; }
warn() { printf '  warn  %-10s %s\n' "$1" "${2:-}"; WARN=1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# `|| exit` matters here in a way it does not in backup.sh: this script
# runs without `set -e` on purpose, because it collects failures instead
# of aborting at the first one. A failed cd would otherwise fall through
# and probe the wrong project and the wrong backup directory.
cd "$REPO_ROOT" || exit 1

command -v docker >/dev/null 2>&1 || { echo "healthcheck: docker is not on PATH"; exit 1; }
docker info >/dev/null 2>&1 || { echo "healthcheck: the Docker daemon is not reachable"; exit 1; }
docker compose config -q 2>/dev/null || { echo "healthcheck: 'docker compose config' failed here"; exit 1; }
PROJECT="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null)"
PG_USER="${MAILOSH_PG_USER:-mailosh}"
PG_DB="${MAILOSH_PG_DB:-mailosh}"

echo "Mailosh health -- project '$PROJECT' -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo

# --- containers ------------------------------------------------------------
# `ps -a`, not `ps`: a stopped service is simply absent from the default
# listing, which reads as "nothing to see here" exactly when something is
# wrong.
for svc in stalwart postgres mailosh; do
	cid="$(docker compose ps -aq "$svc" 2>/dev/null | head -1)"
	if [ -z "$cid" ]; then
		bad "$svc" "no container in project '$PROJECT'"
		continue
	fi
	status="$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' "$cid" 2>/dev/null)"
	case "$status" in
		running*unhealthy*) bad "$svc" "$status" ;;
		running*)           ok  "$svc" "$status" ;;
		*)                  bad "$svc" "$status" ;;
	esac
done

# --- stalwart's own endpoints ---------------------------------------------
for path in /healthz/live /healthz/ready; do
	if docker compose exec -T stalwart curl -fsS -m 5 -o /dev/null "http://localhost:8080$path" 2>/dev/null; then
		ok "stalwart" "$path 200"
	else
		bad "stalwart" "$path did not answer 200 -- the mail server is not serving"
	fi
done

# --- postgres --------------------------------------------------------------
if docker compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
	ok "postgres" "accepting connections"
else
	bad "postgres" "pg_isready says no"
fi

ALEMBIC="$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -At -c 'select version_num from alembic_version' 2>/dev/null | tr -d '\r')"
if [ -n "$ALEMBIC" ]; then
	ok "schema" "alembic revision $ALEMBIC"
else
	bad "schema" "no alembic_version row -- the database is empty or unmigrated"
fi

# --- the app ---------------------------------------------------------------
# The mailosh image has no curl and no wget (verified); it does have the
# python it runs on, so the probe uses that.
APP_CODE="$(docker compose exec -T mailosh python3 -c '
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen("http://localhost:8000/healthz", timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERR", e, file=sys.stderr); print(0)
' 2>/dev/null | tr -d '\r')"
if [ "$APP_CODE" = "204" ]; then
	ok "mailosh" "GET /healthz 204"
else
	bad "mailosh" "GET /healthz returned '${APP_CODE:-nothing}' (404 means the app predates the liveness route; anything else means the app process itself is unhealthy -- Postgres and Stalwart are probed separately above)"
fi

# --- backup freshness ------------------------------------------------------
MAXAGE="${MAILOSH_BACKUP_MAX_AGE_DAYS:-7}"
BACKUP_DIR="${MAILOSH_BACKUP_DIR:-$REPO_ROOT/backups}"
NEWEST="$(find "$BACKUP_DIR" -maxdepth 1 -type d -name 'mailosh-????????T??????Z' 2>/dev/null | sort | tail -1)"
if [ -z "$NEWEST" ]; then
	warn "backups" "no backup found in $BACKUP_DIR -- run scripts/backup.sh"
else
	# The directory name is the UTC timestamp the backup was taken at, which
	# is more trustworthy than the mtime (copying a backup rewrites mtime).
	STAMP="$(basename "$NEWEST" | sed 's/^mailosh-//')"
	AGE_DAYS="$(python3 - "$STAMP" <<'PY'
import datetime, sys
t = datetime.datetime.strptime(sys.argv[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.UTC)
print(int((datetime.datetime.now(datetime.UTC) - t).total_seconds() // 86400))
PY
)"
	if [ "${AGE_DAYS:-999}" -le "$MAXAGE" ]; then
		ok "backups" "newest is $STAMP (${AGE_DAYS}d old)"
	else
		warn "backups" "newest is $STAMP (${AGE_DAYS}d old, limit ${MAXAGE}d)"
	fi
fi

echo
if [ "$FAIL" -ne 0 ]; then
	echo "UNHEALTHY. See docs/operations.md -- 'What the logs say when something is wrong'."
	exit 1
fi
if [ "$WARN" -ne 0 ]; then
	echo "Healthy, with warnings."
	exit 2
fi
echo "Healthy."
exit 0
