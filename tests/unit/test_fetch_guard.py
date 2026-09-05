"""Adversarial tests for the image proxy's SSRF guard (Task 6, plan §Task 6).

`mailosh.render.fetch_guard` fetches a URL *the sender of an email chose*,
from inside the server. Every test here is therefore written as the payload
that defeats a naive implementation rather than as a demonstration that the
happy path works: the split-horizon DNS answer, the redirect to the cloud
metadata endpoint, the IPv4 address wearing an IPv6 costume, the decimal
hostname `getaddrinfo` resolves to loopback, the body that lies about its own
length, and the upstream that answers immediately and then dribbles.

DNS is faked module-wide (`_getaddrinfo` is the guard's single resolution
point, patched by name) — every name here resolves to one public address
unless a test says otherwise, so no test in this file can touch a real
resolver or leak a lookup for `cdn.test`. HTTP is faked by `respx_mock`,
respx's own fixture.

The route half of Task 6 (`GET /img`, its session requirement, its 403/502
statuses and its response headers) is **not** here: `mailosh/web/frames.py`
is Task 4's file and does not exist yet. See `tests/unit/test_img_proxy.py`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

import httpx
import pytest

from mailosh.render.fetch_guard import (
    _EXTRA_BLOCKED_NETWORKS,
    ALLOWED_IMAGE_TYPES,
    ALLOWED_PORTS,
    CONNECT_TIMEOUT,
    MAX_IMAGE_BYTES,
    MAX_REDIRECTS,
    TOTAL_TIMEOUT,
    BlockedUrl,
    check_ip,
    check_url,
    fetch_image,
    resolve_public,
)

#: example.com. Any address that is genuinely globally routable will do; this
#: one is used throughout so a failure reads as "the guard rejected a public
#: address" rather than "the fixture picked a weird one".
PUBLIC = "93.184.216.34"

PNG = {"content-type": "image/png"}


def _fake_addrinfo(addresses: list[str]):
    """An async stand-in for `getaddrinfo` answering exactly `addresses`.

    Shaped like the real thing — 5-tuples whose `sockaddr[0]` is the address —
    so a guard that reached into the tuple differently would fail here rather
    than silently pass against a convenient fake.
    """
    infos = []
    for address in addresses:
        if ":" in address:
            infos.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0)))
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)))

    async def fake(host: str, port: int) -> list:
        return infos

    return fake


def _fake_addrinfo_by_host(mapping: dict[str, list[str]]):
    """`_fake_addrinfo` per hostname — for proving that a *redirect hop*
    re-resolves rather than reusing the first hop's verdict.
    """

    async def fake(host: str, port: int) -> list:
        answers = mapping.get(host)
        if answers is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return await _fake_addrinfo(answers)(host, port)

    return fake


class _Peer:
    """A stand-in for httpcore's `network_stream` extension, reporting the
    address the socket is supposedly connected to.
    """

    def __init__(self, address: str | None):
        self._address = address

    def get_extra_info(self, name: str):
        return (self._address, 443) if name == "server_addr" and self._address else None


@pytest.fixture(autouse=True)
def resolves_public(monkeypatch):
    """Every name in this module resolves to one public address by default.

    Autouse so that no test can accidentally perform a real lookup: `cdn.test`
    does not exist, and a guard that skipped `resolve_public` would otherwise
    pass a test that a real resolver would have failed.
    """
    monkeypatch.setattr("mailosh.render.fetch_guard._getaddrinfo", _fake_addrinfo([PUBLIC]))


# --------------------------------------------------------------------------
# check_ip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        # IPv4: loopback, unspecified, RFC1918, link-local, CGNAT, multicast,
        # broadcast.
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "198.18.0.1",
        "240.0.0.1",
        "192.0.2.1",
        # IPv6: loopback, unspecified, link-local, unique-local, and the two
        # ways an IPv4 address hides inside one.
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fec0::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:224.0.0.1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "2001:db8::1",
    ],
)
def test_private_and_reserved_addresses_are_blocked(ip):
    with pytest.raises(BlockedUrl):
        check_ip(ip)


@pytest.mark.parametrize("ip", ["1.1.1.1", "93.184.216.34", "2606:4700::1111"])
def test_public_addresses_pass(ip):
    check_ip(ip)


@pytest.mark.parametrize(
    ("vendor", "ip"),
    [
        ("aws/gcp/azure", "169.254.169.254"),
        ("alibaba", "100.100.100.200"),
        ("oracle", "192.0.0.192"),
        ("gcp over ipv6", "fd00:ec2::254"),
        ("aws mapped into ipv6", "::ffff:169.254.169.254"),
    ],
)
def test_cloud_metadata_endpoints_are_blocked(vendor, ip):
    """The single highest-value SSRF target: one GET returns instance
    credentials. Named separately from the range table above so that a change
    to the ranges can never quietly re-open one of these.
    """
    with pytest.raises(BlockedUrl):
        check_ip(ip)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "localhost",
        "not-an-ip",
        "1.1.1.1 ",
        " 1.1.1.1",
        "fe80::1%en0",
        "1.1.1.1/32",
        "2130706433",
    ],
)
def test_anything_that_is_not_a_bare_address_literal_is_refused(raw):
    """`check_ip` is only ever asked about something that must already be an
    address, so it fails closed rather than guessing — including on a zone id,
    which `ipaddress` cannot parse and a socket happily would.
    """
    with pytest.raises(BlockedUrl):
        check_ip(raw)


def test_an_ipv4_mapped_address_is_judged_by_the_ipv4_address_inside_it(monkeypatch):
    """`::ffff:127.0.0.1` opens a loopback socket whatever `ipaddress` calls
    it — and what `ipaddress` calls it depends on the interpreter. CPython
    only taught `IPv6Address` to delegate its properties to `.ipv4_mapped` in
    3.13 (backported to 3.12.2), and `requires-python` allows 3.12, so on a
    3.12.0 the recursion in `check_ip` is the *only* thing standing between a
    hostile AAAA record and a loopback connection.

    Rather than trust whichever interpreter happens to run the suite, this
    switches the delegation off and checks the recursion on its own. Delete
    the two lines it guards and this test goes red on every Python; without
    it, it would pass on this one and quietly stop being true on an older one.
    """
    for name in (
        "is_private",
        "is_loopback",
        "is_link_local",
        "is_reserved",
        "is_multicast",
        "is_unspecified",
    ):
        monkeypatch.setattr(ipaddress.IPv6Address, name, property(lambda self: False))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda self: True))

    check_ip("::ffff:93.184.216.34")  # the control: a public address still passes
    for hostile in ("::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254"):
        with pytest.raises(BlockedUrl):
            check_ip(hostile)


def _rules_firing(raw: str) -> set[str]:
    ip = ipaddress.ip_address(raw)
    fired = {
        name
        for name in (
            "is_private",
            "is_loopback",
            "is_link_local",
            "is_reserved",
            "is_multicast",
            "is_unspecified",
        )
        if getattr(ip, name)
    }
    if not ip.is_global:
        fired.add("not is_global")
    if any(ip in net for net in _EXTRA_BLOCKED_NETWORKS if net.version == ip.version):
        fired.add("_EXTRA_BLOCKED_NETWORKS")
    return fired


@pytest.mark.parametrize(
    ("ip", "rule"),
    [
        ("100.64.0.1", "not is_global"),
        ("100.100.100.200", "not is_global"),
        ("224.0.0.1", "is_multicast"),
        ("fec0::1", "_EXTRA_BLOCKED_NETWORKS"),
    ],
)
def test_each_of_these_addresses_has_exactly_one_rule_standing_between_it_and_a_socket(ip, rule):
    """`check_ip`'s rules deliberately overlap — but not everywhere, and this
    pins the three places where the overlap runs out. `not is_global` is the
    only thing catching CGNAT and Alibaba's metadata endpoint; `is_multicast`
    is the only thing catching `224.0.0.1`, which reports `is_global` **True**;
    and `_EXTRA_BLOCKED_NETWORKS` is the only thing catching site-local
    `fec0::/10`, which sets no property at all.

    Written against `ipaddress` rather than against `check_ip` on purpose:
    `is_global` has been redefined across CPython releases more than once, so
    when this goes red it means the classification moved underneath the guard
    and somebody has to re-read `check_ip` — which is exactly the moment the
    remaining, currently-redundant rules stop being redundant.
    """
    assert _rules_firing(ip) == {rule}


# --------------------------------------------------------------------------
# check_url
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Schemes that are not a fetch at all.
        "file:///etc/passwd",
        "gopher://x/",
        "ftp://x/",
        "data:image/png;base64,AAAA",
        "javascript:alert(1)",
        # Addresses that name this machine or its network.
        "http://[::1]/x",
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://localhost./x",
        "http://db.localhost/x",
        "http://metadata.internal/x",
        "https://192.168.0.1/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://169.254.169.254/latest/meta-data/",
        # Userinfo: reads as a hostname to a human, as 127.0.0.1 to a parser.
        "http://user:pw@evil.test/x",
        "http://cdn.test@127.0.0.1/x",
        # Ports that are not a web server.
        "http://evil.test:22/x",
        "http://evil.test:25/x",
        "http://evil.test:6379/x",
        "http://evil.test:11211/x",
        "http://evil.test:99999/x",
        # Numeric hosts `ipaddress` refuses and `getaddrinfo` resolves anyway.
        "http://2130706433/x",
        "http://0x7f.0.0.1/x",
        "http://127.1/x",
        # Not a URL, or not one both parsers would read the same way.
        "http://",
        "not-a-url",
        "",
        "http://cdn.test\n@127.0.0.1/x",
        "http://cdn.test\t/x",
        "http://①.com/x",
        "http://cdn..test/x",
    ],
)
def test_hostile_urls_are_refused_before_any_socket(url):
    with pytest.raises(BlockedUrl):
        check_url(url)


def test_ordinary_urls_yield_host_and_default_port():
    assert check_url("https://cdn.test/a.png") == ("cdn.test", 443)
    assert check_url("http://cdn.test/a.png") == ("cdn.test", 80)
    assert check_url("http://cdn.test:8080/a.png") == ("cdn.test", 8080)
    assert check_url("https://cdn.test:8443/a.png") == ("cdn.test", 8443)
    assert check_url("https://CDN.Test/a.png") == ("cdn.test", 443)


def test_only_web_ports_are_reachable():
    assert ALLOWED_PORTS == frozenset({80, 443, 8080, 8443})


def test_an_oversized_url_is_refused_rather_than_parsed():
    with pytest.raises(BlockedUrl):
        check_url("https://cdn.test/" + "a" * 8192)


# --------------------------------------------------------------------------
# resolve_public — the DNS half of the rebinding defence
# --------------------------------------------------------------------------


async def test_a_hostname_resolving_to_any_private_address_is_blocked(monkeypatch):
    """Any, not all. A split-horizon record answering one public address and
    one loopback address is refused: httpx picks which answer to connect to,
    so one private answer is a private connection this guard cannot rule out.
    """
    monkeypatch.setattr(
        "mailosh.render.fetch_guard._getaddrinfo",
        _fake_addrinfo(["93.184.216.34", "127.0.0.1"]),
    )
    with pytest.raises(BlockedUrl):
        await resolve_public("split.test", 443)


async def test_an_aaaa_record_smuggling_an_ipv4_address_is_blocked(monkeypatch):
    monkeypatch.setattr(
        "mailosh.render.fetch_guard._getaddrinfo",
        _fake_addrinfo(["::ffff:169.254.169.254"]),
    )
    with pytest.raises(BlockedUrl):
        await resolve_public("metadata.test", 443)


async def test_a_public_name_resolves_and_reports_its_addresses(monkeypatch):
    monkeypatch.setattr(
        "mailosh.render.fetch_guard._getaddrinfo",
        _fake_addrinfo(["93.184.216.34", "2606:4700::1111"]),
    )
    assert await resolve_public("cdn.test", 443) == ["93.184.216.34", "2606:4700::1111"]


async def test_a_name_that_does_not_resolve_is_blocked_not_raised(monkeypatch):
    async def fail(host: str, port: int):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr("mailosh.render.fetch_guard._getaddrinfo", fail)
    with pytest.raises(BlockedUrl):
        await resolve_public("nope.test", 443)


async def test_a_name_resolving_to_nothing_is_blocked(monkeypatch):
    monkeypatch.setattr("mailosh.render.fetch_guard._getaddrinfo", _fake_addrinfo([]))
    with pytest.raises(BlockedUrl):
        await resolve_public("empty.test", 443)


# --------------------------------------------------------------------------
# fetch_image — redirects
# --------------------------------------------------------------------------


async def test_an_allowed_image_is_returned_with_its_media_type(respx_mock):
    respx_mock.get("https://cdn.test/a.png").respond(200, headers=PNG, content=b"PNGDATA")
    assert await fetch_image("https://cdn.test/a.png") == ("image/png", b"PNGDATA")


async def test_redirect_to_a_private_host_is_blocked_at_the_hop(respx_mock):
    respx_mock.get("https://cdn.test/a.png").respond(
        302, headers={"location": "http://127.0.0.1/x"}
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")


async def test_redirect_to_the_metadata_endpoint_is_blocked(respx_mock):
    """The classic bypass: a URL that passes every check, then 302s to
    169.254.169.254. A guard that validates only the URL it was handed
    fetches instance credentials and hands them back as an image.
    """
    respx_mock.get("https://cdn.test/a.png").respond(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/iam/"}
    )
    metadata = respx_mock.get("http://169.254.169.254/latest/meta-data/iam/").respond(
        200, headers=PNG, content=b"CREDENTIALS"
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")
    assert not metadata.called


async def test_a_redirect_hop_is_re_resolved_not_trusted_from_the_first(respx_mock, monkeypatch):
    """The hop check is a *DNS* check too: a second host that resolves to a
    private address must be refused even though the first one did not.
    """
    monkeypatch.setattr(
        "mailosh.render.fetch_guard._getaddrinfo",
        _fake_addrinfo_by_host({"cdn.test": [PUBLIC], "inside.test": ["10.0.0.5"]}),
    )
    respx_mock.get("https://cdn.test/a.png").respond(
        302, headers={"location": "https://inside.test/a.png"}
    )
    inside = respx_mock.get("https://inside.test/a.png").respond(
        200, headers=PNG, content=b"SECRET"
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")
    assert not inside.called


async def test_a_relative_location_is_resolved_against_the_current_hop(respx_mock):
    respx_mock.get("https://cdn.test/a.png").respond(302, headers={"location": "/b.png"})
    respx_mock.get("https://cdn.test/b.png").respond(200, headers=PNG, content=b"PNGDATA")
    assert await fetch_image("https://cdn.test/a.png") == ("image/png", b"PNGDATA")


async def test_a_relative_location_pointing_at_a_new_scheme_is_still_checked(respx_mock):
    respx_mock.get("https://cdn.test/a.png").respond(302, headers={"location": "//127.0.0.1/b.png"})
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")


async def test_a_redirect_without_a_location_is_blocked(respx_mock):
    respx_mock.get("https://cdn.test/a.png").respond(302)
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")


async def test_redirect_chains_stop_at_the_limit(respx_mock):
    for i in range(6):
        respx_mock.get(f"https://cdn.test/{i}").respond(
            302, headers={"location": f"https://cdn.test/{i + 1}"}
        )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/0")


async def test_a_chain_of_exactly_max_redirects_still_arrives(respx_mock):
    """The other side of the limit — proof that `MAX_REDIRECTS` counts
    redirects and not requests, so tightening the loop by one would break a
    legitimate CDN rather than only an attacker.
    """
    for i in range(MAX_REDIRECTS):
        respx_mock.get(f"https://cdn.test/{i}").respond(
            302, headers={"location": f"https://cdn.test/{i + 1}"}
        )
    respx_mock.get(f"https://cdn.test/{MAX_REDIRECTS}").respond(
        200, headers=PNG, content=b"PNGDATA"
    )
    assert await fetch_image("https://cdn.test/0") == ("image/png", b"PNGDATA")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_redirect_status_is_followed_and_re_checked(respx_mock, status):
    respx_mock.get("https://cdn.test/a.png").respond(
        status, headers={"location": "http://192.168.1.1/admin"}
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")


@pytest.mark.parametrize("status", [204, 206, 304, 401, 403, 404, 500])
async def test_a_non_200_upstream_is_a_blocked_url_not_a_body(respx_mock, status):
    respx_mock.get("https://cdn.test/a.png").respond(status, headers=PNG, content=b"nope")
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")


# --------------------------------------------------------------------------
# fetch_image — the DNS-rebinding backstop
# --------------------------------------------------------------------------


async def test_a_socket_that_landed_on_a_private_peer_is_blocked_before_the_body(respx_mock):
    """DNS said 93.184.216.34 (and `resolve_public` agreed); the socket is on
    127.0.0.1. That is a rebind, and the only place left to catch it is the
    live connection — so the peer address is checked *before* a byte of body
    is read, which `started` proves.
    """
    started = {"n": 0}

    async def body():
        started["n"] += 1
        yield b"SECRET"

    respx_mock.get("https://cdn.test/a.png").mock(
        return_value=httpx.Response(
            200,
            headers=PNG,
            content=body(),
            extensions={"network_stream": _Peer("127.0.0.1")},
        )
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")
    assert started["n"] == 0


async def test_a_rebind_onto_the_metadata_endpoint_is_blocked(respx_mock):
    respx_mock.get("https://cdn.test/a.png").mock(
        return_value=httpx.Response(
            200,
            headers=PNG,
            content=b"CREDENTIALS",
            extensions={"network_stream": _Peer("169.254.169.254")},
        )
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/a.png")


async def test_a_public_peer_address_passes_and_an_absent_one_does_not_block(respx_mock):
    """The backstop is best-effort: `network_stream` is absent under HTTP/2
    and behind a forward proxy, and its absence must not break the fetch — the
    other tests in this file are what keep `resolve_public` load-bearing.
    """
    respx_mock.get("https://cdn.test/a.png").mock(
        return_value=httpx.Response(
            200, headers=PNG, content=b"PNG", extensions={"network_stream": _Peer(PUBLIC)}
        )
    )
    assert await fetch_image("https://cdn.test/a.png") == ("image/png", b"PNG")

    respx_mock.get("https://cdn.test/b.png").mock(
        return_value=httpx.Response(
            200, headers=PNG, content=b"PNG", extensions={"network_stream": _Peer(None)}
        )
    )
    assert await fetch_image("https://cdn.test/b.png") == ("image/png", b"PNG")


# --------------------------------------------------------------------------
# fetch_image — size and time
# --------------------------------------------------------------------------


async def test_oversized_body_is_aborted_not_buffered(respx_mock):
    respx_mock.get("https://cdn.test/big.png").respond(
        200, headers=PNG, content=b"x" * (MAX_IMAGE_BYTES + 4096)
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/big.png")


async def test_an_endless_body_is_dropped_a_chunk_over_the_cap(respx_mock):
    """The one that proves "aborted, not buffered": this upstream would send
    bytes forever, and a guard that read the body and *then* measured it would
    never return at all.
    """
    chunks = {"n": 0}

    async def endless():
        while True:
            chunks["n"] += 1
            yield b"x" * 65536

    respx_mock.get("https://cdn.test/endless.png").mock(
        return_value=httpx.Response(200, headers=PNG, content=endless())
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/endless.png")
    assert chunks["n"] <= MAX_IMAGE_BYTES // 65536 + 1


async def test_a_body_that_lies_about_its_length_is_still_capped(respx_mock):
    """`Content-Length` is a hint, never the check."""
    respx_mock.get("https://cdn.test/liar.png").mock(
        return_value=httpx.Response(
            200,
            headers={**PNG, "content-length": "12"},
            content=b"x" * (MAX_IMAGE_BYTES + 1),
        )
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/liar.png")


async def test_a_declared_length_over_the_cap_is_refused_before_the_body(respx_mock):
    started = {"n": 0}

    async def body():
        started["n"] += 1
        yield b"x"

    respx_mock.get("https://cdn.test/huge.png").mock(
        return_value=httpx.Response(
            200,
            headers={**PNG, "content-length": str(MAX_IMAGE_BYTES + 1)},
            content=body(),
        )
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/huge.png")
    assert started["n"] == 0


async def test_a_body_exactly_at_the_cap_is_allowed(respx_mock):
    respx_mock.get("https://cdn.test/edge.png").respond(
        200, headers=PNG, content=b"x" * MAX_IMAGE_BYTES
    )
    media_type, body = await fetch_image("https://cdn.test/edge.png")
    assert media_type == "image/png" and len(body) == MAX_IMAGE_BYTES


async def test_a_slow_loris_upstream_is_cut_off_by_the_wall_clock(respx_mock, monkeypatch):
    """A server that answers its headers immediately and then dribbles never
    trips httpx's *per-operation* read timeout, so `fetch_image` keeps its own
    wall-clock deadline over the whole call. Without it this test hangs.
    """
    monkeypatch.setattr("mailosh.render.fetch_guard.TOTAL_TIMEOUT", 0.05)

    async def dribble():
        yield b"x"
        await asyncio.sleep(30)
        yield b"x"

    respx_mock.get("https://cdn.test/slow.png").mock(
        return_value=httpx.Response(200, headers=PNG, content=dribble())
    )
    started = time.monotonic()
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/slow.png")
    assert time.monotonic() - started < 5


async def test_a_slow_redirect_chain_cannot_outlast_the_deadline(respx_mock, monkeypatch):
    """The deadline covers the whole call, hops included — three redirects
    each answering just inside a per-request timeout are unbounded otherwise.
    """
    monkeypatch.setattr("mailosh.render.fetch_guard.TOTAL_TIMEOUT", 0.05)

    async def slow_redirect(request):
        await asyncio.sleep(30)
        return httpx.Response(302, headers={"location": "https://cdn.test/1"})

    respx_mock.get("https://cdn.test/0").mock(side_effect=slow_redirect)
    started = time.monotonic()
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/0")
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------
# fetch_image — content type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctype",
    ["text/html", "image/svg+xml", "application/pdf", "", "text/plain", "application/json"],
)
async def test_non_image_content_types_are_refused(respx_mock, ctype):
    respx_mock.get("https://cdn.test/x").respond(
        200, headers={"content-type": ctype}, content=b".."
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/x")


async def test_svg_is_refused_because_it_is_a_document_that_runs_script(respx_mock):
    """Served back from the app's own origin an SVG is an XSS, not a picture —
    so it is absent from `ALLOWED_IMAGE_TYPES` deliberately, not by oversight.
    """
    assert "image/svg+xml" not in ALLOWED_IMAGE_TYPES
    respx_mock.get("https://cdn.test/x.svg").respond(
        200,
        headers={"content-type": "image/svg+xml"},
        content=b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/x.svg")


async def test_a_missing_content_type_is_never_sniffed(respx_mock):
    """A PNG magic number in the body does not make it a PNG: the header is
    the only thing consulted, so nothing can be typed by what it looks like.
    """
    respx_mock.get("https://cdn.test/x").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={})
    )
    with pytest.raises(BlockedUrl):
        await fetch_image("https://cdn.test/x")


@pytest.mark.parametrize("media_type", sorted(ALLOWED_IMAGE_TYPES))
async def test_every_allowed_type_is_returned_normalised(respx_mock, media_type):
    respx_mock.get("https://cdn.test/a").respond(
        200, headers={"content-type": f"{media_type.upper()}; charset=binary"}, content=b"B"
    )
    assert await fetch_image("https://cdn.test/a") == (media_type, b"B")


# --------------------------------------------------------------------------
# fetch_image — what is (never) sent upstream
# --------------------------------------------------------------------------


async def test_no_cookie_or_referer_is_ever_sent(respx_mock):
    route = respx_mock.get("https://cdn.test/a.png").respond(200, headers=PNG, content=b"PNG")
    await fetch_image("https://cdn.test/a.png")
    sent = route.calls.last.request.headers
    assert "cookie" not in sent and "referer" not in sent and "authorization" not in sent
    assert sent["user-agent"] == "Mailosh"
    assert sent["accept"] == "image/*"


async def test_a_redirect_hop_leaks_no_referer_either(respx_mock):
    """httpx would attach a `Referer` if it followed the redirect itself; this
    guard builds every hop by hand, so it never does.
    """
    respx_mock.get("https://cdn.test/a.png").respond(302, headers={"location": "/b.png"})
    hop = respx_mock.get("https://cdn.test/b.png").respond(200, headers=PNG, content=b"PNG")
    await fetch_image("https://cdn.test/a.png")
    sent = hop.calls.last.request.headers
    assert "referer" not in sent and "cookie" not in sent and "authorization" not in sent


async def test_an_upstream_set_cookie_never_becomes_a_cookie_jar(respx_mock):
    """A fresh client per request is what makes this true: one message's
    upstream cannot set a cookie that the next fetch — possibly for another
    user — carries back out.
    """
    respx_mock.get("https://cdn.test/a.png").respond(
        200, headers={**PNG, "set-cookie": "sess=abc; Path=/"}, content=b"PNG"
    )
    second = respx_mock.get("https://cdn.test/b.png").respond(200, headers=PNG, content=b"PNG")
    await fetch_image("https://cdn.test/a.png")
    await fetch_image("https://cdn.test/b.png")
    assert "cookie" not in second.calls.last.request.headers


async def test_the_client_is_fresh_isolated_and_ignores_the_environment(respx_mock, monkeypatch):
    """`trust_env=False` is not tidiness: with the default, httpx reads the
    operator's `.netrc` and can attach an `Authorization` header to a URL the
    sender of an email chose, and honours `HTTPS_PROXY` from the environment.
    """
    seen: list[dict] = []
    real = httpx.AsyncClient

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    respx_mock.get("https://cdn.test/a.png").respond(200, headers=PNG, content=b"PNG")
    await fetch_image("https://cdn.test/a.png")
    await fetch_image("https://cdn.test/a.png")

    assert len(seen) == 2, "a client must not be shared between two fetches"
    for kwargs in seen:
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        assert kwargs["cookies"] == {}
        assert kwargs["timeout"].connect == CONNECT_TIMEOUT
        assert kwargs["timeout"].read == TOTAL_TIMEOUT
        assert set(kwargs["headers"]) == {"User-Agent", "Accept", "Accept-Encoding"}


async def test_an_upstream_failure_is_a_blocked_url_and_says_nothing_useful(respx_mock):
    """Every upstream failure collapses into one exception type: the
    difference between "connection refused" and "TLS handshake failed" is a
    probe result the sender of the email must not read back out of the proxy.
    """
    respx_mock.get("https://cdn.test/a.png").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BlockedUrl) as caught:
        await fetch_image("https://cdn.test/a.png")
    assert "refused" not in str(caught.value)


async def test_the_guard_refuses_a_hostile_url_without_opening_a_connection(respx_mock):
    route = respx_mock.get("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(BlockedUrl):
        await fetch_image("http://169.254.169.254/latest/meta-data/")
    assert not route.called


# --------------------------------------------------------------------------
# The constants later tasks and the route are written against
# --------------------------------------------------------------------------


def test_the_published_limits_are_the_ones_the_plan_names():
    assert MAX_IMAGE_BYTES == 5 * 1024 * 1024
    assert MAX_REDIRECTS == 3
    assert CONNECT_TIMEOUT == 5.0
    assert TOTAL_TIMEOUT == 10.0
    assert ALLOWED_IMAGE_TYPES == frozenset(
        {"image/png", "image/gif", "image/jpeg", "image/webp", "image/bmp", "image/x-icon"}
    )


def test_blocked_url_is_a_value_error_so_one_except_clause_covers_the_route():
    assert issubclass(BlockedUrl, ValueError)
