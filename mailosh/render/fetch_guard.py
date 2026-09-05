"""The SSRF guard in front of the image proxy.

`GET /img?u=…` fetches a URL **the sender of an email chose**, from inside the
server, and hands the bytes back to the reader. That is a server-side request
forgery primitive with a friendly name, and since Mailosh is self-hosted "the
internal network" is somebody's home or company LAN: the cloud metadata
endpoint, the router's admin page on `192.168.1.1`, a Postgres on `localhost`,
an unauthenticated Prometheus. Nothing in this module is defence in depth —
each rule is the only thing standing between a `<img src>` in a newsletter and
one of those.

The order is the whole design:

    check_url        parse and reject before a single packet leaves
    resolve_public   resolve the name, and check *every* address it answers
    stream + verify  connect, check the peer we actually landed on, then read

**Resolve, then check, then look at what you connected to.** Validating a
hostname and handing the *URL* to httpx would re-resolve it, and a DNS record
with a one-second TTL answering `93.184.216.34` for the check and `127.0.0.1`
for the connection defeats the check entirely — the classic rebind. httpx
gives no hook to pin a connection to an address we already approved, so this
module closes the window from the other end: after the response headers
arrive and *before* a byte of body is read, it reads the peer address off the
live socket and runs `check_ip` on that. Best-effort by design and said so out
loud: `network_stream` is absent under HTTP/2 and behind a forward proxy, so
it **supplements** `resolve_public` rather than replacing it. The residual gap
is a request (never a response body) reaching an internal host in the window
between our resolution and httpx's.

Everything else follows from "the URL is hostile":

- Every private, loopback, link-local, reserved, multicast, unspecified and
  non-global range, in both families, plus the IPv4 address hiding inside an
  IPv4-mapped IPv6 one — `::ffff:127.0.0.1` is *none* of those as an IPv6
  address, so `check_ip` recurses into `.ipv4_mapped`.
- Redirects are followed by hand, at most `MAX_REDIRECTS`, and every hop
  re-runs the full check from scratch. A public URL that 302s to
  `169.254.169.254` is the bypass this exists for.
- Hard caps on bytes and on wall-clock, the second because httpx's timeouts
  are per-operation: a server dribbling one byte every four seconds never
  trips a read timeout and holds the socket forever.
- A fresh client per request with `trust_env=False`: no cookie jar, no
  connection reuse across users, no `Referer`, no `Authorization` (`.netrc`
  can inject one), and no proxy inherited from the environment.
- `Content-Type` is checked before a byte is read and the body is never
  sniffed, because `image/svg+xml` is a script-execution vector and the
  browser must never be offered one from this origin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

__all__ = [
    "ALLOWED_IMAGE_TYPES",
    "ALLOWED_PORTS",
    "CONNECT_TIMEOUT",
    "MAX_IMAGE_BYTES",
    "MAX_REDIRECTS",
    "TOTAL_TIMEOUT",
    "BlockedUrl",
    "check_ip",
    "check_url",
    "fetch_image",
    "resolve_public",
]


class BlockedUrl(ValueError):
    """This URL, hop or address must not be fetched.

    A `ValueError` so a route can catch it alongside the token's own rejection
    and answer with a status code and an empty body; a distinct type so the
    *reason* stays greppable in a log the operator reads.
    """


#: 5 MB. Above any legitimate mail image by a wide margin, and low enough that
#: a mail full of them cannot exhaust the process.
MAX_IMAGE_BYTES: int = 5 * 1024 * 1024

#: Redirects followed, not requests made: `MAX_REDIRECTS + 1` responses.
MAX_REDIRECTS: int = 3

CONNECT_TIMEOUT: float = 5.0

#: Wall-clock ceiling on one `fetch_image`, redirects included — enforced by
#: this module with `asyncio.timeout`, not by httpx, whose `timeout` is
#: per-operation and so never fires on a slow-drip body (see the docstring).
TOTAL_TIMEOUT: float = 10.0

#: The only content types this proxy will hand a browser. `image/svg+xml` is
#: absent deliberately: SVG is a document that can run script, and served from
#: the app's own origin it would be an XSS, not a picture.
ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/gif", "image/jpeg", "image/webp", "image/bmp", "image/x-icon"}
)

#: Ports an image may live on. Everything else — 22, 25, 6379, 11211, 5432 —
#: is a service the proxy could otherwise be aimed at as a crude port scanner
#: or a request smuggler.
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443, 8080, 8443})

#: Ranges the `ipaddress` properties below do not flag on their own. Checked
#: empirically against this interpreter, not assumed:
#: `fec0::/10` (deprecated site-local) reports `is_global` **True** and no
#: other flag at all, so without this line an old site-local address would
#: sail straight through.
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("fec0::/10"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)

#: Hosts that name the machine itself or a private namespace without ever
#: parsing as an IP. `resolve_public` would catch these anyway when they
#: resolve to a private address; refusing them by name means no lookup is made
#: at all, and means `http://localhost/x` is refused identically on a host
#: whose resolver answers something exotic for it.
_BLOCKED_HOST_SUFFIXES = ("localhost", "local", "internal", "localdomain", "home.arpa")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: A URL far longer than this is not an image, and is a parser-differential
#: hunt in progress.
_MAX_URL_CHARS = 4096


def check_ip(raw: str) -> None:
    """Raise `BlockedUrl` unless `raw` is a public, routable internet address.

    Blocks on any of `is_private`, `is_loopback`, `is_link_local`,
    `is_reserved`, `is_multicast`, `is_unspecified`, on `not is_global`, and on
    `_EXTRA_BLOCKED_NETWORKS` — the last two because the six named properties
    are *not* between them a partition. Verified against this interpreter, not
    assumed: `100.64.0.1` (CGNAT) and `100.100.100.200` (Alibaba's metadata
    endpoint) set **no** property at all and are caught only by
    `not is_global`; `224.0.0.1` reports `is_global` **True** and is caught
    only by `is_multicast`; `fec0::/10` sets nothing *and* reports
    `is_global` True, so only the extra networks catch it.

    The rules deliberately overlap, and the overlap is not symmetric. Those
    three are each the only thing standing between some address and a socket
    (pinned by `test_each_of_these_addresses_has_exactly_one_rule_standing_
    between_it_and_a_socket`); the six named properties, by contrast, are
    individually redundant here, because `not is_global` already catches
    everything they do. They stay anyway: `is_global`'s definition has moved
    across CPython releases more than once, and a rule that is redundant on
    3.14 is not a rule that is redundant on the 3.12 `requires-python`
    admits.

    An IPv6 address carrying an IPv4 one recurses, for the same reason.
    `::ffff:127.0.0.1` opens a loopback socket on every interpreter, but only
    from 3.13 (backported to 3.12.2) does `IPv6Address` delegate its
    properties to `.ipv4_mapped` and say so — before that the recursion here
    is the only thing that catches it, which
    `test_an_ipv4_mapped_address_is_judged_by_the_ipv4_address_inside_it`
    proves by switching the delegation off.

    Anything that is not a bare address literal — a name, an empty string, an
    address with a `%eth0` zone id — is refused rather than guessed at,
    because this function is only ever asked about something that must
    already be an address.
    """
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise BlockedUrl(f"not an IP address: {raw!r}") from exc

    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        check_ip(str(mapped))

    blocked = (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
        or any(ip in net for net in _EXTRA_BLOCKED_NETWORKS if net.version == ip.version)
    )
    if blocked:
        raise BlockedUrl(f"not a public address: {raw}")


def _check_host(host: str) -> None:
    """Reject a hostname that names the local machine, is not plain ASCII, or
    is a numeric address in disguise.

    The numeric rule is the interesting one. `ipaddress` refuses `2130706433`,
    `0x7f.0.0.1` and `127.1` — but glibc's `getaddrinfo` happily turns all
    three into `127.0.0.1`, so a host that *looks* numeric and does not parse
    as an address is a deliberate attempt to be resolved by a laxer parser
    than the one checking it. `resolve_public` would catch it on the answer;
    this catches it before the lookup.

    Non-ASCII is refused outright rather than IDNA-encoded here: encoding it
    ourselves and letting httpx encode the original independently is exactly
    the parser differential this module exists to avoid, and an IDN host on a
    remote mail image is not a case worth that risk.
    """
    if not host.isascii():
        raise BlockedUrl(f"non-ASCII host: {host!r}")
    # One trailing dot is a legal absolute name and resolves identically, so it
    # must not be a way to spell "localhost" past the suffix list below.
    name = host.rstrip(".")
    if not name:
        raise BlockedUrl("empty host")
    labels = name.split(".")
    if any(not label for label in labels):
        raise BlockedUrl(f"malformed host: {host!r}")
    for suffix in _BLOCKED_HOST_SUFFIXES:
        if name == suffix or name.endswith("." + suffix):
            raise BlockedUrl(f"local host name: {host!r}")
    if all(char in "0123456789." for char in name) or name.startswith("0x"):
        raise BlockedUrl(f"numeric host that is not an IP address: {host!r}")


def check_url(url: str) -> tuple[str, int]:
    """`(host, port)` for a URL this proxy may attempt, or `BlockedUrl`.

    Refuses, before any socket exists: a scheme outside `{http, https}` (which
    is what stops `file://`, `gopher://` and friends), a URL carrying userinfo
    (`http://cdn.test@127.0.0.1/` reads as a hostname to a human and as
    `127.0.0.1` to every parser), an empty or malformed host, a host that is
    an IP literal failing `check_ip`, and a port outside `ALLOWED_PORTS`.

    Control characters and whitespace are refused up front because `urlsplit`
    *silently deletes* tabs and newlines: `http://cdn.test\\n@127.0.0.1/`
    otherwise risks meaning one thing here and another to httpx.
    """
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_CHARS:
        raise BlockedUrl("missing or oversized URL")
    if any(char <= " " or char == "\x7f" for char in url):
        raise BlockedUrl("URL contains whitespace or control characters")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BlockedUrl(f"unparseable URL: {url!r}") from exc

    if parts.scheme not in ("http", "https"):
        raise BlockedUrl(f"scheme not allowed: {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise BlockedUrl("URL carries userinfo")

    host = parts.hostname
    if not host:
        raise BlockedUrl("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise BlockedUrl("URL has an invalid port") from exc
    port = port if port is not None else (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedUrl(f"port not allowed: {port}")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        _check_host(host)
    else:
        check_ip(host)
    return host, port


async def _getaddrinfo(host: str, port: int) -> list:
    """Indirection so tests can replace resolution by patching one name, and
    so the guard has exactly one place where a name becomes an address.
    """
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def resolve_public(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, having checked that **all** of them
    are public — or `BlockedUrl`.

    All, not any. A split-horizon record answering one public address and one
    loopback address is refused outright: httpx picks which answer to connect
    to, so a single private answer in the set is a private connection this
    module cannot rule out. Resolution failing at all is also a `BlockedUrl` —
    there is no fetch to attempt, and the caller has exactly one way to fail.
    """
    try:
        infos = await _getaddrinfo(host, port)
    except (OSError, UnicodeError) as exc:
        raise BlockedUrl(f"could not resolve {host!r}") from exc

    addresses = [str(info[4][0]) for info in infos if info[4]]
    if not addresses:
        raise BlockedUrl(f"{host!r} resolved to nothing")
    for address in addresses:
        check_ip(address)
    return addresses


def _check_peer_address(response: httpx.Response) -> None:
    """Run `check_ip` on the address the live socket is actually connected to.

    The DNS-rebinding backstop, and **best-effort by design**: the
    `network_stream` extension is absent under HTTP/2 and behind a forward
    proxy, and this returns quietly when it is. It supplements
    `resolve_public`; it does not replace it, and `resolve_public` must never
    be dropped on the strength of it.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return
    try:
        info = stream.get_extra_info("server_addr")
    except Exception:
        # A best-effort backstop must never itself be what breaks the fetch.
        return
    if not info:
        return
    peer = info[0] if isinstance(info, (tuple, list)) else info
    if isinstance(peer, str) and peer:
        check_ip(peer)


def _accepted_content_type(response: httpx.Response) -> str:
    """The response's media type, checked against `ALLOWED_IMAGE_TYPES` before
    a byte of body is read — an oversized `text/html` is not worth streaming,
    and an `image/svg+xml` is not worth having in memory.
    """
    raw = response.headers.get("content-type", "")
    media_type = raw.split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_IMAGE_TYPES:
        raise BlockedUrl(f"content type not allowed: {raw!r}")
    return media_type


def _redirect_target(current: str, response: httpx.Response) -> str:
    """The absolute URL of the next hop.

    Resolved against the current URL because `Location` is allowed to be
    relative — and because a relative `Location` that this module failed to
    join would be re-checked as a *different* URL from the one httpx would
    have fetched.
    """
    location = response.headers.get("location")
    if not location:
        raise BlockedUrl("redirect without a Location")
    try:
        return str(httpx.URL(current).join(location))
    except (httpx.InvalidURL, ValueError) as exc:
        raise BlockedUrl(f"unusable redirect target: {location!r}") from exc


async def _read_capped(response: httpx.Response) -> bytes:
    """The body, refusing anything over `MAX_IMAGE_BYTES`.

    Streamed and counted as it arrives, so an upstream promising 1 KB and
    sending 5 GB is dropped after one chunk over the line rather than after
    the process has bought all of it. A declared `Content-Length` over the cap
    short-circuits before the first chunk; it is a hint, never the check.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_IMAGE_BYTES:
                raise BlockedUrl(f"image declares {declared} bytes")
        except ValueError as exc:
            raise BlockedUrl("unparseable Content-Length") from exc

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_IMAGE_BYTES:
            raise BlockedUrl(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    return bytes(body)


async def _fetch(url: str) -> tuple[str, bytes]:
    headers = {
        "User-Agent": "Mailosh",
        "Accept": "image/*",
        # Images are already compressed, so identity costs nothing and removes
        # the decompression-bomb surface: `aiter_bytes` counts *decoded* bytes,
        # and 5 MB of gzip that inflates to 5 GB should never be started.
        "Accept-Encoding": "identity",
    }
    # A fresh client per request: no cookie jar shared between users, no
    # connection reused across them. trust_env=False keeps a proxy out of
    # `HTTPS_PROXY` and, more importantly, an `Authorization` header out of
    # the operator's `.netrc`.
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT),
        headers=headers,
        cookies={},
        trust_env=False,
    ) as client:
        target = url
        for _hop in range(MAX_REDIRECTS + 1):
            host, port = check_url(target)
            await resolve_public(host, port)
            async with client.stream("GET", target) as response:
                _check_peer_address(response)
                if response.status_code in _REDIRECT_STATUSES:
                    target = _redirect_target(target, response)
                    continue
                if response.status_code != 200:
                    raise BlockedUrl(f"upstream returned {response.status_code}")
                media_type = _accepted_content_type(response)
                return media_type, await _read_capped(response)
    raise BlockedUrl(f"more than {MAX_REDIRECTS} redirects")


async def fetch_image(url: str) -> tuple[str, bytes]:
    """`(content_type, body)` for a checked, public, allow-listed image, or
    `BlockedUrl` — the only exception this function raises.

    Every upstream failure collapses into `BlockedUrl` on purpose: the caller
    is a route that answers `502` with an empty body, and the difference
    between "connection refused" and "TLS handshake failed" is a probe result
    the sender of the email must not be able to read back out of the proxy.

    The wall-clock deadline covers the whole call, redirects included. httpx's
    own `timeout` is per-operation, so three hops each answering just inside
    the read timeout are unbounded without it.
    """
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            return await _fetch(url)
    except BlockedUrl:
        raise
    except TimeoutError as exc:
        raise BlockedUrl(f"upstream took longer than {TOTAL_TIMEOUT}s") from exc
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as exc:
        raise BlockedUrl(f"upstream fetch failed: {type(exc).__name__}") from exc
