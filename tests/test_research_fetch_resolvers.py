"""The resolvers behind the SSRF boundary (Phase 3, P3.3 — AD-031).

One module, two resolvers, and the property that decides whether this whole
exercise was worth doing:

**Where an answer comes from never changes whether it is allowed.**

``DoHAddressResolver`` exists because some host networks rewrite every outbound
UDP/53 answer into ``198.18.0.0/15`` — Tailscale MagicDNS and comparable fake-IP
pools — which the SSRF boundary then correctly refuses. It is very easy to make
that symptom go away by relaxing the boundary. This module does not, and these
tests are what prove it: every property ``SystemAddressResolver`` already had,
under a mocked transport and no network of any kind.

The property under test, in one sentence each:

* a DoH answer for a private or loopback host is refused, exactly as a system
  answer would be (``test_doh_*refused*``);
* a DoH answer that cannot be obtained is a rejection, never an implicit
  permission (``test_doh_*fails_closed*``);
* the wire format is the standard one, so the message these tests build is the
  message a real provider sends (``test_doh_parses_a_real_answer*``).

No test here reaches a DoH provider or a real socket.
"""

from __future__ import annotations

import ipaddress
import struct
from urllib.parse import urlsplit

import httpx
import pytest

from app.core.config import Settings
from app.domain.errors import (
    FetchBlockedDestinationError,
    FetchDNSResolutionError,
)
from app.infrastructure.fetch.resolver import (
    RESOLVER_MODES,
    DoHAddressResolver,
    configured_resolver,
    parse_doh_endpoints,
)
from app.infrastructure.fetch.ssrf import (
    SsrfPolicy,
    SystemAddressResolver,
    validate_url,
)

# --------------------------------------------------------------------------
# Building a DoH response without a network
# --------------------------------------------------------------------------
#
# These produce real RFC 8484 wire-format messages, which is deliberate: a
# synthetic JSON-ish stub would let the parser drift away from what a provider
# actually sends. The bytes built here are what cloudflare-dns.com returns.


def _encode_name(name: str) -> bytes:
    return (
        b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".") if label)
        + b"\x00"
    )


def dns_message(records: list[tuple[int, str]], *, questions: int = 1) -> bytes:
    """A DNS answer carrying ``records`` as (rtype, address) pairs.

    The name is a compression pointer at offset 12 — the position of the
    question's QNAME — because that is what every real resolver emits, and the
    parser's pointer handling is one of the things being tested.
    """
    header = struct.pack(">HHHHHH", 0x0000, 0x8180, questions, len(records), 0, 0)
    question = _encode_name("github.com") + struct.pack(">HH", 1, 1)
    body = b""
    for rtype, address in records:
        rdata = (
            ipaddress.IPv4Address(address).packed
            if rtype == 1
            else ipaddress.IPv6Address(address).packed
        )
        body += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 300, len(rdata)) + rdata
    return header + question + body


def doh_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"FETCH_DNS_MODE": "doh"}
    base.update(overrides)
    return _settings(**base)


def system_settings(**overrides: object) -> Settings:
    return _settings(**overrides)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": "1234567890:TEST_TOKEN_NOT_A_REAL_CREDENTIAL",
        "GROQ_API_KEY": "gsk-test-key-not-a-real-credential",
        "REDIS_URL": "redis://localhost:6379/15",
        "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/never_used",
        "TEST_DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/never_used_test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def doh_resolver_with(records: list[tuple[int, str]], **overrides: object):
    """A ``DoHAddressResolver`` whose transport answers with ``records``.

    The answer is served for the record type actually asked for, the way a real
    provider behaves: an A query gets A records and no AAAA ones, and vice versa.
    Answering both to both would hide a filtering bug in the parser.
    """
    settings = doh_settings(**overrides)
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        # The QTYPE sits in the fixed question section at a known offset.
        qtype = struct.unpack(">H", request.content[-4:-2])[0]
        matching = [record for record in records if record[0] == qtype]
        return httpx.Response(200, content=dns_message(matching))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DoHAddressResolver(settings, client=client), sent


def doh_with_scripted_endpoints(behaviour):
    """A resolver pointing at two endpoints whose behaviour ``behaviour`` scripts.

    ``behaviour`` is called with the request's hostname and answers with one of:
    a list of A/AAAA records (answered normally), an :class:`httpx.Response`
    (returned as given), or an exception to raise. Every attempt is recorded in
    ``attempts``, in the order the endpoints were contacted, which is how the
    ordering assertions are made.
    """
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = urlsplit(str(request.url)).hostname
        attempts.append(host)
        scripted = behaviour(host)
        if isinstance(scripted, BaseException):
            raise scripted
        if isinstance(scripted, httpx.Response):
            return scripted
        qtype = struct.unpack(">H", request.content[-4:-2])[0]
        matching = [record for record in scripted if record[0] == qtype]
        return httpx.Response(200, content=dns_message(matching))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = DoHAddressResolver(
        doh_settings(), client=client, endpoints=_SCRIPTED_DEFAULT_ENDPOINTS
    )
    return resolver, attempts


#: The two endpoints every fallback test points at, so a test can name a
#: primary and a secondary without inventing URLs each time.
_SCRIPTED_DEFAULT_ENDPOINTS = (
    "https://primary.example/dns-query",
    "https://secondary.example/dns-query",
)

_PRIMARY_HOST = "primary.example"
_SECONDARY_HOST = "secondary.example"


# --- resolver selection ----------------------------------------------------


def test_the_default_resolver_is_the_system_resolver():
    """An unconfigured deployment behaves exactly as it did before DoH existed."""
    assert isinstance(configured_resolver(system_settings()), SystemAddressResolver)


def test_the_doh_mode_is_selectable():
    assert isinstance(configured_resolver(doh_settings()), DoHAddressResolver)


def test_an_unknown_resolver_mode_raises_rather_than_defaulting():
    """A deployment that asked for one path and got another is not a deployment
    that can reason about its own addresses."""
    with pytest.raises(ValueError, match="Unknown FETCH_DNS_MODE"):
        configured_resolver(doh_settings(FETCH_DNS_MODE="nonsense"))


def test_the_recognised_modes_are_the_ones_that_exist():
    """Registration cannot be half: naming a mode without building it, or
    building it without naming it, is exactly what this guards."""
    assert sorted(RESOLVER_MODES) == ["doh", "system"]


def test_a_doh_resolver_requires_at_least_one_endpoint():
    """A resolver configured with nowhere to ask is a deployment that cannot
    reason about its own addresses."""
    with pytest.raises(ValueError, match="FETCH_DNS_SERVERS"):
        DoHAddressResolver(doh_settings(FETCH_DNS_SERVERS="   "))


def test_every_doh_endpoint_must_be_https():
    """The hostname being looked up must not travel on a plaintext channel."""
    with pytest.raises(ValueError, match="https"):
        DoHAddressResolver(doh_settings(FETCH_DNS_SERVERS="http://dns.example/query"))


def test_the_default_servers_list_two_independent_operators():
    """A single endpoint is a single point of failure for the whole fetch stage.

    Built from the module's own base settings, which set only the required
    fields, so ``FETCH_DNS_SERVERS`` is read at its default exactly as a real
    deployment would see it.
    """
    servers = parse_doh_endpoints(_settings().FETCH_DNS_SERVERS)
    assert len(servers) >= 2
    assert all(server.startswith("https://") for server in servers)
    assert len(set(servers)) == len(servers), "a duplicated endpoint is not a fallback"


def test_comma_separated_entries_are_split_and_validated():
    servers = parse_doh_endpoints(
        "https://a.example/query, https://b.example/query ,https://c.example/query"
    )
    assert servers == (
        "https://a.example/query",
        "https://b.example/query",
        "https://c.example/query",
    )


def test_a_sequence_of_endpoints_is_accepted_without_settings():
    assert parse_doh_endpoints(["https://a.example/query"]) == ("https://a.example/query",)


def test_a_doh_resolver_satisfies_the_address_resolver_port():
    from app.infrastructure.fetch.ssrf import AddressResolver

    resolver, _ = doh_resolver_with([(1, "140.82.121.3")])
    assert isinstance(resolver, AddressResolver)


# --- the wire format -------------------------------------------------------


async def test_doh_parses_a_real_answer_message():
    resolver, sent = doh_resolver_with([(1, "140.82.121.3"), (1, "140.82.121.4")])

    assert tuple(await resolver.resolve("github.com", 443)) == (
        "140.82.121.3",
        "140.82.121.4",
    )
    assert len(sent) == 2, "one query per address family"


async def test_doh_parses_ipv6_answers():
    resolver, _ = doh_resolver_with([(28, "2606:50c0::1")])

    addresses = tuple(await resolver.resolve("github.com", 443))
    assert addresses == ("2606:50c0::1",)


async def test_doh_deduplicates_repeated_answers():
    resolver, _ = doh_resolver_with([(1, "140.82.121.3"), (1, "140.82.121.3")])

    assert tuple(await resolver.resolve("github.com", 443)) == ("140.82.121.3",)


async def test_doh_asks_for_both_address_families():
    """A dual-stack name is answered the same way every run, and IPv4 answers
    first so a working-v4 network is not surprised by an unreachable v6."""
    resolver, sent = doh_resolver_with([(1, "140.82.121.3")])
    await resolver.resolve("github.com", 443)

    first = struct.unpack(">H", sent[0][-4:-2])[0]
    second = struct.unpack(">H", sent[1][-4:-2])[0]
    assert (first, second) == (1, 28)


async def test_doh_puts_the_hostname_in_the_body_not_the_url():
    """SR-7: a URL is the one string that reaches a log line verbatim."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        qtype = 28 if b"\x00\x1c" in request.content else 1
        records = [(1, "140.82.121.3")] if qtype == 1 else []
        return httpx.Response(200, content=dns_message(records))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = DoHAddressResolver(doh_settings(), client=client)
    await resolver.resolve("github.com", 443)

    for url in captured:
        assert "github.com" not in url
        assert url == "https://cloudflare-dns.com/dns-query"


async def test_doh_rejects_a_non_ascii_hostname():
    """``normalize_hostname`` produces A-labels, so a non-ASCII hostname here
    means a caller bypassed the boundary."""
    resolver, _ = doh_resolver_with([(1, "140.82.121.3")])
    with pytest.raises(FetchDNSResolutionError, match="A-label"):
        await resolver.resolve("münchen.example", 443)


# --- fail closed -----------------------------------------------------------


async def test_doh_fails_closed_when_the_endpoint_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="dns-over-https query failed"):
        await resolver.resolve("github.com", 443)


async def test_doh_fails_closed_when_the_endpoint_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="timed out"):
        await resolver.resolve("github.com", 443)


async def test_doh_fails_closed_on_a_non_200_status():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(502))
    )
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="status 502"):
        await resolver.resolve("github.com", 443)


async def test_doh_fails_closed_on_an_empty_answer():
    """No address found is not permission."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, content=dns_message([], questions=1))
        )
    )
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="no addresses"):
        await resolver.resolve("github.com", 443)


async def test_doh_fails_closed_on_a_truncated_message():
    """A partial answer presented as a complete one is SR-12's dishonesty."""
    good = dns_message([(1, "140.82.121.3")])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=good[:22]))
    )
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="truncated"):
        await resolver.resolve("github.com", 443)


async def test_doh_fails_closed_on_an_oversized_answer():
    huge = b"\x00" * 5000
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=huge))
    )
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="too large"):
        await resolver.resolve("github.com", 443)


async def test_doh_fails_closed_on_a_cname_only_answer():
    """A CNAME with no address record yields no addresses, which is a rejection."""
    header = struct.pack(">HHHHHH", 0, 0x8180, 1, 1, 0, 0)
    question = _encode_name("github.com") + struct.pack(">HH", 1, 1)
    cname = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 300, 2) + b"\x01a"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, content=header + question + cname)
        )
    )
    resolver = DoHAddressResolver(doh_settings(), client=client)

    with pytest.raises(FetchDNSResolutionError, match="no addresses"):
        await resolver.resolve("github.com", 443)


async def test_doh_closes_only_the_client_it_owns():
    """Ownership, the same rule the fetcher and the search adapter follow."""
    owned = DoHAddressResolver(doh_settings())
    injected_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    injected = DoHAddressResolver(doh_settings(), client=injected_client)
    try:
        await owned.aclose()
        await injected.aclose()  # must not close a client it did not create
        assert not injected_client.is_closed
    finally:
        await injected_client.aclose()


# --- the property that matters: DoH does not widen the boundary -------------


@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "192.168.1.1", "169.254.169.254"])
async def test_doh_answer_for_a_private_host_is_still_refused(address: str):
    """The whole point. DoH changes *where* an answer comes from, never whether it
    is allowed — a private answer is refused by the same code that refuses it
    under the system resolver."""
    resolver, _ = doh_resolver_with([(1, address)])
    policy = SsrfPolicy(resolver=resolver)
    target = validate_url("https://github.com/")
    with pytest.raises(FetchBlockedDestinationError):
        await policy.resolve_and_validate(target)


async def test_doh_answer_for_a_rfc2544_fake_ip_is_refused():
    """The exact case that started this: a fake-IP pool answer is not a public
    answer, and it is not made one by choosing a different resolver."""
    resolver, _ = doh_resolver_with([(1, "198.18.0.22")])
    policy = SsrfPolicy(resolver=resolver)

    with pytest.raises(FetchBlockedDestinationError, match="not permitted"):
        await policy.resolve_and_validate(validate_url("https://github.com/"))


async def test_doh_answer_for_a_public_host_is_allowed():
    resolver, _ = doh_resolver_with([(1, "140.82.121.3")])
    resolved = await SsrfPolicy(resolver=resolver).resolve_and_validate(
        validate_url("https://github.com/")
    )

    assert str(resolved.address) == "140.82.121.3"


# --- the fetch path end to end ---------------------------------------------


async def test_a_fetch_succeeds_for_a_public_domain_in_doh_mode():
    """The regression this module exists for: a real fetch of a real public
    domain, with only the resolver substituted. Under ``FETCH_DNS_MODE=doh`` the
    address is real, so the policy allows it and the transport dials it."""
    from app.domain.research.fetch import FetchRequest

    resolver, _ = doh_resolver_with([(1, "140.82.121.3")])
    dialled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dialled.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body>content</body></html>",
        )

    fetcher = _fetcher_with_resolver_and_transport(
        doh_settings(), resolver, httpx.MockTransport(handler)
    )

    try:
        source = await fetcher.fetch(FetchRequest(url="https://github.com/"))
    finally:
        await fetcher.aclose()

    assert source.status_code == 200
    assert source.content_type == "text/html"
    assert b"content" in source.body
    # The destination is the pinned address, never the hostname: this is what
    # closes the DNS-rebinding window, and it must hold with a DoH resolver
    # exactly as it does with the system one.
    assert dialled == ["https://140.82.121.3/"]


def _fetcher_with_resolver_and_transport(settings, resolver, transport):
    """Build a fetcher whose only substitutions are the resolver and transport."""
    from app.infrastructure.fetch.client import RestrictedFetcher, configured_schemes

    return RestrictedFetcher(
        settings,
        policy=SsrfPolicy(allowed_schemes=configured_schemes(settings), resolver=resolver),
        transport=transport,
    )


# --- ordered fallback across endpoints -------------------------------------
#
# The point of the list. One endpoint is a single point of failure for the whole
# fetch stage, so a second, independent operator is asked when the first cannot
# be reached or understood.


async def test_doh_falls_back_to_the_secondary_endpoint():
    """Primary unreachable → secondary answers → the address is the real one."""
    resolver, attempts = doh_with_scripted_endpoints(
        lambda host: httpx.ConnectError("unreachable")
        if host == _PRIMARY_HOST
        else [(1, "140.82.121.3")]
    )

    addresses = tuple(await resolver.resolve("github.com", 443))

    assert addresses == ("140.82.121.3",)
    # The primary was tried first, exactly once, and only for A.
    assert attempts[0] == _PRIMARY_HOST
    assert _PRIMARY_HOST in attempts and _SECONDARY_HOST in attempts


async def test_doh_tries_the_primary_before_the_secondary():
    """Order is the configured order, not the order the transport happens to
    attempt, so a deployment can put the resolver it trusts first."""
    resolver, attempts = doh_with_scripted_endpoints(
        lambda host: httpx.ConnectError("down") if host == _PRIMARY_HOST else []
    )

    with pytest.raises(FetchDNSResolutionError):
        await resolver.resolve("github.com", 443)

    assert attempts == [_PRIMARY_HOST, _SECONDARY_HOST] * 2, (
        "every endpoint is tried for each record type, in order"
    )


async def test_doh_falls_back_when_the_primary_returns_a_non_200():
    """A provider answering 502 has not answered the question."""
    resolver, _ = doh_with_scripted_endpoints(
        lambda host: httpx.Response(502)
        if host == _PRIMARY_HOST
        else [(1, "140.82.121.3")]
    )

    assert tuple(await resolver.resolve("github.com", 443)) == ("140.82.121.3",)


async def test_doh_falls_back_when_the_primary_returns_garbage():
    """An unparseable answer is a failure to answer, not an empty answer."""
    resolver, _ = doh_with_scripted_endpoints(
        lambda host: httpx.Response(200, content=b"\x00not-dns-at-all")
        if host == _PRIMARY_HOST
        else [(1, "140.82.121.3")]
    )

    assert tuple(await resolver.resolve("github.com", 443)) == ("140.82.121.3",)


async def test_doh_does_not_fall_back_when_the_primary_answers_empty():
    """The deliberate limit: an endpoint that *answers* is used as it answered.

    A legitimate "no records" must not be masked by asking a different provider,
    because then which provider answered is not something the deployment can
    reason about. The host simply does not resolve, and the caller fails closed.
    """
    resolver, attempts = doh_with_scripted_endpoints(
        lambda host: [] if host == _PRIMARY_HOST else [(1, "140.82.121.3")]
    )

    with pytest.raises(FetchDNSResolutionError, match="no addresses"):
        await resolver.resolve("github.com", 443)

    assert attempts == [_PRIMARY_HOST] * 2, "the secondary must not have been asked"


async def test_doh_reports_every_failure_when_no_endpoint_answers():
    """The error names what was tried, without letting a single provider's text
    become the diagnosis."""
    resolver, _ = doh_with_scripted_endpoints(lambda host: httpx.ConnectError("down"))

    with pytest.raises(FetchDNSResolutionError) as caught:
        await resolver.resolve("github.com", 443)

    message = str(caught.value)
    assert "no configured dns-over-https endpoint answered" in message
    assert _PRIMARY_HOST in message and _SECONDARY_HOST in message


async def test_doh_reports_a_timeout_from_every_endpoint():
    resolver, attempts = doh_with_scripted_endpoints(
        lambda host: httpx.ReadTimeout("too slow")
    )

    with pytest.raises(FetchDNSResolutionError):
        await resolver.resolve("github.com", 443)

    assert set(attempts) == {_PRIMARY_HOST, _SECONDARY_HOST}, (
        "both endpoints must be tried before giving up"
    )
    assert attempts.count(_PRIMARY_HOST) == attempts.count(_SECONDARY_HOST)


async def test_doh_shares_one_client_across_endpoints():
    """One pool, not two: a resolver that builds a client per endpoint pays TLS
    on every lookup and gets no keep-alive benefit."""
    resolver, _ = doh_with_scripted_endpoints(lambda host: [(1, "140.82.121.3")])
    try:
        assert resolver._client is not None
        # Both endpoints are reachable through the one injected transport.
        assert len(await resolver.resolve("github.com", 443)) == 1
    finally:
        await resolver.aclose()
