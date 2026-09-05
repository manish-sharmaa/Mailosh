#!/usr/bin/env bash
set -euo pipefail

# Development stack only: bring a fresh local Stalwart from "just started" to
# "you can log in as demo@mailosh.test".
#
# ---------------------------------------------------------------------------
# What this is, and what it is not
# ---------------------------------------------------------------------------
# This is the one-command dev convenience. It hardcodes the development
# domain and the development hostname, and it creates a demo mailbox from
# MAILOSH_DEMO_USER / MAILOSH_DEMO_PASSWORD. **Do not point it at a real
# deployment.**
#
# The production equivalent is `scripts/stalwart-bootstrap.sh`, which takes
# the real domain and hostname as arguments, runs entirely over the internal
# Docker network (no published admin port), refuses to reconfigure a server
# that is already set up, adds the 587 submission listener that Stalwart does
# not ship with (the dev compose file publishes `1587:587`, so without it that
# port refuses every connection), and verifies that the mail listeners
# actually came up. This script now *calls* it for the first-boot half rather
# than carrying a second copy of it -- so there is one implementation of
# "leave bootstrap mode", exercised by both paths.
#
# What is left here is the part that is genuinely development-only: creating
# the demo mailbox, and printing the DKIM key.
#
# ---------------------------------------------------------------------------
# How Stalwart's admin surface actually works (SPK-5, docs/spikes/p0-findings.md)
# ---------------------------------------------------------------------------
# None of the obvious candidate routes exist on stalwartlabs/stalwart:v0.16.20
# (no STALWART_ADMIN_SECRET env var, no POST /api/domain/{name}, no POST
# /api/principal). The real mechanism, verified against the live container:
#
#   1. The admin credential is pinned with STALWART_RECOVERY_ADMIN=admin:<pw>
#      in the compose environment. Without it the server prints a random
#      one-time admin password to stderr on first boot. It is re-read at every
#      container start, so changing it is `up -d stalwart`, not a data
#      migration.
#   2. There is no separate REST management API. Almost everything rides the
#      *same* /jmap endpoint and Basic auth as regular JMAP traffic, using
#      custom JMAP-shaped methods on "x:"-prefixed types discovered from the
#      webui's self-describing schema at GET /api/schema:
#        x:Bootstrap/get|set     initial-setup singleton (id "singleton")
#        x:Domain/get|query      mail domains
#        x:Account/get|query|set user/group accounts
#        x:DkimSignature/get|query  DKIM signing keys
#        x:SystemSettings/get    the server's own hostname and default domain
#        x:NetworkListener/*     the listeners, which is how you check that
#                                the mail ports are really up
#   3. Creating an account needs an "@type" discriminator ("User") and
#      credentials as an *index-keyed map* ({"0": {...}}) -- a plain array or
#      an arbitrarily-keyed map both fail with "invalidPatch".
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   scripts/stalwart-init.sh
#
# Requires MAILOSH_STALWART_ADMIN_SECRET and MAILOSH_DEMO_PASSWORD, from ./.env
# or the environment. Safe to re-run: the bootstrap half converges, and the
# demo account is created only if it is missing.
#
# For a real deployment use scripts/stalwart-bootstrap.sh instead. See
# docs/operations.md.

usage() {
	sed -n '/^# Usage$/,/^$/p' "$0" | sed 's/^# \{0,1\}//' | grep -v '^-\{10,\}$'
	exit "${1:-0}"
}

log() { printf '%s  %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
die() { printf 'stalwart-init.sh: ERROR: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
	-h|--help) usage 0 ;;
	'') ;;
	*) die "unexpected argument '$1' (try --help)" ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Auto-load ./.env (the same file docker compose reads for ${MAILOSH_...}
# interpolation) so this works standalone, e.g. `make up && bash
# scripts/stalwart-init.sh`, with no exports first.
#
# The environment wins over the file, which is what the comment here always
# claimed and what the code did not do: `set -a; . ./.env` assigns
# unconditionally, so an exported override was silently replaced by whatever
# .env happened to say.
if [ -f .env ]; then
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in
			''|'#'*) continue ;;
			*'='*) ;;
			*) continue ;;
		esac
		key="${line%%=*}"
		val="${line#*=}"
		case "$key" in
			MAILOSH_*|STALWART_URL) ;;
			*) continue ;;
		esac
		[ -n "${!key:-}" ] && continue
		case "$val" in
			\"*\") val="${val%\"}"; val="${val#\"}" ;;
			\'*\') val="${val%\'}"; val="${val#\'}" ;;
		esac
		printf -v "$key" '%s' "$val"
		export "${key?}"
	done < .env
fi

BASE="${STALWART_URL:-http://localhost:8080}"
ADMIN_USER="${MAILOSH_STALWART_ADMIN_USER:-admin}"
ADMIN_PASS="${MAILOSH_STALWART_ADMIN_SECRET:?MAILOSH_STALWART_ADMIN_SECRET must be set}"
AUTH="${ADMIN_USER}:${ADMIN_PASS}"
DOMAIN="mailosh.test"
SERVER_HOSTNAME="mail.mailosh.test"
DEMO_EMAIL="${MAILOSH_DEMO_USER:-demo@$DOMAIN}"
DEMO_LOCAL="${DEMO_EMAIL%%@*}"
DEMO_PASSWORD="${MAILOSH_DEMO_PASSWORD:?MAILOSH_DEMO_PASSWORD must be set}"

# ---------------------------------------------------------------------------
# Talking to the dev server
# ---------------------------------------------------------------------------
# Both the admin credential and the request body travel to curl on stdin, in
# a curl config file (`-K -`), and never appear in an argv. Two of the bodies
# below carry a password (the account-create call), and `ps` on this machine
# is readable by every local user. The previous version of this script passed
# `-u admin:$SECRET` and `-d '{... "secret": "..."}'` as arguments, and
# printed the raw x:Bootstrap/set response -- which, verified against the
# live server, contains a freshly generated admin username and password:
#
#     {"updated":{"singleton":{"username":"admin@...","secret":"..."}}}
#
# Nothing in this script prints a response body any more.
cfg_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

jmap_call() {
	# $1 = JSON request body. Prints the response body on stdout.
	{
		printf 'user = "%s"\n' "$(cfg_escape "$AUTH")"
		printf 'url = "%s"\n' "$(cfg_escape "$BASE/jmap")"
		printf 'request = "POST"\n'
		printf 'header = "content-type: application/json"\n'
		printf 'data = "%s"\n' "$(cfg_escape "$1")"
		printf 'silent\nshow-error\nfail\n'
	} | curl -K -
}

# ---------------------------------------------------------------------------
# 1. First boot -- delegated
# ---------------------------------------------------------------------------
# stalwart-bootstrap.sh detects bootstrap mode, applies the domain and
# hostname, restarts the container, and verifies that the mail listeners came
# up. On a server that is already set up with these values it changes nothing
# and exits 0, which is what makes re-running this script safe.
log "first boot: scripts/stalwart-bootstrap.sh --domain $DOMAIN --hostname $SERVER_HOSTNAME"
"$REPO_ROOT/scripts/stalwart-bootstrap.sh" --domain "$DOMAIN" --hostname "$SERVER_HOSTNAME" --brief \
	|| die "first-boot setup failed (see above). Nothing else was attempted."

# The bootstrap script probes Stalwart on the container's own loopback, which
# can be serving a moment before the published dev port is. Everything below
# is host-side, so wait for that too.
for _ in $(seq 1 30); do
	curl -fsS -o /dev/null "$BASE/healthz/live" 2>/dev/null && break
	sleep 2
done
curl -fsS -o /dev/null "$BASE/healthz/live" 2>/dev/null \
	|| die "stalwart is configured but $BASE is not answering from the host. Is 8080 published? (the dev compose file publishes 127.0.0.1:8080; the production one deliberately does not, and this script is dev-only.)"

ACCOUNT_ID="$(printf 'user = "%s"\n' "$(cfg_escape "$AUTH")" | curl -fsS -K - "$BASE/jmap/session" | python3 -c '
import json, sys
print(next(iter(json.load(sys.stdin)["accounts"])))
')"

# ---------------------------------------------------------------------------
# 2. Resolve the domain bootstrap created
# ---------------------------------------------------------------------------
DOMAIN_ID="$(jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:Domain/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:Domain/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:Domain/query\",\"path\":\"/ids\"},\"properties\":[\"id\",\"name\"]},\"c1\"]]}" \
	| python3 -c "
import json, sys
d = json.load(sys.stdin)
for item in d['methodResponses'][1][1]['list']:
    if item['name'] == '$DOMAIN':
        print(item['id'])
        break
else:
    sys.exit('ERROR: domain $DOMAIN not found after bootstrap')
")"
log "domain $DOMAIN -> id $DOMAIN_ID"

# ---------------------------------------------------------------------------
# 3. The demo mailbox (idempotent: skipped when it already exists)
# ---------------------------------------------------------------------------
DEMO_ID="$(jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:Account/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:Account/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:Account/query\",\"path\":\"/ids\"},\"properties\":[\"id\",\"emailAddress\"]},\"c1\"]]}" \
	| python3 -c "
import json, sys
d = json.load(sys.stdin)
for item in d['methodResponses'][1][1]['list']:
    if item.get('emailAddress') == '$DEMO_EMAIL':
        print(item['id'])
        break
")"

if [ -z "$DEMO_ID" ]; then
	log "creating $DEMO_EMAIL"
	# The response is checked, not printed: `x:Account/set` answers HTTP 200
	# with a `notCreated` body when it rejects the payload, so a bare exit
	# status proves nothing.
	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:Account/set\",{\"accountId\":\"$ACCOUNT_ID\",\"create\":{\"demo\":{\"@type\":\"User\",\"name\":\"$DEMO_LOCAL\",\"domainId\":\"$DOMAIN_ID\",\"credentials\":{\"0\":{\"@type\":\"Password\",\"secret\":\"$DEMO_PASSWORD\"}}}}},\"c0\"]]}" \
		| python3 -c "
import json, sys
d = json.load(sys.stdin)
args = d['methodResponses'][0][1]
if 'demo' in (args.get('created') or {}):
    raise SystemExit(0)
bad = (args.get('notCreated') or {}).get('demo') or {}
sys.exit('ERROR: could not create $DEMO_EMAIL: %s' % (bad.get('description') or bad.get('type') or 'no reason given'))
" || die "creating $DEMO_EMAIL failed."
else
	log "$DEMO_EMAIL already exists (id $DEMO_ID); skipping create"
fi

# ---------------------------------------------------------------------------
# 4. The DKIM key generated for the domain (informational; non-fatal)
# ---------------------------------------------------------------------------
if DKIM="$(jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:DkimSignature/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:DkimSignature/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:DkimSignature/query\",\"path\":\"/ids\"},\"properties\":[\"selector\",\"domainId\",\"publicKey\",\"@type\"]},\"c1\"]]}" 2>/dev/null)"; then
	printf '%s' "$DKIM" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for item in d['methodResponses'][1][1]['list']:
    if item.get('domainId') == '$DOMAIN_ID':
        print(f\"  DKIM selector={item['selector']} type={item['@type']} publicKey={item['publicKey'][:24]}...\")
" >&2
else
	log "WARNING: could not read DKIM keys (route differs from what SPK-5 recorded); check p0-findings.md"
fi

log "stalwart-init: done ($DEMO_EMAIL ready on domain $DOMAIN)"
