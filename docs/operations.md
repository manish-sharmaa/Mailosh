# Running Mailosh in anger — first boot, backup, restore, health, upgrades

Everything below was **run**, against the live `docker compose` dev stack in this
worktree, on 2026-09-05 (Stalwart `v0.16.20`, Postgres `16.15`, Docker `29.4.0`,
Compose `5.1.2`, app at commit `35de4dd`). Numbers are measured, log excerpts are
copied out of real containers, and where something could not be verified it says
so instead of sounding confident. Following the convention of
`docs/spikes/p1a-findings.md` and `docs/hosting.md`: an operations doc that
overstates what has been tested is worse than one that admits a gap. The gaps
are listed at the end.

---

## 0. The two stores. Read this before anything else.

Mailosh keeps its data in two places, and they are **not** equally precious.
An operator who does not know which is which will back up the wrong one.

| | Where | What is in it | What losing it costs |
|---|---|---|---|
| **Mail** | Stalwart's Docker volumes `stalwart-data` + `stalwart-etc` | Every message, mailbox, account, password, DKIM key — and Stalwart's own configuration, which it stores *inside* the same store | **Permanent.** There is nothing to re-fetch it from. |
| **App state** | Postgres, database `mailosh` | `app_user`, `session`, `label_meta`, `ui_pref`, `contact`, `image_sender_allow`, `sender_pref`, `login_attempt`, `audit_log` (`mailosh/db/models.py` is the complete list) | Annoying. Everyone signs in again, UI preferences reset, the audit trail is gone. **No mail is lost.** |

Not one byte of mail content lives in Postgres. Not one row of application state
lives in Stalwart, apart from the per-session API key. If you can only save one,
save Stalwart's volumes.

`/etc/stalwart/config.json` is a 128-byte pointer at the store, not the
configuration:

```json
{"@type":"RocksDb","path":"/var/lib/stalwart/","blobSize":16834,"bufferSize":134217728,"poolWorkers":null,"cacheSize":134217728}
```

That is why `stalwart-etc` is backed up too, and why it is tiny.

### What is deliberately not backed up

`.env`. It holds `MAILOSH_SECRET_KEY` and `MAILOSH_STALWART_ADMIN_SECRET`, and a
backup archive carrying live credentials is a bigger problem than the one it
solves — backups get copied to laptops, object stores, and other people's
machines. Keep `.env` somewhere a backup does not reach (a password manager).

Losing `.env` costs **one re-login for every user** and nothing else: the Fernet
key derived from `MAILOSH_SECRET_KEY` is what decrypts stored session keys
(`session.api_key_secret_enc`), so old sessions stop decrypting and users sign in
again. Mail is unaffected — it is protected by the account passwords inside
Stalwart's store, which *are* in the backup.

---

## 1. First boot: configuring Stalwart

**A freshly deployed stack does not receive mail until this is done.** It is
the single largest gap between `docker compose up -d` and a working mail
server, and it fails silently: `docker compose ps` reports `stalwart` as
*healthy*, Caddy holds a real certificate for the webmail, the firewall is
open — and every connection to port 25 is refused.

A fresh Stalwart container starts in **bootstrap mode**. Its own first log
line says so:

```
WARN Server started in bootstrap mode (server.bootstrap-mode)
     hostname = "4fca5c60b632"
     details  = "No configuration file was found. Port 8080 is open for initial setup."
INFO Network listener started (network.listen-start) listenerId = "http-recovery", localPort = 8080
```

One listener. `http-recovery` on 8080. Probed from inside the container in
that state, every mail port refuses:

```
port 25 refused   port 465 refused   port 587 refused   port 993 refused
```

`/healthz/live` answers 200 throughout, which is why nothing in the stack
tells you. The server leaves bootstrap mode only when it is given a mail
domain and a server hostname and is then restarted.

### The two hostnames, which are not the same thing

The single most common way to get this wrong is to conflate them.

| Name | Whose | What it is |
| --- | --- | --- |
| `MAILOSH_SITE_ADDRESS` — e.g. `app.mailosh.com` | Caddy's | the **webmail** hostname. Caddy obtains a publicly trusted certificate for this name over ACME |
| `mail.mailosh.com` | Stalwart's | the **mail** hostname. The SMTP banner, the MX target, the name on the mail-port certificate, and the name the PTR record must match |
| `mailosh.com` | — | the **mail domain**. The part after the `@` in your addresses. Not a hostname at all |

The reference DNS layout, and the one the walkthrough below assumes:

```
mailosh.com        -> reserved for the project's own site (NOT the app)
app.mailosh.com    A     -> this server    webmail, Caddy terminates TLS   (MAILOSH_SITE_ADDRESS)
mail.mailosh.com   A     -> this server    SMTP/IMAP, and the PTR target
mailosh.com        MX 10 -> mail.mailosh.com
```

Passing the webmail hostname where the mail domain belongs produces a
plausible-looking DNS block for a domain nobody sends mail to, and a
certificate for the wrong name. `scripts/stalwart-bootstrap.sh` defaults its
`--hostname` to `mail.<domain>` for exactly this reason, and says so when it
notices the two are the same name.

### It does not need the admin port published

Stalwart's HTTP port carries both JMAP and the admin API, and it accepts
`MAILOSH_STALWART_ADMIN_SECRET` — one string that can create or delete any
account and mint an API key for any mailbox. `docker-compose.prod.yml`
publishes it nowhere, and first-boot setup is not a reason to weaken that,
not even temporarily.

Both commands below run *inside* containers that are already on the internal
Docker network. No port is bound, no forwarder is opened, and nothing is
published for the duration.

### The walkthrough

Two commands. Substitute your own domain.

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml

# 1. Take the server out of bootstrap mode and bring the mail listeners up.
scripts/stalwart-bootstrap.sh --domain mailosh.com

# 2. Create the first mailbox and print the DNS records to publish.
docker compose exec -T mailosh mailosh setup \
    --domain mailosh.com --email you@mailosh.com
```

**Step 1** applies the domain and the hostname (`mail.mailosh.com` by
default), restarts Stalwart so the persisted configuration takes effect, adds
the submission listener on 587 that Stalwart does not ship (see below),
restarts once more so that binds, and then *verifies* — it does not assume.
Real output, from a fresh production-shaped stack:

```
05:44:27  server is in bootstrap mode -- configuring it
05:44:28    mail domain      mailosh.com
05:44:28    server hostname  mail.mailosh.com
05:44:28    mail-port TLS    self-signed for now; configure DNS-01 later
05:44:28  configuration accepted
05:44:28  restarting stalwart so the configuration takes effect
05:44:32  adding a submission listener on 587 (SMTP, STARTTLS -- not implicit TLS)
05:44:32  restarting stalwart so the new 587 listener binds

Verifying, against the running server:
  ok    server is out of bootstrap mode
  ok    server hostname is 'mail.mailosh.com'
  ok    domain 'mailosh.com' is configured
  ok    listener 'submission' (smtp) is accepting connections on 587
  ok    listener 'http' (http) is accepting connections on 8080
  ok    listener 'https' (http) is accepting connections on 443
  ok    listener 'sieve' (manageSieve) is accepting connections on 4190
  ok    listener 'pop3s' (pop3) is accepting connections on 995
  ok    listener 'imaps' (imap) is accepting connections on 993
  ok    listener 'submissions' (smtp) is accepting connections on 465
  ok    listener 'smtp' (smtp) is accepting connections on 25
  ok    SMTP greeting on 25: 220 mail.mailosh.com Stalwart ESMTP at your service
  ok    submission on 587 advertises STARTTLS: 220 mail.mailosh.com Stalwart ESMTP at your service
  ok    587 offers no password mechanism before STARTTLS (250-AUTH XOAUTH2 OAUTHBEARER)
```

Every one of those lines is a live check against the running server, and the
last three are the point. The port-25 one opens a TCP connection and reads
the greeting, which proves the listener is a working SMTP server *and* that
the hostname the world will see is the one you asked for. The two 587 ones
speak EHLO and read the reply: a submission port that answered but did not
offer `STARTTLS`, or that offered `AUTH PLAIN`/`LOGIN` before the session was
encrypted, would **fail** this script rather than warn.

The probes run **inside** the container, deliberately. A published port
answers from the host even when nothing is listening behind it — Docker's
proxy accepts the connection and drops it. Verified: `curl telnet://127.0.0.1:2526`
from the host succeeded against a Stalwart still in bootstrap mode with 25
refused internally. A host-side probe of a published port is not a listener
check.

**Step 2** is `mailosh setup`, the existing CLI (`mailosh/cli.py`), run in
the app container — which is on the same `mail` network with
`MAILOSH_STALWART_URL=http://stalwart:8080`. It creates the domain if it is
missing, creates the mailbox, reads the DKIM key **from the server** and
prints a copy-paste DNS block: A/AAAA, MX, SPF (with the relay variant),
DKIM, DMARC, and the PTR reminder. It prints the generated password, so pass
`--password` or run it where the scrollback is yours.

Then publish those records, set the PTR for `mail.mailosh.com` in your
provider's console, and point the MX at the box.

### Running it twice

Safe, and useful: the second run is a verifier.

| Server state | What happens |
| --- | --- |
| bootstrap mode | configures it, restarts, verifies |
| already configured, **same** domain and hostname | changes nothing, runs the full verification, exits 0 |
| already configured, **different** domain or hostname | **refuses**, exit 3, printing both the configured and the requested values |

The refusal is deliberate. First-boot setup runs once — `x:Bootstrap/get`
reports its singleton as `notFound` after the restart, so there is no second
bootstrap to perform in any case — and rewriting the identity of a mail
server that is already carrying mail is not something a re-run should do
quietly. The refusal message names the two supported ways to make the change
on purpose: `mailosh setup --domain` to *add* a domain, and Stalwart's own
admin UI to change the hostname.

`--verify-only` reports the state and changes nothing, ever. Use it to answer
"is this server actually serving?" without touching it:

```bash
scripts/stalwart-bootstrap.sh --domain mailosh.com --verify-only
```

### When it fails

Three different incidents, three different messages and exit codes — never
one generic failure:

| Exit | Meaning | The usual cause |
| --- | --- | --- |
| 2 | the server did not end up in the expected state | it accepted the configuration but did not come back; check `docker compose logs stalwart` |
| 3 | already configured, differently. Refused | you are pointed at the wrong stack, or you meant to *add* a domain |
| 4 | Stalwart rejected the admin credential | `.env` no longer matches what the **running container** was started with |
| 5 | not reachable on the internal network | the `stalwart` service is not running in this compose project |

Exit 4 is worth expanding, because the fix is not obvious. Stalwart reads
`STALWART_RECOVERY_ADMIN` **once, at container start**. Editing `.env`
afterwards changes nothing until the container is recreated. That also means
losing the secret is recoverable — the recovery admin is not stored anywhere
else, so setting it again is enough:

```bash
# 1. put the intended value in .env
docker compose up -d stalwart      # 2. recreates it with the new value
scripts/stalwart-bootstrap.sh --domain mailosh.com --verify-only    # 3.
```

Verified: rotating `MAILOSH_STALWART_ADMIN_SECRET` and running
`docker compose up -d stalwart` made the new secret work immediately.

Neither script ever prints the admin secret, a generated password, or a raw
response body — and that last one is not paranoia. The successful
`x:Bootstrap/set` response contains a freshly generated admin credential:

```
{"updated":{"singleton":{"username":"admin@…","secret":"…"}}}
```

The previous version of `scripts/stalwart-init.sh` echoed that response to
stderr. It no longer does, and the credential now reaches `curl` on stdin
inside a config file (`-K -`) rather than as `-u admin:$SECRET`, because
`docker exec` argv is visible in `ps` **on the host**.

### Port 587, and the published-vs-listening rule

Stalwart v0.16.20's default post-bootstrap listener set, read from its own
`x:NetworkListener` objects and confirmed by connecting to each one, is:

```
smtp        25     imaps  993    sieve  4190
submissions 465    pop3s  995    http   8080    https 443
```

There is **no 587** in that list, and no 143 — while both compose files
published 587, and the development one also published `1143:143`. That is the
worst shape this failure can take. Docker's proxy accepts the connection on
the host and then drops it, so the client reports a hang or a reset, the
packet never reaches Stalwart, and **nothing appears in any log** to say why.
Thunderbird, Apple Mail, iOS Mail and Outlook all commonly default to 587, so
this was the first thing a real mail client would hit.

Both halves are fixed, in opposite directions:

- **587 — the listener was added.** `scripts/stalwart-bootstrap.sh` now
  creates a `submission` listener (`protocol: smtp`, `bind: [::]:587`,
  `useTls: true`, `tlsImplicit: false`) on first boot, and adds it to an
  already-configured server that is missing it. Clients need 587; removing it
  from the compose file would have cost more than adding the listener.
- **143 — the published port was removed.** Plain IMAP is not configured and
  is recorded in the deployment spec as opt-in rather than default; 993
  covers every modern client. `docker-compose.yml` publishes `1993:993`
  instead of `1143:143`, so the host-side IMAP forward points at something
  that exists. No plaintext-capable IMAP listener was added.

What the 587 listener does and does not accept, verified against the running
server rather than reasoned about:

| Attempt | Answer |
| --- | --- |
| `EHLO` in the clear | `250-STARTTLS`, `250-AUTH XOAUTH2 OAUTHBEARER` — **no** `PLAIN`, no `LOGIN` |
| `AUTH PLAIN <base64>` in the clear | `554 5.7.8 Authentication mechanism not supported.` |
| `MAIL FROM` with no authentication | `503 5.5.1 You must authenticate first.` — a submission port, not a second MX |
| `EHLO` after `STARTTLS` | `250-AUTH PLAIN LOGIN XOAUTH2 OAUTHBEARER`, and the login succeeds |

That gating is Stalwart's own default, not something configured here — which
is exactly why `scripts/stalwart-bootstrap.sh` checks it on every run instead
of trusting it. A 587 that offered a password mechanism before STARTTLS would
be a **failure** (exit 2), not a note.

Two mechanics to know before touching any of this:

- `x:NetworkListener` cannot be written while the server is in bootstrap
  mode — it answers `forbidden`, *"Only the 'Bootstrap' object type can be
  modified until the bootstrap process is complete."* The listener is
  therefore created after the post-bootstrap restart.
- Creating a listener does not bind the port. The running process keeps the
  sockets it started with, and `x:Action/ReloadSettings` does not rebind
  either (tried — 587 stayed refused). **Only a restart binds it.**

That second point is the one exception to "this script never restarts a
configured server": if a converging run has to *create* the 587 listener, it
restarts Stalwart so the port actually comes up, and says so in its log
before it does. It restarts only on the run that made the change; every later
run finds the listener, changes nothing, and restarts nothing.
`--verify-only` never changes or restarts anything at all — on a server
missing 587 it reports the gap and exits 2.

### Relay mode — sending through a smarthost

Most budget VPS providers block outbound port 25 (`docs/hosting.md`), so the
recommended deployment sends through a relay (Amazon SES, SMTP2GO, your
provider's smarthost). The bootstrap script configures it, on a first boot or
on an already-configured server, and it is safe to re-run:

```
printf '%s\n' 'the-relay-password' > /root/relay-password   # mode 0600
scripts/stalwart-bootstrap.sh --domain example.com \
    --relay-host email-smtp.eu-west-1.amazonaws.com:587 \
    --relay-user AKIA... --relay-password-file /root/relay-password
```

Port 465 means implicit TLS; anything else means STARTTLS. TLS is **required**
either way — a relay that cannot negotiate it gets no mail, rather than a
password in the clear. The password is read from the file and travels to
Stalwart inside curl's stdin config, so it appears in no `ps` listing, no
shell history, and no output. There is deliberately no `--relay-password`.

What it writes, read from the running server's `GET /api/schema` and then
written and read back live on 2026-09-06 (Stalwart `v0.16.20`):

| Object | Value |
|---|---|
| `x:MtaRoute` name `relay` | `@type: Relay`, `address`, `port`, `protocol: smtp`, `implicitTls`, `allowInvalidCerts: false`, `authUsername`, `authSecret: {"@type":"Value","secret":…}` — the secret reads back as `****` |
| `x:MtaTlsStrategy` name `relay` | `startTls: require`, `dane: disable`, `mtaSts: disable`, `allowInvalidCerts: false` |
| `x:MtaOutboundStrategy` singleton | `route`: `is_local_domain(rcpt_domain)` → `'local'`, else `'relay'`; `tls`: else `'relay'` (the stock retry-with-`invalid-tls` downgrade is dropped) |
| `x:Action` | `{"@type":"ReloadSettings"}` — the running server picks the change up; no restart |

Two things learned on the way: the `match` list of an `x:Expression` must be
written as the index-keyed map the server returns it as (`{"0": {...}}`), the
same idiom as `x:Account.credentials`; and a route named `relay` that already
exists is *updated* in place (the password re-applied — it cannot be compared,
since it is never returned), so the script converges instead of failing on a
second run.

`--verify-only` reports the current outbound mode in either case:

```
  ok    outbound delivery: direct to each recipient's MX (route 'mx') -- needs outbound port 25 and a PTR record, see docs/hosting.md
  ok    outbound delivery: via relay relay.mailosh.test:587 (STARTTLS, tls strategy 'relay', auth as ses-user)
```

and with `--relay-host` also given it **fails** (exit 2) when what is
configured is not what was asked for — verified by asking for `:465` against a
server configured for `:587`.

Not verified: an actual TLS session and authentication against a real relay.
The dev stack has no outbound network, so what was proven is object creation,
read-back, idempotence, reload, and the verify reporting; the first message you
send through a real relay is the test of the credential. Watch
`x:QueuedMessage` (or the admin UI's queue) for it.

Publish the relay's SPF `include:` rather than the direct-send `v=spf1 mx` —
`mailosh setup` prints both and says which applies.

Going back to direct delivery is not something the script does: set the
outbound strategy's route `else` back to `'mx'` and `tls` to the stock
expression in the admin UI (or with `x:MtaOutboundStrategy/set`), and read
`docs/hosting.md` about port 25 and PTR first.

### Mail-port TLS — what is and is not configured

Caddy owns 80 and 443 on the host, so Stalwart can answer **neither** HTTP-01
**nor** TLS-ALPN-01. **DNS-01 is the supported path**, and it is not
automated here. The alternatives — sharing Caddy's certificate files, or
proxying `/.well-known/acme-challenge/*` to Stalwart — were considered and
rejected with reasons in the deployment spec §4.3; that decision stands.

Until you configure DNS-01:

- **Inbound mail on 25 keeps working.** Sending MTAs use opportunistic TLS
  and do not authenticate the certificate.
- **Mail clients on 465 and 993 will warn**, and should. Do not train users
  to click through it — configure DNS-01, or use only the webmail, which is
  on Caddy's real certificate.
- The **webmail is unaffected** either way.

What was verified about DNS-01 in this pass, and what was not:

- **Verified**, by reading the running server's own schema at
  `GET /api/schema` (gzipped, and it redirects — `curl -L` and decompress):
  Stalwart v0.16.20 exposes an `x:AcmeProvider` whose `challengeType` enum
  is exactly `TlsAlpn01 | DnsPersist01 | Dns01 | Http01`, and `x:DnsServer`
  has **70** provider variants. So the capability is in this exact image,
  not merely in the documentation.

  An earlier version of this note said 45 providers and listed DigitalOcean,
  Hetzner and deSEC as "notable absences". **Both halves were wrong** —
  re-counted from `schemas."x:DnsServer".variants`, all three are present,
  alongside Cloudflare, Route53, Ovh, Bunny, Porkbun, Dnsimple, Spaceship
  and a generic RFC 2136 `Tsig`. Nobody should choose a host on the strength
  of that old sentence; check the list yourself with the query above.

- **The Cloudflare shape, read from the schema** (`x:DnsServerCloudflare`):
  `secret` (the API token) is what authenticates; `email` is only for
  Cloudflare's legacy Global API Key and should be left unset with a scoped
  token. `ttl`, `propagationDelay`, `propagationTimeout`, `pollingInterval`
  and `timeout` all have defaults and are worth leaving alone until a
  renewal actually times out.

  Create the token at **Cloudflare → My Profile → API Tokens**, from the
  *Edit zone DNS* template, scoped to **one zone**. A token that can edit
  every zone on the account is not the token to hand a mail server. Stalwart
  stores it in its own data volume, which is why `scripts/backup.sh` treats
  that volume as irreplaceable.

- **Verified: actual certificate issuance**, on 2026-09-06 against
  `mail.mailosh.com` with Let's Encrypt **production** and a Cloudflare token
  scoped to the one zone. The listeners picked the certificate up live, with
  no restart. Confirm with

  ```
  openssl s_client -connect mail.<domain>:465 -servername mail.<domain> </dev/null 2>/dev/null \
    | openssl x509 -noout -issuer -subject -dates
  ```

  A real certificate names an ACME issuer (Let's Encrypt) rather than
  Stalwart itself (`CN=rcgen self signed cert` is the placeholder).

  **How the objects fit together** (v0.16.20, read from the schema and then
  exercised): the `x:AcmeProvider` carries only the ACME account —
  `directory`, `challengeType: Dns01`, `contact` — and has no domain or DNS
  field of its own. The `x:DnsServerCloudflare` carries the token as
  `secret: {"@type": "Value", "secret": "<token>"}`. Both attach to the
  **domain**: `certificateManagement: {"@type": "Automatic", acmeProviderId,
  subjectAlternativeNames: {"mail.<domain>": true}}` and `dnsManagement:
  {"@type": "Automatic", dnsServerId, publishRecords: {...}}`. All of it is
  settable over the admin JMAP API (`x:*/set` on `/jmap`) from a container
  on the internal network, so the token never has to pass through a browser.

  Three things that cost time, and are not in Stalwart's documentation:

  1. **`publishRecords` is what Stalwart will write into your zone.** Left at
     its default it publishes MX, SPF, DMARC, CAA, MTA-STS, TLS-RPT,
     autoconfig and SRV records — over whatever is there. If those are
     hand-managed, set it to `{"dkim": true}` only. That still publishes the
     **Ed25519 DKIM key**, which `mailosh setup` does not print (it prints
     only the RSA one) and which Stalwart signs with regardless — so without
     this, half of every message's signatures fail.
  2. **Set `propagationDelay` on the DNS server (30 s works).** With the
     default of none, the first poll for `_acme-challenge` can race the
     record into existence and be answered negatively by the host's
     resolver, which then caches that answer for the zone's negative TTL —
     and the order stalls with nothing in the log past `auth-start`.
  3. **The `AcmeRenewal` task is not re-run after a restart.** It is queued
     when `certificateManagement` is saved and executed then; a task still
     `Pending` in `x:Task` when the server restarts stays pending. To
     re-trigger, save `certificateManagement` again (switch to `Manual` and
     back). Whether the *renewal* due in ~60 days is affected the same way
     is unknown until it comes due — check `x:Task` and the certificate's
     `validTo` around then, and re-arm the same way if needed.

  Stalwart leaves the `_acme-challenge` TXT in the zone after validation;
  delete it by hand or let the next order overwrite it.

One lead recorded rather than acted on: the same schema exposes an
`x:Certificate` object that accepts a PEM certificate and private key over
the API. That is a *different* mechanism from the shared-volume idea §4.3
rejected — a renewal hook could push Caddy's renewed certificate in rather
than hoping Stalwart notices a changed file. Neither the API nor the reload
behaviour was exercised; it is written down so the next person can evaluate
it instead of rediscovering it.

### The development stack

`scripts/stalwart-init.sh` is the development one-liner and is **dev-only**:
it hardcodes `mailosh.test` / `mail.mailosh.test` and creates the demo
mailbox from `MAILOSH_DEMO_USER` / `MAILOSH_DEMO_PASSWORD`. It now *calls*
`scripts/stalwart-bootstrap.sh` for the first-boot half rather than carrying
a second copy of it, so there is one implementation of "leave bootstrap
mode" and both paths exercise it. Do not point it at a real deployment.

---

## 2. Backup

```
make backup                      # -> ./backups/mailosh-<UTC stamp>/
make backup DEST=/mnt/nas/mail
make backup KEEP=14              # prune all but the 14 newest afterwards
scripts/backup.sh --help
```

### What it does, in order

1. **Postgres, online.** `pg_dump` inside the running container, plain SQL,
   gzipped. `pg_dump` takes its snapshot in a single transaction, so a dump taken
   while the app is writing is internally coherent by construction. No downtime.
2. **Stalwart, stopped.** `docker compose stop stalwart`, `tar -czf` both volumes
   from a helper container, `docker compose start stalwart`, wait for healthy.
3. **Manifest and checksums**, then the directory is renamed from
   `<name>.partial` to `<name>` — so an interrupted run can never be mistaken for
   a complete backup.

### Why Stalwart has to be stopped

Because a raw copy of a live RocksDB store may not open again, and because
Stalwart's own exporter cannot help. This is not inherited from a manual —
`stalwart --export` was run inside the running container and answered:

```
⚠️ Startup failed: Failed to open database: Error { message: "IO error:
While lock file: /var/lib/stalwart//LOCK: Resource temporarily unavailable" }
```

The running server holds the store's exclusive lock. `stalwart --export` and
`--import` (the version-portable logical pair, `stalwart --help`) therefore need
the same downtime a `tar` does, so `tar` is what this uses: byte-exact, no
second format to trust, and restorable with nothing but `tar`.

The script never leaves the mail server down. An `EXIT` trap restarts Stalwart
even if the archive step fails or you Ctrl-C it — verified by making it fail
(`stalwart-data` had no `./CURRENT`; the script died and Stalwart came back
anyway). If Stalwart was already stopped when you invoked the script, it is left
stopped.

### The two halves are not one instant

Postgres is dumped first, Stalwart a few seconds later. The only value that
crosses the two stores is the per-session Stalwart API key
(`session.api_key_secret_enc` here, the key object there), and **both** skew
directions cost at most a re-login:

- session created between the two captures → its API key is in the Stalwart half
  but not the Postgres half → an orphaned key in Stalwart, harmless (the app
  mints one key per user and reuses it).
- session row present but its key not yet minted → that session fails to
  authenticate → the user signs in again.

No mail is affected either way. If you want a genuinely atomic capture, stop the
whole stack first and then run the script; it works fine that way, and costs the
app's uptime instead of Stalwart's.

### What it produces

```
backups/mailosh-20260905T041916Z/
  MANIFEST.txt            what, when, which images, which volumes, how to restore
  SHA256SUMS
  postgres.sql.gz         5,313 B   <- app state
  stalwart-data.tar.gz  927,836 B   <- THE MAIL
  stalwart-etc.tar.gz       227 B   <- the store pointer
```

Measured on the live dev stack (3.8 MB Stalwart store holding 28 messages, 7.9 MB
Postgres database):

| | |
|---|---|
| Total wall time | **12.1 s** |
| Stalwart unavailable | **~9 s** (04:19:17 stop → 04:19:26 healthy again), of which ~3 s is the actual archive; the rest is a clean shutdown plus the healthcheck interval |
| Backup size on disk | **928 KB** |

Those numbers scale with the store, not with the message count in any simple way
— a 40 GB mailbox is a 40 GB tar, and the stop window grows with it. **This has
not been tested at that size**; see the gaps at the end.

### `./backups` and git

The first time it writes to a destination inside a git checkout, the script drops
a `.gitignore` containing `*` there. That ignores the backups *and* itself, so
`git status` stays clean and a hurried `git add -A` cannot commit a copy of
everyone's mail. Verified: after a live backup, `git status --short` showed no
`backups/` entry.

### It fails loudly rather than half-working

Every one of these was triggered on purpose, not reasoned about:

- Docker daemon unreachable, `docker compose` v1, no `sha256sum`/`shasum`, no
  `gzip`, helper image absent locally → refuses before writing anything.
- Postgres not running → `the 'postgres' service is not running in project
  '<name>'. Start the stack (make up) and retry.`
- Not enough free space for the *uncompressed* source → refuses. (Deliberately
  pessimistic: gzip needs far less.)
- Dump with no `-- PostgreSQL database dump complete` trailer, or with zero
  tables → refuses to call it a backup.
- Mail archive with files but no `./CURRENT` → refuses (that is a torn RocksDB
  capture).
- Mail archive that is *empty* → warns, does not fail. That is what a
  never-bootstrapped Stalwart legitimately looks like (`Server started in
  bootstrap mode … No configuration file was found`), and there is no mail to
  lose yet.

One thing worth knowing if you ever write your own checker: `pg_dump` 16.15 (and
any build with the CVE-2025-8714 fix) wraps its output in psql restricted mode,
so the **last** lines of the file are `\unrestrict <token>` and a blank — the
completion marker sits several lines above. Checking only `tail -3` reports a
perfectly good dump as truncated. That happened here, and is why the check reads
the last 20 lines.

### Running it on a schedule

`scripts/backup.sh` is the interface; `make backup` is a convenience. The
supported scheduler is a **systemd timer**, installed by
`scripts/backup-timer.sh`. From the checkout, as root, with the same
`COMPOSE_FILE` you deploy with:

```
sudo COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml \
  scripts/backup-timer.sh --dest /srv/mailosh-backups --keep 14 --user deploy
```

It prints the two units before writing them (`--print` prints and stops), then
`daemon-reload`s and `enable --now`s `mailosh-backup.timer`: daily at 03:15
local time (`--calendar` takes any `OnCalendar=` spec), `Persistent=true` so a
box that was off at 03:15 runs the backup at boot, and a 10-minute randomised
delay. Afterwards:

```
systemctl list-timers mailosh-backup.timer     # next and last run
sudo systemctl start mailosh-backup.service    # one run now, to prove it
journalctl -u mailosh-backup.service -n 50     # what the last run said
sudo scripts/backup-timer.sh --uninstall
```

Why a host timer and not a compose sidecar: a backup container would need the
Docker socket mounted into it to stop Stalwart and exec `pg_dump`, and a
container holding `/var/run/docker.sock` is root on the host. It would be the
most privileged thing in the stack, running all day to do ten seconds of work.
The host already has docker access, the checkout, the `.env`, and a scheduler
with logs and catch-up. If you have no systemd, the old cron line still works:

```cron
15 3 * * *  cd /srv/mailosh && ./scripts/backup.sh /srv/backups >> /var/log/mailosh-backup.log 2>&1
```

`--keep` now defaults to **14**; pass `--keep 0` to never prune.

### Encryption and off-host copies

Two flags, both optional, both off by default — a plain `scripts/backup.sh`
still writes a plaintext directory to `./backups` exactly as before.

**`--encrypt-to RECIPIENT`** packs the finished, checksummed directory into a
single `mailosh-<stamp>.tar.age` and removes the plaintext. It needs
[`age`](https://age-encryption.org) on the host (`apt install age`,
`brew install age`). Make a key once, and keep the identity file *outside*
anything the backup reaches — a password manager is the right place, since it
is one line:

```
age-keygen -o ~/.config/mailosh/backup-identity.txt
#   Public key: age1...        <- this is what --encrypt-to takes
```

Repeat `--encrypt-to` for a second recipient (a colleague's key, or an
`ssh-ed25519 ...` public key). The script refuses an `AGE-SECRET-KEY-` on the
command line. Restore needs the identity:

```
scripts/restore.sh --check --identity ~/.config/mailosh/backup-identity.txt \
    /srv/mailosh-backups/mailosh-20260906T070700Z.tar.age
```

`--check` on an encrypted backup decrypts into a private temporary directory
(removed on exit, including on failure) and then runs every check the
directory form gets — so it also proves the identity you hold actually
decrypts it. A wrong identity fails as `age: error: no identity matched any of
the recipients` with nothing changed. Retention prunes directories and
`.tar.age` files in one list, by the timestamp in the name, so turning
encryption on mid-rota does not exempt the older shape.

**`--rclone-remote REMOTE:PATH`** runs `rclone copy` of the result (the
`.tar.age`, or the whole directory) after everything else succeeded. `rclone`
must be configured on the host (`rclone config`). `rclone copy` never deletes
on the remote, so remote retention is a separate decision — a bucket lifecycle
rule is the usual answer. A failed upload exits 1 (so the timer shows failed
and `journalctl` says why) but the local backup is complete and intact. An
unencrypted upload is logged with a warning: it holds everyone's mail in the
clear at the destination.

Both were exercised on 2026-09-06 against the dev stack: `--encrypt-to
--rclone-remote :local:... --keep 1` produced a 1.9 MB `.tar.age`, removed the
plaintext, pruned three seeded older entries (two directories, one `.tar.age`)
and left an unrelated directory alone, uploaded, and `restore.sh --check
--identity` on the result decrypted and verified it (42 store files, 10
tables). `age` and `rclone` ran from a container on that machine (neither is
installed on the host), invoked through PATH shims — the scripts themselves
are unaware of the difference.

### The routine verification

After every backup, and in the same timer if you like a belt with your braces:

```
scripts/restore.sh --check BACKUP            # a mailosh-<stamp> directory
scripts/restore.sh --check --identity KEYFILE BACKUP.tar.age
```

`backup.sh` prints the exact command for the backup it just made as its last
line (`Next: verify it.  ...`). `make backup-check` runs it against the newest
directory.

### Retention advice

There is no correct number, but there is a shape. The failure a backup rota has
to survive is not "the disk died last night" — you notice that. It is
**corruption or deletion you notice late**: a mailbox someone emptied three weeks
ago, a bad restore, a bug that quietly dropped rows. So keep more history than
your detection time.

A defensible default for a personal or small-team server:

- **daily** for 14 days (`--keep 14` on a daily cron)
- **weekly** for 8 weeks — a second cron writing to a different `DEST` with its
  own `--keep 8`, rather than trying to make one directory do both
- **monthly** for 6–12 months if you have regulatory or sentimental reasons
- **at least one copy off the machine, and one copy you have actually restored**

Cost is small at this scale: the whole dev stack backs up to 928 KB. A real
mailbox is dominated by the blob store, so a rough sizing rule is "one backup ≈
the size of `/var/lib/stalwart`", compressed by however well your mail
compresses — mostly not much, since attachments are already compressed.

`make backup KEEP=n` only ever deletes directories matching this script's own
`mailosh-<stamp>` pattern, so pointing it at a directory that holds other things
cannot eat them.

---

## 3. Restore

**This is the half everyone skips.** A backup nobody has restored is not a
backup, it is a hope.

```
scripts/restore.sh --check BACKUP_DIR    # verify only; changes nothing
make backup-check                        # same, on the newest backup

scripts/restore.sh BACKUP_DIR            # DESTRUCTIVE; asks first
make restore BACKUP=backups/mailosh-...

scripts/restore.sh --identity KEYFILE [--check] BACKUP.tar.age   # encrypted backup
```

### `--check` first

Verifies the checksums, the gzip streams, the `pg_dump` completion marker, and
that the mail archive really is a RocksDB store. Cheap enough for cron. Verified
in both directions: it passes a good backup and it catches a corrupted one —
flipping seven bytes inside `stalwart-data.tar.gz` produced

```
sha256sum: WARNING: 1 computed checksum did NOT match
restore.sh: ERROR: CHECKSUM MISMATCH. This backup is corrupt -- do not restore it.
```

`--check` proves the bytes are readable. It does **not** prove the data comes
back. Only a real restore does that.

### What a restore destroys

In the **target** compose project, before it loads anything:

- volume `<project>_stalwart-data` — **all mail in that stack**
- volume `<project>_stalwart-etc`
- database `mailosh` — dropped and recreated

Not touched: `.env`, the images, anything outside the target project.

### The gate

Restoring over live data destroys what is there, so the script is deliberately
awkward:

1. It verifies the archive **before** it touches anything. There is no path that
   stops a live stack and then discovers the backup is unreadable.
2. It prints exactly what it will destroy, with the *current* sizes of those
   volumes and that database, so you can see whether you are about to overwrite
   something that matters.
3. It prints a loud **CROSS-STACK RESTORE** warning when the backup came from a
   different compose project than the target — correct for a drill or a new host,
   catastrophic if you meant to name a different target.
4. You must type the **target project name** at a prompt. Verified: typing the
   *source* project name — the natural mistake, since it is on screen — is
   rejected with `you typed 'phase1a-foundation', not 'mailosh-drill'. Nothing
   was changed.`
5. Without a terminal it refuses outright unless you pass `--yes`:
   `refusing to restore non-interactively without --yes (stdin is not a
   terminal)`. A stray `restore.sh backup </dev/null` in a script cannot proceed.

### Restoring somewhere else (a drill, or a new host)

Use Compose's own environment variables — the scripts add no flags of their own
for this:

```sh
COMPOSE_PROJECT_NAME=mailosh-drill \
COMPOSE_FILE=/tmp/drill/docker-compose.yml \
  scripts/restore.sh backups/mailosh-20260905T041916Z
```

On a new host the sequence is: install Docker, copy the repo and your
`.env` (from your password manager — it is not in the backup), copy the backup
directory over, `scripts/restore.sh <backup>`, then sign in.

### After a restore

The script prints the row counts and the alembic revision it ended at, then tells
you to prove the rest yourself, because it cannot: `docker compose ps`, sign in,
open a mailbox you know had messages.

Sessions survive a restore if `MAILOSH_SECRET_KEY` is unchanged — the cookie in
someone's browser still matches a restored `session` row. If the key changed,
everyone signs in again.

---

## 4. The restore drill that was actually performed

Not a description of what one would do. This is what was run on 2026-09-05,
end to end.

**Setup.** A throwaway compose project `mailosh-drill` — same service names, same
logical volume names, same images, different project (so different volumes:
`mailosh-drill_stalwart-data` etc.) and different host ports (8001/8081/55433).
The live dev stack `phase1a-foundation` was never a restore target.

**The drill stack was seeded with data of its own first**, so "the restore
replaced it" would be provable rather than assumed:

- bootstrapped with `scripts/stalwart-init.sh`, creating `demo@mailosh.test`
- three messages delivered over SMTP with subjects `DRILL-PRE-RESTORE-1..3`
  (they land in Junk — the known Stalwart quirk in `p1a-findings.md`, unauthenticated
  SMTP is classified as spam)
- an `app_user` row with `display_name = 'DRILL MARKER'` inserted into its Postgres

**Live state at backup time** (`phase1a-foundation`, taken while another agent's
integration tests were actively using it — session and audit-log counts moved
between snapshots, which is the point: this was a live system):

```
Inbox 28 total / 24 unread · Work 2 · Archive/Drafts/Sent/Junk/Trash 0 · 28 emails total
app_user=1  session=9  ui_pref=1  audit_log=29  sender_pref=2
alembic revision 0002_reading
```

**Backup**: `scripts/backup.sh` with defaults → `backups/mailosh-20260905T041916Z`,
928 KB, 12.1 s, ~9 s of Stalwart downtime. Live stack verified healthy
immediately afterwards, mail intact, `git status` clean.

**Restore**: into `mailosh-drill`, through the interactive prompt (a wrong answer
was tried first and rejected). **13.9 s** wall. The same restore re-run against
the final version of the script — which additionally waits until the app answers
`GET /login`, so that "Restore finished" is not printed while the web UI is still
refusing connections — takes **16.2 s**.

**What it proved.** Every one of these was checked after the restore:

| Check | Result |
|---|---|
| Message count | 28 — matches live exactly |
| Mailbox shape | Inbox 28/24 unread, **Work 2**, Archive present — the drill stack had no `Work` or `Archive` mailbox before |
| Message identity | same JMAP ids (`beaaaaaj`, `bqaaaaam`, …) and same subjects/timestamps as live |
| Drill's own mail | **gone** — Junk 0, and a JMAP text search for `DRILL-PRE-RESTORE` returns 0 |
| Drill's own Postgres row | **gone** — `select count(*) from app_user where display_name='DRILL MARKER'` → 0 |
| App state | `app_user=1 session=9 audit_log=29 sender_pref=2 ui_pref=1`, alembic `0002_reading` — matches live |
| File ownership | restored store files are `2000:2000`; the script asserts this, because Stalwart runs as uid 2000 and a wrong-ownership restore fails in a way that looks like data loss |
| **Accounts and passwords** | signed in to the restored stack's web UI at `:8001` as `demo@mailosh.test` with the live password → `303` then `GET /mail/inbox 200`, 20 rows rendered |
| **Message bodies / blob store** | `GET /m/bqaaaaam/source` fetched from the restored stack and from live, and diffed: **identical**, 274 bytes, `sha256 64033f8b…cc8844eb` on both — the RocksDB blob store came back byte for byte |

That last row is the one that matters. Row counts can agree while the blobs are
gone; reading a real message body out of the restored stack cannot.

**Do this yourself, on your own data, at least once — and then quarterly.** The
drill costs about a minute once the throwaway compose file exists. Keep the
throwaway project name distinct from your real one and check it twice; the
confirmation prompt is the last thing between a rehearsal and an outage.

---

## 5. Monitoring — what to watch, and how

```
make health           # one command, one answer
scripts/healthcheck.sh
```

Exit `0` healthy, `1` something is down, `2` warnings only — so it drops straight
into cron, a systemd timer, or an uptime checker.

Every probe runs **inside** the compose network rather than against a published
host port. That works on a production deployment that publishes nothing, and it
distinguishes "the service is broken" from "the port is not reachable from where
I am standing", which are different incidents.

### The health endpoints that exist

Found, not invented. Verified against the running stack on 2026-09-05:

| Target | Probe | Verified |
|---|---|---|
| Stalwart | `GET :8080/healthz/live` | `200 {"detail":"OK",...}` — also what `docker-compose.yml`'s healthcheck uses, so `docker compose ps` reflects it |
| Stalwart | `GET :8080/healthz/ready` | `200` |
| Postgres | `pg_isready -U mailosh` | same probe as the compose healthcheck |
| Schema | `select version_num from alembic_version` | proves Postgres is not just up but holds a migrated Mailosh database |
| Mailosh app | `GET :8000/login` → `200` | **the app has no health endpoint of its own** |
| Backups | age of the newest `backups/mailosh-*` | warns past `MAILOSH_BACKUP_MAX_AGE_DAYS` (default 7) |

**The app has no health endpoint, and that is worth stating plainly.**
`/healthz`, `/health`, `/healthz/live` and `/healthz/ready` all return **404**
from the Mailosh app (checked directly against the running app at commit
`35de4dd`; the routers in `mailosh/web/` declare no such route). `GET /login` is
the cheapest route that renders without a session, so it is the honest liveness
probe until the app grows a real one.

Know what that probe does and does not tell you: **`/login` returned 200 with
Stalwart completely stopped.** It proves the process is alive and serving HTTP.
It does not prove the mail server is reachable, which is why `healthcheck.sh`
probes Stalwart separately.

Also: `docker-compose.yml` defines **no healthcheck for the `mailosh` service**,
so `docker compose ps` shows it as plain `Up` and never `healthy`/`unhealthy`.
Do not read "Up" as "working".

### Two Docker habits worth having

`docker compose ps` **hides stopped services entirely** — a stack with a dead
Stalwart looks like a two-service stack, which reads as "nothing to see here"
exactly when something is wrong. Use `docker compose ps -a`, which is what
`healthcheck.sh` does.

Verified output with Stalwart stopped:

```
  FAIL  stalwart   exited (unhealthy)
  ok    postgres   running (healthy)
  ok    mailosh    running
  FAIL  stalwart   /healthz/live did not answer 200 -- the mail server is not serving
  ...
UNHEALTHY.
```

### What is not monitored

No metrics, no alerting, no history, no disk-space watch, no certificate-expiry
watch, no queue-depth or delivery-failure monitoring. `healthcheck.sh` answers
"is it up right now". Anything longitudinal is Phase 2's health panel.

---

## 6. What the logs say when something is wrong

All four signatures below were produced deliberately in the throwaway stack and
copied out of the real containers. `docker compose logs -f <service>`.

### Stalwart is down

The app answers **502** and its log says nothing else at all:

```
INFO:     192.168.147.1:41232 - "GET /mail/inbox HTTP/1.1" 502 Bad Gateway
```

No traceback, no explanation. **502 on a mail page is your Stalwart signal.**
Confirm with `docker compose ps -a` and `curl :8080/healthz/live` (connection
refused when it is really down).

### Postgres is down, app already running

**500**, with a traceback that names the query:

```
sqlalchemy.exc.InterfaceError: (…asyncpg.InterfaceError) <class
'asyncpg.exceptions._base.InterfaceError'>: connection is closed
[SQL: SELECT session.id AS session_id, … FROM session WHERE session.id = $1::VARCHAR]
```

A `session` lookup failing on `connection is closed` means Postgres, not the app.

### Postgres is down when the app starts

The `mailosh` container **exits with code 1** during `docker/entrypoint.sh`'s
`alembic upgrade head`, before uvicorn ever starts:

```
socket.gaierror: [Errno -2] Name or service not known
```

and `docker compose ps -a` shows:

```
mailosh: exited Exited (1)
```

DNS for the `postgres` service name fails because the container is not running.
The app deliberately does not serve traffic against an unmigrated database.

### Stalwart has lost its store (or never had one)

```
WARN Server started in bootstrap mode (server.bootstrap-mode)
     details = "No configuration file was found. Port 8080 is open for initial setup."
```

This is what a **restore that forgot Stalwart** looks like — and what a
never-set-up server looks like. `/healthz/live` still answers 200 in this state,
so the healthcheck will not catch it; the tell is that no account can sign in.
If you see this after a restore, the `stalwart-data`/`stalwart-etc` volumes did
not come back. If you see it on a first install, run `scripts/stalwart-init.sh`.

### Healthy, for comparison

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO:     Started server process [1]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

Stalwart logs one benign `WARN` on every start that is **not** a problem:
`Configuration build warning (registry.build-warning) source = "DnsResolver" …
"The configured DNS resolver cannot validate DNSSEC. DANE has been disabled…"`.
Expected in Docker with the default resolver.

---

## 7. Upgrades

**Back up first, every time.** `make backup` takes twelve seconds; the restore
drill above is what makes that backup worth having.

### Mailosh itself

```sh
make backup
git pull
docker compose build mailosh
docker compose up -d
make health
```

Schema migrations apply themselves: `docker/entrypoint.sh` runs
`alembic upgrade head` before uvicorn starts, on every boot — visible in the logs
as the `alembic.runtime.migration` lines above. `make db-upgrade` re-runs it by
hand inside the running container if you need to.

Rolling back a bad app upgrade is `git checkout <previous>` plus
`docker compose build mailosh && docker compose up -d`. Rolling back a **schema**
change is a restore: there is no `alembic downgrade` path exercised here.

### Stalwart

Bump the pinned tag in `docker-compose.yml` and `docker compose up -d stalwart`.
Stalwart migrates its own store in place on first start of a new version.

**Not verified here.** Only `v0.16.20` was ever run. Before a version bump:
back up, read the upstream release notes for store-format changes, and be
prepared to restore. If you want a version-portable escape hatch, Stalwart's own
`--export` / `--import` pair (`stalwart --help`) writes a logical dump that does
not depend on the RocksDB on-disk format — it needs the server stopped, exactly
like the tar does. **That path was not exercised**; the tar was, and the tar is
what `scripts/restore.sh` restores.

### Postgres minor versions

`16.15 → 16.x` is an image bump and a restart. Safe.

### Postgres major versions — the one that bites

A Postgres data directory belongs to its major version. Changing the image tag
from `16-alpine` to `17-alpine` against the existing `pg-data` volume does not
upgrade anything; the server refuses to start on a data directory it did not
create. The path is dump → fresh volume → restore, which is exactly what these
scripts do.

**Verified**: the `postgres.sql.gz` from this stack's Postgres **16.15** loaded
cleanly into a throwaway **18.4** container — `ON_ERROR_STOP=1`, no errors, and
identical row counts afterwards (`app_user=1 session=9 audit_log=29
sender_pref=2`, alembic `0002_reading`). Plain SQL rather than `pg_dump -Fc` is
part of why: it restores with `psql` alone, with no `pg_restore` version-matching
dance.

**Also verified, and a trap**: the `postgres:18` image changed where data lives.
Reusing this project's `pg-data:/var/lib/postgresql/data` mount with the 18 image
fails at startup with

```
Counter to that, there appears to be PostgreSQL data in:
  /var/lib/postgresql/data (unused mount/volume)
```

18 wants a single mount at `/var/lib/postgresql`. So a 16 → 18 move is: back up,
change **both** the image tag and the volume mount path, delete/replace the old
volume, start the new server, load the dump.

### Scaling

**Vertical only, for now.** Give the box more CPU and RAM, tune Postgres
(`docs/hosting.md` sizes `shared_buffers`), give Stalwart more memory. That is
the whole supported story today.

**Do not raise the uvicorn worker count.** It is the most natural instinct when
something feels slow, and it is currently unsafe: session revocation does not
cross processes. `auth.logout` (`mailosh/web/auth.py`) revokes the `session` row
in Postgres — shared — and then drops the pooled JMAP client from
`app.state.pool`, which is **per process**. With more than one worker, another
worker's already-open `GET /events` keeps streaming that user's mail on the
client it still holds, and `mailosh/jmap/pool.py` exempts a streaming client from
the idle sweep, so nothing bounds it in time. If the user still has another
session open anywhere, the Stalwart API key is not destroyed either, and the
revoked session's stream simply continues.

Nothing sets `--workers` today (`docker/entrypoint.sh` starts a single uvicorn
process), so this is a landmine rather than a live bug — but an operations doc is
where someone goes looking for permission, so: horizontal scaling is blocked on
cross-process session revocation. Leave the worker count at one.

---

## 8. What this does not cover

Stated plainly, because the gaps matter more than the coverage:

- **Scale.** Everything here was measured against a 3.8 MB Stalwart store with 28
  messages and a 7.9 MB database. The approach is size-independent; the
  *numbers* are not. A large mailbox means a longer stop window, and nothing here
  tells you how long.
- **Off-host copies and encryption** are optional flags (`--encrypt-to`,
  `--rclone-remote`, §2), exercised against the dev stack with a local rclone
  remote. They were not exercised against a real object store, and nothing
  manages remote retention for you.
- **Stalwart version upgrades.** Never exercised. Only `v0.16.20` was run.
- **`stalwart --export` / `--import`.** Confirmed to exist and confirmed to need
  the server stopped. Never used for a real backup or restore here.
- **Partial / selective restore.** All or nothing. There is no "restore one
  mailbox" and no point-in-time recovery — no WAL archiving, no incremental
  backups. The granularity is whatever your cron interval is.
- **Postgres physical backups.** `pg_basebackup`, streaming replicas and PITR are
  all reasonable for a larger deployment and none of them are set up here.
- **A real health endpoint.** The app has none; `/login` stands in.
- **Monitoring over time.** No metrics, alerting, disk-space or certificate
  watch.
- **Restore onto a genuinely different host.** The drill restored into a separate
  compose project on the same machine. Different architecture, different Docker
  version, different filesystem — untested, though nothing in the archive format
  is host-specific (a `tar` of numeric-owned files and a plain SQL dump).
- **Concurrent writes during the backup.** The Postgres dump is transactionally
  consistent; the Stalwart archive is taken with the server stopped. What is *not*
  tested is a backup taken during heavy delivery — Stalwart's clean shutdown
  should flush and the tar should be sound, but it was not deliberately stressed.
