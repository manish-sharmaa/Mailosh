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
#   caddy     container status      only when the compose project has a caddy
#                                 service (docker-compose.prod.yml). The dev
#                                 stack has none, and says so instead of
#                                 failing.
#   tls       certificate expiry  openssl s_client against 443 (Caddy, via the
#                                 mailosh container -- it has openssl, Caddy's
#                                 alpine image does not) and against 465, 993
#                                 and 587 (STARTTLS) on Stalwart's own
#                                 loopback. Warns under 21 days, fails under 7,
#                                 and always names the issuer, so a
#                                 `CN=rcgen self signed cert` placeholder or a
#                                 Let's Encrypt STAGING certificate is called
#                                 out rather than passing as "valid for 80
#                                 days". The mail-port SNI name is the server
#                                 hostname read from x:SystemSettings.
#   acme      x:Task              a Pending `AcmeRenewal` task whose `due` is
#                                 more than 15 minutes in the past is stuck:
#                                 Stalwart does not re-run it after a restart
#                                 (docs/operations.md, "Mail-port TLS"). Read
#                                 over the admin JMAP API from the mailosh
#                                 container, whose environment already holds
#                                 the admin credential -- it is never printed
#                                 and never put on a command line.
#   disk      df -P               free space on the filesystem holding
#                                 Stalwart's store (measured inside the
#                                 container -- that is the disk the mail is
#                                 on, wherever Docker keeps it), on Docker's
#                                 data root when that path exists on this
#                                 host, and on the backups directory. Warns
#                                 under 20 % free, fails under 10 %.
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
SERVICES="$(docker compose config --services 2>/dev/null)"
has_service() { printf '%s\n' "$SERVICES" | grep -qx -- "$1"; }

# MAILOSH_SITE_ADDRESS is the one value read from .env: it is the name Caddy
# holds the webmail certificate for, so it is the SNI name the 443 probe must
# send. Same rules as scripts/stalwart-bootstrap.sh -- only this key, the
# environment wins over the file, one layer of quotes stripped.
ENV_FILE="${MAILOSH_ENV_FILE:-$REPO_ROOT/.env}"
if [ -z "${MAILOSH_SITE_ADDRESS:-}" ] && [ -f "$ENV_FILE" ]; then
	val="$(sed -n 's/^MAILOSH_SITE_ADDRESS=//p' "$ENV_FILE" | tail -1)"
	case "$val" in
		\"*\") val="${val%\"}"; val="${val#\"}" ;;
		\'*\') val="${val%\'}"; val="${val#\'}" ;;
	esac
	MAILOSH_SITE_ADDRESS="$val"
fi

echo "Mailosh health -- project '$PROJECT' -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo

# --- containers ------------------------------------------------------------
# `ps -a`, not `ps`: a stopped service is simply absent from the default
# listing, which reads as "nothing to see here" exactly when something is
# wrong.
container_status() {
	local svc="$1" cid status
	cid="$(docker compose ps -aq "$svc" 2>/dev/null | head -1)"
	if [ -z "$cid" ]; then
		bad "$svc" "no container in project '$PROJECT'"
		return
	fi
	status="$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}' "$cid" 2>/dev/null)"
	case "$status" in
		running*unhealthy*) bad "$svc" "$status" ;;
		running*)           ok  "$svc" "$status" ;;
		*)                  bad "$svc" "$status" ;;
	esac
}
for svc in stalwart postgres mailosh; do
	container_status "$svc"
done
# Caddy exists only under the production overlay. Its absence from the dev
# stack is the expected shape, not a failure -- but on a stack that declares
# it, a stopped or unhealthy proxy means nobody can reach the webmail at all,
# so it is checked exactly like the other three.
if has_service caddy; then
	container_status caddy
else
	ok "caddy" "not in project '$PROJECT' (docker-compose.prod.yml adds it)"
fi

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

# --- stalwart's admin view: hostname, stuck ACME renewals -------------------
# One probe, run inside the mailosh container, because that container's
# environment already carries MAILOSH_STALWART_URL and the admin credential
# (MAILOSH_STALWART_ADMIN_SECRET). The secret therefore never appears in this
# script, in a `docker exec` argv, or in the output: the program below reads
# it from its own os.environ and sends it as HTTP Basic auth, nothing else.
# The program travels on stdin (`python3 -`), not as a `-c` argument, for the
# same reason scripts/stalwart-bootstrap.sh keeps its parser out of argv.
#
# Output, one record per line, tab-separated, and nothing else:
#   HOSTNAME <name>                      x:SystemSettings.defaultHostname
#   ACME     <id> <due> <minutes-late>   every x:Task of @type AcmeRenewal
#                                        whose status is Pending
#   ERR      <reason>                    the probe could not complete
STALWART_ADMIN_PROBE="$(docker compose exec -T mailosh python3 - 2>/dev/null <<'PY' | tr -d '\r'
import base64, datetime, json, os, sys, urllib.error, urllib.request

url = os.environ.get("MAILOSH_STALWART_URL", "http://stalwart:8080").rstrip("/")
user = os.environ.get("MAILOSH_STALWART_ADMIN_USER", "admin")
secret = os.environ.get("MAILOSH_STALWART_ADMIN_SECRET")
if not secret:
    print("ERR\tMAILOSH_STALWART_ADMIN_SECRET is not set in the mailosh container")
    raise SystemExit(0)
auth = base64.b64encode(f"{user}:{secret}".encode()).decode()
headers = {"authorization": "Basic " + auth, "content-type": "application/json"}

def call(body):
    req = urllib.request.Request(url + "/jmap", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

try:
    req = urllib.request.Request(url + "/jmap/session", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        account = next(iter(json.load(r)["accounts"]))
    using = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
    out = call({"using": using, "methodCalls": [
        ["x:SystemSettings/get", {"accountId": account, "ids": ["singleton"]}, "s0"],
        ["x:Task/query", {"accountId": account}, "q0"],
        ["x:Task/get", {"accountId": account,
                        "#ids": {"resultOf": "q0", "name": "x:Task/query", "path": "/ids"}}, "g0"],
    ]})
except urllib.error.HTTPError as e:
    print(f"ERR\tHTTP {e.code} from {url} (401 means the admin credential was rejected)")
    raise SystemExit(0)
except Exception as e:  # every failure is one warn line, never a traceback
    print(f"ERR\t{type(e).__name__}: {e}")
    raise SystemExit(0)

by_id = {c: args for name, args, c in out.get("methodResponses", []) if name != "error"}
errors = [args for name, args, c in out.get("methodResponses", []) if name == "error"]
if errors:
    print("ERR\t" + "; ".join(str(a.get("type") or a) for a in errors))
    raise SystemExit(0)
settings = (by_id.get("s0") or {}).get("list") or [{}]
print("HOSTNAME\t" + (settings[0].get("defaultHostname") or ""))
now = datetime.datetime.now(datetime.UTC)
for task in (by_id.get("g0") or {}).get("list") or []:
    status = task.get("status") or {}
    if task.get("@type") != "AcmeRenewal" or status.get("@type") != "Pending":
        continue
    due_raw = task.get("due") or status.get("due") or ""
    try:
        due = datetime.datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
        late = int((now - due).total_seconds() // 60)
    except ValueError:
        late = 0
    print(f"ACME\t{task.get('id', '?')}\t{due_raw}\t{late}")
PY
)"
MAIL_HOSTNAME="$(printf '%s\n' "$STALWART_ADMIN_PROBE" | sed -n 's/^HOSTNAME\t//p' | head -1)"
ACME_LATE_MINUTES=15
PROBE_ERR="$(printf '%s\n' "$STALWART_ADMIN_PROBE" | sed -n 's/^ERR\t//p' | head -1)"
if [ -n "$PROBE_ERR" ]; then
	warn "acme" "could not query x:Task -- $PROBE_ERR"
elif [ -z "$STALWART_ADMIN_PROBE" ]; then
	warn "acme" "could not query x:Task -- the probe inside the mailosh container produced nothing (is the container running with python3?)"
else
	ACME_STUCK=0
	ACME_SEEN=0
	while IFS="$(printf '\t')" read -r tag tid tdue tlate; do
		[ "$tag" = ACME ] || continue
		ACME_SEEN=$((ACME_SEEN + 1))
		if [ "${tlate:-0}" -gt "$ACME_LATE_MINUTES" ]; then
			ACME_STUCK=$((ACME_STUCK + 1))
			warn "acme" "AcmeRenewal task $tid is still Pending ${tlate} min after its due time ($tdue) -- Stalwart does not re-run it after a restart. Re-save the domain's certificate management (see docs/operations.md, 'Mail-port TLS')."
		fi
	done <<-EOF
	$STALWART_ADMIN_PROBE
	EOF
	if [ "$ACME_STUCK" -eq 0 ]; then
		if [ "$ACME_SEEN" -gt 0 ]; then
			ok "acme" "$ACME_SEEN pending AcmeRenewal task(s), none overdue"
		else
			ok "acme" "no pending AcmeRenewal task (no ACME-managed mail certificate, or nothing queued)"
		fi
	fi
fi

# --- TLS certificate expiry ----------------------------------------------
# Every probe connects from inside the compose network, like everything else
# here: 443 via the mailosh container (it is on the same `edge` network as
# Caddy, and has openssl -- caddy:2.10-alpine does not), the mail ports on
# Stalwart's own loopback. The SNI name matters: Caddy picks the certificate
# by it, and Stalwart with several domains does too.
TLS_WARN_DAYS=21
TLS_FAIL_DAYS=7

# cert_probe SERVICE HOST:PORT SERVERNAME [STARTTLS-PROTO]
# Sets CERT_NOTAFTER / CERT_ISSUER / CERT_SUBJECT (empty when nothing
# answered TLS) and CERT_REFUSED=yes when the TCP connection itself failed.
CERT_NOTAFTER=""; CERT_ISSUER=""; CERT_SUBJECT=""; CERT_REFUSED=no
cert_probe() {
	local svc="$1" target="$2" sni="$3" starttls="${4:-}" raw
	CERT_NOTAFTER=""; CERT_ISSUER=""; CERT_SUBJECT=""; CERT_REFUSED=no
	# `printf QUIT |` rather than `</dev/null`: with STARTTLS, openssl needs
	# stdin to stay open through the protocol dance; an immediate EOF makes
	# it exit before the certificate is ever received.
	raw="$(printf 'QUIT\r\n' | docker compose exec -T "$svc" timeout 20 openssl s_client \
		-connect "$target" -servername "$sni" ${starttls:+-starttls "$starttls"} 2>&1 \
		| tr -d '\r')"
	case "$raw" in
		*"Connection refused"*|*"connect:errno"*) CERT_REFUSED=yes; return ;;
	esac
	local parsed
	parsed="$(printf '%s\n' "$raw" | docker compose exec -T "$svc" openssl x509 -noout -enddate -issuer -subject 2>/dev/null | tr -d '\r')"
	CERT_NOTAFTER="$(printf '%s\n' "$parsed" | sed -n 's/^notAfter=//p')"
	CERT_ISSUER="$(printf '%s\n' "$parsed" | sed -n 's/^issuer=//p')"
	CERT_SUBJECT="$(printf '%s\n' "$parsed" | sed -n 's/^subject=//p')"
}

# The issuer, boiled down to what an operator scans for: "Let's Encrypt",
# "(STAGING) ...", "self-signed placeholder".
describe_issuer() {
	case "$1" in
		*"rcgen self signed"*) printf 'SELF-SIGNED PLACEHOLDER (Stalwart has no real certificate yet)' ;;
		*STAGING*)             printf 'Let'"'"'s Encrypt STAGING -- not trusted by clients' ;;
		*)
			local org
			org="$(printf '%s' "$1" | sed -n 's/.*O=\([^,]*\).*/\1/p')"
			if [ -n "$org" ]; then printf 'issuer %s' "$org"; else printf 'issuer %s' "$1"; fi
			;;
	esac
}

# tls_check LABEL SERVICE HOST:PORT SERVERNAME [STARTTLS-PROTO]
tls_check() {
	local label="$1"; shift
	cert_probe "$@"
	if [ "$CERT_REFUSED" = yes ]; then
		warn "tls" "$label: connection refused -- nothing is listening there (scripts/stalwart-bootstrap.sh --verify-only reports the listeners)"
		return
	fi
	if [ -z "$CERT_NOTAFTER" ]; then
		bad "tls" "$label: no certificate could be read (TLS handshake failed or the port does not speak TLS)"
		return
	fi
	local days issuer
	days="$(python3 - "$CERT_NOTAFTER" <<'PY'
import datetime, sys
s = " ".join(sys.argv[1].split())
try:
    t = datetime.datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.UTC)
except ValueError:
    print(""); raise SystemExit(0)
print(int((t - datetime.datetime.now(datetime.UTC)).total_seconds() // 86400))
PY
)"
	issuer="$(describe_issuer "$CERT_ISSUER")"
	if [ -z "$days" ]; then
		warn "tls" "$label: could not parse expiry '$CERT_NOTAFTER' ($issuer)"
	elif [ "$days" -lt "$TLS_FAIL_DAYS" ]; then
		bad "tls" "$label: certificate expires in ${days}d ($CERT_NOTAFTER; $issuer)"
	elif [ "$days" -lt "$TLS_WARN_DAYS" ]; then
		warn "tls" "$label: certificate expires in ${days}d ($CERT_NOTAFTER; $issuer)"
	else
		case "$CERT_ISSUER" in
			*"rcgen self signed"*|*STAGING*) warn "tls" "$label: valid ${days}d but $issuer" ;;
			*) ok "tls" "$label: valid ${days}d, $issuer" ;;
		esac
	fi
}

if has_service caddy; then
	if [ -n "${MAILOSH_SITE_ADDRESS:-}" ]; then
		tls_check "443 $MAILOSH_SITE_ADDRESS" mailosh caddy:443 "$MAILOSH_SITE_ADDRESS"
	else
		warn "tls" "443: MAILOSH_SITE_ADDRESS is not set (env or $ENV_FILE), so the webmail certificate was not checked"
	fi
fi
MAIL_SNI="${MAIL_HOSTNAME:-localhost}"
tls_check "465 $MAIL_SNI"         stalwart localhost:465 "$MAIL_SNI"
tls_check "993 $MAIL_SNI"         stalwart localhost:993 "$MAIL_SNI"
tls_check "587 $MAIL_SNI STARTTLS" stalwart localhost:587 "$MAIL_SNI" smtp

# --- disk ------------------------------------------------------------------
DISK_WARN_FREE=20
DISK_FAIL_FREE=10
# disk_check LABEL DF-OUTPUT  -- DF-OUTPUT is `df -P` text; only its last line
# is read, so a header or a mount line above it is fine.
disk_check() {
	local label="$1" line used free
	line="$(printf '%s\n' "$2" | tail -1)"
	used="$(printf '%s\n' "$line" | awk '{print $5}' | tr -d '%')"
	case "$used" in
		''|*[!0-9]*) warn "disk" "$label: could not read df output"; return ;;
	esac
	free=$((100 - used))
	local avail
	avail="$(printf '%s\n' "$line" | awk '{printf "%.1f GiB free", $4 / 1048576}')"
	if [ "$free" -lt "$DISK_FAIL_FREE" ]; then
		bad "disk" "$label: ${free}% free ($avail)"
	elif [ "$free" -lt "$DISK_WARN_FREE" ]; then
		warn "disk" "$label: ${free}% free ($avail)"
	else
		ok "disk" "$label: ${free}% free ($avail)"
	fi
}
# The mail store's own filesystem, measured where it is mounted. On Linux
# this is the host disk under Docker's data root; on Docker Desktop it is
# the VM disk -- either way it is the disk that fills when mail does.
STORE_DF="$(docker compose exec -T stalwart df -P /var/lib/stalwart 2>/dev/null | tr -d '\r')"
if [ -n "$STORE_DF" ]; then
	disk_check "stalwart store (/var/lib/stalwart in the container)" "$STORE_DF"
else
	warn "disk" "could not run df inside the stalwart container"
fi
DOCKER_ROOT="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
if [ -n "$DOCKER_ROOT" ] && [ -d "$DOCKER_ROOT" ]; then
	disk_check "docker data root $DOCKER_ROOT" "$(df -P "$DOCKER_ROOT" 2>/dev/null)"
fi

# --- backup freshness ------------------------------------------------------
MAXAGE="${MAILOSH_BACKUP_MAX_AGE_DAYS:-7}"
BACKUP_DIR="${MAILOSH_BACKUP_DIR:-$REPO_ROOT/backups}"
if [ -d "$BACKUP_DIR" ]; then
	disk_check "backups $BACKUP_DIR" "$(df -P "$BACKUP_DIR" 2>/dev/null)"
fi
# Both shapes backup.sh produces: the plain directory, and the .tar.age
# file `--encrypt-to` packs it into.
NEWEST="$(find "$BACKUP_DIR" -maxdepth 1 \
	\( -type d -name 'mailosh-????????T??????Z' -o -type f -name 'mailosh-????????T??????Z.tar.age' \) 2>/dev/null \
	| sed 's/\.tar\.age$//' | sort | tail -1)"
if [ -z "$NEWEST" ]; then
	warn "backups" "no backup found in $BACKUP_DIR -- run scripts/backup.sh"
else
	# The name is the UTC timestamp the backup was taken at, which is more
	# trustworthy than the mtime (copying a backup rewrites mtime).
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
