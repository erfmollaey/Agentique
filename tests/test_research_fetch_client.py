"""The restricted HTTP client: redirects, limits, and connection safety (P3.3).

Covers mandatory Phase 3 cases T-5 (a redirect to a private address is rejected)
and T-8 (size and time limits are enforced), plus the credential, status, and
DNS-rebinding properties the frozen contract § 15.5 requires.

Determinism and the network
---------------------------

No test here reaches the internet or a real DNS server. Two mechanisms:

* A **recording transport** stands in for httpx's socket layer on every test that
  is about fetcher behaviour. It records the request httpx *would* have sent,
  which is exactly the object a real connection is built from.
* A **real loopback HTTP server** on two tests that are specifically about
  httpx's own connection behaviour, because those properties cannot be observed
  through a mock.

Proving the DNS-rebinding defence
---------------------------------

The invariant is not "DNS was called once". It is: **the address the socket
opens is the address the policy validated.** Two tests establish it, and they
compose:

* :func:`test_the_connection_target_is_the_validated_address_not_the_hostname`
  drives the real fetcher and asserts that the request handed to the transport
  has the validated IP as its URL host — never the hostname.
* :func:`test_a_pinned_request_reaches_a_real_socket_without_resolving_its_host`
  drives httpx's *real* transport against a real loopback server and asserts
  the socket was opened to the address in the URL while the ``Host`` header
  carried the hostname, with no DNS lookup of that hostname at all.

Together: the fetcher only ever emits a request whose URL host is a validated
address, and httpx connects to the URL host. There is no step at which the
hostname is resolved.
"""

from __future__ import annotations

import asyncio
import gzip
import zlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.domain.errors import (
    FetchBlockedDestinationError,
    FetchConnectionError,
    FetchDNSResolutionError,
    FetchHTTPStatusError,
    FetchInvalidURLError,
    FetchRedirectError,
    FetchResponseTooLargeError,
    FetchTimeoutError,
    FetchTooManyRedirectsError,
    FetchUnsupportedContentTypeError,
    FetchUnsupportedEncodingError,
    FetchUnsupportedSchemeError,
)
from app.domain.research.fetch import FetchRequest
from app.infrastructure.fetch.client import RestrictedFetcher
from app.infrastructure.fetch.ssrf import ResolvedTarget, SsrfPolicy, validate_url
from app.infrastructure.fetch.transport import _PinnedTransport, pin_request

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "2606:4700::1111"
PRIVATE_IP = "10.0.0.5"
LOOPBACK_IP = "127.0.0.1"

Handler = Callable[[httpx.Request], httpx.Response]


class ScriptedResolver:
    """A resolver whose answers can change between calls.

    ``script`` is a list of answer tuples handed out in order, the last one
    repeating. That is what makes a rebinding attack reproducible: call one sees
    the public answer, call two sees the private one. Every call is recorded.

    Pass ``by_host`` instead when the *identity* of the name matters rather than
    the call order — two different hostnames must be able to have different
    answers in the same test, which a positional script cannot express.
    """

    def __init__(
        self,
        script: list[tuple[str, ...]] | None = None,
        by_host: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._script = script or [(PUBLIC_IP,)]
        self._by_host = by_host or {}
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        if hostname in self._by_host:
            return self._by_host[hostname]
        index = min(len(self.calls) - 1, len(self._script) - 1)
        return self._script[index]


def recording_transport(
    handler: Handler, log: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    sent: list[httpx.Request] = log if log is not None else []

    def wrapped(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def html(body: str = "<html><body>ok</body></html>") -> Handler:
    """A handler returning a successful HTML page with the given body."""
    return lambda _r: httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=body.encode("utf-8"),
    )


def ok() -> httpx.Response:
    """The final response in a redirect chain, for handlers that branch on hop."""
    return html()(httpx.Request("GET", "https://placeholder.test/"))


def build_fetcher(
    settings,
    handler: Handler,
    *,
    resolver: ScriptedResolver | None = None,
    sent: list[httpx.Request] | None = None,
) -> RestrictedFetcher:
    return RestrictedFetcher(
        settings,
        policy=SsrfPolicy(resolver=resolver or ScriptedResolver()),
        transport=recording_transport(handler, sent),
    )





def redirecting(location: str, *, hops: int = 1) -> Handler:
    """A handler that redirects for its first ``hops`` calls, then serves a page.

    Counts its own calls rather than reading the request log, because the
    recording transport appends the request *before* calling the handler — so a
    handler that tests ``len(sent) == 0`` is already one too far along and returns
    the final page on what it thinks is the first hop.
    """
    state = {"calls": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= hops:
            return httpx.Response(302, headers={"location": location})
        return ok()

    return handler


def redirecting_set_cookie(set_cookie: str, location: str) -> Handler:
    """First hop sets a cookie and redirects; later hops serve a page."""

    state = {"calls": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(
                302,
                headers={"location": location, "set-cookie": set_cookie},
            )
        return ok()

    return handler


# --- the happy path and what it proves -------------------------------------


async def test_a_public_page_is_retrieved_with_its_metadata(settings):
    fetcher = build_fetcher(settings, html())

    result = await fetcher.fetch(FetchRequest(url="https://good.test/article"))

    assert result.final_url == "https://good.test/article"
    assert result.status_code == 200
    assert result.content_type == "text/html"
    assert result.charset == "utf-8"
    assert b"ok" in result.body
    assert result.redirect_chain == ("https://good.test/article",)
    assert result.was_redirected is False
    assert result.byte_count == len(result.body)
    await fetcher.aclose()


async def test_plain_text_is_accepted(settings):
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"plain"
        ),
    )

    result = await fetcher.fetch(FetchRequest(url="https://good.test/notes.txt"))

    assert result.content_type == "text/plain"
    assert result.body == b"plain"
    await fetcher.aclose()


async def test_persian_content_survives_the_fetch_unchanged(settings):
    """The product's primary language must not be mangled by the transport.

    The body stays bytes precisely so this is byte-exact; decoding happens at
    extraction, where the charset was preserved.
    """
    text = "<html><body><p>پژوهش دربارهٔ هوش مصنوعی</p></body></html>"

    fetcher = build_fetcher(settings, html(text))

    result = await fetcher.fetch(FetchRequest(url="https://good.test/fa"))

    assert result.body.decode("utf-8") == text
    assert "پژوهش" in result.body.decode("utf-8")
    await fetcher.aclose()


# --- DNS rebinding ---------------------------------------------------------


async def test_the_connection_target_is_the_validated_address_not_the_hostname(
    settings,
):
    """The core invariant, observed on the request httpx would send.

    The URL host is the validated IP. If the transport ever received the hostname
    here, the client would resolve it again and the whole defence would be
    theatre.
    """
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), sent=sent)

    await fetcher.fetch(FetchRequest(url="https://rebind.test/page"))

    assert len(sent) == 1
    assert sent[0].url.host == PUBLIC_IP
    assert sent[0].url.host != "rebind.test", (
        "if the hostname reached the transport, the client would resolve it again"
    )
    assert sent[0].headers["host"] == "rebind.test", (
        "the origin server must still see the hostname it is asked about"
    )
    assert sent[0].extensions.get("sni_hostname") == "rebind.test", (
        "TLS must negotiate and verify against the hostname, not the IP"
    )
    await fetcher.aclose()


async def test_dns_rebinding_does_not_reach_the_private_address(settings):
    """The attack, made deterministic.

    Resolution returns a public address the first time and a private one every
    time after — exactly what an attacker flipping a DNS record between our
    validation and our connection looks like. The fetch succeeds, and it
    succeeded by connecting to the address that was validated.
    """
    resolver = ScriptedResolver(script=[(PUBLIC_IP,), (PRIVATE_IP,), (LOOPBACK_IP,)])
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), resolver=resolver, sent=sent)

    result = await fetcher.fetch(FetchRequest(url="https://rebind.test/secret"))

    # The second and third answers exist in the script, so a re-resolution would
    # have found a private address and (correctly) been refused.
    assert len(resolver.calls) == 1, (
        "the hostname was resolved more than once; the rebinding window is open"
    )
    assert resolver.calls == [("rebind.test", 443)]
    assert sent[0].url.host == PUBLIC_IP
    assert PRIVATE_IP not in str(sent[0].url)
    assert LOOPBACK_IP not in str(sent[0].url)
    assert result.body == b"<html><body>ok</body></html>"
    await fetcher.aclose()


async def test_a_rebinding_name_that_resolves_private_the_first_time_is_rejected(
    settings,
):
    """The other direction: if the *validated* answer is private, it is refused
    outright, and no connection is attempted at all."""
    resolver = ScriptedResolver(script=[(PRIVATE_IP,)])
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), resolver=resolver, sent=sent)

    with pytest.raises(FetchBlockedDestinationError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://rebind.test/"))

    assert excinfo.value.retryable is False
    assert sent == [], "no request may be issued for a blocked destination"
    await fetcher.aclose()


async def test_every_hop_is_resolved_independently(settings):
    """Each redirect target gets its own resolution and its own pinned address.

    A public first hop cannot lend its validation to a second hop, and the second
    hop is not silently connected to the first hop's address.
    """
    resolver = ScriptedResolver(script=[(PUBLIC_IP,), (OTHER_PUBLIC_IP,)])
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        redirecting("https://second.test/next"),
        resolver=resolver,
        sent=sent,
    )

    await fetcher.fetch(FetchRequest(url="https://first.test/"))

    assert resolver.calls == [("first.test", 443), ("second.test", 443)]
    assert [request.url.host for request in sent] == [PUBLIC_IP, OTHER_PUBLIC_IP]
    assert [request.headers["host"] for request in sent] == ["first.test", "second.test"]
    await fetcher.aclose()


async def test_a_pinned_request_reaches_a_real_socket_without_resolving_its_host(
    settings,
):
    """The transport half of the composition, against a real loopback server.

    httpx is given a URL whose host is a literal address and a ``Host`` header
    naming a hostname that **does not exist in DNS**. If httpx resolved that
    hostname to connect, the request would fail with a resolution error. It
    succeeds, and the server records the ``Host`` header it was given — so the
    connection went to the address while the request was made in the hostname's
    name.

    This uses the private pinning function directly rather than the fetcher,
    because the fetcher's policy correctly refuses loopback and this test is not
    about the policy. It is about what httpx does with a pinned request, which no
    amount of mocking can demonstrate.
    """
    received: list[str] = []
    request_lines: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_lines.append((await reader.readline()).decode("latin-1").strip())
        headers: list[str] = []
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line.decode("latin-1").strip())
        received.extend(headers)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: 2\r\n\r\nhi"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        target = validate_url(f"http://does-not-exist.invalid:{port}/page")
        resolved = ResolvedTarget(target=target, address=_ip(LOOPBACK_IP))

        async with httpx.AsyncClient(
            transport=_PinnedTransport(
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                ssl_context=httpx.create_ssl_context(verify=True),
                retries=0,
            ),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            request = client.build_request("GET", target.canonical_url)
            pinned = pin_request(request, resolved)
            response = await client.send(pinned, stream=True)
            body = await response.aread()

    assert body == b"hi"
    assert request_lines[0].startswith("GET /page"), (
        "the origin server received the request path, so the connection worked"
    )
    host_headers = [h for h in received if h.lower().startswith("host:")]
    assert host_headers, f"no Host header reached the server: {received}"
    assert host_headers[0].split(":", 1)[1].strip() == f"does-not-exist.invalid:{port}", (
        "the request was made in the hostname's name, over a connection to the address"
    )


def _ip(text: str):
    import ipaddress

    return ipaddress.ip_address(text)


# --- redirects -------------------------------------------------------------


async def test_a_public_to_public_redirect_is_followed(settings):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PUBLIC_IP and request.url.path == "/start":
            return httpx.Response(301, headers={"location": "https://final.test/end"})
        return ok()

    fetcher = build_fetcher(settings, handler, sent=sent)

    result = await fetcher.fetch(FetchRequest(url="https://start.test/start"))

    assert result.status_code == 200
    assert result.final_url == "https://final.test/end"
    assert result.requested_url == "https://start.test/start"
    assert result.redirect_chain == (
        "https://start.test/start",
        "https://final.test/end",
    )
    assert result.was_redirected is True
    assert [request.url.host for request in sent] == [PUBLIC_IP, PUBLIC_IP]
    await fetcher.aclose()


async def test_a_relative_redirect_location_is_resolved(settings):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a/old":
            return httpx.Response(302, headers={"location": "new"})
        return ok()

    fetcher = build_fetcher(settings, handler, sent=sent)

    result = await fetcher.fetch(FetchRequest(url="https://site.test/a/old"))

    assert result.final_url == "https://site.test/a/new"
    await fetcher.aclose()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_redirect_status_is_followed(settings, status):
    """301, 302, 303, 307 and 308 all carry a ``Location`` and are followed.

    300 and 304 are excluded on purpose and tested separately: neither has a
    defined automatic-redirect meaning for a client that issues no conditional
    requests.
    """
    sent: list[httpx.Request] = []
    state = {"calls": 0}

    def hop(_r: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(status, headers={"location": "https://next.test/"})
        return ok()

    fetcher = build_fetcher(settings, hop, sent=sent)

    result = await fetcher.fetch(FetchRequest(url="https://start.test/"))

    assert result.status_code == 200
    assert result.final_url == "https://next.test/"
    assert len(sent) == 2
    await fetcher.aclose()


@pytest.mark.parametrize("status", [300, 304])
async def test_a_redirect_status_without_a_defined_meaning_is_refused(settings, status):
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(status, headers={"location": "https://next.test/"}),
        sent=sent,
    )

    with pytest.raises(FetchHTTPStatusError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://start.test/"))

    assert excinfo.value.status_code == status
    assert len(sent) == 1
    await fetcher.aclose()
    await fetcher.aclose()


async def test_a_redirect_to_loopback_is_rejected(settings):
    """T-5. The first half: the target is a loopback literal."""
    resolver = ScriptedResolver(script=[(PUBLIC_IP,)])
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}),
        resolver=resolver,
        sent=sent,
    )

    with pytest.raises(FetchBlockedDestinationError):
        await fetcher.fetch(FetchRequest(url="http://public.test/"))

    assert len(sent) == 1, "the blocked hop must never be requested"
    await fetcher.aclose()


async def test_a_redirect_to_a_private_literal_is_rejected(settings):
    """T-5. The second half: the classic cloud-metadata target."""
    resolver = ScriptedResolver(script=[(PUBLIC_IP,)])
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        ),
        resolver=resolver,
        sent=sent,
    )

    with pytest.raises(FetchBlockedDestinationError) as excinfo:
        await fetcher.fetch(FetchRequest(url="http://public.test/"))

    assert excinfo.value.retryable is False
    assert len(sent) == 1
    await fetcher.aclose()


async def test_a_redirect_to_a_private_hostname_is_rejected_after_resolution(settings):
    """T-5, the DNS half: the redirect target is a name, and the name is what
    makes this attack work. Validation must run on the new target, not only on
    the URL the fetcher started from."""
    resolver = ScriptedResolver(
        by_host={
            "public.test": (PUBLIC_IP,),
            "internal.test": (LOOPBACK_IP,),
        },
    )
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        redirecting("http://internal.test/secrets"),
        resolver=resolver,
        sent=sent,
    )

    with pytest.raises(FetchBlockedDestinationError):
        await fetcher.fetch(FetchRequest(url="http://public.test/"))

    assert [name for name, _ in resolver.calls] == ["public.test", "internal.test"]
    assert len(sent) == 1, "the private hop must never be requested"
    await fetcher.aclose()


@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd",
        "ftp://internal.test/x",
        "gopher://internal.test:70/_",
        "data:text/html,<h1>hi</h1>",
        "javascript:alert(1)",
        "mailto:someone@example.com",
    ],
)
async def test_a_redirect_to_an_unsupported_scheme_is_rejected(settings, location):
    """A redirect target goes through the same scheme allowlist as the original.

    ``Location: data:`` is also the case that proved the value of owning the
    transport: with an ``AsyncClient``, httpx parses and builds the redirect
    request regardless of ``follow_redirects`` and raises its own ``InvalidURL``
    from inside that machinery — an error type the caller would never see declared
    anywhere.

    The origin is ``http`` so this is exclusively a scheme-allowlist test and not
    also a downgrade test; the downgrade refusal has its own.
    """
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(302, headers={"location": location}),
        sent=sent,
    )

    with pytest.raises(FetchUnsupportedSchemeError):
        await fetcher.fetch(FetchRequest(url="http://public.test/"))

    assert len(sent) == 1, "the second hop must never be requested"
    await fetcher.aclose()


async def test_an_https_to_http_downgrade_redirect_is_refused(settings):
    """Hardening beyond the frozen contract, recorded as such.

    The contract requires revalidation at each hop; it does not require refusing
    a downgrade. An https source that redirects to http can have its content
    modified in transit, and a research answer built on it is not traceable.
    """
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(301, headers={"location": "http://plain.test/"}),
        sent=sent,
    )

    with pytest.raises(FetchRedirectError, match="downgrade"):
        await fetcher.fetch(FetchRequest(url="https://secure.test/"))

    assert len(sent) == 1, "the downgraded destination must never be contacted"
    await fetcher.aclose()


async def test_an_http_to_https_upgrade_redirect_is_followed(settings):
    """The opposite direction is fine: there is nothing to lose by upgrading."""
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings, redirecting("https://secure.test/"), sent=sent
    )

    result = await fetcher.fetch(FetchRequest(url="http://plain.test/"))

    assert result.final_url == "https://secure.test/"
    await fetcher.aclose()


async def test_a_redirect_loop_terminates_with_a_typed_error(settings):
    """``/a → /b → /a → …``.

    The hop limit alone would also terminate this, but only after repeating the
    work. Recording the URLs already fetched turns the cycle into an immediate
    typed rejection, which is both faster and unambiguous in the failure record.
    """
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "https://loop.test/b"})
        return httpx.Response(302, headers={"location": "https://loop.test/a"})

    fetcher = build_fetcher(settings, handler, sent=sent)

    with pytest.raises(FetchRedirectError, match="revisits"):
        await fetcher.fetch(FetchRequest(url="https://loop.test/a"))

    assert len(sent) == 2, "the third request would repeat the first"
    assert FetchRedirectError.retryable is False
    await fetcher.aclose()


async def test_a_self_redirect_loop_is_refused_immediately(settings):
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(302, headers={"location": "https://self.test/"}),
        sent=sent,
    )

    with pytest.raises(FetchRedirectError, match="revisits"):
        await fetcher.fetch(FetchRequest(url="https://self.test/"))

    assert len(sent) == 1
    await fetcher.aclose()


async def test_a_fragment_only_difference_is_not_counted_as_a_new_hop(settings):
    """``/a`` and ``/a#section`` are the same request to the server.

    Counting them as different visits would let a loop run all the way to the hop
    limit while never tripping the loop check, and would report "too many
    redirects" for what is really an infinite loop.
    """
    sent: list[httpx.Request] = []
    state = {"calls": 0}

    def hop(_r: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        target = "https://frag.test/a#section" if state["calls"] == 1 else "https://frag.test/a"
        return httpx.Response(302, headers={"location": target})

    fetcher = build_fetcher(settings, hop, sent=sent)

    with pytest.raises(FetchRedirectError, match="revisits"):
        await fetcher.fetch(FetchRequest(url="https://frag.test/a"))

    assert len(sent) == 1, (
        "the fragment variant is the same visit, so it is refused before requesting"
    )
    await fetcher.aclose()


async def test_a_redirect_chain_longer_than_the_limit_is_refused(settings, make_settings):
    resolver = ScriptedResolver()
    sent: list[httpx.Request] = []
    limited = make_settings(FETCH_MAX_REDIRECTS=2)

    def handler(request: httpx.Request) -> httpx.Response:
        step = len(sent)
        return httpx.Response(
            302, headers={"location": f"https://chain.test/{step + 1}"}
        )

    fetcher = build_fetcher(limited, handler, resolver=resolver, sent=sent)

    with pytest.raises(FetchTooManyRedirectsError):
        await fetcher.fetch(FetchRequest(url="https://chain.test/0"))

    assert len(sent) == 3, "two redirects allowed, the third request is refused"
    await fetcher.aclose()


async def test_a_redirect_limit_of_zero_refuses_to_follow_anything(settings, make_settings):
    sent: list[httpx.Request] = []
    zero = make_settings(FETCH_MAX_REDIRECTS=0)

    fetcher = build_fetcher(
        zero,
        lambda _r: httpx.Response(302, headers={"location": "https://next.test/"}),
        sent=sent,
    )

    with pytest.raises(FetchTooManyRedirectsError):
        await fetcher.fetch(FetchRequest(url="https://start.test/"))

    assert len(sent) == 1
    await fetcher.aclose()


async def test_a_redirect_without_a_location_is_a_typed_error(settings):
    fetcher = build_fetcher(settings, lambda _r: httpx.Response(302))

    with pytest.raises(FetchRedirectError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://start.test/"))

    assert "Location" in str(excinfo.value)
    await fetcher.aclose()


async def test_an_over_long_redirect_target_is_rejected(settings):
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            302, headers={"location": "https://next.test/" + "a" * 3000}
        ),
    )

    with pytest.raises(FetchInvalidURLError):
        await fetcher.fetch(FetchRequest(url="https://start.test/"))
    await fetcher.aclose()


# --- size limits -----------------------------------------------------------


async def test_a_response_under_the_limit_is_returned(settings, make_settings):
    small = make_settings(FETCH_MAX_RESPONSE_BYTES=1000)
    fetcher = build_fetcher(small, html("x" * 500))

    result = await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert result.byte_count == 500
    await fetcher.aclose()


async def test_a_response_exactly_at_the_limit_is_returned(settings, make_settings):
    """The documented boundary: the cap is inclusive.

    The limit is written in the setting description as "at most", so a body of
    exactly N bytes is within it. A limit that rejected its own boundary would be
    off by one in whichever direction nobody noticed until a real page of exactly
    that size failed to fetch.
    """
    body = "<html><body>ok</body></html>"
    exact = make_settings(FETCH_MAX_RESPONSE_BYTES=len(body))
    fetcher = build_fetcher(exact, html(body))

    result = await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert result.byte_count == len(body)
    await fetcher.aclose()


async def test_a_response_over_the_limit_is_refused(settings, make_settings):
    small = make_settings(FETCH_MAX_RESPONSE_BYTES=100)
    fetcher = build_fetcher(small, html("x" * 101))

    with pytest.raises(FetchResponseTooLargeError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert excinfo.value.retryable is False
    await fetcher.aclose()


async def test_a_lying_content_length_cannot_grant_permission(settings, make_settings):
    """Content-Length is advisory. A response that claims to be tiny and is not
    must be caught by the streaming bound, not by the header."""
    small = make_settings(FETCH_MAX_RESPONSE_BYTES=100)

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "12"},
            content=b"x" * 5000,
        )

    fetcher = build_fetcher(small, handler)

    with pytest.raises(FetchResponseTooLargeError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


async def test_a_chunked_response_without_content_length_is_bounded(
    settings, make_settings
):
    """No Content-Length at all — the common case for a streamed or generated page."""
    small = make_settings(FETCH_MAX_RESPONSE_BYTES=200)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(1000):
            yield b"y" * 64

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "transfer-encoding": "chunked"},
            content=chunks(),
        )

    fetcher = build_fetcher(small, handler)

    with pytest.raises(FetchResponseTooLargeError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


async def test_a_compressed_bomb_cannot_bypass_the_decoded_limit(
    settings, make_settings
):
    """The whole point of bounding the decoded stream.

    2 MB of zeros compresses to about 2 KB. The wire size is far inside the cap;
    the expanded size is far outside it. A limit applied to Content-Length would
    have let this through.
    """
    bomb = gzip.compress(b"\0" * 2_000_000)
    assert len(bomb) < 5000, "the fixture must actually be small on the wire"

    bounded = make_settings(FETCH_MAX_RESPONSE_BYTES=100_000)
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        bounded,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "gzip"},
            content=bomb,
        ),
        sent=sent,
    )

    with pytest.raises(FetchResponseTooLargeError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert "exceeded" in str(excinfo.value)
    await fetcher.aclose()


async def test_a_body_under_the_byte_cap_but_past_the_ratio_cap_is_refused(
    settings, make_settings
):
    """Second decompression defence.

    A payload can stay under the byte ceiling while expanding many times over.
    The ratio cap bounds that independently, so tightening the byte ceiling is
    not the only lever against an expansion attack.
    """
    payload = gzip.compress(b"abcdefgh" * 100_000)
    assert len(payload) < 100_000, "stays under the byte cap"

    strict = make_settings(FETCH_MAX_RESPONSE_BYTES=5_000_000, FETCH_MAX_COMPRESSION_RATIO=5)

    fetcher = build_fetcher(
        strict,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "gzip"},
            content=payload,
        ),
    )

    with pytest.raises(FetchResponseTooLargeError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert "compression ratio" in str(excinfo.value)
    await fetcher.aclose()


async def test_an_ordinary_compressed_page_is_returned(settings):
    """The ratio cap must not break normal content."""
    body = ("<html><body>" + "متن فارسی نمونه. " * 200 + "</body></html>").encode()

    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
            content=gzip.compress(body),
        ),
    )

    result = await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert result.body == body
    assert result.content_encoding == "gzip"
    await fetcher.aclose()


async def test_a_deflate_response_is_decoded(settings):
    body = b"<html><body>deflated</body></html>"
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)

    deflated = compressor.compress(body) + compressor.flush()

    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "deflate"},
            content=deflated,
        ),
    )

    result = await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert result.body == body
    await fetcher.aclose()


async def test_an_oversized_stream_is_terminated_without_buffering_it(
    settings, make_settings
):
    """Memory safety, not just correctness.

    The body is an unbounded generator. If the fetcher buffered before checking,
    this test would not terminate; because it checks per chunk, it stops early —
    and the chunk counter proves it stopped early rather than at the end.
    """
    small = make_settings(FETCH_MAX_RESPONSE_BYTES=1000)
    produced = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal produced
        while True:
            produced += 1
            yield b"z" * 128

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=endless()
        )

    fetcher = build_fetcher(small, handler)

    with pytest.raises(FetchResponseTooLargeError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert produced < 100, f"read {produced} chunks; the bound should stop it near 8"
    await fetcher.aclose()


async def test_a_corrupt_compressed_body_is_a_typed_error_not_a_traceback(settings):
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "gzip"},
            content=b"this is not gzip at all",
        ),
    )

    with pytest.raises((FetchConnectionError, FetchUnsupportedEncodingError)):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


# --- content type ----------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        "application/pdf",
        "image/png",
        "application/json",
        "application/octet-stream",
        "text/xml",
        "application/xhtml+xml",
    ],
)
async def test_an_unsupported_content_type_is_a_per_source_failure(
    settings, content_type
):
    """FR-17. A PDF or an image is not readable text, and pretending otherwise
    would push binary bytes into an extraction stage."""
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200, headers={"content-type": content_type}, content=b"payload"
        ),
    )

    with pytest.raises(FetchUnsupportedContentTypeError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/doc"))

    assert excinfo.value.retryable is False
    await fetcher.aclose()


async def test_a_content_type_with_parameters_is_matched_on_the_media_type(settings):
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "TEXT/HTML ; charset=UTF-8"},
            content=b"<html></html>",
        ),
    )

    result = await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert result.content_type == "text/html"
    assert result.charset == "utf-8"
    await fetcher.aclose()


async def test_a_response_without_a_content_type_is_refused(settings):
    """Fail closed: with no declaration there is no allowlist decision to make,
    and assuming "probably HTML" is how a binary payload reaches a text stage."""
    fetcher = build_fetcher(
        settings, lambda _r: httpx.Response(200, content=b"<html></html>")
    )

    with pytest.raises(FetchUnsupportedContentTypeError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "unknown-thing"])
async def test_an_undecodable_content_encoding_is_refused(settings, encoding):
    """``br`` is decodable only when the optional brotli package happens to be
    installed. A security boundary must not depend on that, so it is refused."""
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": encoding},
            content=b"\x00\x01\x02",
        ),
    )

    with pytest.raises(FetchUnsupportedEncodingError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


# --- HTTP status -----------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201, 204, 206])
async def test_a_2xx_response_is_source_content(settings, status):
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            status, headers={"content-type": "text/html"}, content=b"<html></html>"
        ),
    )

    result = await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert result.status_code == status
    await fetcher.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
async def test_a_4xx_response_is_never_returned_as_content(settings, status):
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            status,
            headers={"content-type": "text/html"},
            content=b"<html><body>Not found</body></html>",
        ),
    )

    with pytest.raises(FetchHTTPStatusError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/missing"))

    assert excinfo.value.status_code == status
    assert excinfo.value.retryable is False
    await fetcher.aclose()


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_a_transient_5xx_or_429_is_reported_as_retryable(settings, status):
    """The status is the whole decision: a 503 may pass shortly, a 404 never
    will, and the orchestrator at P3.9 needs to tell them apart."""
    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            status, headers={"content-type": "text/html"}, content=b"<html></html>"
        ),
    )

    with pytest.raises(FetchHTTPStatusError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert excinfo.value.retryable is True
    await fetcher.aclose()


async def test_an_error_page_body_is_never_read(settings):
    """The rejection happens on the status, before the body is consumed."""
    consumed = False

    async def body() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield b"<html>error</html>"

    fetcher = build_fetcher(
        settings,
        lambda _r: httpx.Response(
            503, headers={"content-type": "text/html"}, content=body()
        ),
    )

    with pytest.raises(FetchHTTPStatusError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert consumed is False
    await fetcher.aclose()


# --- timeouts --------------------------------------------------------------


async def test_a_connect_timeout_is_a_typed_fetch_timeout(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route to host", request=request)

    fetcher = build_fetcher(settings, handler)

    with pytest.raises(FetchTimeoutError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert excinfo.value.retryable is False
    await fetcher.aclose()


async def test_a_read_timeout_is_a_typed_fetch_timeout(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    fetcher = build_fetcher(settings, handler)

    with pytest.raises(FetchTimeoutError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


async def test_a_pool_timeout_is_a_typed_fetch_timeout(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool exhausted", request=request)

    fetcher = build_fetcher(settings, handler)

    with pytest.raises(FetchTimeoutError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


async def test_the_total_deadline_bounds_a_dribbling_body(
    settings, make_settings
):
    """Per-socket timeouts do not bound the operation.

    A server that sends one byte just inside the read timeout, forever, never
    trips a read timeout and never trips a connect timeout. Only a total deadline
    stops it, and that is what this test holds the fetcher to.
    """
    bounded = make_settings(
        FETCH_READ_TIMEOUT_SECONDS=0.5,
        FETCH_TOTAL_TIMEOUT_SECONDS=0.4,
        FETCH_CONNECT_TIMEOUT_SECONDS=0.5,
    )

    async def dribble() -> AsyncIterator[bytes]:
        while True:
            yield b"a"
            await asyncio.sleep(0.01)

    fetcher = build_fetcher(
        bounded,
        lambda _r: httpx.Response(
            200, headers={"content-type": "text/html"}, content=dribble()
        ),
    )

    with pytest.raises(FetchTimeoutError):
        await fetcher.fetch(FetchRequest(url="https://good.test/"))
    await fetcher.aclose()


async def test_the_configured_timeouts_reach_the_request(settings, make_settings):
    """The bounds must be real, not decorative.

    Asserted on the request httpcore will read its timeouts from, because a test
    that waits for a real timeout is slow and flaky — and the read timeout is
    already covered behaviourally above.
    """
    tuned = make_settings(
        FETCH_CONNECT_TIMEOUT_SECONDS=1.5, FETCH_READ_TIMEOUT_SECONDS=2.5
    )
    fetcher = RestrictedFetcher(
        tuned,
        policy=SsrfPolicy(resolver=ScriptedResolver()),
        transport=recording_transport(html()),
    )

    sent: list[httpx.Request] = []
    await fetcher.fetch(FetchRequest(url="https://good.test/"))

    class Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return ok()

    fetcher._transport = Recording()
    await fetcher.fetch(FetchRequest(url="https://good.test/again"))

    timeout = sent[0].extensions["timeout"]
    assert timeout == {"connect": 1.5, "read": 2.5, "write": 2.5, "pool": 1.5}
    await fetcher.aclose()


async def test_a_connection_refused_is_a_retryable_connection_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fetcher = build_fetcher(settings, handler)

    with pytest.raises(FetchConnectionError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert excinfo.value.retryable is True, (
        "a refused connection is the one fetch failure plausibly worth retrying"
    )
    await fetcher.aclose()


async def test_a_dns_failure_during_a_fetch_is_typed(settings):
    """A resolver that raises surfaces as the typed fetch error, not as an
    exception type the caller has never heard of."""
    fetcher = RestrictedFetcher(
        settings,
        policy=SsrfPolicy(resolver=_RaisingResolver()),
        transport=recording_transport(html()),
    )

    with pytest.raises(FetchDNSResolutionError) as excinfo:
        await fetcher.fetch(FetchRequest(url="https://broken.test/"))

    assert excinfo.value.retryable is False
    await fetcher.aclose()


class _RaisingResolver:
    async def resolve(self, hostname: str, port: int):
        raise FetchDNSResolutionError("hostname could not be resolved")


# --- credential containment (SR-8) -----------------------------------------


async def test_no_authorization_header_is_ever_sent(settings):
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), sent=sent)

    await fetcher.fetch(FetchRequest(url="https://good.test/"))

    assert sent[0].headers.get("authorization") is None
    assert sent[0].headers.get("proxy-authorization") is None
    await fetcher.aclose()


async def test_a_search_credential_never_reaches_a_fetched_destination(settings):
    """The end-to-end SR-8 property, across both HTTP clients.

    Settings carry a search subscription token. Nothing from the fetcher may put
    it on the wire to a fetched origin, in any header, in any hop.
    """
    token = "brave-secret-token-for-this-test"
    configured = settings.model_copy(update={"SEARCH_API_KEY": token})

    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        configured,
        redirecting("https://elsewhere.test/final"),
        sent=sent,
    )

    await fetcher.fetch(FetchRequest(url="https://origin.test/start"))

    assert len(sent) == 2, "the redirect was followed, so both hops are checked"
    for request in sent:
        rendered = str(request.url) + repr(request.headers.raw)
        assert token not in rendered


async def test_a_redirect_does_not_carry_credentials_to_the_new_origin(settings):
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, redirecting("https://other.test/"), sent=sent)

    await fetcher.fetch(FetchRequest(url="https://first.test/"))

    assert len(sent) == 2

    for request in sent:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


async def test_url_credentials_are_refused_before_any_connection(settings):
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), sent=sent)

    with pytest.raises(FetchInvalidURLError):
        await fetcher.fetch(
            FetchRequest(url="http://user:secret@example.com/")
        )

    assert sent == []
    await fetcher.aclose()


async def test_a_cookie_set_by_one_origin_is_not_sent_to_another(settings):
    """§ 15.5 requires no cross-fetch cookies.

    A hostile origin that can plant a session cookie on a subsequent request to a
    *different* origin has a cross-site request forgery primitive. With no client
    and therefore no cookie jar, there is nowhere for it to be stored.
    """
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(
        settings,
        redirecting_set_cookie("session=abc123; Path=/", "https://b.test/"),
        sent=sent,
    )

    await fetcher.fetch(FetchRequest(url="https://a.test/"))

    assert len(sent) == 2
    for request in sent:
        assert "cookie" not in request.headers
    assert not hasattr(fetcher, "_cookies")
    await fetcher.aclose()


async def test_the_fetcher_never_consults_a_proxy(settings, monkeypatch):
    """``HTTP(S)_PROXY`` must not be able to reroute a validated fetch.

    An environment variable that redirects a request to an attacker-chosen proxy
    hands that proxy the request and defeats the pinning. The fetcher therefore
    builds its transport with an explicit TLS context and never reads proxy
    configuration from anywhere, so there is no flag to assert — the guarantee is
    structural: there is no proxy lookup in the code path at all.

    Asserted by source inspection plus a live attempt, because a behavioural test
    alone could not distinguish "no proxy support" from "proxy support that
    happened not to fire".
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    fetcher = RestrictedFetcher(
        settings,
        policy=SsrfPolicy(resolver=ScriptedResolver()),
        transport=recording_transport(html()),
    )
    result = await fetcher.fetch(FetchRequest(url="http://good.test/"))

    assert result.status_code == 200, "the proxy was not consulted"

    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "infrastructure"
        / "fetch"
        / "client.py"
    ).read_text()
    assert "proxy" not in source.lower(), (
        "the fetch client must not contain any proxy lookup"
    )
    await fetcher.aclose()


# --- no bypass API ---------------------------------------------------------


def test_the_package_exposes_no_unvalidated_fetch_helper():
    """§ 17 of the brief: there must be no "fetch this URL for me" helper that
    skips the boundary. A reviewer should be able to see that the only public
    surface is a fetcher that always validates."""
    import app.infrastructure.fetch as package

    assert set(package.__all__) == {
        "RestrictedFetcher",
        "SsrfPolicy",
        "create_fetch_client",
    }
    for name in package.__all__:
        assert not name.startswith("get_unvalidated")
        assert "raw" not in name.lower()


def test_the_pinning_transport_is_private_to_its_package():
    """It must be unusable as a general-purpose HTTP transport: reaching it means
    importing a private name from the package that also holds the policy."""
    from app.infrastructure.fetch import transport as transport_module

    assert transport_module._PinnedTransport.__name__.startswith("_")
    assert not hasattr(transport_module, "PinnedTransport")
    assert issubclass(_PinnedTransport, httpx.AsyncHTTPTransport)


def test_the_search_client_is_not_the_fetch_client():
    """Two boundaries, two objects. Sharing one would put a credentialed client
    one attribute away from fetching an untrusted URL."""
    from app.infrastructure.fetch.client import RestrictedFetcher as Fetcher
    from app.infrastructure.search.brave import BraveSearchAdapter

    assert Fetcher is not BraveSearchAdapter
    assert "brave" not in RestrictedFetcher.__module__


def test_only_the_fetch_and_search_packages_import_httpx_for_research():
    """A grep-level check that the boundary is where it is claimed to be."""
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "app"
    importers = {
        path.relative_to(app).as_posix()
        for path in app.rglob("*.py")
        if "import httpx" in path.read_text()
    }

    assert importers == {
        "infrastructure/search/brave.py",
        "infrastructure/search/tavily.py",
        "infrastructure/fetch/client.py",
        "infrastructure/fetch/transport.py",
        # The DNS-over-HTTPS resolver (P3.3). A real HTTP boundary like the
        # others: it owns its own client and speaks a wire protocol to an
        # endpoint the deployment configured. Adding it here and nowhere else
        # is what keeps the guard's promise — that the set stays readable and
        # a new httpx import is a deliberate, recorded decision.
        "infrastructure/fetch/resolver.py",
    }, f"unexpected httpx importers: {sorted(importers)}"


def test_tls_verification_is_never_disabled():
    """No code path may turn certificate checking off to make pinning work.

    Parsed rather than grepped, so a *docstring* warning against ``verify=False``
    does not trip the check and thereby mask a real one. The failure this guards
    against is a future edit adding the flag, and reading a flag at construction
    time would not show it.
    """
    import ast

    forbidden_calls = {"_create_unverified_context", "_create_default_https_context"}
    fetch_dir = Path(__file__).resolve().parents[1] / "app" / "infrastructure" / "fetch"
    contexts_built = 0

    for path in fetch_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "CERT_NONE", f"{path.name}: {node.attr} disables TLS"
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in forbidden_calls, f"{path.name}: {name}() disables TLS"
            if name == "create_ssl_context":
                contexts_built += 1
                verify = next(
                    (kw.value for kw in node.keywords if kw.arg == "verify"), None
                )
                assert not (isinstance(verify, ast.Constant) and verify.value is False), (
                    f"{path.name}: create_ssl_context(verify=False)"
                )

    assert contexts_built, "no TLS context is built, so the check proved nothing"


async def test_the_pinned_transport_builds_a_context_with_verification_enabled():
    """Belt and braces on the same property: the actual context object."""
    import ssl

    from app.infrastructure.fetch.transport import build_ssl_context

    context = build_ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


# --- construction and configuration ----------------------------------------


async def test_a_configuration_naming_a_forbidden_scheme_refuses_to_build(settings):
    from app.infrastructure.fetch import create_fetch_client

    with pytest.raises(ValueError, match="narrow"):
        create_fetch_client(settings.model_copy(update={"FETCH_ALLOWED_SCHEMES": "file,http"}))


async def test_a_configuration_naming_only_https_builds_an_https_only_fetcher(
    settings,
):
    from app.infrastructure.fetch import create_fetch_client

    fetcher = create_fetch_client(
        settings.model_copy(update={"FETCH_ALLOWED_SCHEMES": "https"})
    )

    assert fetcher._policy.allowed_schemes == frozenset({"https"})
    await fetcher.aclose()


async def test_an_empty_content_type_allowlist_refuses_to_build(settings):
    from app.infrastructure.fetch import create_fetch_client

    with pytest.raises(ValueError, match="at least one"):
        create_fetch_client(
            settings.model_copy(update={"FETCH_ALLOWED_CONTENT_TYPES": " , "})
        )


@pytest.mark.parametrize(
    "field",
    [
        "FETCH_MAX_RESPONSE_BYTES",
        "FETCH_CONNECT_TIMEOUT_SECONDS",
        "FETCH_READ_TIMEOUT_SECONDS",
        "FETCH_TOTAL_TIMEOUT_SECONDS",
        "FETCH_MAX_COMPRESSION_RATIO",
        "FETCH_MAX_CONNECTIONS",
    ],
)
def test_a_fetch_limit_must_be_positive(settings, field):
    """A non-positive bound would make the fetcher unusable at best.

    Validation lives on the ``Settings`` field, so it is asserted there rather
    than by constructing a fetcher: the fetcher's job is to trust an
    already-validated limit, and duplicating the check would create two places
    where a limit is enforced.

    ``model_copy`` bypasses validation by design, so this re-validates through
    ``model_validate`` — which is what ``Settings()`` would do from the
    environment.
    """
    with pytest.raises(ValidationError):
        settings.model_validate({**settings.model_dump(), field: 0})


@pytest.mark.parametrize(
    "field", ["FETCH_MAX_REDIRECTS", "FETCH_MAX_KEEPALIVE_CONNECTIONS"]
)
def test_a_fetch_cap_may_be_zero(settings, field):
    """Zero is meaningful for a cap: "follow no redirects", "keep no idle
    connections". Only negatives are nonsense, so zero must be accepted."""
    tuned = settings.model_validate({**settings.model_dump(), field: 0})

    assert getattr(tuned, field) == 0


def test_the_redirect_limit_has_a_ceiling(settings):
    """A very large redirect limit is not a generous default, it is an unbounded
    number of SSRF validation rounds per source."""
    with pytest.raises(ValidationError):
        settings.model_validate({**settings.model_dump(), "FETCH_MAX_REDIRECTS": 1000})


async def test_the_fetcher_sends_only_documentation_header_fields(settings):
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), sent=sent)

    await fetcher.fetch(FetchRequest(url="https://good.test/"))

    headers = {key.lower() for key in sent[0].headers}
    assert "user-agent" in headers
    assert "accept" in headers
    assert "accept-encoding" in headers
    assert "authorization" not in headers
    assert "cookie" not in headers
    await fetcher.aclose()


async def test_the_fetcher_advertises_only_gzip_and_deflate(settings):
    """The advertised encodings must match what this client can actually decode,
    rather than what optional packages happen to be installed."""
    sent: list[httpx.Request] = []
    fetcher = build_fetcher(settings, html(), sent=sent)

    await fetcher.fetch(FetchRequest(url="https://good.test/"))

    advertised = sent[0].headers["accept-encoding"].lower()
    assert advertised == "gzip, deflate"
    await fetcher.aclose()


async def test_an_injected_transport_is_not_closed_by_the_fetcher(settings):
    """Ownership rule, same as the search adapter: the fetcher closes only what
    it built.

    Observed by using the transport afterwards rather than by reading a flag: a
    transport that had been closed would raise, so a working request *is* the
    evidence.
    """
    transport = recording_transport(html())
    fetcher = RestrictedFetcher(
        settings,
        policy=SsrfPolicy(resolver=ScriptedResolver()),
        transport=transport,
    )

    await fetcher.aclose()

    response = await transport.handle_async_request(
        httpx.Request("GET", "https://placeholder.test/")
    )
    assert await response.aread() == b"<html><body>ok</body></html>"


async def test_a_transport_the_fetcher_built_is_closed_with_it(settings):
    owned = RestrictedFetcher(settings)

    await owned.aclose()
    await owned.aclose()  # idempotent: closing twice must not raise


def test_the_fetcher_holds_no_per_fetch_state():
    """A fetcher that remembered the last hostname could carry one hop's state
    into the next; a fetcher that remembered the last body could leak one source
    into another's result.

    Asserted over the source of :meth:`RestrictedFetcher.fetch`, because the
    alternative — introspecting ``__dict__`` after a fetch — would only see the
    attributes that happened to be assigned, not the one that is not.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "infrastructure"
        / "fetch"
        / "client.py"
    )
    text = source.read_text().split("class RestrictedFetcher", 1)[1]
    fetch_method = text.split("    async def fetch(", 1)[1].split("    async def aclose", 1)[0]

    for forbidden in ("self._last", "self._current", "self._body", "self._visited"):
        assert forbidden not in fetch_method, (
            f"{forbidden} would persist state across fetches"
        )

    # Nothing in fetch() may write an instance attribute at all. The only state a
    # fetch needs — current URL, hop count, visited set, accumulated body — is
    # local, so it cannot outlive the call.
    import ast

    tree = ast.parse(source.read_text())
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RestrictedFetcher"
    )
    fetch_node = next(
        node
        for node in klass.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "fetch"
    )
    assignments = [
        node
        for node in ast.walk(fetch_node)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            for target in node.targets
        )
    ]
    assert assignments == [], "fetch() must not assign to self"