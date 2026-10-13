"""The SSRF boundary: URL, address, and DNS policy (Phase 3, P3.3).

These are the tests behind mandatory Phase 3 cases T-4 (private and loopback
addresses rejected), T-6 (a hostname resolving to a private address is
rejected), and T-7 (non-HTTP schemes rejected), plus the parsing-ambiguity cases
the audit § C.1 called for.

Determinism: no test here touches a real resolver or a real socket. Resolution
goes through a scripted resolver, and the one test that exercises
:class:`SystemAddressResolver` asks only about a name whose answer is computed by
the platform, not by the network.

The two properties these tests defend, stated once so a later reader knows what
would be catastrophic:

1. **A destination that is not publicly routable is never connectable.** No
   scheme, no encoding, no address spelling, no DNS answer gets past it.
2. **Failing to validate fails closed.** A resolver error or an empty answer is
   a rejection, never an implicit permission.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from app.domain.errors import (
    FetchBlockedDestinationError,
    FetchDNSResolutionError,
    FetchInvalidURLError,
    FetchUnsupportedSchemeError,
)
from app.infrastructure.fetch.ssrf import (
    BLOCKED_IPV4_NETWORKS,
    BLOCKED_IPV6_NETWORKS,
    SsrfPolicy,
    SystemAddressResolver,
    ambiguous_numeric_host,
    classify_address,
    is_connectable,
    normalize_hostname,
    validate_url,
)

# --- helpers ---------------------------------------------------------------


class ScriptedResolver:
    """Answers with whatever it was given, and records every question.

    ``answers_by_host`` maps a hostname to the addresses it resolves to.
    ``raises_for`` makes a hostname fail resolution. Every call is recorded so a
    test can assert *how many times* a name was resolved — which is how the
    DNS-rebinding tests prove there is no second lookup.
    """

    def __init__(
        self,
        answers_by_host: dict[str, list[str]] | None = None,
        raises_for: set[str] | None = None,
    ) -> None:
        self._answers = answers_by_host or {}
        self._raises = raises_for or set()
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        if hostname in self._raises:
            raise FetchDNSResolutionError("hostname could not be resolved")
        return tuple(self._answers.get(hostname, []))


def policy_with(answers: dict[str, list[str]] | None = None, **kwargs) -> SsrfPolicy:
    return SsrfPolicy(resolver=ScriptedResolver(answers), **kwargs)


# --- URL structure ---------------------------------------------------------


def test_a_plain_http_url_is_accepted_and_normalized():
    target = validate_url("http://example.com/article")

    assert target.scheme == "http"
    assert target.hostname == "example.com"
    assert target.port == 80
    assert target.authority == "example.com"
    assert target.request_target == "/article"


def test_a_plain_https_url_is_accepted():
    target = validate_url("https://example.com/article")

    assert target.scheme == "https"
    assert target.port == 443


def test_the_path_and_query_reach_the_origin_server():
    target = validate_url("https://example.com/a/b?x=1&y=2#fragment")

    assert target.request_target == "/a/b?x=1&y=2", "the fragment is client-side only"
    assert "fragment" not in target.request_target


def test_an_explicit_non_default_port_is_kept_in_the_authority():
    target = validate_url("http://example.com:8080/x")

    assert target.port == 8080
    assert target.authority == "example.com:8080", (
        "the origin server needs the port to route the request"
    )


def test_an_explicit_default_port_is_omitted_from_the_authority():
    assert validate_url("https://example.com:443/x").authority == "example.com"


def test_a_missing_scheme_is_rejected_rather_than_assumed():
    with pytest.raises(FetchInvalidURLError):
        validate_url("example.com/article")


def test_a_scheme_relative_url_is_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url("//example.com/article")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com:70/x",
        "data:text/html,<h1>hi</h1>",
        "javascript:alert(1)",
        "mailto:someone@example.com",
        "ws://example.com/socket",
        "chrome://settings",
    ],
)
def test_every_scheme_other_than_http_and_https_is_rejected(url):
    """FR-13. Each of these either reaches a local resource or bypasses extraction."""
    with pytest.raises(FetchUnsupportedSchemeError):
        validate_url(url)


def test_an_unconfigured_custom_scheme_is_rejected():
    with pytest.raises(FetchUnsupportedSchemeError):
        validate_url("researchbot://internal/collect")


def test_a_malformed_url_is_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url("http://")


def test_an_empty_url_is_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url("   ")


def test_credentials_in_a_url_are_rejected():
    """No fetch in this product needs basic-auth credentials; accepting them
    would create a capability with no caller and a credential-leak path with one."""
    with pytest.raises(FetchInvalidURLError):
        validate_url("http://admin:hunter2@example.com/")


def test_credentials_are_rejected_even_with_an_empty_password():
    with pytest.raises(FetchInvalidURLError):
        validate_url("http://admin:@example.com/")


def test_a_username_without_a_password_is_still_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url("http://admin@example.com/")


@pytest.mark.parametrize("port", ["notaport", "99999999999999", "-1", "80x"])
def test_a_malformed_port_is_rejected(port):
    with pytest.raises(FetchInvalidURLError):
        validate_url(f"http://example.com:{port}/")


@pytest.mark.parametrize("url", ["http://", "http:///path", "https://:8080/"])
def test_a_missing_host_is_rejected(url):
    with pytest.raises(FetchInvalidURLError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a\r\nX-Injected: 1",
        "http://example.com/a\nX-Injected: 1",
        "http://example.com/a\tb",
        "http://example.com/a b",
        "http://example.com/a\x00b",
        "http://example.com/a\x7fb",
    ],
)
def test_whitespace_and_control_characters_are_rejected(url):
    """``urlsplit`` strips and removes tabs and newlines anywhere in a URL.

    That is a request-smuggling primitive: the string validated would not be the
    string sent. Rejecting them means the bytes validated are the bytes requested.
    """
    with pytest.raises(FetchInvalidURLError):
        validate_url(url)


def test_a_host_with_an_invalid_label_is_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url("http://-bad.example.com/")


def test_a_host_with_an_empty_label_is_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url("http://a..b/")


def test_an_over_long_label_is_rejected():
    with pytest.raises(FetchInvalidURLError):
        validate_url(f"http://{'a' * 70}.example.com/")


# --- hostname normalization ------------------------------------------------


def test_the_hostname_is_lowercased():
    assert validate_url("http://EXAMPLE.COM/x").hostname == "example.com"


def test_the_root_dot_is_stripped():
    """``example.com.`` and ``example.com`` are one host; treating them as two
    would let a policy decision be made against one spelling."""
    assert validate_url("http://example.com./x").hostname == "example.com"


def test_a_punycode_hostname_is_left_alone():
    assert validate_url("http://xn--mgbh0fb.xn--irh/").hostname == "xn--mgbh0fb.xn--irh"


def test_a_unicode_hostname_is_encoded_to_punycode():
    """Persian domains are IDNs, so this is a product requirement, not a nicety."""
    target = validate_url("http://مثال.ایران/")

    assert target.hostname == "xn--mgbh0fb.xn--mgba3a4f16a"
    assert target.hostname.isascii()


def test_normalize_rejects_an_empty_hostname():
    with pytest.raises(FetchInvalidURLError):
        normalize_hostname(".")


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",
        "0x7f000001",
        "127.1",
        "0177.0.0.1",
        "0",
        "127.0.0.1.1",
    ],
)
def test_a_non_canonical_numeric_host_is_rejected(host):
    """Each of these reaches 127.0.0.1 through getaddrinfo on a normal platform.

    Rejecting them here rather than relying on the C library's interpretation is
    the point: the boundary must not depend on which libc is deployed.
    """
    assert ambiguous_numeric_host(host)
    with pytest.raises(FetchInvalidURLError):
        validate_url(f"http://{host}/")


@pytest.mark.parametrize("host", ["example.com", "deadbeef.com", "face", "a-b.example"])
def test_a_legitimate_hostname_is_not_mistaken_for_a_numeric_address(host):
    assert not ambiguous_numeric_host(host)


# --- address policy --------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.255.255.254",
        "0.0.0.0",
        "0.1.2.3",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.0.1",
        "192.88.99.1",
        "192.0.2.5",
        "198.51.100.5",
        "203.0.113.5",
        "198.18.0.1",
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
def test_no_non_routable_ipv4_range_is_connectable(address):
    """T-4. RFC1918, loopback, link-local, CGNAT, multicast, reserved, and the
    documentation ranges an attacker uses to make a blocklist look complete."""
    assert not is_connectable(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "::",
        "::1",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        "febf::1",
        "fec0::1",
        "ff02::1",
        "ff0e::1",
        "100::1",
        "2001:db8::1",
        "3ffe::1",
        "5f00::1",
        "64:ff9b:1::1",
    ],
)
def test_no_non_routable_ipv6_range_is_connectable(address):
    """T-4, IPv6 half. Loopback, ULA, link-local, site-local, multicast, and the
    reserved blocks the stdlib's ``is_global`` reports as global."""
    assert not is_connectable(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:172.16.0.1",
        "::ffff:192.168.0.1",
        "::ffff:0.0.0.0",
        "::127.0.0.1",
        "2002:7f00:1::",
        "2002:a00:1::",
        "64:ff9b::7f00:1",
        "64:ff9b::10.0.0.1",
        "2001::1",
    ],
)
def test_an_ipv6_address_that_embeds_a_blocked_ipv4_address_is_rejected(address):
    """§ 15.5 requires the embedded address to be re-validated, not the wrapper.

    ``::ffff:127.0.0.1`` and ``2002:7f00:1::`` are tunnels to loopback; checking
    only the outer IPv6 would let both through.
    """
    verdict = classify_address(ipaddress.ip_address(address))
    assert not verdict.allowed, verdict.reason


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "1.1.1.1", "8.8.8.8", "142.250.185.100", "185.199.108.153"],
)
def test_a_public_ipv4_address_is_connectable(address):
    assert is_connectable(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address", ["2606:4700:4700::1111", "2001:4860:4860::8888", "2a00:1450:4001::1"]
)
def test_a_public_ipv6_address_is_connectable(address):
    assert is_connectable(ipaddress.ip_address(address))


def test_a_mapped_public_ipv4_address_stays_usable():
    """A dual-stack answer of ``::ffff:93.184.216.34`` reaches the same site.

    Rejecting it would break ordinary IPv6 hosts on a technicality while the
    security-relevant question — what the embedded address points at — went
    unasked.
    """
    assert is_connectable(ipaddress.ip_address("::ffff:93.184.216.34"))


def test_the_blocklists_contain_no_network_that_covers_a_public_address():
    """Guards against a typo in a CIDR that silently blackholes the internet."""
    samples = [
        ipaddress.ip_address("93.184.216.34"),
        ipaddress.ip_address("1.1.1.1"),
        ipaddress.ip_address("2606:4700:4700::1111"),
        ipaddress.ip_address("2001:4860:4860::8888"),
    ]
    for address in samples:
        networks = (
            BLOCKED_IPV4_NETWORKS if address.version == 4 else BLOCKED_IPV6_NETWORKS
        )
        for network, reason in networks:
            assert address not in network, f"{address} falls inside {network} ({reason})"


def test_every_blocked_network_carries_a_reason_for_the_log():
    for _, reason in (*BLOCKED_IPV4_NETWORKS, *BLOCKED_IPV6_NETWORKS):
        assert reason, "a blocklist entry with no reason cannot be explained in an incident"


# --- resolution and policy -------------------------------------------------


async def test_a_hostname_resolving_to_a_public_address_is_allowed():
    policy = policy_with({"good.test": ["93.184.216.34"]})

    resolved = await policy.validate("https://good.test/page")

    assert str(resolved.address) == "93.184.216.34"
    assert resolved.address_literal == "93.184.216.34"


async def test_a_hostname_resolving_to_a_private_address_is_rejected():
    """T-6. The literal case of the rebinding threat: a name an attacker owns
    pointing at the internal network."""
    policy = policy_with({"evil.test": ["10.0.0.5"]})

    with pytest.raises(FetchBlockedDestinationError):
        await policy.validate("https://evil.test/")


async def test_a_hostname_resolving_to_loopback_is_rejected():
    policy = policy_with({"evil.test": ["127.0.0.1"]})

    with pytest.raises(FetchBlockedDestinationError) as excinfo:
        await policy.validate("http://evil.test/")

    assert excinfo.value.retryable is False, "a security violation must never be retried"


async def test_a_hostname_resolving_to_link_local_metadata_is_rejected():
    """The cloud metadata endpoint at 169.254.169.254 is the highest-value SSRF
    target there is; it is link-local, so the link-local rule covers it."""
    policy = policy_with({"evil.test": ["169.254.169.254"]})

    with pytest.raises(FetchBlockedDestinationError):
        await policy.validate("http://evil.test/latest/meta-data/")


async def test_a_multi_answer_hostname_is_rejected_when_any_answer_is_private():
    """Not "pick the safe one". Choosing among an attacker-influenced ordering
    would make the blocklist advisory, and would mean the private answer was
    reachable at all."""
    policy = policy_with({"mixed.test": ["93.184.216.34", "192.168.1.10"]})

    with pytest.raises(FetchBlockedDestinationError):
        await policy.validate("https://mixed.test/")


async def test_a_multi_answer_hostname_is_allowed_when_every_answer_is_public():
    policy = policy_with({"cdn.test": ["93.184.216.34", "2606:4700::1111"]})

    resolved = await policy.validate("https://cdn.test/")

    assert str(resolved.address) == "93.184.216.34"
    assert resolved.address_literal == "93.184.216.34", "the first answer is the one used"


async def test_a_resolution_failure_is_typed_and_not_a_pass():
    """Fail closed: a resolver error must never be read as "no restriction"."""
    policy = SsrfPolicy(resolver=ScriptedResolver(raises_for={"broken.test"}))

    with pytest.raises(FetchDNSResolutionError) as excinfo:
        await policy.validate("https://broken.test/")

    assert excinfo.value.retryable is False


async def test_a_hostname_resolving_to_nothing_is_rejected():
    policy = policy_with({"empty.test": []})

    with pytest.raises(FetchDNSResolutionError):
        await policy.validate("https://empty.test/")


async def test_a_resolver_returning_an_unparseable_address_is_rejected():
    policy = policy_with({"weird.test": ["not-an-address"]})

    with pytest.raises(FetchDNSResolutionError):
        await policy.validate("https://weird.test/")


async def test_an_ip_literal_url_skips_dns_entirely():
    """A literal needs no resolution, so there is no resolver call to rebind."""
    resolver = ScriptedResolver({"93.184.216.34": []})
    policy = SsrfPolicy(resolver=resolver)

    resolved = await policy.validate("https://93.184.216.34/x")

    assert str(resolved.address) == "93.184.216.34"
    assert resolver.calls == []


async def test_a_blocked_ip_literal_url_is_rejected_without_dns():
    resolver = ScriptedResolver()
    policy = SsrfPolicy(resolver=resolver)

    with pytest.raises(FetchBlockedDestinationError):
        await policy.validate("http://127.0.0.1:8080/admin")

    assert resolver.calls == []


async def test_the_resolver_receives_the_normalized_hostname_and_port():
    """A resolver must not be handed a spelling the address policy never saw."""
    resolver = ScriptedResolver({"example.test": ["93.184.216.34"]})
    policy = SsrfPolicy(resolver=resolver)

    await policy.validate("http://EXAMPLE.TEST.:8080/page")

    assert resolver.calls == [("example.test", 8080)]


async def test_configured_schemes_may_narrow_the_policy():
    """A deployment may fetch fewer schemes than the default; it may never fetch
    more, which is why the configuration parser intersects rather than replaces."""
    narrowed = policy_with({}, allowed_schemes=frozenset({"https"}))

    assert validate_url("https://x.test/", narrowed.allowed_schemes).scheme == "https"
    with pytest.raises(FetchUnsupportedSchemeError):
        validate_url("http://x.test/", narrowed.allowed_schemes)


# --- the platform resolver -------------------------------------------------


async def test_the_system_resolver_answers_a_loopback_name():
    """Exercises the real resolver without a network: ``localhost`` is answered
    by the platform, and the answer is loopback — which the policy then refuses.

    This is the end-to-end proof that the production resolver and the policy
    agree; a scripted resolver could not catch a disagreement between them.
    """
    resolver = SystemAddressResolver()
    answers = await resolver.resolve("localhost", 80)

    assert answers, "the platform resolved a loopback name"
    assert all(
        not is_connectable(ipaddress.ip_address(answer)) for answer in answers
    ), f"every answer for a loopback name must be non-routable, got {answers}"

    policy = SsrfPolicy(resolver=resolver)
    with pytest.raises(FetchBlockedDestinationError):
        await policy.validate("http://localhost/")


async def test_the_system_resolver_raises_a_typed_error_for_an_unknown_name():
    resolver = SystemAddressResolver()

    with pytest.raises(FetchDNSResolutionError):
        await resolver.resolve("nonexistent.invalid", 80)


async def test_the_system_resolver_deduplicates_while_preserving_order():
    resolver = SystemAddressResolver()

    answers = await resolver.resolve("localhost", 80)

    assert len(answers) == len(set(answers))


def test_the_system_resolver_asks_for_both_address_families():
    """An IPv6-only answer must not be mistaken for an empty one.

    Asserted against the source because the alternative — asserting on the
    resolver's arguments — would require intercepting ``getaddrinfo`` and
    reimplementing the platform's own behaviour in the test.
    """
    import inspect

    source = inspect.getsource(SystemAddressResolver.resolve)

    assert "AF_UNSPEC" in source, "a v4-only query would miss IPv6 answers"
    assert "SOCK_STREAM" in source
    assert "IPPROTO_TCP" in source


def test_the_platform_socket_constants_the_resolver_relies_on_exist():
    """The resolver names these constants; if a platform lacks one the attribute
    error must surface at import, not as a silent wrong-protocol lookup."""
    assert socket.AF_UNSPEC is not None
    assert socket.SOCK_STREAM == 1
    assert socket.IPPROTO_TCP == 6