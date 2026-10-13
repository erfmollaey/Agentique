"""Tavily Search API adapter (Phase 3, P3.2 extension).

The second implementation of the ``SearchProvider`` port, added because a
usable credential for this provider now exists. It proves the T-3 swap
guarantee the port was built for: everything provider-shaped stops at this
module's boundary — the endpoint, the credential header, the JSON body
parameter names, the JSON envelope, the status codes, the freshness names —
and what leaves it is a
:class:`~app.domain.research.search.SearchResponse`. The application depends
on ``app.domain.research.ports.SearchProvider`` and cannot tell which
provider answered.

The same deliberate omissions as the Brave adapter apply, for the same
reasons (see ``brave.py``):

* **No URL validation, no DNS resolution, no fetching.** The SSRF boundary is
  the fetch client (P3.3, AD-031); validating here would create a second,
  weaker boundary that looks like the real one.
* **No ranking, re-ordering, or deduplication.** Provider order is preserved;
  selection policy is P3.5 (FR-27 … FR-30).
* **No retry across providers and no fallback.** One call, one outcome, one
  typed error; retry orchestration belongs to the search stage (P3.9).
* **No credential or payload logging.** Status, timing and result counts only
  (SR-7). The provider's error body is never propagated into an exception
  message.

Known verification gap: the provider endpoint was unreachable (403 HTML block
page from the selection environment's egress) when this adapter was written,
so the contract here follows the published API documentation and the adapter
suite's mocked transport — not a live call. The first live call from an
unblocked network is the outstanding evidence item.
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime
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
PROVIDER_NAME = "tavily"

#: Documented endpoint for web search. An absolute URL rather than a path plus
#: a client ``base_url``, deliberately: the request must be fully determined by
#: this adapter, so an injected or shared client cannot retarget it. Held here
#: rather than in settings, so a provider-specific URL can never leak into the
#: shared search settings namespace.
_SEARCH_URL = "https://api.tavily.com/search"

#: The provider's names for the domain's recency vocabulary. The domain models
#: the window in its own words (SearchFreshness); the translation to the
#: vendor's values is the adapter's job, and it is the only place they appear.
_TIME_RANGE_CODES = {
    SearchFreshness.DAY: "day",
    SearchFreshness.WEEK: "week",
    SearchFreshness.MONTH: "month",
    SearchFreshness.YEAR: "year",
}

#: The provider caps a single request at 20 results. Exceeding it is a
#: configuration error, not a silent truncation, because a truncated result
#: set presented as complete is exactly the dishonesty SR-12 forbids.
_MAX_COUNT = 20


class TavilySearchAdapter:
    """Implements :class:`app.domain.research.ports.SearchProvider` for Tavily."""

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
            # credential to a host we did not choose (SR-8). A redirect is
            # therefore reported as an unexpected status and logged, not
            # followed.
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Run one query and normalize the answer. Never raises anything but a
        typed :class:`app.domain.errors.SearchError`."""
        body = self._body(request)
        log.info("search: provider=%s results=%s", self.name, request.max_results)

        try:
            response = await self._client.post(
                _SEARCH_URL,
                json=body,
                # Auth as a header, never as a query parameter: a URL is the one
                # thing this process later hands to a fetcher, a log line, or an
                # error message (SR-8).
                headers={
                    "Authorization": f"Bearer {self._token}",
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

    def _body(self, request: SearchRequest) -> dict[str, Any]:
        """Build the provider's JSON request body.

        An explicit language, region, freshness window, or safe-search level
        on the request wins; otherwise the configured default applies. An
        unset optional control is simply not sent rather than sent empty,
        because providers treat an empty value differently from an absent one.
        """
        body: dict[str, Any] = {
            "query": request.query,
            "max_results": request.max_results,
            # Pin the parameters we send: auto-selection would rewrite the
            # request's own controls after they were set deliberately, which
            # makes the request non-deterministic (§ 6).
            "auto_parameters": False,
            # The pipeline consumes titles, snippets and URLs; an LLM-authored
            # answer or an image list would be content no stage reads, paid for
            # in provider credits.
            "include_answer": False,
            "include_images": False,
        }

        language = request.language or self._settings.SEARCH_LANGUAGE.strip()
        if language:
            # A hint, not a filter: ``filter_by_language`` stays unset, because
            # the configuration contract is a market hint and a hard language
            # filter would silently drop otherwise-usable sources — a policy
            # decision that belongs to P3.5, not to an adapter.
            body["language"] = language

        region = request.country or self._settings.SEARCH_REGION.strip()
        if region:
            # The domain expresses the market as a two-letter code and the
            # provider documents a country name here. The canonical domain
            # value is passed through rather than translated through an
            # invented name table; the provider's response to an unknown value
            # is a typed error either way.
            body["country"] = region.upper()

        freshness = request.freshness
        if freshness is not None:
            body["time_range"] = _TIME_RANGE_CODES[freshness]

        safe_search = request.safe_search
        if safe_search is not None:
            # The provider exposes a single on/off knob, so MODERATE and
            # STRICT are the same request here; the mapping never relaxes a
            # requested level (off stays off).
            body["safe_search"] = safe_search is not SafeSearchLevel.OFF

        return body

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

        A present but empty ``results`` array — or a response that omits it —
        is an empty result set, which the caller's stage distinguishes as "no
        results" rather than a failure. A ``results`` block of the wrong type
        is a malformed response: an HTML error page served with a 200 must
        never be reported as "found nothing" (FR-41, SR-12). Individual
        entries that cannot form a usable result are dropped, because a
        candidate the pipeline cannot address is not a candidate.
        """
        raw = payload.get("results")
        if raw is None:
            # The provider may omit the array when nothing matched.
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
                        snippet=entry.get("content"),
                        # Tavily reports recency as an RFC-2822 string ("Tue,
                        # 11 Mar 2025 17:00:00 GMT"), not the ISO-8601 the
                        # domain parses; the translation belongs here, where
                        # every provider shape is already the adapter's
                        # business. Anything unparsable passes through and the
                        # domain drops it to ``None`` rather than failing a
                        # usable result.
                        published_at=_published_at(entry.get("published_date")),
                        # Tavily exposes a real relevance score — unlike
                        # position-derived guesses, this is preserved as given
                        # (the SearchResult contract: a relevance number when,
                        # and only when, the provider exposes one).
                        relevance=entry.get("score"),
                    )
                )
            except ValueError:
                # No usable host: the URL cannot be grouped or attributed, and a
                # source must have a domain (FR-10).
                continue
        return results


def _published_at(value: object) -> object:
    """Translate the provider's RFC-2822 date, or pass the value through.

    Passing through keeps every unparsable shape in the domain's documented
    behaviour: an ISO-8601 string is parsed there, and anything else —
    including human phrasing — becomes ``None`` rather than a guess.
    """
    if isinstance(value, str) and value.strip():
        try:
            return parsedate_to_datetime(value.strip())
        except (TypeError, ValueError):
            return value
    return value


def create_tavily_search_adapter(settings: Settings) -> TavilySearchAdapter:
    """Build the Tavily adapter. The factory in this package is the selection point."""
    return TavilySearchAdapter(settings)
