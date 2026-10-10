"""The SSRF boundary — the authoritative rule for "may we connect to this?".

**This module is the single place in the repository where an address is judged
connectable.** If a reviewer has one question, this is the file to open:

```text
    validate_url()          → is this URL even well-formed and permitted?
    resolve_and_validate()  → do ALL of this hostname's addresses pass policy?
    connect target          → the single validated IP, never the hostname
```

Everything else — the transport, the redirect loop, the body reader — is written
in terms of these functions and makes no address decision of its own. There is
no second validator and no escape hatch: the primitive that actually opens a
socket is private to this package's transport, and the only public entry point
for fetching is :class:`app.domain.research.ports.SafeFetcher`.

Threat model
------------

A URL arriving here came from a search engine, which means a hostile party
could have chosen it. It is treated as attacker-controlled input. The attacks
this boundary must defeat, and how:

**Direct internal targeting.** ``http://127.0.0.1/``, ``http://[::1]/``,
``http://10.0.0.5/``, ``http://169.254.169.254/latest/meta-data/``. Defeated by
:class:`AddressPolicy`, which rejects loopback, private, link-local, carrier-grade
NAT, multicast, reserved, and documentation ranges in both address families.

**Alternate encodings of the same target.** ``http://2130706433/``,
``http://0x7f000001/``, ``http://127.1/``, ``http://0177.0.0.1/`` all mean
loopback to a C library but are not canonical dotted-quads, so
:func:`ambiguous_numeric_host` rejects them before resolution rather than
trusting the platform's interpretation. Canonical IPv6 forms that embed an IPv4
address (``::ffff:127.0.0.1``, ``2002:7f00:1::``, ``64:ff9b::7f00:1``,
Teredo) are unwrapped and the **embedded IPv4 address is re-validated** against
the IPv4 rules, as the frozen contract requires (§ 15.5).

**DNS rebinding (TOCTOU).** The window between validating a hostname and
connecting to it is the classic SSRF bypass: validate ``evil.test`` → it
resolves to ``93.184.216.34``; the attacker flips the record; the client
resolves again and gets ``127.0.0.1``. This boundary closes the window *by
construction rather than by timing*: it resolves the name once, validates every
answer, and then hands the transport a **literal IP address** to connect to.
The hostname is used for the ``Host`` header and TLS SNI only — never as
something the resolver sees again. There is no second resolution, so there is no
window. See :mod:`app.infrastructure.fetch.transport` and the tests named
``test_dns_rebinding_*``.

**Fail-open on error.** A resolver failure is a typed rejection, never an
implicit "no address found, so allow". The same holds when a name resolves to
zero addresses.

**One good answer hiding a bad one.** A hostname with both a public and a
private address is rejected outright. Choosing the "safe" one and connecting to
it would make the outcome depend on an attacker-influenced ordering decision,
and would mean the blocklist was advisory.

Multi-answer handling
---------------------

``resolve_and_validate`` validates **every** address the resolver returned and
raises on the first violation. It then returns the *first* address, which is
deterministic given the resolver's ordering. Multiple public addresses for one
name are normal (IPv4/IPv6 dual stack, CDNs); attempting them in order and
failing only if all fail is the transport's job, and each candidate is
individually validated before use.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from app.domain.errors import (
    FetchBlockedDestinationError,
    FetchDNSResolutionError,
    FetchInvalidURLError,
    FetchUnsupportedSchemeError,
)

#: Schemes this fetcher may ever open. Frozen at P3.0 (§ 15.5): HTTP and HTTPS
#: only. ``file:``, ``ftp:``, ``gopher:``, ``data:``, ``javascript:``, and
#: ``mailto:`` are not "unsupported because nobody thought of them" — each is a
#: way to reach a local resource or smuggle content past the extraction stage,
#: so each is rejected by name in the tests.
DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

#: A DNS label: letters, digits, hyphen; not starting or ending with a hyphen.
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: Characters that must never appear anywhere in a URL. Space, tab, CR, LF, NUL
#: and DEL. ``urlsplit`` strips a leading/trailing run of ASCII whitespace and
#: tabs/newlines *anywhere* (WHATWG behaviour), which is a request-smuggling
#: primitive: ``http://good.test/\r\nX-Injected: 1`` becomes a different URL
#: than the caller wrote. Rejecting them up front means the string validated is
#: byte-for-byte the string requested.
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")

#: A host consisting only of characters that appear in hexadecimal/octal/decimal
#: address notations, and containing at least one digit. ``0x7f000001``,
#: ``2130706433``, ``127.1``, and ``0177.0.0.1`` all match. A legitimate hostname
#: does not: ``deadbeef.com`` and ``face`` do not (no digits / letters outside
#: ``[0-9a-f]``).
_AMBIGUOUS_NUMERIC_HOST = re.compile(r"^[0-9a-fA-FxX.]+$")

# --------------------------------------------------------------------------
# Address policy
# --------------------------------------------------------------------------

#: IPv4 networks rejected outright, with the reason recorded for the log. The
#: list is explicit rather than delegated to ``is_global`` because
#: ``is_global`` is **wrong** in both directions for our purpose: it reports
#: ``224.0.0.1`` as global (multicast is routable!), and it reports
#: ``192.88.99.1`` as global (a deprecated anycast relay). A blocklist we can
#: read line by line is also a blocklist a reviewer can audit against the RFCs.
BLOCKED_IPV4_NETWORKS: tuple[tuple[ipaddress.IPv4Network, str], ...] = (
    (ipaddress.IPv4Network("0.0.0.0/8"), "this-network / unspecified"),
    (ipaddress.IPv4Network("10.0.0.0/8"), "RFC1918 private"),
    (ipaddress.IPv4Network("100.64.0.0/10"), "RFC6598 carrier-grade NAT"),
    (ipaddress.IPv4Network("127.0.0.0/8"), "loopback"),
    (ipaddress.IPv4Network("169.254.0.0/16"), "RFC3927 link-local"),
    (ipaddress.IPv4Network("172.16.0.0/12"), "RFC1918 private"),
    (ipaddress.IPv4Network("192.0.0.0/24"), "IETF protocol assignments"),
    (ipaddress.IPv4Network("192.0.2.0/24"), "documentation (TEST-NET-1)"),
    (ipaddress.IPv4Network("192.88.99.0/24"), "deprecated 6to4 relay anycast"),
    (ipaddress.IPv4Network("192.168.0.0/16"), "RFC1918 private"),
    (ipaddress.IPv4Network("198.18.0.0/15"), "RFC2544 benchmarking"),
    (ipaddress.IPv4Network("198.51.100.0/24"), "documentation (TEST-NET-2)"),
    (ipaddress.IPv4Network("203.0.113.0/24"), "documentation (TEST-NET-3)"),
    (ipaddress.IPv4Network("224.0.0.0/4"), "multicast"),
    (ipaddress.IPv4Network("240.0.0.0/4"), "reserved, includes broadcast"),
)

#: IPv6 networks rejected outright. ``::ffff:0:0/96`` (IPv4-mapped) is **not**
#: here on purpose: it is unwrapped and its embedded IPv4 address is re-validated,
#: so a mapped *public* address stays usable while a mapped loopback does not.
BLOCKED_IPV6_NETWORKS: tuple[tuple[ipaddress.IPv6Network, str], ...] = (
    (ipaddress.IPv6Network("::/128"), "unspecified"),
    (ipaddress.IPv6Network("::1/128"), "loopback"),
    (ipaddress.IPv6Network("::/96"), "IPv4-compatible (deprecated); embedded v4"),
    (ipaddress.IPv6Network("64:ff9b::/96"), "RFC6052 NAT64; embedded v4"),
    (ipaddress.IPv6Network("64:ff9b:1::/48"), "RFC8215 local-use NAT64"),
    (ipaddress.IPv6Network("100::/64"), "RFC6666 discard-only"),
    (ipaddress.IPv6Network("2001::/32"), "Teredo; embedded v4"),
    (ipaddress.IPv6Network("2001:db8::/32"), "documentation"),
    (ipaddress.IPv6Network("2002::/16"), "6to4; embedded v4"),
    (ipaddress.IPv6Network("3ffe::/16"), "6bone (deprecated)"),
    (ipaddress.IPv6Network("5f00::/16"), "RFC9602 SRv6 SIDs"),
    (ipaddress.IPv6Network("fc00::/7"), "RFC4193 unique-local"),
    (ipaddress.IPv6Network("fe80::/10"), "link-local"),
    (ipaddress.IPv6Network("fec0::/10"), "site-local (deprecated by RFC3879)"),
    (ipaddress.IPv6Network("ff00::/8"), "multicast"),
)


#: The top 96 bits of an IPv6 address that mean "the low 32 bits are an IPv4
#: address": the deprecated IPv4-compatible form (``::/96``), the IPv4-mapped form
#: (``::ffff:0:0/96``), and the two NAT64 prefixes (``64:ff9b::/96`` and
#: ``64:ff9b:1::/48``). Compared as integers because the stdlib accessors do not
#: report every spelling, and a spelling that goes unnoticed here is an SSRF hole.
def _prefix_of(cidr: str) -> int:
    """The integer value of an IPv6 network's base address, for prefix matching."""
    return int(ipaddress.IPv6Network(cidr).network_address)


_EMBEDDING_PREFIXES: tuple[int, ...] = tuple(
    _prefix_of(cidr)
    for cidr in ("::/96", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48")
)


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address embeds, if it embeds one.

    Covers IPv4-mapped (``::ffff:1.2.3.4``), IPv4-compatible (``::1.2.3.4``),
    6to4 (``2002:0102:0304::``), NAT64 (``64:ff9b::1.2.3.4``), and Teredo, whose
    embedded addresses are the client and the server. Returns the *first* found:
    the caller's job is to decide acceptability, and any embedded private
    address is disqualifying, so the order only affects which reason is
    reported, never whether the address is allowed.
    """
    if (mapped := address.ipv4_mapped) is not None:
        return mapped
    if (sixtofour := address.sixtofour) is not None:
        return sixtofour
    if address.teredo is not None:
        # (server, client). The client half is the one an attacker controls
        # directly; the server half is a real server the tunnel terminates at.
        return address.teredo[1]
    packed = int(address)
    for prefix in _EMBEDDING_PREFIXES:
        if packed >> 32 == prefix >> 32:
            return ipaddress.IPv4Address(packed & 0xFFFFFFFF)
    return None


@dataclass(frozen=True, slots=True)
class AddressVerdict:
    """Why one address is or is not connectable.

    ``reason`` is a short category for logs. It is deliberately coarse: it
    names the *category* of address space, never the address itself in a
    user-facing message, because internal topology is not the user's business.
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


def classify_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> AddressVerdict:
    """Decide whether a resolved address may be connected to.

    Order matters: the explicit blocklists are consulted before the stdlib
    predicates so that the reported reason is the specific one, and the stdlib
    predicates act as a second, independent line of defence rather than the only
    one.
    """
    if isinstance(address, ipaddress.IPv4Address):
        for network, reason in BLOCKED_IPV4_NETWORKS:
            if address in network:
                return AddressVerdict(False, f"ipv4-{reason}")
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or not address.is_global
        ):
            return AddressVerdict(False, "ipv4-not-globally-routable")
        return AddressVerdict(True, "public-ipv4")

    for ipv6_network, reason in BLOCKED_IPV6_NETWORKS:
        if address in ipv6_network:
            if "embedded v4" in reason:
                embedded = _embedded_ipv4(address)
                if embedded is not None:
                    inner = classify_address(embedded)
                    if not inner.allowed:
                        return AddressVerdict(
                            False, f"ipv6-{reason}-blocked: {inner.reason}"
                        )
                    return AddressVerdict(True, f"ipv6-{reason}-public-embedded-v4")
                return AddressVerdict(False, f"ipv6-{reason}")
            return AddressVerdict(False, f"ipv6-{reason}")

    # IPv4-mapped and other embedding forms the blocklists above do not name are
    # judged by the IPv4 address they carry. The stdlib reports `is_reserved` for
    # every mapped address, so without this an ordinary dual-stack answer would
    # be rejected on a technicality while the security-relevant question — what
    # does the embedded address point at — went unasked.
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        inner = classify_address(embedded)
        if inner.allowed:
            return AddressVerdict(True, "ipv6-embeds-public-ipv4")
        return AddressVerdict(False, f"ipv6-embeds-blocked-ipv4: {inner.reason}")

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        return AddressVerdict(False, "ipv6-not-globally-routable")

    return AddressVerdict(True, "public-ipv6")


def is_connectable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """The one-line question. Callers that need the why use :func:`classify_address`."""
    return classify_address(address).allowed


# --------------------------------------------------------------------------
# URL parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedTarget:
    """A URL that passed structural validation and is ready to be resolved.

    ``hostname`` is normalized and lowercased with any trailing root dot
    removed, so ``EXAMPLE.test.`` and ``example.test`` cannot be treated as two
    different hosts by different layers. ``authority`` is what belongs in the
    ``Host`` header, brackets included for IPv6 literals. ``request_target`` is
    the path and query, forwarded verbatim to the origin server.
    """

    scheme: str
    hostname: str
    port: int
    authority: str
    request_target: str
    canonical_url: str

    @property
    def is_ip_literal(self) -> bool:
        """Whether the host was already an address, so DNS is unnecessary."""
        return _parse_ip(self.hostname) is not None


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A :class:`ParsedTarget` plus the one validated address to connect to."""

    target: ParsedTarget
    address: ipaddress.IPv4Address | ipaddress.IPv6Address

    @property
    def address_literal(self) -> str:
        """The address as it appears in a URL — IPv6 literals need brackets."""
        text = str(self.address)
        if isinstance(self.address, ipaddress.IPv6Address):
            return f"[{text}]"
        return text


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a canonical IP literal, or return ``None``.

    Deliberately strict: ``ipaddress.ip_address`` rejects ``2130706433``,
    ``0x7f000001``, and ``0177.0.0.1``, which is what we want — a non-canonical
    form must be *rejected*, not silently canonicalized into something else.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def ambiguous_numeric_host(host: str) -> bool:
    """Whether ``host`` is a non-canonical numeric address notation.

    ``0x7f000001``, ``2130706433``, ``127.1``, and ``0177.0.0.1`` all reach
    ``127.0.0.1`` through ``getaddrinfo`` on a normal platform, but they are not
    canonical dotted-quads and cannot be reasoned about as addresses. They are
    rejected here, before any resolution happens, rather than relying on the
    C library to interpret them the way we expect.
    """
    if not any(char.isdigit() for char in host):
        return False
    return bool(_AMBIGUOUS_NUMERIC_HOST.match(host))


def normalize_hostname(raw_host: str) -> str:
    """Lowercase, strip the root dot, and IDNA-encode a hostname.

    IDNA encoding matters for correctness as much as for safety: a Unicode
    hostname and its punycode form are the same host, and treating them as two
    would let a policy decision be made against one spelling while a connection
    is made to another. ``idna.encode(..., uts46=True)`` is used because it is
    the mapping browsers implement and it rejects the malformed labels (empty
    label, over-long label, label starting or ending with a hyphen) rather than
    passing them through.
    """
    host = raw_host.strip().rstrip(".").lower()
    if not host:
        raise FetchInvalidURLError("url has no hostname")
    if _parse_ip(host) is not None:
        return host

    if host.isascii():
        # Already an A-label. Validated as-is and *not* round-tripped through an
        # IDNA decoder: UTS-46 processing decodes each punycode label and
        # re-encodes it, which rejects legitimate A-labels whose payload uses
        # codepoints outside the UTS-46 table. A pure-ASCII host needs no
        # translation, only label validation.
        encoded = host
    else:
        import idna  # imported lazily: only unicode hostnames need it

        try:
            encoded = idna.encode(host, uts46=True).decode("ascii").lower()
        except (idna.IDNAError, UnicodeError) as exc:
            # The message names no host: a malformed host is attacker input and
            # echoing it back into an error is a needless disclosure.
            raise FetchInvalidURLError("url hostname is not a valid domain name") from exc

    labels = encoded.split(".")
    if not all(_LABEL.match(label) for label in labels):
        raise FetchInvalidURLError("url hostname is not a valid domain name")
    if len(encoded) > 253:
        raise FetchInvalidURLError("url hostname is too long")
    return encoded


def validate_url(raw_url: str, allowed_schemes: frozenset[str] | None = None) -> ParsedTarget:
    """Parse and structurally validate one untrusted URL.

    Enforces, in order: no forbidden characters; a permitted scheme; a non-empty
    authority; **no embedded credentials**; a valid port; a hostname that is
    either a canonical IP literal or a valid domain name. Raises a typed
    :class:`app.domain.errors.FetchError` subclass on any failure.

    Credentials are rejected rather than supported. A URL with userinfo is one
    of two things — a mistake or an attempt to make the fetcher authenticate to
    a host — and there is no fetch in this product that needs HTTP basic
    credentials, so accepting them would create a capability with no caller.
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise FetchInvalidURLError("url is empty")
    url = raw_url.strip()
    if _FORBIDDEN_URL_CHARS.search(url):
        # Explicitly *not* echoed: this is the request-smuggling shape, and the
        # caller does not need it back in an error message.
        raise FetchInvalidURLError("url contains whitespace or control characters")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise FetchInvalidURLError("url cannot be parsed") from exc

    scheme = (parts.scheme or "").lower()
    permitted = allowed_schemes if allowed_schemes is not None else DEFAULT_ALLOWED_SCHEMES
    if not scheme:
        raise FetchInvalidURLError("url has no scheme")
    if scheme not in permitted:
        raise FetchUnsupportedSchemeError(
            f"scheme {scheme!r} is not permitted; only "
            f"{', '.join(sorted(permitted))} may be fetched"
        )

    try:
        raw_host = parts.hostname
        port = parts.port
    except ValueError as exc:
        # urlsplit raises here for a non-numeric or out-of-range port.
        raise FetchInvalidURLError("url has an invalid port") from exc

    if parts.username or parts.password or "@" in parts.netloc:
        raise FetchInvalidURLError("url must not contain credentials")

    if not parts.netloc:
        raise FetchInvalidURLError("url has no authority")
    if raw_host is None or not raw_host.strip():
        raise FetchInvalidURLError("url has no hostname")

    hostname = normalize_hostname(raw_host)
    if _parse_ip(hostname) is None and ambiguous_numeric_host(hostname):
        raise FetchInvalidURLError(
            "url host is a non-canonical numeric address; use the dotted form"
        )
    if hostname.endswith("."):  # defensive: normalize_hostname already strips it
        raise FetchInvalidURLError("url hostname is malformed")

    default_port = _DEFAULT_PORTS[scheme]
    if port is None:
        resolved_port = default_port
    elif not 1 <= port <= 65535:
        raise FetchInvalidURLError("url has an invalid port")
    else:
        resolved_port = port

    literal = _parse_ip(hostname)
    authority_host = f"[{hostname}]" if isinstance(literal, ipaddress.IPv6Address) else hostname
    authority = (
        authority_host if resolved_port == default_port else f"{authority_host}:{resolved_port}"
    )

    request_target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    canonical_url = urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))

    return ParsedTarget(
        scheme=scheme,
        hostname=hostname,
        port=resolved_port,
        authority=authority,
        request_target=request_target,
        canonical_url=canonical_url,
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@runtime_checkable
class AddressResolver(Protocol):
    """Turns a hostname into the addresses it currently maps to.

    A port so the resolver is free to consult ``/etc/hosts``, ``/etc/services``,
    or a service-discovery backend, exactly as the real one does — and so a test
    can substitute a scripted resolver with no network at all.
    """

    async def resolve(self, hostname: str, port: int) -> Sequence[str]: ...


class SystemAddressResolver:
    """The production resolver: the platform's ``getaddrinfo``.

    Off the event loop, because ``getaddrinfo`` is a blocking C call and a slow
    or unreachable DNS server would otherwise stall every other coroutine in the
    worker. Deduplicates while preserving the platform's ordering, and asks for
    both address families so an IPv6-only answer is not mistaken for an empty
    one.
    """

    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (socket.gaierror, UnicodeError, OSError) as exc:
            # The resolver's own text is not propagated: it can echo the query
            # and varies by platform. The category is what a caller needs.
            raise FetchDNSResolutionError("hostname could not be resolved") from exc

        seen: dict[str, None] = {}
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                seen.setdefault(str(sockaddr[0]), None)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class SsrfPolicy:
    """The assembled policy: permitted schemes and the address rule.

    Constructed once per fetcher from configuration. It holds no state that
    changes between fetches, so two instances with the same arguments enforce
    exactly the same thing — which is what makes the tests' scripted resolvers
    meaningful.

    Two rules stay true regardless of which resolver is configured: **every**
    address a resolver returns is validated before use, and a resolver that
    fails or returns nothing is a rejection. Swapping in
    :class:`~app.infrastructure.fetch.resolver.DoHAddressResolver` changes where
    the answers come from, never whether they are allowed.
    """

    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES
    resolver: AddressResolver | None = None

    def _resolver(self) -> AddressResolver:
        # Constructed on first use rather than at import: a module-level
        # resolver would be global state built outside dependency injection,
        # which the development rules prohibit.
        return self.resolver or SystemAddressResolver()

    async def resolve_and_validate(
        self, target: ParsedTarget
    ) -> ResolvedTarget:
        """Resolve ``target`` and validate every answer it returns.

        Raises :class:`~app.domain.errors.FetchBlockedDestinationError` if **any**
        address is not connectable, and
        :class:`~app.domain.errors.FetchDNSResolutionError` on a resolver failure
        or an empty answer. Both are non-retryable.
        """
        literal = _parse_ip(target.hostname)
        if literal is not None:
            verdict = classify_address(literal)
            if not verdict.allowed:
                raise FetchBlockedDestinationError(
                    "destination address is not permitted", reason=verdict.reason
                )
            return ResolvedTarget(target=target, address=literal)

        answers = await self._resolver().resolve(target.hostname, target.port)
        if not answers:
            # Failing closed. An empty answer is not permission.
            raise FetchDNSResolutionError("hostname resolved to no addresses")

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for raw in answers:
            parsed = _parse_ip(str(raw))
            if parsed is None:
                raise FetchDNSResolutionError(
                    "hostname resolved to an unusable address"
                )
            addresses.append(parsed)

        for address in addresses:
            verdict = classify_address(address)
            if not verdict.allowed:
                raise FetchBlockedDestinationError(
                    "destination address is not permitted", reason=verdict.reason
                )

        return ResolvedTarget(target=target, address=addresses[0])

    async def validate(self, raw_url: str) -> ResolvedTarget:
        """Structural validation plus resolution and address policy, in one call."""
        target = validate_url(raw_url, self.allowed_schemes)
        return await self.resolve_and_validate(target)
