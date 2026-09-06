#!/usr/bin/env bash
set -euo pipefail

# Stalwart's first boot -- the step without which a deployed stack receives
# no mail at all.
#
# ---------------------------------------------------------------------------
# The problem this exists for
# ---------------------------------------------------------------------------
# A fresh Stalwart container starts in *bootstrap mode*. Verified, not
# assumed -- this is the container's own first log line:
#
#     WARN Server started in bootstrap mode (server.bootstrap-mode)
#          hostname = "4fca5c60b632"
#          details = "No configuration file was found. Port 8080 is open for
#                     initial setup."
#
# and this is what its ports look like at that moment, probed from inside the
# container:
#
#     port 25 refused   port 465 refused   port 587 refused   port 993 refused
#
# So: MX pointed at the box, firewall open, Caddy holding a real certificate
# -- and every inbound connection on 25 is refused. Nothing in the stack says
# why. `docker compose ps` reports stalwart *healthy*, because
# `/healthz/live` answers 200 in bootstrap mode exactly as it does in
# service.
#
# The server leaves bootstrap mode when `x:Bootstrap/set` is given a mail
# domain and a server hostname, and is then restarted. That is what this
# script does.
#
# ---------------------------------------------------------------------------
# Why it runs over the internal network
# ---------------------------------------------------------------------------
# `docker-compose.prod.yml` publishes Stalwart's HTTP port nowhere. That port
# carries both JMAP and the admin surface and accepts
# MAILOSH_STALWART_ADMIN_SECRET -- one string that can create or delete any
# account and mint an API key for any mailbox. Not publishing it is the most
# valuable single thing that compose file does, and first-boot setup is not a
# reason to weaken it, not even temporarily.
#
# Every call below therefore runs *inside* the stalwart container
# (`docker compose exec -T stalwart curl ... http://localhost:8080/...`), on
# its own loopback. No port is bound, no forwarder is opened, and the script
# works identically on a stack that publishes nothing.
#
# The admin credential reaches curl on **stdin**, inside a curl config file
# (`-K -`), and never appears in an argv. `docker exec` runs its process in
# the container's namespaces but the command line is still visible in `ps` on
# the *host*, so `curl -u admin:$SECRET` would put the highest-value
# credential in the stack on the process table of every local user for the
# duration of the call.
#
# ---------------------------------------------------------------------------
# Idempotency: it converges, and refuses rather than reconfiguring
# ---------------------------------------------------------------------------
# Running it twice is safe, and the second run is a verifier rather than a
# no-op:
#
#   * server in bootstrap mode         -> configure it, restart, add the 587
#                                         submission listener, restart, verify.
#   * already configured, and the domain and hostname are the ones you asked
#     for                              -> add the 587 listener if it is
#                                         missing (and only then restart), run
#                                         the full verification, exit 0.
#                                         Otherwise change nothing: the
#                                         desired state is already true.
#   * already configured with a
#     *different* domain or hostname   -> REFUSE (exit 3), naming both
#                                         values. Rewriting the identity of a
#                                         mail server that is already
#                                         carrying mail is not something a
#                                         re-run should do quietly; see the
#                                         message for the two supported ways
#                                         to make the change deliberately.
#
# `x:Bootstrap/set` is a one-shot object in any case: once the server has
# restarted with a configuration, `x:Bootstrap/get` reports the singleton as
# `notFound` and there is no second bootstrap to perform. "Converge" here
# means "verify what is there", not "re-apply".
#
# The one thing a converging run *will* change on an already-configured server
# is a missing 587 listener -- see "The submission listener on 587" below for
# why that is worth a restart, and note that `--verify-only` changes nothing
# and restarts nothing, ever.
#
# ---------------------------------------------------------------------------
# The submission listener on 587
# ---------------------------------------------------------------------------
# Stalwart v0.16.20's default post-bootstrap listener set is `smtp:25`,
# `submissions:465`, `imaps:993`, `pop3s:995`, `sieve:4190`, `http:8080`,
# `https:443` -- read from its own `x:NetworkListener` objects and confirmed by
# connecting to each. There is **no 587**.
#
# 587 is the standard submission port, and Thunderbird, Apple Mail, iOS Mail
# and Outlook all commonly default to it. The compose files publish it. A
# published port with nothing behind it is the worst shape this failure can
# take: Docker's proxy accepts the connection on the host and then drops it,
# the client reports something unhelpful, and *nothing appears in any log* --
# the packet never reached Stalwart. So this script creates the listener
# rather than leaving the port a lie:
#
#     name        submission
#     protocol    smtp
#     bind        [::]:587
#     useTls      true       -- TLS is available on the listener
#     tlsImplicit false      -- ...but reached via STARTTLS, not from byte one
#
# What makes it safe to carry a password is not the listener, it is Stalwart's
# own default: before STARTTLS it advertises `AUTH XOAUTH2 OAUTHBEARER` only,
# and answers `AUTH PLAIN` with `554 5.7.8 Authentication mechanism not
# supported.` After STARTTLS the same session advertises `AUTH PLAIN LOGIN
# XOAUTH2 OAUTHBEARER` and accepts the login. Both observed on this exact
# image, and both are checked by verify() below rather than assumed -- a 587
# that answered but did not offer STARTTLS, or that offered PLAIN in the
# clear, would fail this script.
#
# Two mechanics worth knowing before changing any of this:
#
#   * `x:NetworkListener` cannot be touched while the server is in bootstrap
#     mode. It answers `forbidden`, "Only the 'Bootstrap' object type can be
#     modified until the bootstrap process is complete." So on a first boot
#     the listener is created *after* the post-bootstrap restart, and needs a
#     second one.
#   * Creating the listener does not bind the port. The running process keeps
#     the sockets it started with; `x:Action/ReloadSettings` does not rebind
#     either (tried -- 587 stayed refused). Only a restart binds it.
#
# That second point is why a *converging* run will restart an
# already-configured server, which this script otherwise never does. It
# restarts only on the run that actually created the listener, it says so
# before it does it, and `--verify-only` never changes or restarts anything.
# The alternative -- write a configuration that cannot take effect and exit 0
# with 587 still refusing -- is exactly the "it returned 200 so it worked"
# failure the rest of this script exists to prevent, and it would make a
# second run fail rather than converge.
#
# ---------------------------------------------------------------------------
# Relay mode (--relay-host)
# ---------------------------------------------------------------------------
# Most budget VPS providers block outbound port 25 (docs/hosting.md), so the
# recommended deployment sends through a smarthost. In Stalwart v0.16.20
# that is three objects, read from the running server's own schema
# (GET /api/schema, `x:MtaRouteRelay`, `x:MtaTlsStrategy`,
# `x:MtaOutboundStrategy`) and then written and read back live:
#
#   x:MtaRoute          @type Relay, name "relay": address, port, protocol
#                       smtp, implicitTls (465) or not (STARTTLS), auth as
#                       authUsername + authSecret {"@type":"Value","secret"}.
#                       The secret is write-only: reads never return it.
#   x:MtaTlsStrategy    name "relay": startTls "require", DANE and MTA-STS
#                       "disable" (they are for MX delivery; a relay host has
#                       neither and looking them up is wasted DNS),
#                       allowInvalidCerts false. The shipped "default"
#                       strategy is *optional* TLS, and a fallback to
#                       "invalid-tls" on retry -- fine for the open internet,
#                       wrong for a host you are handing a password to.
#   x:MtaOutboundStrategy (singleton): the `route` expression's `else`
#                       becomes 'relay' (local domains still match 'local'
#                       first), and `tls` becomes 'relay' unconditionally --
#                       which also removes the downgrade-on-retry match.
#
# Then x:Action/set {"@type":"ReloadSettings"} so the running server picks
# the change up without a restart (verified: the reload is accepted and the
# next queued message is routed through the relay).
#
# Idempotent: a route named "relay" that already exists is UPDATED with the
# values given (the password included -- it cannot be compared, since it
# is never read back, so it is simply re-applied), the strategy likewise,
# and the outbound singleton is rewritten only when its `else` is not
# already 'relay'. --verify-only reports the current outbound mode either
# way and fails if --relay-host was also given and does not match.
#
# This script does not remove relay mode. To go back to direct delivery,
# set the outbound strategy's route back to 'mx' in Stalwart's admin UI, or
# with x:MtaOutboundStrategy/set -- and read docs/hosting.md about port 25
# and PTR first.
#
# ---------------------------------------------------------------------------
# What it does NOT do
# ---------------------------------------------------------------------------
# It does not create user accounts. `mailosh setup --domain X --email Y`
# already does that (mailosh/cli.py) -- it creates the domain and the
# mailbox and prints the MX/SPF/DKIM/DMARC block to publish -- and it runs
# over the same internal network from the app container:
#
#   docker compose exec -T mailosh mailosh setup --domain example.com \
#       --email you@example.com
#
# It does not configure TLS for the mail ports. Caddy owns 80 and 443, so
# Stalwart can answer neither HTTP-01 nor TLS-ALPN-01 and DNS-01 is the
# supported path -- see docs/operations.md and the deployment spec's 4.2.
# `--request-tls-certificate` exists for the deployment where that is not
# true; it is off by default because on the shipped stack it would send
# Stalwart into a challenge it cannot win.
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   scripts/stalwart-bootstrap.sh --domain example.com [--hostname mail.example.com]
#   scripts/stalwart-bootstrap.sh --domain example.com --verify-only
#   scripts/stalwart-bootstrap.sh --domain example.com \
#       --relay-host smtp.example.net:587 --relay-user USER --relay-password-file FILE
#
#   --relay-host HOST[:PORT]
#                   route ALL remote deliveries through this SMTP smarthost
#                   (Amazon SES, SMTP2GO, your provider's relay -- see
#                   docs/hosting.md). Port defaults to 587. Port 465 means
#                   implicit TLS; any other port means STARTTLS, and TLS is
#                   REQUIRED either way -- a relay that cannot do TLS gets no
#                   mail. Local domains still deliver locally. Safe to re-run:
#                   the route is created once and updated in place after.
#   --relay-user USER
#   --relay-password-file FILE
#                   the relay's SMTP credential. The password is read from
#                   FILE (a file with one line, mode 0600), never from the
#                   command line -- an argv is visible to every process on
#                   the host in `ps`. Both must be given together; without
#                   them the relay is used unauthenticated, which almost no
#                   public relay accepts.
#
#   --domain D      the mail domain this server accepts mail for. Required.
#   --hostname H    the mail server's own FQDN -- its SMTP banner, the name on
#                   its mail-port certificate, and the name the PTR record
#                   must match. Defaults to `mail.<domain>`, which is the
#                   same MX target `mailosh setup` prints in its DNS block,
#                   so the two agree by construction.
#
#                   This is NOT the webmail hostname. MAILOSH_SITE_ADDRESS is
#                   the name Caddy serves the web UI on and gets a real
#                   certificate for (e.g. app.mailosh.com); this is the name
#                   the world's mail servers connect to (mail.mailosh.com).
#                   They may be the same name, but they are usually not, and
#                   conflating them produces a certificate for the wrong one.
#   --request-tls-certificate
#                   ask Stalwart to obtain its own certificate at first boot.
#                   OFF by default -- read the section above before using it.
#   --verify-only   change nothing and restart nothing; report whether this
#                   server is configured and whether its mail listeners --
#                   including 587 -- are actually up. This is the read-only
#                   mode: it will not add the 587 listener, only report that
#                   it is absent.
#   --brief         skip the "what to do next" checklist at the end. For
#                   callers that continue with steps of their own --
#                   scripts/stalwart-init.sh passes it, because "point your
#                   MX at this box" is not advice a development stack needs.
#
# Which stack it acts on is chosen with Compose's own environment variables,
# the same contract scripts/backup.sh and scripts/restore.sh use:
#
#   COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml \
#     scripts/stalwart-bootstrap.sh --domain example.com
#
# Exit codes, so this can be driven from something other than a terminal:
#   0  configured and verified (or already configured exactly as asked)
#   1  usage error, or a prerequisite is missing
#   2  the server did not end up in the expected state -- see the message
#   3  already configured, with a different domain or hostname. Refused.
#   4  Stalwart rejected the admin credential
#   5  Stalwart is not reachable on the internal network
#
# See docs/operations.md, "First boot: configuring Stalwart".

usage() {
	sed -n '/^# Usage$/,/^$/p' "$0" | sed 's/^# \{0,1\}//' | grep -v '^-\{10,\}$'
	exit "${1:-0}"
}

log()  { printf '%s  %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
ok()   { printf '  ok    %s\n' "$*" >&2; }
bad()  { printf '  FAIL  %s\n' "$*" >&2; }
note() { printf '  note  %s\n' "$*" >&2; }

# Every failure path goes through one of these, so that "already configured",
# "wrong credential" and "not reachable" can never collapse into one generic
# message -- they are three different incidents with three different fixes.
die()          { printf '\nstalwart-bootstrap.sh: ERROR: %s\n' "$*" >&2; exit 1; }
die_state()    { printf '\nstalwart-bootstrap.sh: ERROR: %s\n' "$*" >&2; exit 2; }
die_exists()   { printf '\nstalwart-bootstrap.sh: REFUSED: %s\n' "$*" >&2; exit 3; }
die_auth()     { printf '\nstalwart-bootstrap.sh: ERROR: %s\n' "$*" >&2; exit 4; }
die_unreach()  { printf '\nstalwart-bootstrap.sh: ERROR: %s\n' "$*" >&2; exit 5; }

need_cmd() {
	command -v "$1" >/dev/null 2>&1 || die "$1 is required but not on PATH. $2"
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
DOMAIN=""
HOSTNAME_ARG=""
REQUEST_TLS=no
VERIFY_ONLY=no
BRIEF=no
RELAY_HOST=""
RELAY_USER=""
RELAY_PASSWORD_FILE=""
while [ $# -gt 0 ]; do
	case "$1" in
		-h|--help) usage 0 ;;
		--domain)
			[ $# -ge 2 ] || die "--domain needs a value"
			DOMAIN="$2"; shift 2 ;;
		--domain=*)  DOMAIN="${1#--domain=}"; shift ;;
		--hostname)
			[ $# -ge 2 ] || die "--hostname needs a value"
			HOSTNAME_ARG="$2"; shift 2 ;;
		--hostname=*) HOSTNAME_ARG="${1#--hostname=}"; shift ;;
		--relay-host)
			[ $# -ge 2 ] || die "--relay-host needs a value, e.g. email-smtp.eu-west-1.amazonaws.com:587"
			RELAY_HOST="$2"; shift 2 ;;
		--relay-host=*) RELAY_HOST="${1#--relay-host=}"; shift ;;
		--relay-user)
			[ $# -ge 2 ] || die "--relay-user needs a value"
			RELAY_USER="$2"; shift 2 ;;
		--relay-user=*) RELAY_USER="${1#--relay-user=}"; shift ;;
		--relay-password-file)
			[ $# -ge 2 ] || die "--relay-password-file needs a file"
			RELAY_PASSWORD_FILE="$2"; shift 2 ;;
		--relay-password-file=*) RELAY_PASSWORD_FILE="${1#--relay-password-file=}"; shift ;;
		--relay-password|--relay-password=*)
			die "--relay-password is not an option, on purpose: a password on the command line is visible to every process on this host. Put it in a file and pass --relay-password-file." ;;
		--request-tls-certificate) REQUEST_TLS=yes; shift ;;
		--verify-only) VERIFY_ONLY=yes; shift ;;
		--brief) BRIEF=yes; shift ;;
		-*) die "unknown option '$1' (try --help)" ;;
		*)  die "unexpected argument '$1' -- this script takes options only (try --help)" ;;
	esac
done

# Run from the repo root so a relative COMPOSE_FILE (the default
# docker-compose.yml) resolves no matter where the script was invoked from.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Configuration, from the same .env Compose interpolates
# ---------------------------------------------------------------------------
# One source of truth. MAILOSH_STALWART_ADMIN_SECRET is the value the compose
# file already passes to Stalwart as STALWART_RECOVERY_ADMIN; this script
# reads that same variable rather than inventing a second name for the same
# credential. MAILOSH_SITE_ADDRESS is read only to notice when the webmail
# hostname and the mail hostname are the same name -- it is not a default for
# anything here (see SERVER_HOSTNAME below for why).
#
# The environment wins over the file, so a one-off run can override a value
# without editing .env -- which is the opposite of what scripts/stalwart-init.sh
# did (it sourced .env with `set -a`, so the file silently overwrote an
# exported variable, contradicting its own comment).
ENV_FILE="${MAILOSH_ENV_FILE:-$REPO_ROOT/.env}"
if [ -f "$ENV_FILE" ]; then
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in
			''|'#'*) continue ;;
			*'='*) ;;
			*) continue ;;
		esac
		key="${line%%=*}"
		val="${line#*=}"
		# Only the handful this script actually reads, and only when the
		# environment has not already set them. Sourcing the whole file
		# would import everything in it into this shell.
		case "$key" in
			MAILOSH_STALWART_ADMIN_SECRET|MAILOSH_STALWART_ADMIN_USER|MAILOSH_SITE_ADDRESS) ;;
			*) continue ;;
		esac
		[ -n "${!key:-}" ] && continue
		# Strip one layer of surrounding quotes, as Compose's own .env
		# parser does.
		case "$val" in
			\"*\") val="${val%\"}"; val="${val#\"}" ;;
			\'*\') val="${val%\'}"; val="${val#\'}" ;;
		esac
		printf -v "$key" '%s' "$val"
		export "${key?}"
	done < "$ENV_FILE"
fi

ADMIN_USER="${MAILOSH_STALWART_ADMIN_USER:-admin}"
ADMIN_SECRET="${MAILOSH_STALWART_ADMIN_SECRET:-}"

# The mail server's own hostname defaults to `mail.<domain>` because that is
# already this project's convention in the one place it is written down:
# `mailosh setup` prints an MX record pointing at `mail.<domain>`
# (mailosh/cli.py's `_format_dns_block`), and the PTR record has to match the
# MX target. Defaulting to anything else would mean the script and the DNS
# block disagreed by default.
#
# Deliberately NOT MAILOSH_SITE_ADDRESS. That variable is the hostname Caddy
# serves the *webmail* on and obtains a publicly trusted certificate for --
# app.mailosh.com in the reference layout. Stalwart's hostname is the name
# other mail servers connect to. Using the webmail name here would put the
# wrong name in the SMTP banner and on the mail-port certificate, and would
# not match the PTR.
SERVER_HOSTNAME="$HOSTNAME_ARG"

# Stalwart's HTTP port as seen from inside its own container. Not a host
# address, and deliberately not configurable: the whole point is that this
# never needs a published port.
STALWART_INTERNAL_URL="http://localhost:8080"

[ -n "$DOMAIN" ] || die "--domain is required. It is the mail domain this server accepts mail for -- the part after the @ in your addresses, e.g. mailosh.com. It is not the webmail hostname."

# ---------------------------------------------------------------------------
# Validate the two names before anything talks to a server
# ---------------------------------------------------------------------------
# A pasted URL is the likeliest mistake here, and it produces a confusing
# server-side error rather than an obvious one, so catch it locally.
check_name() {
	local what="$1" value="$2"
	case "$value" in
		*://*)  die "$what '$value' looks like a URL. Pass the bare name, e.g. mail.example.com." ;;
		*/*)    die "$what '$value' contains '/'. Pass the bare name, with no scheme and no path." ;;
		*:*)    die "$what '$value' contains ':'. Pass the bare name, with no port." ;;
		*' '*)  die "$what '$value' contains a space." ;;
	esac
	[ "${#value}" -le 253 ] || die "$what is longer than 253 characters."
	printf '%s' "$value" | LC_ALL=C grep -Eq '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$' \
		|| die "$what '$value' is not a fully-qualified domain name (at least two labels, letters/digits/hyphens only)."
}
DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
check_name "--domain" "$DOMAIN"
SERVER_HOSTNAME="$(printf '%s' "${SERVER_HOSTNAME:-mail.$DOMAIN}" | tr '[:upper:]' '[:lower:]')"
check_name "the server hostname" "$SERVER_HOSTNAME"

# Relay arguments. Parsed and validated here, before any server is talked
# to, for the same reason the names are.
RELAY_PORT=""
RELAY_IMPLICIT_TLS=false
if [ -n "$RELAY_HOST" ]; then
	case "$RELAY_HOST" in
		*://*|*/*) die "--relay-host '$RELAY_HOST' looks like a URL. Pass host[:port], e.g. smtp.example.net:587." ;;
		*:*) RELAY_PORT="${RELAY_HOST##*:}"; RELAY_HOST="${RELAY_HOST%:*}" ;;
		*)   RELAY_PORT=587 ;;
	esac
	case "$RELAY_PORT" in
		''|*[!0-9]*) die "--relay-host port '$RELAY_PORT' is not a number." ;;
	esac
	[ "$RELAY_PORT" -ge 1 ] && [ "$RELAY_PORT" -le 65535 ] || die "--relay-host port '$RELAY_PORT' is out of range."
	RELAY_HOST="$(printf '%s' "$RELAY_HOST" | tr '[:upper:]' '[:lower:]')"
	check_name "--relay-host" "$RELAY_HOST"
	# 465 is "submissions": TLS from the first byte. Everything else --
	# 587, 2525, 25 -- is plaintext SMTP upgraded with STARTTLS, which the
	# 'relay' TLS strategy below makes mandatory.
	[ "$RELAY_PORT" = 465 ] && RELAY_IMPLICIT_TLS=true
	if [ -n "$RELAY_USER" ] || [ -n "$RELAY_PASSWORD_FILE" ]; then
		[ -n "$RELAY_USER" ] || die "--relay-password-file was given without --relay-user."
		[ -n "$RELAY_PASSWORD_FILE" ] || die "--relay-user was given without --relay-password-file. The password is read from a file, never from an argument."
		[ -f "$RELAY_PASSWORD_FILE" ] || die "--relay-password-file '$RELAY_PASSWORD_FILE' does not exist."
		[ -r "$RELAY_PASSWORD_FILE" ] || die "--relay-password-file '$RELAY_PASSWORD_FILE' is not readable."
		[ -s "$RELAY_PASSWORD_FILE" ] || die "--relay-password-file '$RELAY_PASSWORD_FILE' is empty."
		case "$RELAY_USER" in
			*'"'*|*'\'*) die "--relay-user may not contain quotes or backslashes." ;;
		esac
	fi
else
	[ -z "$RELAY_USER$RELAY_PASSWORD_FILE" ] || die "--relay-user/--relay-password-file need --relay-host."
fi

# One name for both the webmail and the mail server is a legitimate small
# deployment; it is also what someone who has confused the two ends up with,
# and the two cases look identical from here. Say it once, and move on.
if [ -n "${MAILOSH_SITE_ADDRESS:-}" ] && [ "$MAILOSH_SITE_ADDRESS" = "$SERVER_HOSTNAME" ]; then
	note "the mail hostname and MAILOSH_SITE_ADDRESS are both '$SERVER_HOSTNAME'.
        That works -- Caddy serves the webmail on 443 with a real
        certificate, Stalwart serves the mail ports with its own -- but if
        you meant the webmail to be a separate name (app.$DOMAIN, say), pass
        --hostname mail.$DOMAIN and fix MAILOSH_SITE_ADDRESS."
fi

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
need_cmd docker "Install Docker Desktop or the docker engine."
need_cmd python3 "It parses the JMAP responses this script reads."
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is required; 'docker-compose' v1 is not supported."
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable. Start Docker and retry."
docker compose config -q 2>/dev/null \
	|| die "'docker compose config' failed here. Wrong directory, or COMPOSE_FILE points somewhere unreadable?"

PROJECT="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
[ -n "$PROJECT" ] || die "could not determine the compose project name."
docker compose config --services | grep -qx stalwart \
	|| die "compose project '$PROJECT' has no 'stalwart' service. This script configures Mailosh's own mail server."

if [ -z "$ADMIN_SECRET" ] || [ "$ADMIN_SECRET" = changeme ]; then
	die "MAILOSH_STALWART_ADMIN_SECRET is unset or still the placeholder 'changeme'. Generate one with 'openssl rand -hex 24', put it in $ENV_FILE, and bring the stack up again -- Stalwart reads STALWART_RECOVERY_ADMIN only at container start."
fi

# ---------------------------------------------------------------------------
# Talking to Stalwart, over its own loopback, with the credential on stdin
# ---------------------------------------------------------------------------
sw_exec() { docker compose exec -T stalwart "$@"; }

# curl config files take `name = "value"` with backslash escapes, so both
# metacharacters have to be escaped or a secret containing one would be
# truncated (and the failure would look like a wrong password).
cfg_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

JMAP_BODY=""
JMAP_CODE=""
jmap_call() {
	# $1 = JSON request body. Sets JMAP_BODY and JMAP_CODE; never prints.
	# `HTTP:000` means curl never got a response at all.
	#
	# The body travels in the same stdin config file as the credential
	# (`data-binary = "..."`), not as a `--data-binary` argument: since relay
	# mode, a request body can carry the smarthost password, and `docker
	# exec`'s argv is visible in `ps` on the host. Verified against curl in
	# the stalwart image that a JSON body with escaped quotes and
	# backslashes arrives byte for byte this way.
	local raw
	raw="$(
		{
			printf 'user = "%s:%s"\n' "$(cfg_escape "$ADMIN_USER")" "$(cfg_escape "$ADMIN_SECRET")"
			printf 'data-binary = "%s"\n' "$(cfg_escape "$1")"
		} | sw_exec curl -sS -K - \
			-X POST -H 'content-type: application/json' \
			-w '\nHTTP:%{http_code}' \
			"$STALWART_INTERNAL_URL/jmap" 2>/dev/null || true
	)"
	JMAP_CODE="$(printf '%s' "$raw" | tail -1 | sed -n 's/^HTTP://p')"
	JMAP_BODY="$(printf '%s' "$raw" | sed '$d')"
	[ -n "$JMAP_CODE" ] || JMAP_CODE=000
}

# One JMAP request, one method call, with the admin account id already
# substituted. Keeps the call sites readable.
ACCOUNT_ID=""
jmap_method() {
	# $1 = method name, $2 = the method's arguments object *without* accountId
	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"$1\",{\"accountId\":\"$ACCOUNT_ID\",$2},\"c0\"]]}"
}

# Every response goes through python3 rather than grep: a JMAP response can
# carry a top-level error, a per-method error, or a `notUpdated` entry, and
# treating "HTTP 200" as success is precisely the failure this script exists
# to prevent. (`x:Bootstrap/set` answers 200 with a `notUpdated` body when it
# rejects the hostname -- observed, not imagined.)
#
# It also means nothing from a response reaches the terminal unless this
# script chose to put it there: the successful `x:Bootstrap/set` response
# contains a freshly generated admin password, and must never be echoed.
#
# The parser lives in a temp file rather than a `python3 - <<PY` heredoc:
# a heredoc *is* stdin, so it would displace the piped response body and
# python would read its own program where the JSON should be. (shellcheck
# SC2259 catches exactly this; it caught it here.) The response body
# therefore reaches python on stdin and never on an argv -- same reason the
# credential does not: `docker exec` argv is visible in `ps` on the host, and
# one of these bodies contains a generated password.
JMAP_PARSER="$(mktemp "${TMPDIR:-/tmp}/mailosh-stalwart-bootstrap.XXXXXX")"
trap 'rm -f "$JMAP_PARSER"' EXIT
cat > "$JMAP_PARSER" <<'PY'
import json, sys

mode = sys.argv[1]
try:
    doc = json.load(sys.stdin)
except Exception:
    print("ERR|response was not JSON")
    raise SystemExit(0)

responses = doc.get("methodResponses")
if not isinstance(responses, list) or not responses:
    print("ERR|%s" % doc.get("detail") or doc.get("type") or "no methodResponses in the reply")
    raise SystemExit(0)
name, args = responses[0][0], responses[0][1]
if name == "error":
    print("ERR|%s %s" % (args.get("type", "error"), args.get("description", "")))
    raise SystemExit(0)

if mode == "bootstrap-state":
    # "pending"  -> still in bootstrap mode (the singleton is readable)
    # "done"     -> configured (the singleton is gone)
    if args.get("list"):
        print("OK|pending")
    elif "singleton" in (args.get("notFound") or []):
        print("OK|done")
    else:
        print("ERR|x:Bootstrap/get returned neither the singleton nor notFound")
elif mode == "bootstrap-set":
    if "singleton" in (args.get("updated") or {}):
        print("OK|")
    else:
        bad = (args.get("notUpdated") or {}).get("singleton") or {}
        detail = bad.get("description") or bad.get("type") or "no reason given"
        props = ",".join(bad.get("properties") or [])
        print("ERR|%s%s" % (detail, (" [%s]" % props) if props else ""))
elif mode == "system-settings":
    items = args.get("list") or []
    if not items:
        print("ERR|x:SystemSettings/get returned nothing")
    else:
        s = items[0]
        print("OK|%s\t%s" % (s.get("defaultHostname") or "", s.get("defaultDomainId") or ""))
elif mode == "domains":
    # second method response: the /get that followed the /query
    if len(responses) < 2:
        print("ERR|x:Domain/get did not run")
    else:
        got = responses[1][1]
        if responses[1][0] == "error":
            print("ERR|%s" % got.get("description", "x:Domain/get failed"))
        else:
            names = ["%s\t%s" % (d.get("id", ""), d.get("name", "")) for d in (got.get("list") or [])]
            print("OK|" + "\n".join(names))
elif mode == "listener-set":
    if args.get("created"):
        print("OK|")
    else:
        bad = (args.get("notCreated") or {}).get("submission") or {}
        detail = bad.get("description") or bad.get("type") or "no reason given"
        props = ",".join(bad.get("properties") or [])
        print("ERR|%s%s" % (detail, (" [%s]" % props) if props else ""))
elif mode == "listeners":
    if len(responses) < 2:
        print("ERR|x:NetworkListener/get did not run")
    else:
        got = responses[1][1]
        if responses[1][0] == "error":
            print("ERR|%s" % got.get("description", "x:NetworkListener/get failed"))
        else:
            rows = []
            for item in got.get("list") or []:
                for bind in (item.get("bind") or {}):
                    port = bind.rsplit(":", 1)[-1]
                    rows.append("%s\t%s\t%s" % (item.get("name", "?"), port, item.get("protocol", "?")))
            print("OK|" + "\n".join(rows))
elif mode == "tracers":
    # second method response: the /get that followed the /query. One row per
    # tracer: id, variant, enabled, level, path (log-file variant only).
    if len(responses) < 2:
        print("ERR|x:Tracer/get did not run")
    else:
        got = responses[1][1]
        if responses[1][0] == "error":
            print("ERR|%s" % got.get("description", "x:Tracer/get failed"))
        else:
            rows = []
            for item in got.get("list") or []:
                rows.append("%s\t%s\t%s\t%s\t%s" % (
                    item.get("id", "?"), item.get("@type", "?"),
                    "yes" if item.get("enable") else "no",
                    item.get("level", "?"), item.get("path", "")))
            print("OK|" + "\n".join(rows))
elif mode in ("tracer-set", "set-any"):
    # Any x:*/set whose success is "something was created or updated".
    if args.get("created") or args.get("updated"):
        print("OK|")
    else:
        bad = args.get("notCreated") or args.get("notUpdated") or {}
        bad = next(iter(bad.values()), {}) if isinstance(bad, dict) else {}
        detail = bad.get("description") or bad.get("type") or "no reason given"
        props = ",".join(bad.get("properties") or [])
        print("ERR|%s%s" % (detail, (" [%s]" % props) if props else ""))
elif mode == "mta-routes":
    # second method response: the /get that followed the /query. One row per
    # route: id, name, @type, address, port, implicitTls, authUsername.
    if len(responses) < 2:
        print("ERR|x:MtaRoute/get did not run")
    else:
        got = responses[1][1]
        if responses[1][0] == "error":
            print("ERR|%s" % got.get("description", "x:MtaRoute/get failed"))
        else:
            rows = []
            for item in got.get("list") or []:
                rows.append("%s\t%s\t%s\t%s\t%s\t%s\t%s" % (
                    item.get("id", "?"), item.get("name", "?"), item.get("@type", "?"),
                    item.get("address") or "", item.get("port") or "",
                    "yes" if item.get("implicitTls") else "no",
                    item.get("authUsername") or ""))
            print("OK|" + "\n".join(rows))
elif mode == "tls-strategies":
    if len(responses) < 2:
        print("ERR|x:MtaTlsStrategy/get did not run")
    else:
        got = responses[1][1]
        if responses[1][0] == "error":
            print("ERR|%s" % got.get("description", "x:MtaTlsStrategy/get failed"))
        else:
            rows = ["%s\t%s\t%s" % (i.get("id", "?"), i.get("name", "?"), i.get("startTls", "?"))
                    for i in (got.get("list") or [])]
            print("OK|" + "\n".join(rows))
elif mode == "outbound-strategy":
    # The singleton's route and tls expressions, reduced to their `else`
    # branch -- the branch every non-local recipient takes -- with the
    # surrounding quotes Stalwart's expression language puts on a literal
    # stripped: 'mx' -> mx.
    items = args.get("list") or []
    if not items:
        print("ERR|x:MtaOutboundStrategy/get returned nothing")
    else:
        s = items[0]
        def leaf(expr):
            return ((expr or {}).get("else") or "").strip().strip("'")
        print("OK|%s\t%s" % (leaf(s.get("route")), leaf(s.get("tls"))))
else:
    print("ERR|unknown mode %s" % mode)
PY

jmap_extract() {
	# $1 = extraction mode; reads JMAP_BODY on stdin.
	printf '%s' "$JMAP_BODY" | python3 "$JMAP_PARSER" "$1"
}

# Splits jmap_extract's "OK|payload" / "ERR|reason" into two globals.
X_OK=no
X_VAL=""
extract() {
	local out
	out="$(jmap_extract "$1")"
	case "$out" in
		OK\|*)  X_OK=yes; X_VAL="${out#OK|}" ;;
		*)      X_OK=no;  X_VAL="${out#ERR|}" ;;
	esac
}

# ---------------------------------------------------------------------------
# Is the server there at all?
# ---------------------------------------------------------------------------
# Three different answers, three different messages. This is the first of
# them: nothing to talk to.
STALWART_CID="$(docker compose ps -q stalwart 2>/dev/null || true)"
if [ -z "$STALWART_CID" ]; then
	die_unreach "the 'stalwart' service is not running in compose project '$PROJECT'.
  Bring the stack up first:
    docker compose up -d          (add your -f overlays, or set COMPOSE_FILE)
  and check 'docker compose ps'. If you meant a different stack, set
  COMPOSE_PROJECT_NAME / COMPOSE_FILE -- this script targets whatever
  'docker compose config' resolves to, which here was '$PROJECT'."
fi

log "target: compose project '$PROJECT', service 'stalwart' (container ${STALWART_CID:0:12})"

wait_for_http() {
	for _ in $(seq 1 45); do
		if sw_exec curl -fsS -m 5 -o /dev/null "$STALWART_INTERNAL_URL/healthz/live" 2>/dev/null; then
			return 0
		fi
		sleep 2
	done
	return 1
}

restart_stalwart() {
	# $1 = why, for the log line. Every restart in this script goes through
	# here so there is exactly one place that knows how to wait for the
	# server to come back, and exactly one place to look for "what restarts
	# this thing".
	log "restarting stalwart $1"
	docker compose restart stalwart >/dev/null 2>&1 \
		|| die_state "could not restart the stalwart service. The configuration is saved; restart it by hand:
      docker compose restart stalwart"
	wait_for_http || die_state "stalwart did not answer /healthz/live within 90s after the restart.
  The configuration was saved, so this is a startup problem rather than a
  setup one:
      docker compose logs --tail 50 stalwart"
}

wait_for_http || die_unreach "the stalwart container is running, but nothing answered
  GET $STALWART_INTERNAL_URL/healthz/live from inside it within 90s.
  That is the mail server's own liveness endpoint, probed on its own
  loopback -- no published port is involved, so this is not a firewall or a
  port-mapping problem. Look at the container:
    docker compose logs --tail 50 stalwart
    docker compose ps"

# ---------------------------------------------------------------------------
# Is the credential right?
# ---------------------------------------------------------------------------
# Second of the three answers. Note that /jmap/session answers 200 to an
# *unauthenticated* request while the server is in bootstrap mode (verified),
# so it cannot be used as the credential probe. x:Bootstrap/get can: it
# answers 401 to a missing or wrong credential in both modes.
jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:Bootstrap/get\",{\"accountId\":\"unused\",\"ids\":[\"singleton\"]},\"c0\"]]}"
if [ "$JMAP_CODE" = 401 ] || [ "$JMAP_CODE" = 403 ]; then
	die_auth "Stalwart rejected the admin credential (HTTP $JMAP_CODE) for user '$ADMIN_USER'.
  The server is up and reachable on the internal network, and it is not
  already configured-or-not -- this is purely the credential.

  Where the value came from on this run: the environment if it was exported
  there, otherwise MAILOSH_STALWART_ADMIN_SECRET in
    $ENV_FILE

  What is usually wrong:
    * The value there is not the one the *running container* was started
      with. Stalwart reads STALWART_RECOVERY_ADMIN once, at container start,
      so editing .env changes nothing until the container is recreated.
    * MAILOSH_STALWART_ADMIN_USER is not '$ADMIN_USER' on this server.

  The fix, which also covers 'I have lost the secret' -- the recovery admin
  is whatever the running container was handed, it is not stored anywhere
  else, so setting it again is enough:

      1. put the intended value in $ENV_FILE
      2. docker compose up -d stalwart      # recreates it with the new value
      3. re-run this script

  Do not print either value to compare them, and note that this script never
  does: the recovery admin can mint an API key for every mailbox on the
  server, and a terminal scrollback is not a place to keep it."
fi
if [ "$JMAP_CODE" = 000 ]; then
	die_unreach "curl inside the stalwart container could not reach $STALWART_INTERNAL_URL/jmap
  even though /healthz/live answered. That is unusual; check
    docker compose logs --tail 50 stalwart"
fi
[ "$JMAP_CODE" = 200 ] || die_state "Stalwart answered HTTP $JMAP_CODE to x:Bootstrap/get, which this script does not know how to interpret. Check 'docker compose logs stalwart'."

# The admin credential's own JMAP account id, required as `accountId` on every
# x: call. Resolved rather than hardcoded -- it has been "d333333" on every
# fresh install seen, and that is not a guarantee.
ACCOUNT_ID="$(
	printf 'user = "%s:%s"\n' "$(cfg_escape "$ADMIN_USER")" "$(cfg_escape "$ADMIN_SECRET")" \
	| sw_exec curl -sS -K - "$STALWART_INTERNAL_URL/jmap/session" 2>/dev/null \
	| python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(iter(d.get("accounts") or {"":0})))' 2>/dev/null || true
)"
[ -n "$ACCOUNT_ID" ] || die_state "could not read the admin account id from $STALWART_INTERNAL_URL/jmap/session."

# ---------------------------------------------------------------------------
# Verification. Used after a bootstrap, on an already-configured server, and
# on its own with --verify-only.
# ---------------------------------------------------------------------------
# The point of this section: a 200 from x:Bootstrap/set proves nothing. A
# server can accept the call, fail to restart, and sit in bootstrap mode with
# no mail listeners while a script that only checked HTTP status exits 0.
# Everything here is read back from the running server, and the last two
# checks are live TCP connections to the mail ports.

probe_port() {
	# 0 = something accepted a connection on that port *inside the container*.
	#
	# Inside, deliberately. A published port answers from the host even when
	# nothing is listening behind it -- Docker's proxy accepts the connection
	# and then drops it, so `curl telnet://127.0.0.1:25` from the host
	# succeeded against a Stalwart that was still in bootstrap mode with 25
	# refused internally. A host-side probe of a published port is not a
	# listener check.
	#
	# `</dev/null` is load-bearing, not defensive: `docker compose exec -T`
	# forwards stdin, and this is called from inside a `while read` loop over
	# the listener list. Without it the first probe swallows the rest of the
	# loop's input and only one listener is ever checked -- which is exactly
	# how it failed the first time it was run.
	sw_exec timeout 10 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" >/dev/null 2>&1 </dev/null
}

smtp_ehlo() {
	# $1 = port. Prints the SMTP greeting, then every line of the EHLO
	# response, one per line, with the CRs stripped. Empty output means the
	# port said nothing (or is not there at all).
	#
	# Inside the container, for the same reason probe_port is: a published
	# port answers a host-side connect whether or not anything is listening
	# behind it. This is the check that a listener is a *working SMTP server*
	# rather than an open socket, and on 587 it is also the check that
	# STARTTLS is offered before any credential could be sent.
	#
	# The port reaches the inner shell as an argument ($1 there, set by the
	# `probe` placeholder that becomes its $0) rather than by string
	# interpolation, so nothing from this script's scope is spliced into the
	# program text.
	#
	# shellcheck disable=SC2016  # $1 and $line are expanded by the container's bash, not here
	sw_exec timeout 10 bash -c '
		exec 3<>/dev/tcp/127.0.0.1/"$1"
		IFS= read -r -t 5 line <&3 && printf "%s\n" "$line"
		printf "EHLO mailosh-bootstrap-probe\r\n" >&3
		while IFS= read -r -t 5 line <&3; do
			printf "%s\n" "$line"
			case "$line" in 250\ *) break ;; esac
		done
		printf "QUIT\r\n" >&3
	' probe "$1" 2>/dev/null </dev/null | tr -d '\r'
}

# Filled in by verify(); read by the summary.
VERIFIED_HOSTNAME=""
VERIFIED_DOMAINS=""
VERIFY_FAILURES=0

read_state() {
	# Reads hostname + domain list into VERIFIED_*. Fatal on a broken read --
	# not being able to see the configuration is itself a failure.
	jmap_method "x:SystemSettings/get" '"ids":["singleton"]'
	extract system-settings
	[ "$X_OK" = yes ] || die_state "could not read x:SystemSettings: $X_VAL"
	VERIFIED_HOSTNAME="$(printf '%s' "$X_VAL" | cut -f1)"

	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:Domain/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:Domain/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:Domain/query\",\"path\":\"/ids\"},\"properties\":[\"id\",\"name\"]},\"c1\"]]}"
	extract domains
	[ "$X_OK" = yes ] || die_state "could not read the domain list: $X_VAL"
	VERIFIED_DOMAINS="$(printf '%s' "$X_VAL" | cut -f2 | grep -v '^$' || true)"
}

# ---------------------------------------------------------------------------
# The 587 submission listener
# ---------------------------------------------------------------------------
# Read the header section of the same name for why this exists at all. This
# is the part that has to be idempotent: it creates the listener only when
# no listener binds 587, so a second run finds it and changes nothing.
SUBMISSION_PORT=587
SUBMISSION_LISTENER=submission
LISTENER_CREATED=no

jmap_listeners() {
	# The configured listeners, as "name<TAB>port<TAB>protocol" rows once
	# `extract listeners` has parsed the reply. One definition, two callers.
	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:NetworkListener/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:NetworkListener/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:NetworkListener/query\",\"path\":\"/ids\"},\"properties\":[\"name\",\"bind\",\"protocol\"]},\"c1\"]]}"
}

ensure_submission_listener() {
	# Never called with --verify-only: that mode changes nothing.
	jmap_listeners
	extract listeners
	[ "$X_OK" = yes ] || die_state "could not read the configured listeners: $X_VAL"

	# The whole list is scanned before anything is decided, so the outcome
	# does not depend on the order the server happens to return listeners in.
	local rows="$X_VAL" lname lport lproto
	local bound_by="" name_taken_on=""
	while IFS=$'\t' read -r lname lport lproto; do
		[ -n "$lport" ] || continue
		if [ "$lport" = "$SUBMISSION_PORT" ]; then
			bound_by="$lname ($lproto)"
		fi
		if [ "$lname" = "$SUBMISSION_LISTENER" ]; then
			name_taken_on="$lport"
		fi
	done <<-EOF
	$rows
	EOF

	if [ -n "$bound_by" ]; then
		log "listener '$bound_by' already binds $SUBMISSION_PORT -- leaving it alone"
		return 0
	fi
	if [ -n "$name_taken_on" ]; then
		# A hand-edited config that took the name for something else.
		# Creating a second listener with the same name would fail, and
		# rewriting someone else's listener is not this script's business.
		note "a listener named '$SUBMISSION_LISTENER' already exists and binds
        $name_taken_on, not $SUBMISSION_PORT. Nothing was changed -- add the $SUBMISSION_PORT
        listener yourself in Stalwart's admin UI, or rename that one. The
        verification below will report $SUBMISSION_PORT as missing until you do."
		return 0
	fi

	log "adding a submission listener on $SUBMISSION_PORT (SMTP, STARTTLS -- not implicit TLS)"
	jmap_method "x:NetworkListener/set" "\"create\":{\"$SUBMISSION_LISTENER\":{\"name\":\"$SUBMISSION_LISTENER\",\"protocol\":\"smtp\",\"bind\":{\"[::]:$SUBMISSION_PORT\":true},\"useTls\":true,\"tlsImplicit\":false}}"
	[ "$JMAP_CODE" = 200 ] || die_state "x:NetworkListener/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
	# Same trap as x:Bootstrap/set: 200 with a notCreated body is a rejection.
	extract listener-set
	[ "$X_OK" = yes ] || die_state "Stalwart refused to create the $SUBMISSION_PORT listener: $X_VAL"
	LISTENER_CREATED=yes
}

# ---------------------------------------------------------------------------
# Logging that actually goes somewhere
# ---------------------------------------------------------------------------
# A configured Stalwart ships one tracer: a rotating log *file* under
# /var/log/stalwart/. That directory does not exist in the container image
# and nothing in docker-compose.yml mounts it, so every line the server
# wrote after first boot -- every delivery, every rejection, every TLS
# handshake -- went nowhere, silently. Found on the first public deployment,
# while looking for a message that had in fact arrived. `docker logs` is
# where an operator of this stack looks, so a stdout tracer is what gets
# created, and a file tracer aimed at a directory that is not there is
# disabled rather than left to fail quietly forever.
TRACER_CHANGED=no
ensure_stdout_tracer() {
	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:Tracer/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:Tracer/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:Tracer/query\",\"path\":\"/ids\"},\"properties\":[\"@type\",\"enable\",\"level\",\"path\"]},\"c1\"]]}"
	[ "$JMAP_CODE" = 200 ] || die_state "x:Tracer/query answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
	extract tracers
	[ "$X_OK" = yes ] || die_state "could not list Stalwart's tracers: $X_VAL"
	local rows="$X_VAL" have_stdout=no tid ttype tenabled tlevel tpath
	while IFS="$(printf '\t')" read -r tid ttype tenabled tlevel tpath; do
		[ -n "$tid" ] || continue
		if [ "$ttype" = Stdout ] && [ "$tenabled" = yes ]; then
			have_stdout=yes
		fi
		if [ "$ttype" = Log ] && [ "$tenabled" = yes ] && [ -n "$tpath" ] && ! sw_exec test -d "$tpath"; then
			log "disabling the log-file tracer: its directory $tpath does not exist in the container"
			jmap_method "x:Tracer/set" "\"update\":{\"$tid\":{\"enable\":false}}"
			[ "$JMAP_CODE" = 200 ] || die_state "x:Tracer/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
			extract tracer-set
			[ "$X_OK" = yes ] || die_state "Stalwart refused to disable tracer $tid: $X_VAL"
			TRACER_CHANGED=yes
		fi
	done <<-EOF
	$rows
	EOF
	if [ "$have_stdout" = yes ]; then
		log "a stdout tracer is already enabled -- 'docker compose logs stalwart' will show mail activity"
		return 0
	fi
	log "adding a stdout tracer (level info) so 'docker compose logs stalwart' shows mail activity"
	jmap_method "x:Tracer/set" "\"create\":{\"stdout\":{\"@type\":\"Stdout\",\"enable\":true,\"level\":\"info\",\"ansi\":false,\"lossy\":false,\"multiline\":false}}"
	[ "$JMAP_CODE" = 200 ] || die_state "x:Tracer/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
	extract tracer-set
	[ "$X_OK" = yes ] || die_state "Stalwart refused to create the stdout tracer: $X_VAL"
	TRACER_CHANGED=yes
}

# ---------------------------------------------------------------------------
# Relay mode -- see the header section of the same name
# ---------------------------------------------------------------------------
RELAY_ROUTE=relay
RELAY_CHANGED=no

jmap_mta_routes() {
	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:MtaRoute/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:MtaRoute/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:MtaRoute/query\",\"path\":\"/ids\"},\"properties\":[\"name\",\"@type\",\"address\",\"port\",\"implicitTls\",\"authUsername\"]},\"c1\"]]}"
}
jmap_tls_strategies() {
	jmap_call "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":[[\"x:MtaTlsStrategy/query\",{\"accountId\":\"$ACCOUNT_ID\"},\"c0\"],[\"x:MtaTlsStrategy/get\",{\"accountId\":\"$ACCOUNT_ID\",\"#ids\":{\"resultOf\":\"c0\",\"name\":\"x:MtaTlsStrategy/query\",\"path\":\"/ids\"},\"properties\":[\"name\",\"startTls\"]},\"c1\"]]}"
}

# Filled by read_relay_state(): the configured relay route, if any, and the
# outbound strategy's route/tls leaves ("mx"/"default" on a stock server).
RELAY_ROW=""
OUTBOUND_ROUTE=""
OUTBOUND_TLS=""
read_relay_state() {
	jmap_mta_routes
	extract mta-routes
	[ "$X_OK" = yes ] || die_state "could not read the MTA routes: $X_VAL"
	RELAY_ROW="$(printf '%s\n' "$X_VAL" | awk -F'\t' -v n="$RELAY_ROUTE" '$2 == n && $3 == "Relay"' | head -1)"
	jmap_method "x:MtaOutboundStrategy/get" '"ids":["singleton"]'
	extract outbound-strategy
	[ "$X_OK" = yes ] || die_state "could not read x:MtaOutboundStrategy: $X_VAL"
	OUTBOUND_ROUTE="$(printf '%s' "$X_VAL" | cut -f1)"
	OUTBOUND_TLS="$(printf '%s' "$X_VAL" | cut -f2)"
}

# The relay route's properties as a JSON object fragment (no surrounding
# braces, no `name`), built by python so the password -- read from the file
# here, and nowhere else -- is JSON-escaped correctly whatever it contains.
# The file path and the other fields go on argv; the password never does.
relay_route_json() {
	python3 - "$RELAY_HOST" "$RELAY_PORT" "$RELAY_IMPLICIT_TLS" "$RELAY_USER" "$RELAY_PASSWORD_FILE" <<'PY'
import json, sys
host, port, implicit, user, pwfile = sys.argv[1:6]
obj = {
    "@type": "Relay",
    "description": "Smarthost for all remote delivery (scripts/stalwart-bootstrap.sh --relay-host)",
    "address": host,
    "port": int(port),
    "protocol": "smtp",
    "implicitTls": implicit == "true",
    "allowInvalidCerts": False,
}
if user:
    with open(pwfile, encoding="utf-8") as f:
        secret = f.read()
    # One trailing newline is what an editor leaves; strip exactly that,
    # not every whitespace character, so a password ending in a space
    # survives.
    if secret.endswith("\n"):
        secret = secret[:-1]
    obj["authUsername"] = user
    obj["authSecret"] = {"@type": "Value", "secret": secret}
else:
    obj["authUsername"] = None
    obj["authSecret"] = {"@type": "None"}
print(json.dumps(obj)[1:-1])
PY
}

ensure_relay() {
	# Never called with --verify-only: that mode changes nothing.
	read_relay_state
	local props
	props="$(relay_route_json)" || die "could not read the relay password from $RELAY_PASSWORD_FILE"

	# 1. The route. Create if absent, otherwise update in place -- `name` is
	#    immutable and identifies it, everything else is re-applied.
	if [ -z "$RELAY_ROW" ]; then
		log "adding MTA route '$RELAY_ROUTE' -> $RELAY_HOST:$RELAY_PORT ($([ "$RELAY_IMPLICIT_TLS" = true ] && echo 'implicit TLS' || echo 'STARTTLS')${RELAY_USER:+, auth as $RELAY_USER})"
		jmap_method "x:MtaRoute/set" "\"create\":{\"$RELAY_ROUTE\":{\"name\":\"$RELAY_ROUTE\",$props}}"
	else
		local rid
		rid="$(printf '%s' "$RELAY_ROW" | cut -f1)"
		log "updating MTA route '$RELAY_ROUTE' ($rid) -> $RELAY_HOST:$RELAY_PORT"
		jmap_method "x:MtaRoute/set" "\"update\":{\"$rid\":{$props}}"
	fi
	[ "$JMAP_CODE" = 200 ] || die_state "x:MtaRoute/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
	extract set-any
	[ "$X_OK" = yes ] || die_state "Stalwart refused the relay route: $X_VAL"
	RELAY_CHANGED=yes

	# 2. The TLS strategy the relay connection will use. Same create-or-
	#    update shape.
	jmap_tls_strategies
	extract tls-strategies
	[ "$X_OK" = yes ] || die_state "could not read the TLS strategies: $X_VAL"
	local sid
	sid="$(printf '%s\n' "$X_VAL" | awk -F'\t' -v n="$RELAY_ROUTE" '$2 == n {print $1}' | head -1)"
	local sprops='"description":"Relay host: TLS required, no DANE/MTA-STS lookups (scripts/stalwart-bootstrap.sh)","startTls":"require","dane":"disable","mtaSts":"disable","allowInvalidCerts":false,"mtaStsTimeout":300000,"tlsTimeout":180000'
	if [ -z "$sid" ]; then
		log "adding TLS strategy '$RELAY_ROUTE' (TLS required)"
		jmap_method "x:MtaTlsStrategy/set" "\"create\":{\"$RELAY_ROUTE\":{\"name\":\"$RELAY_ROUTE\",$sprops}}"
	else
		jmap_method "x:MtaTlsStrategy/set" "\"update\":{\"$sid\":{$sprops}}"
	fi
	[ "$JMAP_CODE" = 200 ] || die_state "x:MtaTlsStrategy/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
	extract set-any
	[ "$X_OK" = yes ] || die_state "Stalwart refused the relay TLS strategy: $X_VAL"

	# 3. Point every non-local delivery at it. The `match` list is written
	#    as the index-keyed map the server itself returns it as (the same
	#    idiom x:Account's `credentials` uses); a JSON array is rejected.
	if [ "$OUTBOUND_ROUTE" = "$RELAY_ROUTE" ] && [ "$OUTBOUND_TLS" = "$RELAY_ROUTE" ]; then
		log "outbound strategy already routes remote mail via '$RELAY_ROUTE' -- leaving it alone"
	else
		log "routing all remote delivery through '$RELAY_ROUTE' (was: route '$OUTBOUND_ROUTE', tls '$OUTBOUND_TLS')"
		jmap_method "x:MtaOutboundStrategy/set" "\"update\":{\"singleton\":{\"route\":{\"match\":{\"0\":{\"if\":\"is_local_domain(rcpt_domain)\",\"then\":\"'local'\"}},\"else\":\"'$RELAY_ROUTE'\"},\"tls\":{\"match\":{},\"else\":\"'$RELAY_ROUTE'\"}}}"
		[ "$JMAP_CODE" = 200 ] || die_state "x:MtaOutboundStrategy/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
		extract set-any
		[ "$X_OK" = yes ] || die_state "Stalwart refused the outbound strategy: $X_VAL"
	fi

	# 4. Apply. Route and strategy objects are configuration; the running
	#    server reads them again on ReloadSettings (a restart is not needed
	#    -- unlike a listener, nothing has to rebind).
	jmap_method "x:Action/set" '"create":{"reload":{"@type":"ReloadSettings"}}'
	[ "$JMAP_CODE" = 200 ] || die_state "x:Action/set ReloadSettings answered HTTP $JMAP_CODE."
	extract set-any
	[ "$X_OK" = yes ] || die_state "Stalwart refused to reload its settings: $X_VAL"
	log "settings reloaded"
}

verify() {
	VERIFY_FAILURES=0
	printf '\nVerifying, against the running server:\n' >&2

	# 1. Out of bootstrap mode.
	jmap_method "x:Bootstrap/get" '"ids":["singleton"]'
	extract bootstrap-state
	if [ "$X_OK" = yes ] && [ "$X_VAL" = "done" ]; then
		ok "server is out of bootstrap mode"
	else
		bad "server is STILL IN BOOTSTRAP MODE -- it is running no mail listeners"
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	fi

	# 2. The hostname it will put in its SMTP banner and on its certificate.
	if [ "$VERIFIED_HOSTNAME" = "$SERVER_HOSTNAME" ]; then
		ok "server hostname is '$VERIFIED_HOSTNAME'"
	else
		bad "server hostname is '${VERIFIED_HOSTNAME:-<unset>}', expected '$SERVER_HOSTNAME'"
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	fi

	# 3. The domain exists. Without it the server accepts mail for nobody.
	if printf '%s\n' "$VERIFIED_DOMAINS" | grep -qx -- "$DOMAIN"; then
		ok "domain '$DOMAIN' is configured"
	else
		bad "domain '$DOMAIN' is NOT configured (found: $(printf '%s' "$VERIFIED_DOMAINS" | tr '\n' ' '))"
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	fi

	# 4. Every listener the server says it has configured is actually bound.
	#    Reading the configured set rather than a hardcoded list means this
	#    stays correct on a server whose listeners someone has edited.
	jmap_listeners
	extract listeners
	local listener_ports=""
	if [ "$X_OK" = yes ] && [ -n "$X_VAL" ]; then
		local lname lport lproto
		while IFS=$'\t' read -r lname lport lproto; do
			[ -n "$lport" ] || continue
			listener_ports="$listener_ports $lport"
			if probe_port "$lport"; then
				ok "listener '$lname' ($lproto) is accepting connections on $lport"
			else
				bad "listener '$lname' ($lproto) is configured on $lport but NOTHING is listening there"
				VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
			fi
		done <<-EOF
		$X_VAL
		EOF
	else
		bad "could not read the configured listeners: ${X_VAL:-empty reply}"
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	fi

	# 5. Port 25 specifically. Everything else on this list is a convenience;
	#    without 25 the domain receives no mail, which is the whole failure
	#    this script exists to prevent.
	case " $listener_ports " in
		*" 25 "*) ;;
		*)
			bad "no listener is configured on port 25 -- this server cannot receive mail"
			VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
			;;
	esac

	# 6. The end-to-end check: talk SMTP on 25 and read the greeting. It proves
	#    the listener is a working SMTP server *and* that the hostname the
	#    world will see is the one that was asked for -- one probe covering
	#    both, from outside the configuration API.
	local ehlo25 banner
	ehlo25="$(smtp_ehlo 25 || true)"
	banner="$(printf '%s\n' "$ehlo25" | head -1)"
	if [ -z "$banner" ]; then
		bad "port 25 sent no SMTP greeting"
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	elif printf '%s' "$banner" | grep -qi -- "$SERVER_HOSTNAME"; then
		ok "SMTP greeting on 25: $banner"
	else
		bad "SMTP greeting on 25 does not name '$SERVER_HOSTNAME': $banner"
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	fi

	# 7. Port 587 is the one the compose files publish for mail clients, so
	#    "a socket accepted the connection" is not enough: it has to offer
	#    STARTTLS, and it must not offer a password mechanism before the
	#    session is encrypted. Checked here rather than trusted, because
	#    both are properties of the running server and not of the object
	#    that was written.
	case " $listener_ports " in
		*" $SUBMISSION_PORT "*)
			local ehlo587 auth587
			ehlo587="$(smtp_ehlo "$SUBMISSION_PORT" || true)"
			if [ -z "$ehlo587" ]; then
				bad "port $SUBMISSION_PORT sent no SMTP greeting"
				VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
			elif printf '%s\n' "$ehlo587" | grep -qi '^250[- ]STARTTLS'; then
				ok "submission on $SUBMISSION_PORT advertises STARTTLS: $(printf '%s\n' "$ehlo587" | head -1)"

				# 8. The security half of the same probe. Stalwart's default
				#    is to offer PLAIN and LOGIN only once the session is
				#    encrypted -- verified on v0.16.20: `AUTH XOAUTH2
				#    OAUTHBEARER` before STARTTLS, `AUTH PLAIN LOGIN XOAUTH2
				#    OAUTHBEARER` after it, and `AUTH PLAIN` in the clear
				#    answered `554 5.7.8 Authentication mechanism not
				#    supported.` If that ever changes, a mail client would
				#    hand over a password on an unencrypted socket, so it is
				#    a failure and not a note.
				auth587="$(printf '%s\n' "$ehlo587" | grep -i '^250[- ]AUTH' || true)"
				if printf '%s' "$auth587" | grep -qwiE 'PLAIN|LOGIN'; then
					bad "$SUBMISSION_PORT offers a cleartext password mechanism BEFORE STARTTLS: $auth587"
					VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
				else
					ok "$SUBMISSION_PORT offers no password mechanism before STARTTLS (${auth587:-no AUTH advertised})"
				fi
			else
				bad "port $SUBMISSION_PORT answered but does not advertise STARTTLS -- a client there
        would either fail or send its password in the clear. EHLO said:
        $(printf '%s\n' "$ehlo587" | tr '\n' ' ')"
				VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
			fi
			;;
		*)
			bad "no listener is configured on $SUBMISSION_PORT (submission/STARTTLS), and the
        compose files publish it -- a client pointed there gets a refused
        connection with nothing in any log to explain it. Re-run this script
        without --verify-only to add the listener."
			VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
			;;
	esac

	# 9. Outbound routing, read back from the server: which route every
	#    non-local recipient takes, and -- if a relay was asked for -- that
	#    it is the one asked for. Read back rather than trusted, like all
	#    of the above: a 200 from x:MtaRoute/set is not a delivery path.
	read_relay_state
	local rhost rport rtls ruser rmode
	if [ -n "$RELAY_ROW" ]; then
		rhost="$(printf '%s' "$RELAY_ROW" | cut -f4)"
		rport="$(printf '%s' "$RELAY_ROW" | cut -f5)"
		rtls="$(printf '%s' "$RELAY_ROW" | cut -f6)"
		ruser="$(printf '%s' "$RELAY_ROW" | cut -f7)"
	fi
	if [ "$OUTBOUND_ROUTE" = "$RELAY_ROUTE" ] && [ -n "$RELAY_ROW" ]; then
		rmode="via relay $rhost:$rport ($([ "$rtls" = yes ] && echo 'implicit TLS' || echo 'STARTTLS'), tls strategy '$OUTBOUND_TLS'${ruser:+, auth as $ruser})"
	elif [ "$OUTBOUND_ROUTE" = "$RELAY_ROUTE" ]; then
		rmode="route '$RELAY_ROUTE' selected but NO such relay route exists -- remote mail cannot be delivered"
	else
		rmode="direct to each recipient's MX (route '$OUTBOUND_ROUTE') -- needs outbound port 25 and a PTR record, see docs/hosting.md"
	fi
	if [ -z "$RELAY_HOST" ]; then
		case "$rmode" in
			*"NO such relay"*) bad "outbound delivery: $rmode"; VERIFY_FAILURES=$((VERIFY_FAILURES + 1)) ;;
			*)                 ok  "outbound delivery: $rmode" ;;
		esac
	elif [ "$OUTBOUND_ROUTE" = "$RELAY_ROUTE" ] && [ "$OUTBOUND_TLS" = "$RELAY_ROUTE" ] \
		&& [ "${rhost:-}" = "$RELAY_HOST" ] && [ "${rport:-}" = "$RELAY_PORT" ] \
		&& [ "${rtls:-}" = "$([ "$RELAY_IMPLICIT_TLS" = true ] && echo yes || echo no)" ] \
		&& [ "${ruser:-}" = "$RELAY_USER" ]; then
		ok "outbound delivery: $rmode"
	else
		bad "outbound delivery is $rmode
        expected: via relay $RELAY_HOST:$RELAY_PORT${RELAY_USER:+, auth as $RELAY_USER}, tls strategy '$RELAY_ROUTE'.
        Re-run without --verify-only to configure it."
		VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
	fi
}

# ---------------------------------------------------------------------------
# What state is the server in?
# ---------------------------------------------------------------------------
jmap_method "x:Bootstrap/get" '"ids":["singleton"]'
extract bootstrap-state
[ "$X_OK" = yes ] || die_state "could not read the bootstrap state: $X_VAL"
STATE="$X_VAL"

if [ "$STATE" = "done" ]; then
	read_state
	# Third of the three answers: already configured. Converge if what is
	# there is what was asked for; refuse, loudly and specifically, if it is
	# not.
	DOMAIN_PRESENT=no
	printf '%s\n' "$VERIFIED_DOMAINS" | grep -qx -- "$DOMAIN" && DOMAIN_PRESENT=yes

	if [ "$VERIFY_ONLY" = yes ]; then
		log "server is already configured; --verify-only, changing nothing"
	elif [ "$DOMAIN_PRESENT" = yes ] && [ "$VERIFIED_HOSTNAME" = "$SERVER_HOSTNAME" ]; then
		log "server is already configured with domain '$DOMAIN' and hostname '$SERVER_HOSTNAME' -- nothing to do, verifying"
	else
		die_exists "this Stalwart has already completed first-boot setup, and it is not
  configured the way you asked.

    server hostname   configured: ${VERIFIED_HOSTNAME:-<unset>}
                      requested:  $SERVER_HOSTNAME
    mail domains      configured: $(printf '%s' "$VERIFIED_DOMAINS" | tr '\n' ' ')
                      requested:  $DOMAIN

  Nothing was changed. First-boot setup runs once; re-running it against a
  server that is already carrying mail is not something this script will do
  on its own.

  If you meant to ADD a mail domain, that is a different operation and it is
  already supported:
      docker compose exec -T mailosh mailosh setup --domain $DOMAIN --email you@$DOMAIN
  which creates the domain, creates the mailbox, and prints the DNS records.

  If you meant to CHANGE the server hostname, do it deliberately in
  Stalwart's own admin UI (docs/operations.md has the temporary forwarder for
  reaching it) -- it is the name on the mail-port certificate and in the SMTP
  banner, and changing it on a live server affects delivery.

  If this stack was supposed to be empty, you are pointed at the wrong one.
  This run targeted compose project '$PROJECT'.

  To see what state it is in without changing anything:
      scripts/stalwart-bootstrap.sh --domain $DOMAIN --verify-only"
	fi

	# The one change a converging run makes. It is additive -- a listener on
	# a port that had none -- and it is inert until the process rebinds, so
	# the restart below is part of the change rather than an extra risk. It
	# happens only on the run that actually creates the listener; every
	# later run finds it, changes nothing, and restarts nothing.
	if [ "$VERIFY_ONLY" = no ]; then
		ensure_submission_listener
		[ "$LISTENER_CREATED" = no ] \
			|| restart_stalwart "so the new $SUBMISSION_PORT listener binds -- the only change this run made"
		# The other change a converging run may make: relay mode. Additive
		# on a server without it, an in-place update on one with it, and
		# applied with a settings reload rather than a restart.
		[ -z "$RELAY_HOST" ] || ensure_relay
	fi

	verify
	[ "$VERIFY_FAILURES" -eq 0 ] || die_state "$VERIFY_FAILURES check(s) failed above. This server is configured but not serving mail correctly."
	printf '\n%s\n' "Stalwart is configured and serving: domain '$DOMAIN', hostname '$VERIFIED_HOSTNAME'." >&2
	exit 0
fi

# STATE = pending: the server has never been configured.
if [ "$VERIFY_ONLY" = yes ]; then
	bad "server is in BOOTSTRAP MODE: it has no mail domain, no hostname, and no mail listeners."
	die_state "nothing has been configured on this server yet. Run without --verify-only to configure it:
      scripts/stalwart-bootstrap.sh --domain $DOMAIN --hostname $SERVER_HOSTNAME"
fi

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
log "server is in bootstrap mode -- configuring it"
log "  mail domain      $DOMAIN"
log "  server hostname  $SERVER_HOSTNAME"
if [ "$REQUEST_TLS" = yes ]; then
	log "  mail-port TLS    Stalwart will request its own certificate (--request-tls-certificate)"
else
	log "  mail-port TLS    self-signed for now; configure DNS-01 later (see docs/operations.md)"
fi

REQUEST_TLS_JSON=false
[ "$REQUEST_TLS" = yes ] && REQUEST_TLS_JSON=true

# The response to this call carries a generated admin username and password
# ("updated": {"singleton": {"username": ..., "secret": ...}}). It is never
# printed: only the outcome is read out of it.
jmap_method "x:Bootstrap/set" "\"update\":{\"singleton\":{\"defaultDomain\":\"$DOMAIN\",\"serverHostname\":\"$SERVER_HOSTNAME\",\"requestTlsCertificate\":$REQUEST_TLS_JSON}}"
if [ "$JMAP_CODE" != 200 ]; then
	die_state "x:Bootstrap/set answered HTTP $JMAP_CODE. Check 'docker compose logs stalwart'."
fi
extract bootstrap-set
if [ "$X_OK" != yes ]; then
	# HTTP 200 with a rejection in the body -- the exact shape that makes
	# "it returned 200 so it worked" wrong.
	case "$X_VAL" in
		*"server hostname"*|*serverHostname*)
			die_state "Stalwart rejected the server hostname '$SERVER_HOSTNAME': $X_VAL
  It validates this itself. Observed on v0.16.20: names under reserved,
  undelegated TLDs such as .example and .invalid are rejected; real public
  names and .test are accepted. Pass a name you actually own." ;;
		*)
			die_state "Stalwart rejected the first-boot configuration: $X_VAL" ;;
	esac
fi
log "configuration accepted"

# ---------------------------------------------------------------------------
# Restart -- the step without which none of it takes effect
# ---------------------------------------------------------------------------
# x:Bootstrap/set persists the configuration; the running process is still
# the bootstrap-mode one. Verified: immediately after a successful set, and
# before any restart, x:Bootstrap/get still returns the *defaults*, and 25,
# 465, 587 and 993 are all still refused.
#
# This restart is safe precisely because of the refusal above: the only
# server this script ever restarts is one that has never been configured, so
# there is no mail in flight to interrupt.
restart_stalwart "so the configuration takes effect"

# Only now is x:NetworkListener writable: in bootstrap mode it answers
# `forbidden`, "Only the 'Bootstrap' object type can be modified until the
# bootstrap process is complete." Creating the listener does not bind the
# port either, so this needs a second restart -- free here, because a server
# that has never been configured has no mail in flight to interrupt.
ensure_submission_listener
# Tracer changes take effect on restart too, so both ride the same one.
ensure_stdout_tracer
# Relay mode reloads settings itself; on a first boot the restart below
# covers it as well.
[ -z "$RELAY_HOST" ] || ensure_relay
if [ "$LISTENER_CREATED" = yes ] || [ "$TRACER_CHANGED" = yes ]; then
	restart_stalwart "so the new $SUBMISSION_PORT listener binds and logging takes effect"
fi

read_state
verify

if [ "$VERIFY_FAILURES" -ne 0 ]; then
	die_state "$VERIFY_FAILURES check(s) failed above. Stalwart accepted the configuration but is
  not serving mail correctly. Do NOT point an MX record at this box yet.
      docker compose logs --tail 100 stalwart"
fi

if [ "$BRIEF" = yes ]; then
	log "stalwart-bootstrap: done (domain $DOMAIN, hostname $VERIFIED_HOSTNAME)"
	exit 0
fi

cat >&2 <<DONE

Stalwart first-boot setup complete.
  mail domain      $DOMAIN
  server hostname  $VERIFIED_HOSTNAME
  compose project  $PROJECT

Next, in this order:

  1. Create your first mailbox, and get the DNS records to publish. This runs
     over the same internal network, from the app container:

       docker compose exec -T mailosh mailosh setup \\
           --domain $DOMAIN --email you@$DOMAIN

     It prints MX, SPF, the live DKIM records and DMARC, ready to paste into
     your DNS provider. It prints a generated password too, so run it where
     the scrollback is yours, or pass --password.
$( [ -z "$RELAY_HOST" ] || printf '\n     Outbound mail goes via %s, so use the SPF *include* for that relay\n     (the DNS block shows the common ones), not the direct-send "v=spf1 mx".\n' "$RELAY_HOST" )
  2. Publish those records, then point the MX at this box.

  3. Mail-port TLS. 465, 587 and 993 are serving a self-signed certificate
     until you configure DNS-01 -- Caddy owns 80 and 443, so Stalwart can
     answer neither HTTP-01 nor TLS-ALPN-01. Inbound mail on 25 is
     unaffected; mail clients on 465/587/993 will warn until you do.
     docs/operations.md, "Mail-port TLS", has the steps.

  4. scripts/healthcheck.sh, and a backup before there is anything to lose.

Re-running this script is safe: it will verify rather than reconfigure.
DONE
exit 0
