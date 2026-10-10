"""The restricted HTTP client (Phase 3, P3.3 — AD-031).

This is the only thing in the repository that opens a socket to a URL. It
implements :class:`app.domain.research.ports.SafeFetcher` and enforces the
frozen SSRF contract (§ 15.5) end to end:

```text
  SearchResult.url  (untrusted)
        ↓  validate_url          — scheme, credentials, authority, hostname
        ↓  resolve_and_validate  — every resolved address, or reject
        ↓  pin_request           — dial the validated IP; Host/SNI keep the name
        ↓  stream + bound        — decompressed byte ceiling, hard deadline
   FetchedSource
```

Why this talks to a transport, not to an ``httpx.Client``
--------------------------------------------------------

The fetcher calls ``transport.handle_async_request`` directly rather than
``AsyncClient.send``. That is not a micro-optimisation; it removes three pieces
of client behaviour that are each a hazard at this boundary:

**Redirect following.** ``follow_redirects=False`` stops httpx from *sending* a
redirect request, but httpx still parses ``Location`` and builds the next request
before it decides not to send it. That parse is not free of consequences: a
``Location: data:text/html,<h1>hi</h1>`` makes httpx raise ``InvalidURL`` from
inside the redirect machinery, and a ``Location`` the fetcher would have rejected
outright is nonetheless interpreted by a library that has no policy about it. This
was observed while writing the tests, not theorised. Owning the transport means
there is no redirect machinery to misfire.

**Authentication.** httpx runs an auth flow around every send. We have no
credentials, so the flow has nothing to do — but "nothing to do" is a property of
current configuration, not of the code, and a future ``auth=`` would be one
keyword away from leaking to a fetched origin.

**Cookies.** httpx extracts ``Set-Cookie`` from every response into a jar on the
client. § 15.5 requires "no cross-fetch cookies". With no client there is no jar.

What remains is stock: httpx's connection pool, TLS context, and httpcore
exception mapping, all reached through the same transport the client would have
used. Timeouts travel as the request's ``timeout`` extension, which httpcore
already reads, so they are unchanged.

Redirects, size, and decompression
---------------------------------

Redirects are followed by hand, one hop at a time, and every hop repeats the
*whole* validation — scheme, credentials, hostname, resolution, address policy. A
hop that fails is rejected, not "not followed". The loop also refuses to revisit
a URL it has already fetched, so ``A → B → A`` terminates immediately rather than
after repeating the work. See :class:`RestrictedFetcher` for the per-hop details.

The byte ceiling applies to the **decoded** stream while it is read
(``response.aiter_bytes()``) — not to ``Content-Length``, which is advisory,
absent for chunked responses, and describes the *compressed* size. A
decompression bomb therefore hits the same wall as an honest oversized document,
and the check runs before each chunk is appended, so memory never exceeds the
cap by more than one chunk.

Timeout policy
--------------

Three explicit bounds: connect, read, and a total deadline for the whole hop
including body consumption, enforced with ``asyncio.timeout``. Per-socket
timeouts alone are not enough — a server dribbling one byte just inside the read
timeout holds the connection open indefinitely.

This stage bounds *a single fetch*. The research run's own deadline and the
Celery worker's time limit remain P3.9's problem (AD-035, § 15.11); nothing here
pretends to solve it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from httpx._client import BoundAsyncStream

from app.core.config import Settings
from app.domain.errors import (
    FetchConnectionError,
    FetchHTTPStatusError,
    FetchInvalidURLError,
    FetchRedirectError,
    FetchResponseTooLargeError,
    FetchTimeoutError,
    FetchTooManyRedirectsError,
    FetchUnsupportedContentTypeError,
    FetchUnsupportedEncodingError,
)
from app.domain.research.fetch import FetchedSource, FetchRequest
from app.infrastructure.fetch.ssrf import (
    DEFAULT_ALLOWED_SCHEMES,
    ResolvedTarget,
    SsrfPolicy,
)
from app.infrastructure.fetch.transport import _PinnedTransport, pin_request

log = logging.getLogger(__name__)

#: Statuses that carry a ``Location`` and are followed. 300 (Multiple Choices)
#: and 304 (Not Modified) are excluded: 300 has no defined automatic behaviour
#: and 304 is conditional-request machinery this client never uses. Following
#: either would be inventing policy the contract does not describe.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Content encodings this client accepts. httpx decodes ``gzip`` and ``deflate``
#: itself; ``br`` and ``zstd`` are decodable only when the optional
#: ``brotli``/``zstandard`` packages happen to be installed — exactly the kind of
#: environment-dependent behaviour a security boundary must not have. An encoding
#: outside this set is refused rather than silently returned still compressed.
ALLOWED_CONTENT_ENCODINGS = frozenset({"identity", "gzip", "deflate"})

#: Longest URL this client will attempt, matching the domain's own bound.
_MAX_URL_CHARS = 2048


@dataclass(frozen=True, slots=True)
class FetchLimits:
    """The bounded-resource policy for one hop, every value from configuration.

    Frozen so a limit cannot change mid-fetch; a limit editable between the check
    and the read would not be a limit.
    """

    connect_timeout: float
    read_timeout: float
    total_timeout: float
    max_bytes: int
    max_compression_ratio: int
    max_redirects: int

    def as_timeout(self) -> httpx.Timeout:
        """The per-socket bounds, as httpcore reads them.

        ``write`` mirrors ``read`` because this client issues GETs with no body,
        and ``pool`` mirrors ``connect`` because acquiring a connection slot is
        the same wait.
        """
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )


def _parse_content_type(raw: str) -> tuple[str, str | None]:
    """Split a ``Content-Type`` into ``(media type, charset)``, both lowercased.

    Parameters other than ``charset`` are dropped: they are irrelevant to the
    decision, and keeping them would invite a caller to compare whole strings.
    """
    segments = [segment.strip() for segment in raw.split(";")]
    media_type = segments[0].lower() if segments and segments[0] else ""
    charset: str | None = None
    for segment in segments[1:]:
        name, _, value = segment.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"').lower() or None
    return media_type, charset


def _content_encoding_tokens(response: httpx.Response) -> tuple[str, ...]:
    """Every ``Content-Encoding`` token on the response, lowercased.

    The header may repeat and may list several codings (``gzip, br``), and httpx
    applies decoders in order. Rather than reason about which combinations are
    valid, the full token list is checked against the allowlist: any unsupported
    token means "we cannot have decoded this correctly", which must not be
    allowed to look like plain text.
    """
    tokens: list[str] = []
    for value in response.headers.get_list("content-encoding"):
        tokens.extend(
            token.strip().lower() for token in value.split(",") if token.strip()
        )
    return tuple(tokens)


def _redact(url: str) -> str:
    """A URL safe to log: scheme, host, port, path. Never query or userinfo.

    The query string is where signed-URL tokens, session identifiers, and
    provider credentials appear, and a fetched URL must never be written to a log
    in full (SR-7, SR-8). The path is kept because it is what distinguishes one
    page of a site from another during an incident.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "unparseable-url"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _visit_key(url: str) -> str:
    """Identity for redirect-loop detection: the URL without its fragment.

    A fragment is never sent to a server, so ``/a`` and ``/a#top`` are the same
    fetch. Counting them as different would let a loop run to the hop limit
    without ever tripping the visit check.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def configured_schemes(settings: Settings) -> frozenset[str]:
    """The configured scheme allowlist, intersected with what may be fetched.

    The intersection is the point: configuration may *narrow* what is fetched but
    must never widen it past HTTP and HTTPS. Naming ``file`` raises at
    construction rather than silently permitting it.
    """
    configured = frozenset(
        part.strip().lower()
        for part in settings.FETCH_ALLOWED_SCHEMES.split(",")
        if part.strip()
    )
    if not configured:
        raise ValueError("FETCH_ALLOWED_SCHEMES must name at least one scheme")
    unsupported = configured - DEFAULT_ALLOWED_SCHEMES
    if unsupported:
        raise ValueError(
            "FETCH_ALLOWED_SCHEMES may only narrow the permitted schemes; "
            f"unsupported: {sorted(unsupported)}"
        )
    return configured


def configured_content_types(settings: Settings) -> frozenset[str]:
    """The media types this deployment processes, from configuration."""
    allowed = frozenset(
        part.strip().lower()
        for part in settings.FETCH_ALLOWED_CONTENT_TYPES.split(",")
        if part.strip()
    )
    if not allowed:
        raise ValueError("FETCH_ALLOWED_CONTENT_TYPES must name at least one type")
    return allowed


class RestrictedFetcher:
    """Implements :class:`app.domain.research.ports.SafeFetcher`.

    Constructed once and reused, so the connection pool and TLS context are
    shared. No per-fetch state lives on ``self``: a fetcher that remembered the
    last hostname would be a fetcher that could carry one hop's state into the
    next.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        policy: SsrfPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._limits = FetchLimits(
            connect_timeout=settings.FETCH_CONNECT_TIMEOUT_SECONDS,
            read_timeout=settings.FETCH_READ_TIMEOUT_SECONDS,
            total_timeout=settings.FETCH_TOTAL_TIMEOUT_SECONDS,
            max_bytes=settings.FETCH_MAX_RESPONSE_BYTES,
            max_compression_ratio=settings.FETCH_MAX_COMPRESSION_RATIO,
            max_redirects=settings.FETCH_MAX_REDIRECTS,
        )
        self._allowed_content_types = configured_content_types(settings)
        self._policy = policy or SsrfPolicy(allowed_schemes=configured_schemes(settings))

        # Built per request, never reused across hops: a default header set on a
        # shared client could carry one hop's Host into the next, and a client is
        # where a cookie jar and an auth flow would live.
        self._default_headers = {
            "User-Agent": settings.FETCH_USER_AGENT,
            "Accept": ", ".join(sorted(self._allowed_content_types)),
            # Named explicitly rather than accepting httpx's default, which varies
            # with which optional compression packages are installed.
            "Accept-Encoding": "gzip, deflate",
        }

        self._transport: httpx.AsyncBaseTransport = (
            transport
            if transport is not None
            else _PinnedTransport(
                limits=httpx.Limits(
                    max_connections=settings.FETCH_MAX_CONNECTIONS,
                    max_keepalive_connections=settings.FETCH_MAX_KEEPALIVE_CONNECTIONS,
                    keepalive_expiry=settings.FETCH_KEEPALIVE_EXPIRY_SECONDS,
                ),
                # Verification on. There is no configuration path that turns it
                # off, and a test greps the package to keep it that way.
                ssl_context=httpx.create_ssl_context(verify=True),
                retries=0,
            )
        )
        self._owns_transport = transport is None

    # --- port ------------------------------------------------------------

    async def fetch(self, request: FetchRequest) -> FetchedSource:
        """Retrieve one untrusted URL, or raise a typed ``FetchError``.

        Never raises anything else: transport exceptions are translated where
        they occur, so a caller branches on the failure category without
        inspecting messages (FR-43).
        """
        started = time.perf_counter()
        requested = request.url
        log.info("fetch: start host=%s", _redact(requested))

        current_url = requested
        chain: list[str] = []
        visited: set[str] = set()
        hops = 0

        while True:
            if hops > self._limits.max_redirects:
                log.warning("fetch: redirect limit exceeded host=%s", _redact(current_url))
                raise FetchTooManyRedirectsError(
                    f"redirect limit of {self._limits.max_redirects} exceeded"
                )

            key = _visit_key(current_url)
            if key in visited:
                log.warning("fetch: redirect loop host=%s", _redact(current_url))
                raise FetchRedirectError("redirect chain revisits a URL it already fetched")
            visited.add(key)

            # --- the entire SSRF boundary, on every hop --------------------
            resolved = await self._policy.validate(current_url)
            chain.append(resolved.target.canonical_url)

            # The total deadline spans *both* the send and the body read. A
            # deadline around the send alone would be decorative: a server that
            # answers promptly and then dribbles the body one byte at a time
            # would hold the connection open indefinitely, which is exactly the
            # case a read timeout does not catch.
            try:
                async with asyncio.timeout(self._limits.total_timeout):
                    response = await self._send(resolved)

                    if response.status_code in REDIRECT_STATUSES:
                        hops += 1
                        location = response.headers.get("location")
                        await response.aclose()
                        if not location or not location.strip():
                            raise FetchRedirectError(
                                "redirect response carried no usable Location"
                            )
                        next_url = self._next_url(resolved, location)
                        self._check_downgrade(resolved, next_url)
                        current_url = next_url
                        log.info(
                            "fetch: redirect host=%s status=%s hops=%s",
                            _redact(current_url),
                            response.status_code,
                            hops,
                        )
                        continue

                    try:
                        self._check_response(response)
                        body = await self._read_bounded(response, resolved)
                    finally:
                        await response.aclose()
            except TimeoutError as exc:
                log.warning(
                    "fetch: total deadline exceeded host=%s total=%ss",
                    _redact(resolved.target.canonical_url),
                    self._limits.total_timeout,
                )
                raise FetchTimeoutError("fetch exceeded its time limit") from exc

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            media_type, charset = _parse_content_type(
                response.headers.get("content-type", "")
            )
            log.info(
                "fetch: done host=%s status=%s bytes=%s hops=%s elapsed_ms=%s",
                _redact(resolved.target.canonical_url),
                response.status_code,
                len(body),
                hops,
                elapsed_ms,
            )
            return FetchedSource(
                requested_url=requested,
                final_url=resolved.target.canonical_url,
                status_code=response.status_code,
                content_type=media_type,
                content_encoding=response.headers.get("content-encoding"),
                charset=charset,
                body=body,
                redirect_chain=tuple(chain),
                elapsed_ms=elapsed_ms,
            )

    async def aclose(self) -> None:
        """Close the transport this fetcher created.

        Only closes a transport it built. An injected one belongs to whoever
        injected it, which is what keeps tests honest about ownership.
        """
        if self._owns_transport:
            await self._transport.aclose()

    # --- internals ------------------------------------------------------

    def _next_url(self, resolved: ResolvedTarget, location: str) -> str:
        """Resolve one ``Location`` against the URL that produced it.

        A relative ``Location`` is legal and common, so it is resolved rather
        than rejected. Nothing else is decided here: what the next candidate is,
        yes; whether it is allowed, no — that is the next iteration's full
        validation.
        """
        try:
            joined = urljoin(resolved.target.canonical_url, location.strip())
        except ValueError as exc:
            raise FetchRedirectError("redirect Location is not a usable URL") from exc
        if not joined.strip():
            raise FetchRedirectError("redirect response carried an empty Location")
        if len(joined) > _MAX_URL_CHARS:
            raise FetchInvalidURLError("redirect target is too long")
        return joined

    def _check_downgrade(self, current: ResolvedTarget, next_url: str) -> None:
        """Refuse an ``https → http`` redirect.

        Hardening beyond the frozen contract, recorded as such: § 15.5 requires
        each hop to be revalidated but does not forbid a downgrade. Following one
        would mean the bytes that reach the research record can be modified in
        transit, and an answer citing them is not traceable to the source the user
        was shown.

        Decided before the next hop is validated or requested, so a downgraded
        destination is never contacted.
        """
        next_scheme = urlsplit(next_url).scheme.lower()
        if current.target.scheme == "https" and next_scheme == "http":
            log.warning("fetch: refused https to http downgrade")
            raise FetchRedirectError(
                "redirect downgrades from https to http, which is not permitted"
            )

    def _check_response(self, response: httpx.Response) -> None:
        """Reject anything that is not a 2xx, before a body byte is read.

        An error page is not source content. Reading it and leaving the decision
        to the caller would let a 404 page be presented as retrieved evidence,
        which is the dishonesty SR-12 forbids.
        """
        status = response.status_code
        if not 200 <= status <= 299:
            log.warning("fetch: rejected status=%s", status)
            raise FetchHTTPStatusError.for_status(status)

        for token in _content_encoding_tokens(response):
            if token not in ALLOWED_CONTENT_ENCODINGS:
                log.warning("fetch: unsupported content encoding %s", token)
                raise FetchUnsupportedEncodingError(
                    f"content encoding {token!r} is not supported by this fetcher"
                )

        raw_type = response.headers.get("content-type", "")
        if not raw_type.strip():
            # No declaration means no allowlist decision is possible. Refusing is
            # fail-closed; assuming "probably HTML" is how a binary payload
            # reaches a text pipeline.
            log.warning("fetch: response declared no content type")
            raise FetchUnsupportedContentTypeError("response declared no content type")
        media_type, _ = _parse_content_type(raw_type)
        if media_type not in self._allowed_content_types:
            log.warning("fetch: unsupported content type %s", media_type)
            raise FetchUnsupportedContentTypeError(
                f"content type {media_type!r} is not processed by this pipeline"
            )

    async def _send(self, resolved: ResolvedTarget) -> httpx.Response:
        """One hop: build the request, pin it, and hand it to the transport.

        The total deadline is applied by the caller around both this call and the
        body read; only the per-socket bounds are translated here.
        """
        request = self._build_request(resolved)
        try:
            response = await self._transport.handle_async_request(request)
        except (TimeoutError, httpx.TimeoutException) as exc:
            log.warning("fetch: timeout host=%s", resolved.target.hostname)
            raise FetchTimeoutError("fetch exceeded its time limit") from exc
        except httpx.HTTPError as exc:
            # Transport failure: refused, reset, TLS handshake failure. The httpx
            # text can embed the URL, so it is not propagated.
            log.warning("fetch: connection failed host=%s", resolved.target.hostname)
            raise FetchConnectionError("could not establish a connection") from exc

        # httpcore returns a response whose stream is the raw network stream.
        # ``AsyncClient`` normally wraps it in ``BoundAsyncStream`` so that
        # ``aiter_bytes()`` reports elapsed time and ``num_bytes_downloaded`` is
        # tracked; the client does that in ``_send_single_request``, which this
        # fetcher bypasses on purpose. Re-wrapping here keeps the read path
        # identical to a client's — in particular the byte counter the
        # compression-ratio check reads.
        #
        # httpcore's stream is statically ``SyncByteStream | AsyncByteStream``;
        # the transport used here is async-only, so the async branch always
        # holds. Asserting keeps mypy honest without an unchecked cast.
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = BoundAsyncStream(
            response.stream, response=response, start=time.perf_counter()
        )
        return response

    def _build_request(self, resolved: ResolvedTarget) -> httpx.Request:
        """One fully determined request for one validated destination.

        Built per hop and never reused: no client-level default headers to leak
        between hops, no cookie jar, no auth flow. The timeout travels as the
        request's ``timeout`` extension, which httpcore reads — the same
        mechanism ``AsyncClient`` uses.
        """
        timeout = self._limits.as_timeout()
        extensions = {"timeout": timeout.as_dict()}
        request = httpx.Request(
            "GET",
            resolved.target.canonical_url,
            headers=self._default_headers,
            extensions=extensions,
        )
        return pin_request(request, resolved)

    async def _read_bounded(
        self, response: httpx.Response, resolved: ResolvedTarget
    ) -> bytes:
        """Consume the decoded body incrementally, refusing to exceed the cap.

        The check runs on every chunk *before* it is appended, so memory never
        exceeds the cap by more than one chunk and a response of unbounded length
        is terminated rather than buffered.

        ``resolved`` is passed rather than read off the response because a
        response's ``.request`` is populated by ``AsyncClient``, which this
        fetcher deliberately does not use — reaching through the response for a
        URL would reintroduce exactly the coupling being avoided.
        """
        limit = self._limits.max_bytes
        collected = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                if len(collected) + len(chunk) > limit:
                    log.warning(
                        "fetch: body exceeded limit host=%s limit=%s",
                        resolved.target.hostname,
                        limit,
                    )
                    raise FetchResponseTooLargeError(
                        f"response body exceeded {limit} bytes"
                    )
                collected.extend(chunk)
        except FetchResponseTooLargeError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            log.warning("fetch: timeout while reading body host=%s", resolved.target.hostname)
            raise FetchTimeoutError("reading the response body timed out") from exc
        except (httpx.HTTPError, httpx.DecodingError, zlib.error) as exc:
            log.warning("fetch: body read failed host=%s", resolved.target.hostname)
            raise FetchConnectionError("the response could not be read") from exc

        ratio = len(collected) / self._wire_bytes(response)
        if ratio > self._limits.max_compression_ratio:
            log.warning(
                "fetch: compression ratio %s over limit host=%s",
                round(ratio, 1),
                resolved.target.hostname,
            )
            raise FetchResponseTooLargeError(
                "response expanded beyond the permitted compression ratio"
            )
        return bytes(collected)

    @staticmethod
    def _wire_bytes(response: httpx.Response) -> int:
        """How many bytes came off the wire for this response.

        ``num_bytes_downloaded`` is exact when httpx streamed the body, which is
        the production path. It stays at zero when a response was already fully
        buffered before it reached us — which is what a test transport produces —
        so the advertised ``Content-Length`` is the fallback. That fallback is
        only ever used to compute a ratio, never to *grant* permission, so
        trusting an attacker-supplied number here is safe: an understated length
        makes the ratio look worse, not better.
        """
        downloaded = response.num_bytes_downloaded
        if downloaded > 0:
            return downloaded
        try:
            return max(int(response.headers.get("content-length", "0")), 0)
        except ValueError:
            return 0