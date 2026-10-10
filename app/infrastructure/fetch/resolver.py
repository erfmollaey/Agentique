"""Where an address answer comes from (Phase 3, P3.3, AD-031).

Two implementations of the resolver the SSRF boundary asks for, and the one
place the deployment chooses between them. The choice is **where the answer
comes from**, never which answer is acceptable — :mod:`app.infrastructure.fetch.ssrf`
still decides that, for both.

```text
    FETCH_DNS_MODE=system  →  SystemAddressResolver   (platform getaddrinfo)
    FETCH_DNS_MODE=doh     →  DoHAddressResolver      (DNS over HTTPS, RFC 8484)
```

Why a second resolver exists at all
----------------------------------
It was measured, not theorised. A developer host with a fake-IP DNS layer —
Tailscale MagicDNS, or a comparable DNS-filtering product — rewrites *every*
outbound UDP/53 answer into ``198.18.0.0/15``. A raw query to ``1.1.1.1:53`` from
inside a Docker container on such a host still comes back fake, and so does a
lookup for a name that does not exist. Those addresses are RFC 2544
benchmarking space: not globally routable, so the SSRF boundary refuses them,
correctly. The observable result is that a real, valid, public domain fails to
fetch with ``FetchBlockedDestinationError`` — indistinguishable from "the
internet is down".

Setting the container's DNS does not help, and that is the point: the rewrite is
on the network path, not in the resolver configuration, so any name server the
container is pointed at is intercepted the same way. HTTPS is not rewritten on
those networks, which is why DNS-over-HTTPS answers are real.

**What this module must never do.** DoH is not a way around the address policy.
A DoH lookup for ``localhost`` still resolves to loopback, and the boundary
refuses it exactly as it would have under the system resolver. There is no code
path here that treats an answer differently because of where it came from.
"""

from __future__ import annotations

import ipaddress
import struct
from collections.abc import Sequence

import httpx

from app.core.config import Settings
from app.domain.errors import FetchDNSResolutionError
from app.infrastructure.fetch.ssrf import SystemAddressResolver

#: The resolver modes :func:`configured_resolver` recognises. Kept beside the
#: branches so a new resolver cannot be half-registered — named but not built,
#: or built but not named.
RESOLVER_MODES: frozenset[str] = frozenset({"system", "doh"})

#: DNS wire-format record types. Asked for separately, in this order, so a
#: dual-stack name is answered the same way on every run rather than depending
#: on how the provider happens to interleave the two families.
_QTYPE_A = 1
_QTYPE_AAAA = 28

#: Class IN. The only class this resolver asks about; CHAOS/HS answers are not
#: addresses and are not useful here.
_QCLASS_IN = 1

#: Maximum DNS message this module will parse. A legitimate answer for one name
#: is a few hundred bytes. This bound is what stops a hostile or broken endpoint
#: from handing the parser a multi-kilobyte blob to walk.
_MAX_DNS_MESSAGE_BYTES = 4096

#: A legitimate compressed name needs two or three jumps at most. This bound is
#: what stops a crafted message from making the parser walk forever.
_MAX_DNS_POINTER_JUMPS = 16


def configured_resolver(settings: Settings) -> SystemAddressResolver | DoHAddressResolver:
    """The resolver this deployment's configuration selects.

    Defaults to the platform resolver, so an unconfigured deployment behaves
    exactly as it did before this module existed. An unrecognised name raises
    rather than silently falling back: a deployment that asked for one
    resolution path and got another could not reason about its own addresses.
    """
    mode = (settings.FETCH_DNS_MODE or "system").strip().lower()
    if mode == "system":
        return SystemAddressResolver()
    if mode == "doh":
        return DoHAddressResolver(settings)
    raise ValueError(
        f"Unknown FETCH_DNS_MODE {mode!r}. "
        f"Supported: {', '.join(sorted(RESOLVER_MODES))}. "
        "Add a branch here to register another resolver."
    )


class DoHAddressResolver:
    """Answers a hostname through a DNS-over-HTTPS endpoint.

    Not a faster resolver and not a more private one by policy — those are
    consequences, not the reason it is here. It exists because some networks
    rewrite UDP/53 answers into non-routable space and nothing in the container
    can see past that. HTTPS answers, on those same networks, are real.

    The endpoint is configured, so a deployment may run its own resolver rather
    than a public one; the wire format is standard RFC 8484, so any of them
    works. ``trust_env=False`` matches the rest of the fetch and search
    boundaries: a proxy variable in the environment must not be able to reroute
    a resolver the deployment chose deliberately.
    """

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        endpoint = (settings.FETCH_DOH_ENDPOINT or "").strip()
        if not endpoint:
            raise ValueError("FETCH_DOH_ENDPOINT must be set when FETCH_DNS_MODE=doh")
        if httpx.URL(endpoint).scheme != "https":
            # A plaintext or scheme-less endpoint would put the hostname being
            # looked up on an unnecessary channel.
            raise ValueError("FETCH_DOH_ENDPOINT must be an https:// URL")
        self._endpoint = endpoint
        self._client = client or httpx.AsyncClient(
            timeout=settings.FETCH_DOH_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        """The addresses ``hostname`` maps to, or a typed failure.

        Never raises anything but
        :class:`~app.domain.errors.FetchDNSResolutionError`. A resolver that
        cannot answer is a rejection, never an implicit "no address found, so
        allow" — the same fail-closed rule ``SystemAddressResolver`` follows, and
        the reason an unreachable DoH endpoint fails a fetch instead of letting
        one through.
        """
        if not hostname.isascii():
            # ``normalize_hostname`` produces A-labels before anything reaches a
            # resolver, so a non-ASCII hostname here means a caller bypassed it.
            raise FetchDNSResolutionError("hostname is not an ASCII A-label")

        addresses: list[str] = []
        for qtype in (_QTYPE_A, _QTYPE_AAAA):
            addresses.extend(await self._query(hostname, qtype))
        if not addresses:
            # Empty is not permission (see ``resolve_and_validate``).
            raise FetchDNSResolutionError("hostname resolved to no addresses")

        # Deduplicate while preserving order: a provider that repeats an answer
        # must not make the boundary re-validate the same address.
        return tuple(dict.fromkeys(addresses))

    async def _query(self, hostname: str, qtype: int) -> tuple[str, ...]:
        """One RFC 8484 POST for one record type, parsed into addresses.

        The question travels in the body, not the URL. A URL is the one string
        that reaches a log line verbatim, and a hostname in a query parameter
        would be metadata this pipeline has no business writing down (SR-7).
        """
        try:
            response = await self._client.post(
                self._endpoint,
                content=_encode_query(hostname, qtype),
                headers={
                    "content-type": "application/dns-message",
                    "accept": "application/dns-message",
                },
                timeout=self._settings.FETCH_DOH_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise FetchDNSResolutionError("dns-over-https query timed out") from exc
        except httpx.HTTPError as exc:
            raise FetchDNSResolutionError("dns-over-https query failed") from exc

        if response.status_code != httpx.codes.OK:
            raise FetchDNSResolutionError(
                f"dns-over-https query returned status {response.status_code}"
            )
        if len(response.content) > _MAX_DNS_MESSAGE_BYTES:
            raise FetchDNSResolutionError("dns-over-https answer was too large")
        return _parse_answers(response.content, qtype)

    async def aclose(self) -> None:
        """Close the transport this resolver created, and only that one.

        An injected client belongs to whoever injected it, which is what keeps a
        test honest about ownership.
        """
        if self._owns_client:
            await self._client.aclose()


# --- RFC 8484 wire format -------------------------------------------------
#
# The message is built and parsed here rather than with a dependency, so a
# resolver stays a boundary component rather than a library. This is not a
# general DNS implementation: it encodes one question and reads A/AAAA answers
# out of the reply, which is all the ``SafeFetcher`` port needs.


def _encode_query(hostname: str, qtype: int) -> bytes:
    """One DNS query for ``hostname`` and ``qtype``, in wire format."""
    # Standard query with recursion desired. The identifier is opaque to a DoH
    # provider and is not used for correlation, so a constant keeps the message
    # reproducible for the same question.
    header = struct.pack(">HHHHHH", 0x0000, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(label)]) + label.encode("ascii")
        for label in hostname.split(".")
        if label
    )
    question = qname + b"\x00" + struct.pack(">HH", qtype, _QCLASS_IN)
    return header + question


def _decode_name(message: bytes, offset: int) -> tuple[str, int]:
    """Read one (possibly compressed) name; return it and the next offset.

    Compression pointers may point backwards, so a name can occupy few bytes on
    the wire and decode to several characters. The offset returned is the
    position *after* the name in the sequence being parsed, not after the
    pointer's target — which is what keeps the enclosing record's fields
    readable.
    """
    labels: list[str] = []
    jumps = 0
    cursor = offset
    next_offset = offset
    followed = False

    while True:
        if cursor >= len(message):
            raise FetchDNSResolutionError("dns answer is truncated")
        length = message[cursor]
        if length == 0:
            cursor += 1
            if not followed:
                next_offset = cursor
            return ".".join(labels), next_offset
        if length & 0xC0 == 0xC0:
            if cursor + 1 >= len(message):
                raise FetchDNSResolutionError("dns answer is truncated")
            pointer = ((length & 0x3F) << 8) | message[cursor + 1]
            if not followed:
                next_offset = cursor + 2
                followed = True
            jumps += 1
            if jumps > _MAX_DNS_POINTER_JUMPS:
                raise FetchDNSResolutionError("dns answer has a malformed name")
            cursor = pointer
            continue
        cursor += 1
        if cursor + length > len(message):
            raise FetchDNSResolutionError("dns answer is truncated")
        labels.append(message[cursor : cursor + length].decode("ascii", "replace"))
        cursor += length


def _parse_answers(message: bytes, qtype: int) -> tuple[str, ...]:
    """The addresses of one record type in a DNS answer message.

    Only the answer section is read, and only the type asked for. A CNAME answer
    contributes no address — the address record the chain terminates in does —
    so a CNAME-only reply correctly yields nothing and the caller reports "no
    addresses". Declared counts that do not match the bytes are truncation, which
    is a rejection rather than a partial answer: a truncated reply presented as a
    complete one is exactly the dishonesty SR-12 forbids.
    """
    if len(message) < 12:
        raise FetchDNSResolutionError("dns answer is truncated")
    _ident, _flags, questions, answers, _nscount, _arcount = struct.unpack(
        ">HHHHHH", message[:12]
    )
    if not questions or not answers:
        return ()

    cursor = 12
    for _ in range(questions):
        _, cursor = _decode_name(message, cursor)
        cursor += 4  # QTYPE + QCLASS

    found: list[str] = []
    for _ in range(answers):
        _, cursor = _decode_name(message, cursor)
        if cursor + 10 > len(message):
            raise FetchDNSResolutionError("dns answer is truncated")
        rtype, _rclass, _ttl, rdlength = struct.unpack(
            ">HHIH", message[cursor : cursor + 10]
        )
        cursor += 10
        if cursor + rdlength > len(message):
            raise FetchDNSResolutionError("dns answer is truncated")
        rdata = message[cursor : cursor + rdlength]
        cursor += rdlength
        if rtype != qtype:
            continue
        if rtype == _QTYPE_A:
            found.append(str(ipaddress.IPv4Address(rdata)))
        elif rtype == _QTYPE_AAAA:
            found.append(str(ipaddress.IPv6Address(rdata)))
    return tuple(found)
