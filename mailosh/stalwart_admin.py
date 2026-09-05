"""Async client for Stalwart's admin/management surface (Task 10, SPK-3/SPK-5).

Task 2 (SPK-5, see `docs/spikes/p0-findings.md`) found there is no
separate REST management API on `stalwartlabs/stalwart:v0.16.20` -- domains,
accounts, and DKIM keys are all managed through the *same* `POST /jmap`
endpoint and HTTP Basic auth as regular mail JMAP traffic, using custom
JMAP-shaped methods on `x:`-prefixed object types (capability
`urn:stalwart:jmap`). This client is deliberately a separate, standalone
`httpx.AsyncClient` from `mailosh.jmap.client.JmapClient` -- not built on top
of it -- because the two operate as fundamentally different principals (an
admin/recovery credential managing the whole server, vs. an individual
mailbox's own JMAP session) even though they share a wire protocol shape.

Unlike `JmapClient.connect`, there is no `.well-known/jmap` session-discovery
step here: every call in this module POSTs straight to `{base_url}/jmap`,
exactly the way `scripts/stalwart-init.sh`'s already-proven `jmap_call()`
shell function does. The one thing this client does still need from a
session is the admin credential's own JMAP *account id* (required as the
`accountId` argument on every `x:Domain`/`x:Account`/`x:DkimSignature` call)
-- resolved once, lazily, from `GET {base_url}/jmap/session` and cached
(SPK-5 note: this id looked stable, `"d333333"` on every fresh install Task 2
tried, but must still be resolved dynamically rather than hardcoded, in case
that changes across versions/environments).

SPK-5 close-out: `x:Domain/set create` -- the one call Task 2 didn't get to
exercise (bootstrap's `defaultDomain` only creates a domain as a side effect,
once, on a fresh server) -- was verified live for the first time while
writing this module. A bare `{"name": "<domain>"}` create payload succeeds
with no `"@type"` discriminator needed (`x:Domain` is a plain object, not a
tagged union like `x:Account`), auto-generates DKIM keys immediately (same
`dkimManagement: Automatic` default bootstrap's `defaultDomain` relies on),
and a duplicate-name retry comes back as a normal 200 with
`notCreated: {"<id>": {"type": "primaryKeyViolation", "properties": ["name"],
"objectId": {...}}}` -- not a batch-level `error` tuple, not a 4xx. Every
`create_*` method below tolerates exactly that shape as an idempotent no-op
(chosen over a query-then-create round trip: it's atomic -- no
check-then-act race against a concurrent caller -- and one HTTP call instead
of two on the common "already exists" path) and raises on anything else.

SPK-3 probe: `try_mint_user_token`'s full findings, including why
`x:OAuthClient`/the live `/.well-known/oauth-authorization-server` endpoint
(a real, working OAuth2 device-code authorization server) is NOT usable for
this method's contract, live all in `docs/spikes/p0-findings.md`
under SPK-3 -- summary: `x:ApiKey/set create`, called with `accountId` set to
the TARGET user's own JMAP account id (not the admin's -- everywhere else in
this module `accountId` means the admin's), mints a real, working per-user
Bearer credential, server-secret-and-all, in one HTTP call.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import Self

import httpx

from mailosh.jmap.errors import JmapError, MethodError, TransportError

logger = logging.getLogger(__name__)

#: Capabilities every admin request declares. `urn:stalwart:jmap` is what
#: gates the `x:`-prefixed custom methods this whole module is built on
#: (SPK-5); `urn:ietf:params:jmap:core` is the base JMAP capability every
#: request needs regardless of which object types it touches.
_USING = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]

#: `x:DkimSignature`'s `@type` variants (SPK-5) mapped to the DKIM TXT
#: record's `k=` tag. Both of v0.16.20's default-generated algorithms hash
#: with SHA-256 (it's baked into each variant's own name --
#: `Dkim1RsaSha256`/`Dkim1Ed25519Sha256` -- there is no separate "hash
#: algorithm" field in the schema to read this from), which is also exactly
#: what every live `h=sha256` tag Task 2/10 observed in a real generated
#: zone file already said -- so `h=sha256` below is reproduced from that
#: observation, not assumed independently of it.
_DKIM_ALGO_K = {"Dkim1RsaSha256": "rsa", "Dkim1Ed25519Sha256": "ed25519"}

#: Preference order when a domain has more than one DKIM algorithm
#: configured -- Stalwart's default `dkimManagement: Automatic` always
#: generates both an RSA and an Ed25519 key per domain (SPK-5). RSA is
#: preferred: it's the algorithm every major mailbox provider's DKIM
#: verifier is guaranteed to support, where Ed25519 (RFC 8463) verifier
#: support is still inconsistent industry-wide as of this writing. A domain
#: with only one algorithm configured (or some future third variant) still
#: resolves via the `next(iter(...))` fallback in `get_dkim_record`.
_DKIM_PREFERENCE = ["Dkim1RsaSha256", "Dkim1Ed25519Sha256"]

#: `get_dkim_record`'s retry budget for `_DKIM_PREFERENCE[0]` (RSA) to
#: appear: up to this many attempts, `_DKIM_POLL_DELAY_SECONDS` apart
#: (worst case ~3s total). Found necessary live, not anticipated by SPK-5 or
#: the task brief: creating a brand-new domain and querying
#: `x:DkimSignature` in the very same batched request shows ZERO keys for
#: it yet -- Stalwart generates a domain's `dkimManagement: Automatic` keys
#: asynchronously, not as part of `x:Domain/set create` itself -- and a
#: query moments later can catch a partial state, the faster-to-generate
#: Ed25519 key already present while the slower RSA key isn't yet. Without
#: this, `get_dkim_record` called immediately after `create_domain` (as
#: `mailosh/cli.py`'s `setup` command always does) would nondeterministically
#: return the wrong algorithm, or occasionally raise outright, depending on
#: how much wall-clock time happened to pass between the two calls. Costs
#: nothing on the common path -- an already-provisioned domain's RSA key is
#: found on the very first attempt, no sleep ever happens.
_DKIM_POLL_ATTEMPTS = 10
_DKIM_POLL_DELAY_SECONDS = 0.3


@dataclass(frozen=True)
class DkimRecord:
    """One DKIM DNS TXT record to publish: the host name to create it under
    (`<selector>._domainkey.<domain>`) and the record's full value
    (`v=DKIM1; k=...; h=sha256; p=...`). A plain 2-tuple would work just as
    well here, but a named dataclass makes `get_dkim_record`'s two
    call sites (the CLI's DNS-block formatter, and this module's own tests)
    self-documenting about which half is which, the same reasoning
    `mailosh.jmap.client.SseFrame` already uses for its own 2-field shape.
    """

    host: str
    value: str


@dataclass(frozen=True)
class ApiKey:
    """A minted Stalwart `x:ApiKey` (Task 4, SPK-3): its server-assigned
    `id` (needed later to `destroy_api_key`) and its server-generated
    `secret` -- shown exactly once, in the `x:ApiKey/set create` response
    that produces this object, never recoverable afterward (SPK-5/SPK-3:
    `secret` is `update: "serverSet"` and redacted on any later read, the
    same "shown once" shape as `DkimRecord`'s `privateKey.secret`). `secret`
    is a Bearer credential for exactly the target account's own JMAP
    session (`JmapClient.connect_bearer`), not the admin's.
    """

    id: str
    secret: str

    def __repr__(self) -> str:
        # Task 5 controller ruling #4: this task starts logging around
        # mint/destroy (per-user key reuse, the reaper) — a default
        # dataclass repr would print `secret` verbatim into any
        # `logger.debug("...%r...", key)` call, so this overrides it with a
        # fixed redaction marker instead. `@dataclass` only generates its
        # own `__repr__` when the class doesn't already define one, so this
        # is used as-is, not overwritten by the decorator above.
        return f"ApiKey(id={self.id!r}, secret='API_***')"


#: `create_api_key`'s random-suffix width, in raw bytes (rendered as
#: ``2 * _API_KEY_SUFFIX_BYTES`` hex characters -- 4 bytes -> 8 hex chars,
#: matching controller decision #3's `mailosh-session-<8 hex>` naming).
#: Purely a namespacing/identifiability aid, not a secret -- the value that
#: actually authenticates is the server-generated `secret` field, never
#: this suffix -- so 4 bytes (32 bits) is plenty to make repeated calls for
#: the same `name` not collide, without pretending this is itself
#: cryptographic material.
_API_KEY_SUFFIX_BYTES = 4


def _transport_error(exc: httpx.HTTPStatusError | httpx.TransportError) -> TransportError:
    """Translate an httpx-level failure into `TransportError`, keeping status/message.

    Same translation `mailosh.jmap.client`'s own module-level helper of the
    same name does, deliberately duplicated in full rather than imported
    across modules: `StalwartAdmin` is "its own httpx.AsyncClient" (Task 10's
    brief), independent of `JmapClient`, and that helper is a private,
    non-exported implementation detail of `client.py`, not a shared utility.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return TransportError(
            f"Stalwart admin request to {exc.request.url} failed: "
            f"{resp.status_code} {resp.reason_phrase}",
            status_code=resp.status_code,
        )
    return TransportError(f"Stalwart admin request failed: {exc}", status_code=None)


class StalwartAdmin:
    """A Stalwart admin/recovery credential, able to manage domains, accounts,
    and DKIM keys, and to probe per-user token minting (SPK-3).

    Construction is synchronous and does no I/O (unlike `JmapClient.connect`,
    there's no session to discover up front) -- the admin's own JMAP account
    id is resolved lazily, on first use, by `_admin_account_id`.
    """

    def __init__(self, base_url: str, admin_user: str, admin_secret: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            auth=(admin_user, admin_secret),
            http2=False,
            timeout=30,
            follow_redirects=True,
        )
        #: Populated by `_admin_account_id` on its first successful fetch,
        #: and reused after that -- see that method's docstring.
        self._account_id: str | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._http.aclose()

    async def _admin_account_id(self) -> str:
        """Resolve (and cache) this admin credential's own JMAP account id.

        `GET {base_url}/jmap/session` directly, not `.well-known/jmap` the
        way `JmapClient.connect` does -- SPK-5 confirms this endpoint
        answers Basic-auth'd requests directly, no redirect indirection,
        for both the bootstrap-mode and fully-set-up admin. Cached as an
        instance attribute after the first call (never re-fetched), the
        same one-round-trip-then-cache shape `JmapClient.get_identity`
        already uses for its own instance-cached value.
        """
        if self._account_id is not None:
            return self._account_id
        try:
            resp = await self._http.get(f"{self._base_url}/jmap/session")
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise _transport_error(exc) from exc
        data = resp.json()
        self._account_id = next(iter(data["accounts"]))
        return self._account_id

    async def _call(self, method_calls: list[tuple[str, dict, str]]) -> dict[str, dict]:
        """POST a batched JMAP request to `{base_url}/jmap`, keyed by call id.

        Simpler than `JmapClient._call`: nothing in this module ever
        triggers or needs to inspect an RFC 8620 §5.3 implicit method call
        (that machinery -- `_call_raw`, keep-first-response deduplication --
        exists there only for `send`'s `onSuccessUpdateEmail`), so this is
        the plain "one response per call id, raise on the first `error`
        tuple" version. Raises `TransportError` on a non-2xx response or a
        connection failure, `JmapError` if the response body has no
        `methodResponses` at all, `MethodError` on a batch-level `error`
        response (as opposed to a per-object `notCreated`/`notUpdated`
        entry inside an otherwise-normal response, which every caller below
        checks for itself).
        """
        body = {"using": list(_USING), "methodCalls": [[n, a, c] for n, a, c in method_calls]}
        try:
            resp = await self._http.post(f"{self._base_url}/jmap", json=body)
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise _transport_error(exc) from exc
        data = resp.json()
        if "methodResponses" not in data:
            raise JmapError(f"Stalwart admin response missing 'methodResponses': {data!r}")
        results: dict[str, dict] = {}
        for name, args, call_id in data["methodResponses"]:
            if name == "error":
                raise MethodError(args.get("type", "error"), call_id)
            results.setdefault(call_id, args)
        return results

    async def _find_domain_id(self, name: str) -> str | None:
        """Resolve a domain name to its internal id via `x:Domain/query`+`get`.

        Lists every domain and filters client-side by `name` rather than
        trusting a server-side query filter -- the same "list all, match
        locally" shape `scripts/stalwart-init.sh`'s `DOMAIN_ID` lookup
        already uses and Task 2 already proved correct against the live
        server, not a server-side filter capability that was never actually
        confirmed to exist for `x:Domain/query`.
        """
        account_id = await self._admin_account_id()
        out = await self._call(
            [
                ("x:Domain/query", {"accountId": account_id}, "q0"),
                (
                    "x:Domain/get",
                    {
                        "accountId": account_id,
                        "#ids": {"resultOf": "q0", "name": "x:Domain/query", "path": "/ids"},
                        "properties": ["id", "name"],
                    },
                    "g0",
                ),
            ]
        )
        for item in out["g0"].get("list") or []:
            if item.get("name") == name:
                return item.get("id")
        return None

    async def _find_account_id(self, email: str) -> str | None:
        """Resolve an email address to its account's internal id via
        `x:Account/query`+`get`, matching on the server-computed
        `emailAddress` property -- same pattern as `_find_domain_id` above,
        and the one SPK-5 already validated live for `create_account`'s own
        idempotency check (Task 2's `stalwart-init.sh`).
        """
        account_id = await self._admin_account_id()
        out = await self._call(
            [
                ("x:Account/query", {"accountId": account_id}, "q0"),
                (
                    "x:Account/get",
                    {
                        "accountId": account_id,
                        "#ids": {"resultOf": "q0", "name": "x:Account/query", "path": "/ids"},
                        "properties": ["id", "emailAddress"],
                    },
                    "g0",
                ),
            ]
        )
        for item in out["g0"].get("list") or []:
            if item.get("emailAddress") == email:
                return item.get("id")
        return None

    async def create_domain(self, name: str) -> None:
        """Create a mail domain via `x:Domain/set create` (SPK-5 close-out).

        Idempotent: if `name` already exists, this returns normally instead
        of raising (see this module's docstring for why the duplicate is
        *tolerated* -- caught from the create response -- rather than
        avoided with a query-first check).
        """
        account_id = await self._admin_account_id()
        out = await self._call(
            [("x:Domain/set", {"accountId": account_id, "create": {"d0": {"name": name}}}, "c0")]
        )
        result = out["c0"]
        if "d0" in (result.get("created") or {}):
            return
        error = (result.get("notCreated") or {}).get("d0") or {}
        if error.get("type") == "primaryKeyViolation":
            logger.info("create_domain(%r): domain already exists, treating as success", name)
            return
        raise JmapError(f"x:Domain/set create failed for domain {name!r}: {error!r}")

    async def create_account(self, email: str, display_name: str, password: str) -> bool:
        """Create a user account via `x:Account/set create` (SPK-5).

        Encodes the two non-obvious rules Task 2 found by trial and error
        against the live server: the `"@type": "User"` discriminator
        (`x:Account` is a tagged union, unlike `x:Domain`), and `credentials`
        as an index-STRING-keyed map (`{"0": {...}}`) -- a plain JSON array
        or an arbitrarily-keyed map both fail live with `invalidPatch`.

        `display_name` has no dedicated field on `x:UserAccount` (its `name`
        property is documented as "typically an email address local part" --
        i.e. the login name, not a human label) -- this is stored in the
        account's own `description` field, the only free-text property the
        schema offers, a judgment call since neither the brief nor SPK-5
        pinned this down.

        Idempotent the same way `create_domain` is: a duplicate `email`
        comes back as `notCreated`/`primaryKeyViolation` (`properties:
        ["email"]` this time, not `["name"]` -- account uniqueness is
        enforced on the computed `name@domain` address) and is treated as
        success, not raised.

        Returns `True` if `email` was freshly created just now (`password`
        was applied), `False` if it already existed (idempotent no-op --
        `password`/`display_name` were NOT applied; the pre-existing
        account is left exactly as it was). Callers that need to tell a
        caller of their own whether the password they're holding is
        actually the account's live password -- `mailosh/cli.py`'s
        `setup` command, deciding whether to report a freshly generated
        password as real -- must check this return value; treating this
        method as a bare "did it raise" check silently loses that
        distinction.

        Raises `JmapError` if `email`'s domain hasn't been created yet
        (`create_domain` must run first -- this is also the CLI's own call
        order in `mailosh/cli.py`).
        """
        local_part, sep, domain_name = email.partition("@")
        if not sep:
            raise JmapError(f"create_account: {email!r} is not a valid email address")
        domain_id = await self._find_domain_id(domain_name)
        if domain_id is None:
            raise JmapError(
                f"create_account: domain {domain_name!r} does not exist (call create_domain first)"
            )
        account_id = await self._admin_account_id()
        out = await self._call(
            [
                (
                    "x:Account/set",
                    {
                        "accountId": account_id,
                        "create": {
                            "a0": {
                                "@type": "User",
                                "name": local_part,
                                "domainId": domain_id,
                                "description": display_name,
                                "credentials": {"0": {"@type": "Password", "secret": password}},
                            }
                        },
                    },
                    "c0",
                )
            ]
        )
        result = out["c0"]
        if "a0" in (result.get("created") or {}):
            return True
        error = (result.get("notCreated") or {}).get("a0") or {}
        if error.get("type") == "primaryKeyViolation":
            logger.info("create_account(%r): account already exists, treating as success", email)
            return False
        raise JmapError(f"x:Account/set create failed for account {email!r}: {error!r}")

    async def get_dkim_record(self, domain: str) -> DkimRecord:
        """Fetch the DKIM TXT record to publish for `domain`, via
        `x:DkimSignature/query`+`get` (SPK-5).

        Reconstructs the TXT value from the structured `selector`/`@type`/
        `publicKey` fields (`v=DKIM1; k=...; h=sha256; p=<publicKey>`)
        rather than parsing it out of `x:Domain/get`'s `dnsZoneFile` blob --
        SPK-5 left this choice open; reconstructing was picked because the
        zone file interleaves DKIM lines with SPF/MX/DMARC/SRV/CNAME/MTA-STS
        records for the *whole* domain and DNS-wraps a long RSA key's `p=`
        value across multiple quoted strings that would need rejoining,
        where the structured fields give the same information already
        parsed. See this module's `_DKIM_ALGO_K`/`_DKIM_PREFERENCE` for the
        `k=`/algorithm-choice details.

        Polls (see `_DKIM_POLL_ATTEMPTS`/`_DKIM_POLL_DELAY_SECONDS`) until
        the preferred algorithm appears, since a domain's DKIM keys are
        generated asynchronously after `x:Domain/set create` returns rather
        than as part of it (found live -- see those constants' own
        docstring) -- so this can legitimately be called immediately after
        `create_domain` (as `mailosh/cli.py`'s `setup` command does) and
        still get the intended RSA record, not a race-dependent one.

        Two different outcomes if the preferred algorithm (`_DKIM_PREFERENCE[0]`,
        RSA) never shows up within the poll budget, depending on whether
        `by_algo` -- what actually did turn up -- is empty or not:

        - **Nothing at all turned up**: raises `JmapError`. Either `domain`
          doesn't exist, or it exists but has no DKIM keys generated at all
          within the poll budget (e.g. `dkimManagement` set to Manual, or a
          slower-than-expected key-generation delay past the ~3s budget).
        - **Some other algorithm turned up, just not the preferred one**:
          does NOT raise -- a working non-preferred (Ed25519) record is
          still a usable DKIM record, and a wizard that hard-fails here
          over an algorithm preference would be worse than one that
          degrades gracefully. Returns that record instead, but calls
          `logger.warning` first, naming the algorithm that never appeared,
          the algorithm actually being returned, and the poll budget spent
          waiting -- so this is at least visible/loggable, not a silent
          substitution a caller has no way to notice.
        """
        domain_id = await self._find_domain_id(domain)
        if domain_id is None:
            raise JmapError(f"get_dkim_record: domain {domain!r} does not exist")
        account_id = await self._admin_account_id()
        by_algo: dict[str, dict] = {}
        for attempt in range(_DKIM_POLL_ATTEMPTS):
            out = await self._call(
                [
                    ("x:DkimSignature/query", {"accountId": account_id}, "q0"),
                    (
                        "x:DkimSignature/get",
                        {
                            "accountId": account_id,
                            "#ids": {
                                "resultOf": "q0",
                                "name": "x:DkimSignature/query",
                                "path": "/ids",
                            },
                            "properties": ["selector", "domainId", "publicKey", "@type"],
                        },
                        "g0",
                    ),
                ]
            )
            by_algo = {
                item["@type"]: item
                for item in (out["g0"].get("list") or [])
                if item.get("domainId") == domain_id and item.get("@type") in _DKIM_ALGO_K
            }
            if _DKIM_PREFERENCE[0] in by_algo:
                break
            if attempt < _DKIM_POLL_ATTEMPTS - 1:
                await asyncio.sleep(_DKIM_POLL_DELAY_SECONDS)
        if not by_algo:
            raise JmapError(
                f"get_dkim_record: no DKIM signature found for domain {domain!r} "
                f"after {_DKIM_POLL_ATTEMPTS} attempts "
                f"(~{_DKIM_POLL_ATTEMPTS * _DKIM_POLL_DELAY_SECONDS:.1f}s)"
            )
        for algo in _DKIM_PREFERENCE:
            if algo in by_algo:
                chosen = by_algo[algo]
                break
        else:
            chosen = next(iter(by_algo.values()))
        if chosen["@type"] != _DKIM_PREFERENCE[0]:
            logger.warning(
                "get_dkim_record(%r): preferred algorithm %s never appeared after %d attempts "
                "(~%.1fs) -- returning %s instead",
                domain,
                _DKIM_PREFERENCE[0],
                _DKIM_POLL_ATTEMPTS,
                _DKIM_POLL_ATTEMPTS * _DKIM_POLL_DELAY_SECONDS,
                chosen["@type"],
            )
        selector = chosen["selector"]
        k = _DKIM_ALGO_K[chosen["@type"]]
        return DkimRecord(
            host=f"{selector}._domainkey.{domain}",
            value=f"v=DKIM1; k={k}; h=sha256; p={chosen['publicKey']}",
        )

    async def create_api_key(self, username: str, name: str) -> ApiKey:
        """Mint a per-user Stalwart `x:ApiKey` for `username`, scoped to
        their own JMAP account (Task 4, SPK-3), and return its id + secret.

        The finding this implements (full narrative in
        `docs/spikes/p0-findings.md` under SPK-3): `x:ApiKey` is
        one of `x:Account.credentials`'s tagged-union variants (alongside
        `Password`, which `create_account` uses, and `AppPassword`) -- but
        attempting to add one through `x:Account/set update`'s
        `credentials` property directly is rejected outright ("Secondary
        credentials cannot be set directly"). The real mechanism, found by
        probing `x:ApiKey` as its own independently-settable top-level
        object (it has its own `permissionPrefix` in the schema, like
        `x:Domain`/`x:Account` do): `x:ApiKey/set create`, called with
        `accountId` set to the TARGET user's own JMAP account id (resolved
        via `_find_account_id` -- every other call in this module uses the
        *admin's* account id instead), returns a server-generated `secret`
        directly in the `created` response. Confirmed live to work as
        `Authorization: Bearer <secret>` against that exact user's own JMAP
        session (not the admin's), via `JmapClient.connect_bearer` -- a
        genuine, mintable, non-interactive per-user credential. The create
        payload is exactly `{"description": ...}` -- copied verbatim from
        SPK-3's own live-captured request, no other fields.

        This directly answers design spec §9's credential-design question:
        **the preferred path (mint a per-user token) is viable** -- no
        session-vault fallback is required for P1A. (`x:AppPassword/set
        create` was also probed and works identically, but pairs with Basic
        auth + the account's email as username rather than a Bearer token;
        `x:ApiKey` was chosen here as the closer semantic match for "token".)

        Not the same as the JMAP session's OAuth surface: Stalwart also
        exposes a real, working OAuth 2.0 authorization server (found via
        `GET /.well-known/oauth-authorization-server`, backed by
        `x:OAuthClient`), but every grant type it advertises
        (`authorization_code`, `refresh_token`,
        `urn:ietf:params:oauth:grant-type:device_code`) requires an
        interactive human step (a browser redirect or a device-code entry
        page) -- structurally unable to silently mint a token for an
        arbitrary `username` the way this method's contract needs, and so
        was ruled out rather than used here.

        `name` (controller decision #3) is a short, caller-chosen base
        (e.g. `"mailosh-session"`) -- this method appends `-<8 hex>`
        itself (`secrets.token_hex(_API_KEY_SUFFIX_BYTES)`) to build the
        `description` Stalwart actually stores, so every call for the same
        purpose is still unique-ish and identifiable in an admin listing,
        without every caller re-deriving that suffix by hand.

        Unlike `try_mint_user_token` (this method's own former self, before
        Task 4's refactor -- see that method's docstring), this is the
        **raising** primitive: raises `JmapError` if `username` has no
        matching account, if the mint call itself is rejected by the
        server (`notCreated`), or if a `created` response is missing an
        `id`/`secret` (phase0 final-review FIX 5 established that a bare
        `created["secret"]` KeyError is the wrong failure mode here -- this
        raises a clear `JmapError` instead, never an opaque `KeyError`). A
        minted secret is shown exactly once, here, in this call's own
        response -- like `x:DkimSignature`'s `privateKey.secret` (SPK-5),
        `x:ApiKey`'s `secret` field is server-set and redacted on any later
        read, so there is no way to recover a lost secret short of minting
        a new key.
        """
        target_account_id = await self._find_account_id(username)
        if target_account_id is None:
            raise JmapError(f"create_api_key: no account with email {username!r}")
        description = f"{name}-{secrets.token_hex(_API_KEY_SUFFIX_BYTES)}"
        out = await self._call(
            [
                (
                    "x:ApiKey/set",
                    {
                        "accountId": target_account_id,
                        "create": {"k0": {"description": description}},
                    },
                    "c0",
                )
            ]
        )
        result = out["c0"]
        created = (result.get("created") or {}).get("k0")
        if created is None:
            error = (result.get("notCreated") or {}).get("k0")
            raise JmapError(f"x:ApiKey/set create failed for {username!r}: {error!r}")
        key_id = created.get("id")
        secret = created.get("secret")
        if key_id is None or secret is None:
            # Report which field(s) are missing by name only -- never the
            # `created` dict itself, which (in the id-missing/secret-present
            # branch of this otherwise-defensive check) could otherwise put
            # a real secret in an exception message/log line.
            missing = "/".join(n for n, v in (("id", key_id), ("secret", secret)) if v is None)
            raise JmapError(f"x:ApiKey/set create for {username!r} returned no {missing}")
        return ApiKey(id=key_id, secret=secret)

    async def destroy_api_key(self, username: str, key_id: str) -> None:
        """Destroy a Stalwart `x:ApiKey` by id via `x:ApiKey/set destroy`
        (Task 4), immediately revoking that key's Bearer credential.

        Takes `username` as well as `key_id` -- a deliberate departure from
        this task's plan-time Interfaces block (`destroy_api_key(self,
        key_id: str) -> None`), corrected after live testing disproved the
        assumption that signature was written under. SPK-3 records that api
        keys are destroyable ("used to clean up every probe credential
        minted during this investigation") but never pinned down which
        `accountId` a destroy call needs. This method's first version
        guessed the ADMIN's own account id, matching every other
        admin-scoped call in this module and matching how `x:Domain`/
        `x:Account` behave (global directories, fully visible/manageable
        via the admin's own account regardless of which specific object is
        addressed). Run for real against the live server, that guess failed
        outright: `x:ApiKey/set destroy` scoped to the admin's own account
        id returned `{"type": "notFound"}` for a key that unquestionably
        existed (it had just been minted, and its secret was still
        live-authenticating). A direct follow-up probe confirmed why (full
        transcript: `docs/spikes/p1a-findings.md`, "Auth
        exchange"): unlike `x:Domain`/`x:Account`, `x:ApiKey/query`+`get`
        scoped to the admin's own account id ("d333333", the
        bootstrap/recovery principal) doesn't return "no results" -- it
        returns a batch-level `error`
        (`{"type": "forbidden", "description": "Account not found."}`), and
        `set destroy` returns `notFound` the same way: `x:ApiKey` is
        genuinely a **per-account** resource (each account's own credential
        set), not a server-wide directory the admin can browse/manage via
        its own account id the way `x:Domain`/`x:Account` are. The admin
        CAN still act on any account's api keys -- confirmed live,
        including the actual revocation this method exists for (secret
        authenticates with `username: "demo@..."` before, HTTP 401 after)
        -- but only by setting `accountId` to that key's OWNING account id
        explicitly, exactly the shape `create_api_key` already uses for its
        own create call and this method now mirrors for destroy, resolving
        `username` -> account id via `_find_account_id` first.

        Raises `JmapError` if `username` has no matching account, or if
        `key_id` isn't found in the response's `destroyed` list -- whether
        it's reported in `notDestroyed` (an explicit rejection) or not
        mentioned at all (the same "a server that says nothing about the id
        isn't evidence it worked" stance
        `mailosh.jmap.client._check_updated` already takes for
        `Email/set`).
        """
        target_account_id = await self._find_account_id(username)
        if target_account_id is None:
            raise JmapError(f"destroy_api_key: no account with email {username!r}")
        out = await self._call(
            [("x:ApiKey/set", {"accountId": target_account_id, "destroy": [key_id]}, "c0")]
        )
        result = out["c0"]
        if key_id in (result.get("destroyed") or []):
            return
        error = (result.get("notDestroyed") or {}).get(key_id)
        if error is None:
            raise JmapError(f"x:ApiKey/set destroy failed for {key_id!r}: not reported in response")
        raise JmapError(f"x:ApiKey/set destroy failed for {key_id!r}: {error!r}")

    async def try_mint_user_token(self, email: str) -> str | None:
        """SPK-3 probe: mint a per-user API-key credential for `email` and
        return its secret, or `None` if that isn't possible -- a thin,
        never-raising wrapper around `create_api_key` (Task 4 controller
        decision #1: one implementation, not two; see that method's own
        docstring for the full SPK-3 narrative this used to carry directly).

        Kept for the CLI probe (a standalone throwaway script exercising
        this exact method live, per SPK-3's own task record) -- best-effort
        by design, not a required step in account setup:
        `mailosh/cli.py`'s `setup` command never calls this. Every failure
        mode `create_api_key` can raise for (`username` has no matching
        account, the mint call is rejected, the response is missing an
        id/secret) is caught here as a plain `JmapError` (which
        `TransportError` -- a genuine connectivity failure -- is also a
        subclass of, so that degrades the same way) and turned into a
        logged warning plus `None`, preserving this method's original
        "never raises" contract byte for byte.
        """
        try:
            key = await self.create_api_key(email, "mailosh-session")
        except JmapError:
            logger.warning("try_mint_user_token(%r): create_api_key failed", email, exc_info=True)
            return None
        return key.secret
