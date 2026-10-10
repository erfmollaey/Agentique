"""Fetching infrastructure (Phase 3, P3.3 — AD-031).

The SSRF boundary and the one HTTP client that is allowed near an untrusted
URL. Three modules, and the split is the point:

``ssrf``
    **The authoritative rule for whether an address is safe to connect to.**
    URL parsing and validation, hostname normalization, and the address
    blocklists. Standard library only, by design: the policy must be readable
    without following a network client.
``resolver``
    Where an address answer comes from. The system resolver and the
    DNS-over-HTTPS resolver, selected by ``FETCH_DNS_MODE``. Which resolver runs
    changes only where an answer originates, never whether it is allowed —
    ``ssrf`` decides that in every mode.
``transport``
    The connection. Its job is to make the validated address the *actual*
    destination, which is what closes the DNS-rebinding window.
``client``
    The :class:`app.domain.research.ports.SafeFetcher` implementation:
    manual redirect following, status and content-type policy, bounded
    streaming of the decoded body, and typed errors.

:func:`create_fetch_client` is the construction point, mirroring
``create_search_provider`` and ``create_llm_client``. Unlike those, it makes no
vendor choice — there is one fetcher, and it is not replaceable by
configuration, because a configurable fetcher is a configurable security
boundary.
"""

from app.core.config import Settings
from app.infrastructure.fetch.client import RestrictedFetcher, configured_schemes
from app.infrastructure.fetch.resolver import configured_resolver
from app.infrastructure.fetch.ssrf import SsrfPolicy


def create_fetch_client(settings: Settings) -> RestrictedFetcher:
    """Build the restricted fetcher from configuration.

    Raises ``ValueError`` when configuration asks for something the boundary will
    not do — a scheme outside HTTP/HTTPS, an empty content-type allowlist, a
    limit that is not positive, or an unknown ``FETCH_DNS_MODE``. Failing here is
    better than discovering it on the first fetch of a research run.

    The resolver is the *only* choice configuration makes, and it is a choice of
    where addresses come from rather than which addresses are permitted: the
    scheme allowlist, the blocklists, and the "every answer must pass" rule are
    the same code in every mode.
    """
    return RestrictedFetcher(
        settings,
        policy=SsrfPolicy(
            allowed_schemes=configured_schemes(settings),
            resolver=configured_resolver(settings),
        ),
    )


__all__ = ["RestrictedFetcher", "SsrfPolicy", "create_fetch_client"]