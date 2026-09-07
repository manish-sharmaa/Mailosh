# Mailosh — Production Deployment Design Specification

- **Date:** 2026-09-05
- **Status:** Approved direction. Binding for `docker-compose.prod.yml`, `docker/Caddyfile` and the README's deployment section.
- **Owner:** Manish Sharma
- **Supersedes:** the README's "Deployment is developer-grade" bullet and `SECURITY.md`'s "No TLS in the shipped compose file" limitation, both of which described the only deployment that existed before this document.
- **Amends:** Phase 1 spec (`2026-09-02-phase1-webmail-design.md`) §9's deployment note and §14's 1E row, which scheduled "Stalwart HTTP internal-only" and Caddy for the end of Phase 1. Both land now instead. Everything else in §9 stands unchanged.
- **Inputs:** `docs/hosting.md` (sizing, VPS port-25 realities, Stalwart's ACME challenge support), `SECURITY.md` (the known-limitations list this closes half of), `docker-compose.yml` and `docker-compose.dev.yml` (what exists), `scripts/stalwart-init.sh` (what Stalwart's first boot actually requires).

## 1. Goal, and what "deployable" means here

Everything shipped before today was a development stack. It publishes Stalwart's admin/JMAP port, publishes Postgres with a hardcoded `mailosh:mailosh` credential, terminates no TLS, and — via the dev overlay — mounts the working tree over the image and reloads on save. The README said so plainly. This document closes that gap: a second overlay, `docker-compose.prod.yml`, that turns the same three services into something a person can point `mail.example.com` at.

**Definition of done:** an operator with a domain, a 4 GB VPS and Docker can fill in `.env`, run one `docker compose` command, complete Stalwart's own first-boot setup, and reach a working webmail over HTTPS with a certificate that renews itself — with no port open to the internet that does not need to be, and with an honest, written account of which parts of that sentence this repository has verified and which it has only reasoned about.

**Non-goal:** the Phase 2 setup wizard, the DNS/health panel, and multi-worker scale-out. This is the compose file and the proxy, not the product feature. It also does not automate Stalwart's own configuration — see §12.

## 2. Decisions

| Decision | Choice | Alternatives considered | Why |
|---|---|---|---|
| Production shape | A third compose file, `docker-compose.prod.yml`, overlaid on the same base | a standalone prod compose; a separate `deploy/` tree | one base means the service graph, image build and entrypoint cannot drift between dev and prod; the diff *is* the deployment story and reviews as one |
| Removing dev ports | Compose's `!reset` / `!override` tags | omitting the key (does nothing); a `dev` profile on the base | Compose **concatenates** `ports:` across files — an override that stays silent about 8080 still publishes 8080. `!reset` is the only thing that deletes an inherited key (§3) |
| TLS termination | Caddy, in the stack, on 80/443 | Traefik; nginx + certbot; Stalwart's own HTTPS listener | ACME is built in with no sidecar, no cron and no volume-sharing dance; the whole config is 40 readable lines; a self-hoster's failure mode is an expired certificate nobody renewed, and Caddy's default is to renew |
| Certificate for the **webmail** | Caddy's automatic ACME, HTTP-01 on port 80 | DNS-01 everywhere | needs no provider credential at all, which is the difference between "works on any VPS" and "works if you can mint a DNS API token" |
| Certificate for the **mail ports** | Stalwart's own ACME, DNS-01, configured by the operator | share Caddy's certificate files into Stalwart; proxy `/.well-known/acme-challenge` to Stalwart | Caddy owns 80 and 443, so Stalwart can answer neither HTTP-01 nor TLS-ALPN-01. The rejected options are in §4.3, with why |
| App's published port | none | keep `127.0.0.1:8000` for debugging | loopback is not a boundary on a shared box, and Caddy reaches the app over `edge`. §9 explains what replaces it |
| Stalwart's HTTP (JMAP + admin) port | published **nowhere** | loopback, as dev does; behind Caddy on a second hostname | that port accepts `MAILOSH_STALWART_ADMIN_SECRET`, which can mint an API key for any mailbox on the server. It is the highest-value credential in the stack and it belongs on an internal network only |
| Postgres port | published **nowhere** | loopback 55432, as dev does | the dev publication exists so host tooling works against a hardcoded credential. Neither applies here |
| Stalwart mail ports | 25, 465, 587, 993 on all interfaces — and a 587 listener created at first boot, because Stalwart ships none | none (webmail only); add 143 and 4190; drop 587 rather than configure it | a mail server that receives no mail is not a deployment, and a published port with no listener behind it is worse than a closed one — Docker's proxy accepts and drops, so the client sees nothing and no log records it. 587 is what Thunderbird, Apple Mail, iOS Mail and Outlook default to, so it earns the listener. 143 (plaintext-capable IMAP) and 4190 (ManageSieve) remain opt-in, not default |
| Network layout | three networks: `edge`, `backend` (`internal: true`), `mail` | one flat network | "we did not publish that port" is a promise; "that container has no route to it" is a property. Postgres gets the property (§3.2) |
| Secrets | `.env`, mode 0600, interpolated by Compose | Docker secrets (`*_FILE`) | two of the three consumers cannot read a file-based secret without application changes. Reasoning and the cost in §6 |
| Client IP | `MAILOSH_TRUST_PROXY=true` **paired with** a proxy that overwrites `X-Forwarded-For` | leave it false (rate limiter sees only Caddy's IP) | the pairing is the whole point and is easy to get half-right. §5 is about nothing else |
| Workers | **pinned** to one, `WEB_CONCURRENCY=1` | leave uvicorn's default of one; raise it now that `mailosh.db.notify` exists | one worker is a **correctness** constraint, not a performance default — logout does not cross a process boundary (§12.1). Pinned rather than left to default so the next person to raise it reads why first |

## 3. What is published, and what is not

### 3.1 The port list

This is the entire externally reachable surface of a production deployment:

| Port | Service | Why it is open |
|---|---|---|
| 80/tcp | caddy | ACME HTTP-01 challenge, and the redirect that turns `http://mail.example.com` into HTTPS instead of a refused connection |
| 443/tcp | caddy | the webmail |
| 443/udp | caddy | HTTP/3. Drop it if UDP is awkward on your firewall; nothing breaks |
| 25/tcp | stalwart | inbound MX. Without it the domain receives no mail |
| 465/tcp | stalwart | submissions, implicit TLS |
| 587/tcp | stalwart | submission, STARTTLS — both, because real clients are split between them. Stalwart `v0.16.20` has no default listener here; `scripts/stalwart-bootstrap.sh` creates one (`protocol: smtp`, `bind: [::]:587`, `useTls: true`, `tlsImplicit: false`) and verifies on every run that it advertises STARTTLS and offers no password mechanism before it |
| 993/tcp | stalwart | IMAPS |

And this is what the development stack publishes that production does not: **8080** (Stalwart HTTP — JMAP *and* the admin API, which accepts the recovery-admin secret), **55432** (Postgres, behind `mailosh:mailosh`), **8000** (uvicorn, unauthenticated to anything that can reach it), **2525 / 1587 / 1993** (the dev SMTP/submission/IMAPS forwards; `1143 -> 143` was published against a listener that does not exist and has been removed).

Stalwart also listens, inside its container, on **995** (pop3s) and **4190** (ManageSieve) — both are its own defaults — and on **443** (its `https` listener). None of the three is published, deliberately: nothing in this deployment needs POP3 or ManageSieve from the internet, and Caddy owns 443 on the host. An unpublished listener is a socket inside a container rather than a promise to a client, so it is not the failure this section is about.

### 3.2 The three networks

Not publishing a port is a promise about the compose file. A network topology is a property of the runtime, and it survives someone adding a port back for an afternoon's debugging and forgetting.

- **`edge`** — caddy ↔ mailosh. Not internal: Caddy needs outbound HTTPS to reach the ACME directory, or it can never obtain a certificate.
- **`backend`** — `internal: true`. mailosh ↔ postgres, and mailosh ↔ Stalwart's HTTP port. No gateway is attached, so Postgres has no route to the internet and the internet has none to it, independently of whether anyone remembers not to publish 5432.
- **`mail`** — mailosh ↔ stalwart, and Stalwart's route out. Not internal, because a mail server that cannot open outbound connections cannot deliver mail or resolve MX records.

Caddy is on `edge` alone. It cannot open a socket to Postgres or to Stalwart's admin port even if its configuration told it to. That is the point of separating them rather than putting all four services on one network and relying on the Caddyfile's good behaviour.

`mailosh` sits on all three. It genuinely needs egress — Phase 1B's remote-image proxy (`GET /img`) fetches from arbitrary hosts on the reader's behalf — and it gets that from `edge` and `mail`.

### 3.3 Why `!reset` is load-bearing

Compose merges `ports:` by **concatenation**, not replacement. Verified rather than assumed: a base publishing `127.0.0.1:8080:8080` plus an override publishing `127.0.0.1:9999:9999` renders *three* published ports. An override file that simply declines to mention Stalwart's 8080 therefore still publishes Stalwart's 8080 — which is exactly the failure this whole document exists to prevent, and it would be invisible in review because the dangerous line is not in the file you are reading.

`!reset` (Compose v2.24+) deletes an inherited key. `!override` replaces it. The distinction matters and cost one debugging cycle to learn: `ports: !reset` followed by four entries renders a service with **no** published ports — the tag discards the value written under it too. So `mailosh` and `postgres` use `ports: !reset []` (publish nothing) and `stalwart` uses `ports: !override` with its four mail ports (publish exactly these).

`mailosh` also carries `volumes: !reset []` and `MAILOSH_RELOAD: "0"`. Those are not needed for the documented command; they are insurance against `-f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.prod.yml`, where the dev overlay's `.:/app` bind mount would otherwise survive into a production run and serve the working tree instead of the built image. Verified: with all three files, `mailosh` renders no volumes, no ports, and `MAILOSH_RELOAD=0`.

## 4. TLS and the certificate lifecycle

### 4.1 The webmail

Caddy holds `MAILOSH_SITE_ADDRESS` as its site address and obtains a publicly trusted certificate for it over ACME, answering HTTP-01 on port 80. Renewal happens on Caddy's own schedule, in-process, with no cron entry and no reload for the operator to forget. `MAILOSH_ACME_EMAIL` is where the CA sends expiry warnings when renewal has been failing — it should be an inbox that is **not on this server**, because if the mail server is what broke you will not read it.

The prerequisite is unavoidable and belongs in §7: `MAILOSH_SITE_ADDRESS` must already resolve publicly to this box, and ports 80 and 443 must reach it, *before* the first `up`. Caddy will retry, but a stack whose first act is to fail a challenge is a confusing first impression.

Two deliberate departures from a bare default:

- **HSTS**, `max-age=31536000`, set by Caddy because only the thing terminating TLS knows the claim is true. No `includeSubDomains` and no `preload`: the first is a commitment on behalf of every name under the registrable domain, including one you may later point at something that is not HTTPS, and the second is close to irreversible. Both are fine to add knowingly; neither belongs in a default someone inherits.
- **`immutable` caching on versioned static assets only.** `mailosh.ui.static` renders every asset URL as `/static/<path>?v=<sha256:8>` of the file's own bytes, so a versioned URL can never go stale. The Caddy matcher therefore requires the `v` parameter rather than matching the path — an unversioned `/static/...` falls through to the app's own validators instead of being frozen in every cache for a year.

For a deployment that is not internet-facing, `tls internal` inside the site block gives Caddy's own local CA; setting the site address to `:80` puts a plain HTTP server behind whatever you already terminate with. Both are operator choices, and neither is the default, because a real certificate is what a self-hoster actually needs.

### 4.2 The mail ports

A reverse proxy speaks HTTP. SMTP and IMAP are not HTTP, so Caddy cannot terminate 25, 465, 587 or 993 — Stalwart terminates its own TLS on all four, and needs its own certificate to do it.

Caddy owns 80 and 443 on the host, which rules out HTTP-01 and TLS-ALPN-01 for Stalwart. **DNS-01 is therefore the supported path**, configured in Stalwart's own settings with a DNS provider token (`docs/hosting.md` records Stalwart's native support for HTTP-01, DNS-01, DNS-PERSIST-01 and TLS-ALPN-01).

Until that is configured, Stalwart serves its default self-signed certificate on those ports. Be precise about what that costs:

- **Inbound mail on 25 keeps working.** Sending MTAs use opportunistic TLS and do not authenticate the certificate; a self-signed one is still better than cleartext.
- **Mail clients on 465/587/993 will warn**, and should. Do not train users to click through it — configure DNS-01, or use only the webmail, which is on Caddy's real certificate.

### 4.3 Alternatives rejected, and why

- **Share Caddy's certificate files with Stalwart.** Caddy's on-disk layout under `/data/caddy/certificates/...` is stable in practice, so a shared volume would work on day one. Nothing in Stalwart is known — to this project, on this version — to notice a changed certificate file and reload it without a restart. That converts a silent, successful renewal into a silent outage roughly sixty days later, which is the worst failure shape available. Not shipping a mechanism whose renewal path has not been observed.
- **Proxy `/.well-known/acme-challenge/*` for the mail hostname to Stalwart** so it can answer HTTP-01 through Caddy. Plausible — Caddy's own challenge handler passes unrecognised tokens through to the next handler — but it depends on the interaction of two ACME clients on one port, and this pass has not tested it. Written down here so the next person can evaluate it rather than rediscover it.
- **A Layer-4 proxy in front of everything** (Caddy's `layer4` app, or HAProxy). Solves the port contention properly, at the cost of a non-standard Caddy build or a fourth moving part. Out of proportion to the problem for a single-box self-host.

## 5. The proxy, `X-Forwarded-For`, and `MAILOSH_TRUST_PROXY`

This section exists because the pairing is easy to get half-right, and half-right is worse than either half.

`mailosh.web.auth._client_ip` takes `X-Forwarded-For`'s first hop as the client address **when `MAILOSH_TRUST_PROXY` is true**, and the Postgres-backed login rate limiter (5 failures per account, 20 per IP, per 15 minutes) keys on the result. So:

- **Turn it off behind a proxy** and every request appears to come from Caddy's container IP. The per-IP limiter becomes a global limiter: one attacker's failures lock out every user behind that address, which is all of them.
- **Turn it on in front of a proxy that appends to `X-Forwarded-For` instead of overwriting it**, and a client that sends its own header supplies the first hop. The per-IP limiter now keys on an attacker-chosen string, and the audit log records whatever they typed. This is the failure mode `SECURITY.md` calls a foot-gun, and it is why the setting defaults to false.

The production stack gets it right through two facts, both verified rather than read:

1. **Caddy 2.10 replaces an inbound `X-Forwarded-For` with the peer's real address.** With no `trusted_proxies` configured, the immediate peer is untrusted, so its header is discarded rather than appended to. Tested: a request carrying `X-Forwarded-For: 1.2.3.4` reached the upstream carrying only the real peer address. This is a behavioural guarantee rather than a configuration one, which is why `docker-compose.prod.yml` pins `caddy:2.10-alpine` rather than tracking a floating tag.
2. **Nothing but Caddy can reach uvicorn.** The app publishes no port and lives on `edge`, so there is no path by which a client's own header arrives unfiltered.

`X-Real-IP` is the exception that the Caddyfile handles explicitly: the same test showed Caddy passes it through **untouched**, spoofed value and all. Nothing in Mailosh reads `X-Real-IP` today; `header_up X-Real-IP {remote_host}` overwrites it so that nothing which starts reading it tomorrow inherits a spoofable input.

**If you put anything else in front of Caddy** — Cloudflare, a load balancer, another nginx — both facts change. Caddy's immediate peer becomes that thing, so `X-Forwarded-For` gets replaced with *its* address and the real client is lost; recovering it needs `trusted_proxies` in the Caddyfile naming that hop. Work it out before you deploy it, not after the rate limiter starts behaving strangely.

### 5.1 `FORWARDED_ALLOW_IPS`, and why it is not the same knob

uvicorn's own proxy-header handling is on by default but trusts only `127.0.0.1`, and Caddy is not on loopback — it is another container. Without `FORWARDED_ALLOW_IPS=*`, `request.url.scheme` stays `"http"` behind an HTTPS proxy, and the app builds its own origin from that (`mailosh/web/mail.py`'s `origin`, `mailosh/web/frames.py`'s `base_url`). Two concrete Phase 1B breakages follow: the sandboxed mail frame posts from the opaque origin and the parent compares `http://host` against a page served from `https://host`, rejecting its own frames; and signed remote-image URLs come out `http://` and are blocked as mixed content.

`*` is the honest value in Docker, where Caddy's container IP is not stable, and it is safe for exactly the reason in §5 fact 2: nothing but Caddy can open a connection to that port. It is set as an environment variable, not a uvicorn flag, because uvicorn reads `FORWARDED_ALLOW_IPS` from the environment — which is what lets production differ from development without touching `docker/entrypoint.sh` at all.

### 5.2 Live updates through the proxy

`GET /events` is an SSE stream (`sse_starlette`, 25-second keepalive ping). Two proxy defaults would break it and neither is configured: no response buffering, and no stream timeout. Verified: a `text/event-stream` response proxied through this exact `encode zstd gzip` + `reverse_proxy` pair, with the client negotiating gzip, arrived one event at a time as it was written — not buffered to completion.

## 6. Secrets

Four values are secret, and all four live in `.env`, which Compose reads for `${...}` interpolation and passes to the app as `env_file`:

| Variable | What it protects |
|---|---|
| `MAILOSH_SECRET_KEY` | HKDF root for the Fernet key that encrypts each session's Stalwart API key, and for the undo-token HMAC. Changing it signs everyone out |
| `MAILOSH_STALWART_ADMIN_SECRET` | Stalwart's recovery admin. Can create and delete accounts and mint an API key for any of them — the highest-value credential in the stack |
| `MAILOSH_PG_PASSWORD` | the application database |
| *(the DNS provider token, if you configure Stalwart's DNS-01)* | lives in Stalwart's own settings, not here |

**Decision: `.env` with mode 0600, not Docker secrets.** Docker secrets would be better in principle — a tmpfs-mounted file rather than an environment variable visible to `docker inspect` and to anything that can read `/proc/1/environ` in the container. It is rejected because two of the three consumers cannot use it: `mailosh.config.Settings` is `pydantic-settings` with no `_FILE` convention, and Stalwart's `STALWART_RECOVERY_ADMIN` has no documented file variant. Only Postgres (`POSTGRES_PASSWORD_FILE`) could, and converting one of three buys a false sense of coverage. Revisit when the app grows `_FILE` support; it is a small change and it is listed in §12.

Two properties the compose file gives you for free:

- **Fail-fast on a missing value.** `${MAILOSH_PG_PASSWORD:?...}` makes `docker compose config` — and therefore `up` — exit non-zero with an explanatory message rather than starting a stack with an empty password. The same applies to `MAILOSH_SITE_ADDRESS`, `MAILOSH_ACME_EMAIL` and `MAILOSH_STALWART_ADMIN_SECRET`, whose base-file default is the literal string `changeme`. Verified: unsetting `MAILOSH_SITE_ADDRESS` makes `config` exit 1 with `required variable MAILOSH_SITE_ADDRESS is missing a value: set MAILOSH_SITE_ADDRESS in .env — …`.
- **`environment:` beats `env_file:`.** The three security-relevant settings — `MAILOSH_COOKIE_SECURE`, `MAILOSH_TRUST_PROXY`, `FORWARDED_ALLOW_IPS` — are set inline in the prod overlay, so a production run cannot be talked into a non-`Secure` session cookie by a `.env` still carrying `.env.example`'s `MAILOSH_COOKIE_SECURE=false` for localhost.

**One variable, not two, for Postgres.** `MAILOSH_PG_PASSWORD` feeds both `POSTGRES_PASSWORD` and the `MAILOSH_DATABASE_URL` DSN. Two variables that must agree is a foot-gun; one is not. Generate it with `openssl rand -hex 24` — hex, so the value is always safe inside a URL.

## 7. Before first boot

In order, with the reason each one exists.

1. **DNS.** Three records for three different names, which is the part most often got wrong:

   ```
   app.mailosh.com    A     -> this box   the webmail — MAILOSH_SITE_ADDRESS, Caddy's certificate
   mail.mailosh.com   A     -> this box   SMTP/IMAP, the SMTP banner, and the PTR target
   mailosh.com        MX 10 -> mail.mailosh.com
   ```

   The mail *domain* (`mailosh.com`) is not the webmail *host* (`app.mailosh.com`) and is not the mail *host* (`mail.mailosh.com`). Caddy's first ACME challenge fails without the `A` for `MAILOSH_SITE_ADDRESS`; nothing arrives without the `MX`; and nothing arrives either if the `MX` target itself has no `A`, which fails silently on this end because no sender ever connects.
2. **Reverse DNS (PTR)** for the box's IP, matching the **mail** hostname (`mail.mailosh.com`), not the webmail one. Set it in your provider's console. A missing or mismatched PTR is the single most common reason self-hosted mail lands in spam.
3. **Outbound port 25.** Most budget VPS providers block it by default, some permanently. `docs/hosting.md` has the per-provider table and the relay alternatives; sort this out before you migrate a mailbox, not after.
4. **Firewall.** Allow exactly the §3.1 list. Docker publishes ports by writing DNAT rules that on many distributions bypass a `ufw`/`firewalld` policy entirely — check what is actually reachable from outside rather than what your firewall rules say.

   That bypass is **IPv4-only**, and the asymmetry is the trap: Docker writes `iptables` rules and not `ip6tables` ones, so v4 sails past `ufw` to the published ports while v6 is dropped by `ufw`'s default `INPUT` policy. A box in this state answers `ping6` with every service port black-holed — so if you publish `AAAA` records for it (§3's DNS block offers them), senders that prefer IPv6 wait out a connection timeout before retrying over IPv4, and browsers stall on each new connection to the webmail host. Inbound mail still arrives, just late, and nothing logs an error, which is why this survives for months.

   This was live on this project's own deployment: removing the two `AAAA` records took webmail TTFB from 0.65 s to 0.14 s. Verify from another machine, per port, not per host:

   ```bash
   nc -6 -z -v <the box's IPv6 address> 25    # and 443, 465, 587, 993
   ```

   If those time out, either leave the `AAAA` records unpublished — IPv4-only is a fully supported deployment — or give Docker IPv6 (`"ip6tables": true` plus an IPv6 subnet in `/etc/docker/daemon.json`) and open the §3.1 ports over v6 in `ufw`, then re-check with the same command before publishing.
5. **`.env`**, from `.env.example`, `chmod 600`. Generate every secret; the app refuses to start while `MAILOSH_SECRET_KEY` begins with `change-me`, and the prod overlay refuses to render while the others are unset. Delete `MAILOSH_DEMO_USER` / `MAILOSH_DEMO_PASSWORD` — they name a dev mailbox and nothing in the web app reads them.
6. **Stalwart's own first-boot setup.** A fresh Stalwart container starts in *bootstrap mode*: only its HTTP port is up and no mail listeners are running at all, until `x:Bootstrap/set` completes and the server restarts. Meanwhile `/healthz/live` answers 200 and `docker compose ps` says *healthy*, so nothing signals the problem.

   This is now scripted, and it does **not** need the admin port published — `scripts/stalwart-bootstrap.sh` runs over the internal network, via `docker compose exec` against the stalwart container's own loopback:

   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml \
     scripts/stalwart-bootstrap.sh --domain mailosh.com
   ```

   It takes the domain and hostname as arguments (`--hostname` defaults to `mail.<domain>`, matching the MX target and the PTR), restarts Stalwart, and then verifies against the running server: out of bootstrap mode, hostname and domain as requested, every configured listener accepting connections, and an SMTP greeting on 25 naming the hostname. It refuses (exit 3) rather than reconfiguring a server that is already set up, and distinguishes "already configured", "wrong admin credential" and "not reachable" as separate exit codes. Verified end to end against a throwaway production-shaped stack; `docs/operations.md` §1 has the transcript.

   `scripts/stalwart-init.sh` remains the *development* one-liner — it hardcodes `mailosh.test` and creates the demo mailbox — and now calls the same script for its first-boot half. Do not point it at production.
7. **The first mailbox, DKIM, SPF, DMARC.** `mailosh setup` (`mailosh/cli.py`) creates the mailbox and prints the records, and it too runs over the internal network:

   ```bash
   docker compose exec -T mailosh mailosh setup --domain mailosh.com --email you@mailosh.com
   ```

   It reads the DKIM key from the server rather than templating one, and prints A/AAAA, MX, SPF, DKIM and DMARC as a copy-paste block. Note the SPF line it prints, `v=spf1 mx ~all`, is the **direct-send** record; under relay mode (`docs/hosting.md`'s recommendation for most deployments) the sending IP is the relay's and is not covered by `mx`, so the block prints the relay variant alongside it.
8. **Backups**, configured before there is anything to lose. §8.3.

## 8. Running it

### 8.1 Upgrades and migrations

`docker/entrypoint.sh` runs `alembic upgrade head` before uvicorn binds anything, on every start. That is the whole migration story and it needs no separate step: `docker compose ... up -d --build` rebuilds the image, restarts the container, and the new code's migrations run before the new code serves a request. The `mailosh` healthcheck's 60-second `start_period` exists so a long migration on a slow disk is not reported as an unhealthy app.

The consequences worth stating plainly:

- **There is a downtime window**, of a few seconds plus however long the migrations take. One app container, one worker, restarted in place. No rolling upgrade exists and none is claimed.
- **Migrations run before the operator can inspect them.** Take the §8.3 database backup *first*; Alembic has no automatic down-migration on failure, and a failed migration aborts the container rather than serving traffic against a half-migrated schema — which is the right behaviour and also means the site is down until you deal with it.
- **Pin what you deploy.** `stalwartlabs/stalwart:v0.16.20`, `postgres:16-alpine`, `caddy:2.10-alpine` are pinned in the compose files. A Postgres *major* upgrade is not a `docker compose pull` — it needs a dump and reload.

### 8.2 Reaching Stalwart's admin UI

Production publishes no HTTP port for Stalwart, which is the point, and it means the admin UI has no address. First-boot setup, creating accounts and reading the DKIM record no longer need it — `scripts/stalwart-bootstrap.sh` and `mailosh setup` both work over the internal network (§7 steps 6 and 7). What is left is DNS-01 configuration and anything else only the UI can do; for those, open a temporary forwarder onto the `mail` network and close it afterwards:

```bash
docker run --rm -d --name stalwart-admin-tunnel \
  --network <project>_mail -p 127.0.0.1:8080:8080 \
  alpine/socat TCP-LISTEN:8080,fork,reuseaddr TCP:stalwart:8080
# ... work at http://127.0.0.1:8080 (over `ssh -L 8080:localhost:8080 you@box` if remote)
docker rm -f stalwart-admin-tunnel
```

**Untested.** It could not be exercised in this pass: the only Docker network available was the running development stack's, which another workstream was mid-integration-test against and which must not be disturbed. The shape is standard and the container image is a one-purpose socat wrapper, but treat the exact invocation as unverified until someone runs it. `<project>_mail` is the network name Compose derives from the project directory — `docker network ls` will show it.

Note that the Stalwart image ships only the server binary (`/usr/local/bin/stalwart`); there is no bundled CLI, so there is no `docker compose exec` route that avoids the tunnel. `curl` *is* present in the image, so scripted admin work can go through `docker compose exec stalwart curl … http://localhost:8080/jmap` without any forwarder at all — that is the better path for anything repeatable.

### 8.3 Backups, and which volume matters

Named volumes, in descending order of how much you will regret losing them:

- **`stalwart-data` — all mail.** Every message, every mailbox, the full-text index. This is the irreplaceable one and the reason a backup exists.
- **`stalwart-etc`** — Stalwart's configuration and its DKIM *private* keys. Losing it loses no mail, but it does lose the keys your published DNS records point at, so signing breaks until you publish new ones.
- **`pg-data`** — application state only, never mail content: users, sessions, label colours and visibility, UI preferences, login-attempt counters, audit log. Losing it signs everyone out and forgets their settings. It does not lose a single message. `pg_dump` is enough here and is what to take before an upgrade.
- **`caddy-data`** — the ACME account key and every issued certificate. Survivable but not free: Caddy re-issues from scratch, against Let's Encrypt's duplicate-certificate rate limit of five per week per exact name.
- **`caddy-config`** — Caddy's autosaved JSON, regenerated from the Caddyfile on every start. Genuinely disposable.

A file-level copy of `stalwart-data` while Stalwart is running is a copy of a live database. Stop the service, or use a filesystem snapshot, or accept that you are testing your luck. `scripts/backup.sh` and `scripts/restore.sh` handle that correctly for both stores; point them at this overlay the way they document, through Compose's own environment rather than a flag of their own:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml \
  scripts/backup.sh /var/backups/mailosh --keep 7
```

*(That invocation follows the scripts' documented `COMPOSE_FILE` contract; it has not been exercised against a production stack from here.)*

### 8.4 Rotating the Postgres password

`POSTGRES_PASSWORD` is read only at `initdb`, on the first boot with an empty `pg-data` volume. Changing `MAILOSH_PG_PASSWORD` afterwards changes the DSN the app dials and nothing about the database, so the app simply stops authenticating. To rotate: `ALTER USER mailosh WITH PASSWORD '<new>'` inside the running container, *then* update `.env`, then `up -d`. The same trap catches anyone converting a development volume — one that was initialised with `mailosh:mailosh` — into a production one. Start clean if you can.

## 9. Threat model

Two deployments wear the same compose file and face different worlds. The line between them is worth drawing explicitly, because most of the advice a self-hoster reads silently assumes one of the two.

**A home box, LAN-only.** The adversary is a compromised device on the same network, a housemate, or a browser on a hostile page. TLS is still worth having (`tls internal` costs nothing and stops a phone on the same Wi-Fi reading session cookies), but ACME, PTR and port-25 unblocking are all irrelevant. Not publishing 8080 still matters here — arguably more, because "everything on the LAN is friendly" is exactly the assumption that makes an unpublished admin port the difference between one compromised laptop and every mailbox on the server.

**Internet-facing.** The adversary is the whole internet, continuously, from the first minute the DNS record resolves. What changes:

- **The login form is under constant automated attack.** The rate limiter is the defence, it keys on client IP, and §5 is the entire reason that key is trustworthy. There is no second factor — a stolen mail password is enough (`SECURITY.md`; TOTP and passkeys are Phase 2).
- **Port 25 is an open door by design.** Stalwart's own spam and abuse handling is what stands there; nothing in this document changes it.
- **Stalwart's admin secret is now the crown jewel**, because a network-reachable admin API means one leaked string equals every mailbox. It is not network-reachable in this design, which is the single most important thing this compose file does.
- **"Sign out everywhere" has to actually mean it**, which is one more reason the deployment runs a single worker — §12.1. A revoked session whose event stream keeps delivering mail is not a stale cache; it is the app telling a user something untrue about their own account, and it is the failure a second worker would introduce.
- **Access logs become a retention question.** Caddy logs request paths, which in this app carry thread ids (`/t/{thread_id}`) and message ids. That is not message content, but it is a record of what each user read and when, kept for as long as the Docker log driver keeps it. The Caddyfile documents how to discard it and what discarding it costs.
- **The AGPL applies.** Running a modified version as a network service obliges you to offer its source to your users.

**What is the same in both:** mail is not rendered as HTML yet (Phase 1B), so the hostile-HTML surface does not exist yet — and will, so do not conclude from today's quiet that it is permanently safe. Nothing here has had an external audit.

## 10. Sizing

`docs/hosting.md` owns this and its numbers are verified against primary sources: the reference box is 2 vCPU / 4 GB / ≥40 GB NVMe, Stalwart idles near 100 MB, and Postgres wants `shared_buffers` around 512 MB–1 GB on a shared 4 GB machine. That tuning is deliberately **not** in `docker-compose.prod.yml`: it depends on how much RAM the box actually has, a wrong value is a boot failure rather than a slow query, and hosting.md is where sizing lives. Set it with a `command: postgres -c shared_buffers=…` override when you have measured something.

## 11. Verification

Everything below was executed in this pass, against Docker Compose v5.1.2 and `caddy:2.10-alpine` (v2.10.2), unless the line says otherwise.

**Run this after any change to either compose file:**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```

It must exit 0 and the published-port list must be exactly §3.1 — nothing for `mailosh` or `postgres`, four mail ports for `stalwart`, three for `caddy`. Confirmed, along with: `backend` rendering `internal: true`; `mailosh` rendering zero bind mounts, `MAILOSH_RELOAD: "0"` and `command: /app/docker/entrypoint.sh`; every service carrying a healthcheck and `restart: unless-stopped`.

Also verified:

- Compose concatenates `ports:` across files (§3.3), `!reset` deletes the key including anything written under it, `!override` replaces it.
- The three-file defence: `-f base -f dev -f prod` renders `mailosh` with no volumes, no ports and `MAILOSH_RELOAD=0`.
- Fail-fast: unsetting `MAILOSH_SITE_ADDRESS` makes `config` exit 1 with its explanatory message.
- Caddy replaces a spoofed inbound `X-Forwarded-For` and passes `X-Real-IP` through untouched; `header_up X-Real-IP {remote_host}` fixes the latter (§5).
- SSE streams through `encode zstd gzip` + `reverse_proxy` one event at a time with gzip negotiated (§5.2).
- The `@versioned_static` matcher applies `immutable` to `/static/app.css?v=…` and to neither `/static/app.css` nor `/login`.
- The Caddy healthcheck (`wget -q --spider http://127.0.0.1:2019/config/`) exits 0 inside a running container; `caddy:2.10-alpine` has `wget`, `curl` and `nc`, while `python:3.12-slim` — the app's base — has none of them, which is why the app's healthcheck is `python -c … urllib`.
- `GET /login` answers 200 to an anonymous request, which is what that healthcheck depends on.
- `WEB_CONCURRENCY` is honoured by uvicorn only when no `--workers` flag is given (`uvicorn/config.py`), and uvicorn forks only above 1 (`uvicorn/main.py`) — so `WEB_CONCURRENCY: "1"` pins §12.1's constraint without changing what runs today, and without touching `docker/entrypoint.sh`. Read in the installed uvicorn, and it renders in the merged config.
- `.venv/bin/python -m pytest tests/unit -q` → 1770 passed, 2 skipped. No application code was changed in this pass.

**Asserted, not verified** — no production-like host with a public DNS name was available:

- End-to-end ACME issuance and renewal for `MAILOSH_SITE_ADDRESS`.
- ~~The full stack coming up under `docker-compose.prod.yml`~~ — **since verified**, on 2026-09-05, in a throwaway compose project (`mailosh-bootstrap-drill`) rendered from `docker-compose.yml` + `docker-compose.prod.yml` plus a drill-only overlay that moved the four mail ports to high host ports and changed nothing else. `stalwart`, `postgres` and `mailosh` came up healthy with the production network layout (`backend` internal, no published port for `mailosh`, `postgres` or Stalwart's 8080); Stalwart was bootstrapped over the internal network, accepted a message on port 25 for a mailbox created by `mailosh setup`, and the message was read back over JMAP. Caddy was **not** started (it would have wanted 80/443 on the test machine), so ACME remains unverified.
- The §8.2 admin tunnel, for the reason given there.
- Stalwart's DNS-01 configuration for the mail ports (§4.2) — Stalwart's challenge support is `docs/hosting.md`'s reading of Stalwart's documentation, not an observation of this deployment.

## 12. What this pass does not make production-safe

Stated here rather than left for a reader to discover.

- **One uvicorn worker, pinned.** This has its own subsection below, because it is the item on this list most likely to be read as a tuning default and quietly changed.
- ~~**Stalwart's first-boot configuration is manual.**~~ **Closed** — `scripts/stalwart-bootstrap.sh` (§7 step 6) does it over the internal network, idempotently, and verifies the listeners actually came up. What is *not* closed is that it is still two commands in a terminal rather than the Phase 2 wizard.
- **Stalwart's mail-port TLS is manual** (§4.2), and is self-signed until the operator configures DNS-01. The capability is confirmed present in `v0.16.20` — its own schema exposes `x:AcmeProvider` with a `Dns01` challenge type and 70 built-in DNS provider variants (an earlier note said 45 and wrongly listed DigitalOcean, Hetzner and deSEC as absent; all three are present) — but **issuance was never exercised**, because it needs a public domain whose DNS the box can edit. `docs/operations.md` §1 records exactly what was and was not checked.
- ~~**Nothing listens on 587.**~~ **Closed.** Stalwart `v0.16.20`'s default post-bootstrap listener set is `smtp:25`, `submissions:465`, `imaps:993`, `pop3s:995`, `sieve:4190`, `http:8080`, `https:443` — read from its own `x:NetworkListener` objects and confirmed by connecting to each — and both compose files published 587 against nothing. `scripts/stalwart-bootstrap.sh` now creates the listener (on first boot, and on an already-configured server that is missing it) and verifies against the running server that it advertises `STARTTLS` and offers no password mechanism before the session is encrypted. Verified end to end on a throwaway stack: a message submitted over 587 with STARTTLS and `AUTH LOGIN` arrived and was read back over JMAP; `AUTH PLAIN` in the clear was answered `554 5.7.8 Authentication mechanism not supported.`; and `MAIL FROM` with no authentication was answered `503 5.5.1 You must authenticate first.` The 143 half went the other way — the dev compose file's `1143:143` was removed rather than a plaintext-capable IMAP listener added, since 993 covers every modern client and this spec already recorded 143 as opt-in.
  - One consequence to know: `x:NetworkListener` cannot be written in bootstrap mode, and creating a listener does not bind the port (`x:Action/ReloadSettings` does not rebind either — tried). Only a restart does, which is why a converging run that has to *create* the listener will restart Stalwart, announcing it first. `--verify-only` changes and restarts nothing.
- **Secrets are environment variables**, visible to `docker inspect` and to anything that can read the container's environment (§6). Fixing it properly needs `_FILE` support in `mailosh.config.Settings`.
- **No log shipping, no metrics, no alerting.** `docker compose logs` and `docker compose ps` are the whole observability story. Log volume is capped (10 MB × 3 per service) so a chatty failure cannot fill the disk, which is the one failure this pass does prevent.
- **The app has no health endpoint.** The container healthcheck renders the full `/login` page every 30 seconds because it is the only anonymous 200 in the app. A trivial `GET /healthz` returning 204 would be cheaper and more precise; it is an application change and is not in this pass's scope.
- **No *scheduled* backups.** `scripts/backup.sh` and `scripts/restore.sh` exist and do the right thing for both stores; nothing in this deployment runs them on a timer, and nothing verifies that the last one restores. A cron entry and a restore drill are the operator's, and they are the difference between having backups and believing you do.
- **No rolling upgrade, no zero-downtime deploy** (§8.1).

### 12.1 One worker, and why it is a correctness constraint

`docker-compose.prod.yml` sets `WEB_CONCURRENCY: "1"`. That pins today's behaviour rather than changing it — uvicorn reads `WEB_CONCURRENCY` when no `--workers` flag is given (`uvicorn/config.py`) and only forks above 1 (`uvicorn/main.py`), and nothing sets `--workers` anywhere. It is written down explicitly so that the next person to consider raising it finds the reason attached to the number, instead of finding it in an incident.

**The failure is a revoked session that keeps delivering mail.** `mailosh.web.auth.logout` does two things in sequence: it revokes the session row in Postgres, which is shared and which every process therefore sees, and then it calls `pool.drop(session.id)` on `app.state.pool` — which is **per process**. With a second worker, that worker's already-open `GET /events` goes on streaming the user's mail on the pooled JMAP client it still holds, because the drop never crossed the process boundary.

There is no time bound on it. `mailosh.jmap.pool` deliberately exempts a client carrying a live stream from `stop_idle` — correctly, since a tab sitting on an open `/events` makes no requests of its own and would otherwise have its connection closed underneath it — so the sweep that would eventually reap an ordinary idle client will never touch this one.

It self-heals in exactly one case: if that logout was the user's **last** session, the Stalwart API key is destroyed and the upstream listener dies with it. If they still have a phone or a second browser signed in, the key survives and the revoked session's stream continues indefinitely. "Sign out" would have told the user something untrue, which is the part that makes this a security property and not a bug in a cache.

**`MAILOSH_SSE_FANOUT=postgres` does not fix this.** `mailosh/db/notify.py` relays live-update *events* between workers over `LISTEN`/`NOTIFY`, which is a real and necessary piece — without it, tabs on one worker never hear what another worker's listener received. But it is transport. It does not carry a logout, and turning it on is not clearance to scale out. The `.env.example` entry says so at the point of use, because the setting's existence is precisely what would read as permission.

What multi-worker actually needs first is for session revocation to reach every process — the same `LISTEN`/`NOTIFY` channel is the obvious place to put it, and it is a small change on top of what now exists. Until then, concurrency comes from async I/O within the one worker, which is what the app is built for. For the target deployment — a family or small team on a 4 GB box — that is not the bottleneck, and if it ever is, that is a finding to investigate before it is a reason to touch this line.

## 13. Open items (do not block)

Cross-process session revocation, which is what `WEB_CONCURRENCY` stops being pinned at 1 for (§12.1) and which the `LISTEN`/`NOTIFY` channel `mailosh/db/notify.py` already opens is the natural home for; Caddy `request_body { max_size … }` once Phase 1C can upload attachments; a `trusted_proxies` recipe for the Cloudflare-in-front case (§5); whether Stalwart can be made to reload a renewed certificate file, which would reopen §4.3's first rejected option and remove the DNS-01 requirement entirely; `_FILE` secret support; and folding all of this into the Phase 2 setup wizard, which is where it stops being a document and starts being a product.
