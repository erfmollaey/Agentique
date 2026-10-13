"""Brave Search API adapter (Phase 3, P3.2 — AD-036).

One adapter, one provider, chosen on the measured record in
``docs/audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`` § K.

Everything provider-shaped stops at this module's boundary: the endpoint path,
the credential header, the query-parameter names, the JSON envelope, the status
codes, and the freshness codes. What leaves it is a
:class:`~app.domain.research.search.SearchResponse`. The application depends on
``app.domain.research.ports.SearchProvider`` and cannot tell which provider
answered (FR-5, T-3).

What this adapter deliberately does **not** do:

* **No URL validation, no DNS resolution, no fetching.** The URLs it returns are
  untrusted external input; the SSRF boundary belongs to the fetch client at
  P3.3 (AD-031). Validating here would create a second, weaker boundary that
  looks like the real one.
* **No ranking, re-ordering, or deduplication.** Provider order is preserved as
  given; selection policy is P3.5 (FR-27 … FR-30).
* **No retry across providers and no fallback.** One call, one outcome, one
  typed error. Orchestrating retries is the search stage's job (P3.9), not the
  adapter's, so it cannot happen here by accident.
* **No credential or payload logging.** Status, timing and result counts only
  (SR-7). The provider's error body is never propagated into an exception
  message: it can echo request headers, which carry the subscription token.

Latency note: the adapter's default timeout (15 s) is a budget, not a
measurement of Brave's service. This provider's latency was **not** measured —
no usable credential was available in the selection environment (§ K. The
consumer-web-search baseline measured there was 0.53–1.74 s.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.domain.errors import (
    SearchAuthenticationError,
    SearchNoResultsError,
    SearchProviderError,
    SearchRateLimitError,
    SearchRequestInvalidError,
    SearchResponseInvalidError,
    SearchTimeoutError,
)
from app.domain.research.search import (
    SafeSearchLevel,
    SearchFreshness,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

log = logging.getLogger(__name__)

#: The provider's own identifier, recorded on every response so a research run
#: can be re-explained later (§ 6 determinism).
PROVIDER_NAME = "brave"

#: Documented endpoint for web search. An absolute URL rather than a path plus a
#: client ``base_url``, deliberately: the request must be fully determined by
#: this adapter, so an injected or shared client cannot retarget it. Held here
#: rather than in settings, so a provider-specific URL can never leak into the
#: shared search settings namespace.
_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

#: Documented relative-freshness codes. The domain models the window in its own
#: vocabulary (SearchFreshness); the translation to the vendor's codes is the
#: adapter's job, and it is the only place those codes appear.
_FRESHNESS_CODES = {
    SearchFreshness.DAY: "pd",
    SearchFreshness.WEEK: "pw",
    SearchFreshness.MONTH: "pm",
    SearchFreshness.YEAR: "py",
}

_SAFE_SEARCH_CODES = {
    SafeSearchLevel.OFF: "off",
    SafeSearchLevel.MODERATE: "moderate",
    SafeSearchLevel.STRICT: "strict",
}

#: The provider caps a single page. Exceeding it is a configuration error, not a
#: silent truncation, because a truncated result set presented as complete is
#: exactly the dishonesty SR-12 forbids.
_MAX_COUNT = 20


class BraveSearchAdapter:
    """Implements :class:`app.domain.research.ports.SearchProvider` for Brave."""

    #: Read by the factory and by callers that log which provider ran.
    name = PROVIDER_NAME

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        key = settings.SEARCH_API_KEY
        if key is None or not key.get_secret_value().strip():
            # Raised here rather than at import or at first call so a missing
            # credential is a configuration error with a name, not an
            # unauthenticated request the provider rejects (SR-11).
            raise SearchAuthenticationError(
                f"{PROVIDER_NAME} search requires SEARCH_API_KEY to be set"
            )
        if settings.SEARCH_MAX_RESULTS > _MAX_COUNT:
            raise ValueError(
                f"SEARCH_MAX_RESULTS must be <= {_MAX_COUNT} for {PROVIDER_NAME} search"
            )
        # The token is read once into a private attribute and attached per
        # request, rather than baked into the client's default headers. A client
        # default would attach the credential to *every* request that client
        # ever made — including one made for another reason, on another host, by
        # a future caller. Per-request attachment means the credential exists on
        # exactly the one request that needs it, and nowhere else.
        self._token = key.get_secret_value()
        self._client = client or httpx.AsyncClient(
            timeout=settings.SEARCH_TIMEOUT_SECONDS,
            # trust_env=False: a proxy variable in the environment must not be
            # able to reroute a credentialed request (the same reasoning the
            # P3.3 fetch client uses, and the reason the two clients are never
            # shared).
            trust_env=False,
            # follow_redirects=False: a redirect must never carry the
            # subscription token to a host we did not choose (SR-8). A redirect
            # is therefore reported as an unexpected status and logged, not
            # followed.
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Run one query and normalize the answer. Never raises anything but a
        typed :class:`app.domain.errors.SearchError`."""
        params = self._params(request)
        log.info("search: provider=%s results=%s", self.name, request.max_results)

        try:
            response = await self._client.get(
                _WEB_SEARCH_URL,
                params=params,
                # Auth as a header, never as a query parameter: a URL is the one
                # thing this process later hands to a fetcher, a log line, or an
                # error message (SR-8).
                headers={
                    "X-Subscription-Token": self._token,
                    "Accept": "application/json",
                },
                timeout=self._settings.SEARCH_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            log.warning(
                "search provider timed out after %ss", self._settings.SEARCH_TIMEOUT_SECONDS
            )
            raise SearchTimeoutError("search provider timed out") from exc
        except httpx.HTTPError as exc:
            # Transport-level failure: DNS, TLS, connection reset, proxy. The
            # provider text is not attached; it can echo the request.
            log.warning("search provider unreachable")
            raise SearchProviderError("search provider unreachable") from exc

        if response.status_code != httpx.codes.OK:
            raise self._map_status(response.status_code)

        payload = self._payload(response)
        results = self._normalize(payload, request.max_results)
        if not results:
            # A successful call that produced no usable candidate is a
            # distinct outcome from a provider failure, and the frozen failure
            # model (§ 15.9) requires them to be told apart: "no results" is
            # terminal for this query, while a rate limit or a timeout is not.
            # Folding them together would make FR-41 unreportable.
            log.info("search: provider=%s usable_results=0", self.name)
            raise SearchNoResultsError("search returned no usable results")
        log.info("search: provider=%s usable_results=%s", self.name, len(results))
        return SearchResponse(
            query=request.query,
            provider=self.name,
            results=tuple(results),
        )

    async def aclose(self) -> None:
        """Close the transport this adapter created.

        Only closes a client it built. An injected client belongs to whoever
        injected it, which is what keeps tests (and any future pool) honest
        about ownership.
        """
        if self._owns_client:
            await self._client.aclose()

    # --- internals ---------------------------------------------------------

    def _params(self, request: SearchRequest) -> dict[str, str]:
        """Build the provider's query string.

        An explicit language, region, or safe-search level on the request wins;
        otherwise the configured default applies. An unset optional control is
        simply not sent rather than sent empty, because the provider treats an
        empty value differently from an absent one.
        """
        params = {"q": request.query, "count": str(request.max_results)}

        language = request.language or self._settings.SEARCH_LANGUAGE.strip()
        if language:
            params["search_lang"] = language

        region = request.country or self._settings.SEARCH_REGION.strip()
        if region:
            params["country"] = region.upper()

        freshness = request.freshness
        if freshness is not None:
            params["freshness"] = _FRESHNESS_CODES[freshness]

        safe_search = request.safe_search
        if safe_search is not None:
            params["safesearch"] = _SAFE_SEARCH_CODES[safe_search]

        return params

    def _map_status(self, status: int) -> Exception:
        """Turn a non-200 status into the one typed error that describes it.

        Status is logged; the provider's body is not read and not propagated.
        The mapping is status-only, so a new vendor error string cannot change
        which category a caller sees.
        """
        if status in (401, 403):
            log.error("search provider rejected credentials (status %s)", status)
            return SearchAuthenticationError("search provider rejected credentials")
        if status == 429:
            log.warning("search provider rate limit reached")
            return SearchRateLimitError("search provider rate limit reached")
        if status in (400, 422):
            log.warning("search provider rejected the request (status %s)", status)
            return SearchRequestInvalidError(
                "search provider rejected the request as invalid"
            )
        if status in (301, 302, 303, 307, 308):
            # Redirects are not followed (SR-8), so this means the configured
            # endpoint is wrong. Retrying would redirect again.
            log.error("search provider redirected the request (status %s); endpoint is wrong", status)
            return SearchProviderError("search provider redirected the request")
        log.warning("search provider returned status %s", status)
        return SearchProviderError(f"search provider returned status {status}")

    def _payload(self, response: httpx.Response) -> dict[str, Any]:
        """Parse the JSON envelope, rejecting anything we cannot trust.

        A response that is not a JSON object is a contract violation, not an
        empty result set. The distinction matters: an HTML error page served
        with a 200 must never be reported as "found nothing" (FR-41, SR-12).
        """
        try:
            payload = response.json()
        except ValueError as exc:
            log.warning("search provider returned a non-JSON body")
            raise SearchResponseInvalidError(
                "search provider returned a malformed response"
            ) from exc
        if not isinstance(payload, dict):
            log.warning("search provider returned an unexpected payload shape")
            raise SearchResponseInvalidError(
                "search provider returned a malformed response"
            )
        return payload

    def _normalize(self, payload: dict[str, Any], limit: int) -> list[SearchResult]:
        """Map the provider's result array onto domain results.

        Missing ``web``/``results`` is a malformed response; a present but empty
        array is an empty result set, which the caller's stage distinguishes as
        "no results" rather than a failure. Individual entries that cannot form
        a usable result are dropped, because a candidate the pipeline cannot
        address is not a candidate.
        """
        web = payload.get("web")
        if not isinstance(web, dict):
            log.warning("search provider response has no web result block")
            raise SearchResponseInvalidError(
                "search provider returned a malformed response"
            )
        raw = web.get("results")
        if raw is None:
            # The provider omits the array when nothing matched.
            return []
        if not isinstance(raw, list):
            log.warning("search provider results block is not a list")
            raise SearchResponseInvalidError(
                "search provider returned a malformed response"
            )

        results: list[SearchResult] = []
        for entry in raw[:limit]:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("url")
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            try:
                results.append(
                    SearchResult.from_provider_fields(
                        url=candidate,
                        rank=len(results) + 1,
                        title=entry.get("title"),
                        snippet=entry.get("description"),
                        # Brave reports recency as a human phrase ("3 days ago"),
                        # not a machine timestamp. It is dropped rather than
                        # guessed: freshness belongs in the query
                        # (SearchRequest.freshness) or in P3.5's ranking policy,
                        # and an invented date would be worse than none.
                        published_at=None,
                    )
                )
            except ValueError:
                # No usable host: the URL cannot be grouped or attributed, and a
                # source must have a domain (FR-10).
                continue
        return results


def create_brave_search_adapter(settings: Settings) -> BraveSearchAdapter:
    """Build the Brave adapter. The factory in this package is the selection point."""
    return BraveSearchAdapter(settings)