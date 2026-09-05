# Security Policy

Mailosh is a mail client. A bug here can expose someone's entire
correspondence, so security reports get priority over everything else.

Please read the **[Known limitations](#known-limitations)** section before
deploying this anywhere that matters. This is an early release, it has had
no external audit, and the piece of a mail client's security model that
matters most — safe rendering of hostile HTML mail — is newly built rather
than battle-tested.

## Reporting a vulnerability

**Do not open a public issue, pull request, or discussion for a security
problem.**

Use the repository's **Security** tab → **Report a vulnerability**, which
opens a GitHub private advisory visible only to the maintainers. It is the
only reporting channel, deliberately: a published address on a project this
size collects more spam than reports, and a private advisory gives us a
place to work on a fix with you before anything becomes public.

If you cannot use GitHub, open a public issue containing **no detail** —
just a request for a contact address — and a maintainer will follow up.

Please include:

- what the issue is and what an attacker gains;
- the version or commit you tested, and how Mailosh was deployed;
- reproduction steps, ideally with a minimal request or message; and
- any log output, redacted of real mail content and credentials.

**Expectations.** This is a small volunteer project, so no response-time SLA
is promised. You should expect an acknowledgement that someone has read your
report, a decision on whether it is in scope, and — if it is — a fix and a
credit in the release notes unless you would rather stay anonymous. Please
give the maintainers a reasonable window to ship a fix before publishing
details.

Please do not run automated scanners against anyone else's Mailosh
deployment, and do not test against mailboxes you do not own.

## Scope

**Supported:** the tip of the default branch only. There are no released
versions to backport to yet.

### In scope

- Authentication and session handling: login, the Postgres session store,
  cookie attributes, expiry and revocation, sign-out-everywhere.
- The Stalwart credential exchange — password verification, API-key minting
  and destruction, and the encryption of stored key material.
- CSRF and the Fetch-Metadata checks on mutating routes.
- The login rate limiter, and anything that lets it be bypassed (including
  spoofing the client IP).
- Cross-user data exposure: one authenticated user reaching another's mail,
  mailboxes, preferences or sessions.
- Injection into rendered pages — template escaping of mail metadata
  (sender names, subjects, mailbox names, previews), header injection,
  open redirects (`?next=`), and CSP bypasses.
- SSRF or request smuggling through the JMAP client or the SSE bridge.
- Secrets handling: anything that writes a password, API key, session id or
  CSRF token to a log, an error page, or a URL.

### Out of scope

- **Stalwart itself.** Report mail-server vulnerabilities to
  [the Stalwart project](https://github.com/stalwartlabs/stalwart).
- **Dev-stack defaults.** `changeme` in `.env.example`, the
  `mailosh:mailosh` Postgres credentials in `docker-compose.yml`, and
  Stalwart's admin port published on loopback are development conveniences,
  documented as such. Report them against `docker-compose.prod.yml`, which
  is the deployment we make claims about — not against the dev stack.
- **Missing features.** Compose, search, 2FA and label
  management are not implemented; "Mailosh does not do X" is a roadmap
  item, not a vulnerability. The consequences of what *is* missing are
  listed below.
- Self-XSS, clickjacking on pages that already send
  `frame-ancestors 'none'`, missing hardening headers with no demonstrated
  impact, and findings that require an already-compromised host or browser.
- Denial of service by resource exhaustion against a single-worker
  deployment. Phase 1 is explicitly single-worker.

## Current security posture

What is actually implemented today, so you can tell a gap from a bug.

### Credentials and sessions

- **Your mail password is never stored.** Login verifies it against
  Stalwart's JMAP session endpoint and then discards it. What is kept is a
  Stalwart **API key** minted through the admin client for that account, and
  it is stored Fernet-encrypted (`mailosh/security/crypto.py`), never in
  plaintext. Stalwart caps API keys per account, so one key is minted per
  *user* and reused across that user's sessions; when the last session for a
  user is logged out or reaped, the key is destroyed at Stalwart.
- **Sessions live in Postgres.** Session id and CSRF token are drawn
  independently from `secrets.token_urlsafe(32)` — neither is derived from
  the other. Each login creates a fresh row with a fresh id.
- **Cookie:** `__Host-sid`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and
  `Secure` when `MAILOSH_COOKIE_SECURE` is true (it must be, on anything
  served over HTTPS). With the flag off — for `http://localhost` — the
  cookie is named `sid` instead, because the `__Host-` prefix requires
  `Secure`.
- **Expiry:** sliding idle expiry of 14 days (30 with "Keep me signed in"),
  and a hard absolute expiry of 90 days that is fixed at login and never
  extended. A background sweep reaps expired sessions and destroys the
  Stalwart key material they held.
- **Key derivation.** `MAILOSH_SECRET_KEY` is never used directly. Two
  unrelated keys are derived from it with HKDF-SHA256 under separate
  purposes: the Fernet key for session credentials, and the HMAC key for
  undo tokens. Compromising one says nothing about the other.

### Request protection

- **CSRF on every mutating route.** All mutations are `POST`, and
  `mailosh/web/deps.py`'s `csrf_protect` requires the session's own CSRF
  token — as the `X-CSRF-Token` header (htmx sends it via inherited
  `hx-headers` from `<meta name="csrf-token">`) or as a hidden form field —
  compared with `secrets.compare_digest`. Any unsafe request whose
  `Sec-Fetch-Site` is `cross-site` is rejected outright, even with a correct
  token. `HX-Request` is never trusted as evidence of origin.
- **`POST /login` is the one exception, by necessity:** there is no session
  yet to bind a token to. It applies the same `Sec-Fetch-Site` check plus
  rate limiting.
- **Login rate limiting**, in Postgres, applied *before* Stalwart is
  contacted (Stalwart auto-bans a source IP after repeated failures, and the
  webmail host must never trip that on its users' behalf): 5 failures per
  account and 20 per IP in a 15-minute window, then exponential backoff from
  60 seconds, capped at one hour. A wrong password and an unknown account
  produce identical copy and status. An unreachable mail server is *not*
  counted as a failure. Login success, failure and logout are written to an
  audit log.

### Headers

Every response carries:

```
Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self';
  connect-src 'self'; object-src 'none'; base-uri 'none';
  frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
```

`script-src 'self'` with **no `unsafe-eval`** is a hard constraint the whole
frontend is built around: Alpine is vendored in its CSP build (which parses
expressions rather than constructing functions), and htmx's
`Function`-compiling attributes are not used anywhere. See
`CONTRIBUTING.md`. Nothing is loaded from a CDN at runtime — every script,
icon and font is vendored and served from the app's own origin.

### Data handling

- **Mail content never touches Mailosh's database.** Postgres holds users,
  sessions, label metadata, UI preferences, harvested-contact and
  image-allow tables, login attempts and the audit log — nothing else.
- **Undo tokens are HMAC-signed** with an independently derived key and a
  60-second TTL, so an undo request cannot be forged or replayed later.
- Passwords and key material are excluded from log output and error pages.

## Known limitations

These are the things to weigh before pointing a real mailbox at this.

- **HTML mail rendering is new code, and it is the highest-value target
  in this repository.** It is built and heavily tested, not absent — the
  earlier text here said otherwise and was stale. Three independent layers
  handle a hostile message, each written on the assumption that the other
  two may fail: `nh3` strips the HTML server-side, `tinycss2` re-serialises
  CSS through an allow-list, and the result is served into a sandboxed
  iframe under its own restrictive CSP. `allow-same-origin` appears in no
  sandbox anywhere in the served application, which is the single property
  keeping a hostile message away from the reader's session.
  The frame's `img-src` is `'self' data:` and never gains `http:` or
  `https:` — byte-identical whether remote images are on or off — so a
  sanitiser miss still cannot leak the reader's IP to a sender.
  Two denial-of-service bugs in this pipeline were found and fixed
  pre-release (unbounded recursion in the CSS serialiser; quadratic
  behaviour on deeply nested markup). Both are now bounded and regression-
  tested. **Rendering is verified in Chromium; Firefox and Safari have not
  been checked by a human.** Treat this pipeline as the most rewarding place
  to look for a bug.
- **Image URLs inside a message frame are bearer capabilities.** The frame
  is served with an opaque origin (no `allow-same-origin`), so its own
  subresource requests carry no session cookie — by design, and the reason
  a hostile message cannot reach the reader's session. Inline parts and
  proxied remote images are therefore authorised by a short-lived
  HMAC-signed token in the URL instead. Each names the reader, and the
  inline-part token additionally names the message and the content id, so
  one cannot be spent on another mailbox, another message, or another part
  — this is verified in-browser, including with a second reader's cookie
  attached. The honest trade is that within its one-hour lifetime such a
  URL is spendable by whoever holds it, without a session. Signing out
  everywhere invalidates outstanding URLs immediately rather than at
  expiry. Report anything that lets one of these tokens be reused across
  readers, messages or parts, or that extends its lifetime.
- **No second factor.** There is no TOTP and no passkey support; a stolen
  mail password is enough to sign in. Both are planned for Phase 2 alongside
  the platform security work.
- **TLS depends on which compose file you deploy.** `docker-compose.yml`
  is a *development* stack: it publishes ports on `127.0.0.1` and
  terminates no TLS. `docker-compose.prod.yml` adds Caddy, which terminates
  TLS and obtains certificates over ACME. Deploying the development file to
  a public address is a misconfiguration, not a vulnerability — but it is
  an easy one to make, so it is called out here and in the README.
- **`MAILOSH_TRUST_PROXY` is a foot-gun.** Enable it only behind a proxy
  that overwrites `X-Forwarded-For`; otherwise a client can spoof its
  address and walk past the per-IP login limiter.
- **Stalwart's admin port.** That port accepts the admin secret and is
  therefore never published in production: `docker-compose.prod.yml`
  publishes only the four mail ports (25, 465, 587, 993), reaching the
  admin/JMAP API over an internal Docker network instead. The dev compose
  file does publish it on loopback for convenience. Note that Compose
  *concatenates* `ports:` across files, so an override that merely omits a
  port still publishes it — the production file uses `!reset` for exactly
  this reason, and any change to it should be re-checked with
  `docker compose ... config` rather than by reading the file.
- **Single uvicorn worker, pinned as a correctness constraint.**
  `WEB_CONCURRENCY=1` is set deliberately. Signing out revokes the session
  row in Postgres, which every process sees, but drops the pooled upstream
  client only in the process that served the request. With a second worker,
  that other worker's already-open event stream keeps delivering the user's
  mail on a connection the idle sweep deliberately exempts — so a revoked
  session can keep receiving mail with no time bound, unless it was that
  user's last session. In other words "sign out" would report something
  untrue about the account, which is why this is a security property and
  not a performance note. `MAILOSH_SSE_FANOUT=postgres` relays live-update
  events between workers but does **not** carry a logout, and is not
  clearance to scale out. Cross-process session revocation is the work that
  would unpin this.
- **No external audit.** Nothing in this repository has been reviewed by
  anyone outside the project.
