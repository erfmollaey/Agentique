"""The connection layer: how a validated address actually becomes a socket.

The SSRF decision lives in :mod:`app.infrastructure.fetch.ssrf`. This module
owns the one thing that makes that decision *mean* something: the guarantee that
the address validated is the address connected to.

How DNS rebinding is closed
---------------------------

The vulnerable shape is the obvious one:

```text
    validate(hostname)                   # resolves now: 93.184.216.34, looks fine
    client.get(f"https://{hostname}/")   # resolves AGAIN: 127.0.0.1
```

Between those two lines an attacker controlling DNS flips the record, and the
second resolution returns an internal address that was never validated. No
amount of care in the validator helps, because the validator and the HTTP client
are resolving independently and nothing ties them together.

This module removes the second resolution rather than trying to close the window:

```text
    resolved = policy.validate(url)                  # resolves once, checks all
    transport.send(..., connect_to=resolved.address_literal)
        ├─ the socket is opened to          93.184.216.34   ← the validated IP
        ├─ the Host header carries          example.test    ← the hostname
        └─ TLS SNI + certificate check use  example.test    ← the hostname
```

The URL handed to the HTTP client has its host replaced by the literal IP, so
the client's own resolver is never asked about the hostname. TLS certificate
verification still runs against the **hostname**, through httpcore's
``sni_hostname`` request extension — the standard mechanism for exactly this
(connect-by-address, verify-by-name), and the same one ``curl --resolve`` uses.
Verification is never disabled: an attacker who can present a valid certificate
for ``example.test`` still has to, and one who cannot is rejected.

Two consequences worth stating plainly:

- **The hostname is metadata at this point, not a destination.** It is the
  ``Host`` header's value; it is never something a resolver sees.
- **Every address is validated; one is used.** Dual-stack and CDN hostnames
  legitimately return several addresses. All are checked before anything
  connects; the transport attempts them in order so an unreachable first
  address does not fail the fetch. "Try the next one" can therefore never mean
  "try an unvalidated one" — the list arrives already cleared.

Nothing here may be used to fetch a URL without passing the policy first.
:class:`_PinnedTransport` is private to the package and is reachable only
through :class:`app.infrastructure.fetch.client.RestrictedFetcher`, which calls
:meth:`SsrfPolicy.validate` before building any request.
"""

from __future__ import annotations

import ssl

import httpx

from app.infrastructure.fetch.ssrf import ResolvedTarget


def pin_request(request: httpx.Request, resolved: ResolvedTarget) -> httpx.Request:
    """Rewrite ``request`` so its connect target is the validated address.

    Three things change, all deliberate:

    1. **URL host → the IP literal.** This is what removes the second DNS
       resolution. httpx hands the URL's host straight to the socket layer, so an
       IP literal means no resolver is consulted.
    2. **``Host`` header → the hostname.** Without this the origin server sees
       ``Host: 93.184.216.34``, serves a default page, and virtual-host-only
       sites break. It is set per request rather than on the client, so no host
       can leak from one hop into another.
    3. **``sni_hostname`` → the hostname.** Without this, TLS negotiates against
       the IP address and every certificate fails. With it, the certificate is
       verified *for the hostname* — which is what makes pinning safe to do at
       all, and is why verification is never disabled to make pinning work.

    Returns a new request; the original is untouched, so a caller cannot
    accidentally reuse an unpinned one.
    """
    target = resolved.target
    headers = httpx.Headers(request.headers)
    # httpx would otherwise synthesize `Host` from the URL, emitting the IP.
    headers["Host"] = target.authority

    extensions = dict(request.extensions)
    if target.scheme == "https":
        # httpcore's AsyncHTTPConnection._connect reads this and passes it as
        # `server_hostname` to the TLS handshake, which drives SNI and the
        # certificate hostname check.
        extensions["sni_hostname"] = target.hostname

    return httpx.Request(
        method=request.method,
        url=request.url.copy_with(host=resolved.address_literal),
        headers=headers,
        extensions=extensions,
        stream=request.stream,
    )


def build_ssl_context() -> ssl.SSLContext:
    """The TLS context used for every fetched HTTPS connection.

    httpx's own factory, so certifi's bundle, hostname checking, and any future
    httpx default stay consistent with the rest of the project's HTTP behaviour.
    Verification is **on**; there is no code path in this package that disables
    it, and tests assert both that the context verifies and that the source
    contains no ``verify=False``.
    """
    return httpx.create_ssl_context(verify=True)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx transport that dials a validated address instead of a hostname.

    Private to :mod:`app.infrastructure.fetch` deliberately: a public helper that
    fetched a URL without validation would be a bypass waiting to be used.
    Everything else is stock — httpx's own pool, timeouts, TLS context, and
    httpcore exception mapping — so this subclass contributes no new behaviour at
    all. The pinning is done by :func:`pin_request`, which rewrites the request
    before it ever reaches here.

    That division matters for reviewability. A subclass that opened its own
    connections would be a second implementation of TLS and pooling, with its own
    bugs; this one is httpx's transport, unchanged, and the destination is simply
    already an address by the time it sees the request.
    """

    def __init__(
        self,
        *,
        limits: httpx.Limits | None = None,
        ssl_context: ssl.SSLContext | None = None,
        retries: int = 0,
        **kwargs: object,
    ) -> None:
        super().__init__(
            limits=limits if limits is not None else httpx.Limits(),
            verify=ssl_context if ssl_context is not None else True,
            retries=retries,
            **kwargs,  # type: ignore[arg-type]
        )