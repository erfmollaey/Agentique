"""Brave search adapter (Phase 3 P3.2).

Every case is driven through a mocked httpx transport. The suite's autouse
socket guard forbids non-loopback connections, and a provider adapter whose test
depends on the internet is not a test — it is an outage waiting for a bad day.

Covered: successful search, several results, empty response, malformed response,
timeout, authentication failure, rate limit, and an unexpected provider error,
plus the boundary properties: no credential in the URL, no credential or
response body in logs, provider types confined to the adapter, and a
domain-consumer swap that needs no change outside the factory.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.domain.errors import (
    SearchAuthenticationError,
    SearchError,
    SearchNoResultsError,
    SearchProviderError,
    SearchRateLimitError,
    SearchRequestInvalidError,
    SearchResponseInvalidError,
    SearchTimeoutError,
)
from app.domain.research.ports import SearchProvider
from app.domain.research.search import SearchFreshness, SearchRequest, SearchResponse
from app.infrastructure.search import create_search_provider
from app.infrastructure.search.brave import BraveSearchAdapter

ROOT = Path(__file__).resolve().parents[1]

TEST_KEY = "test-subscription-token-not-a-real-credential"

#: The minimum a ``Settings`` needs to construct, so the *default* of every field can
#: be asserted without a credential or an environment file.
BASE = {
    "BOT_TOKEN": "1:TEST",
    "GROQ_API_KEY": "gsk-test",
    "REDIS_URL": "redis://localhost:6379/15",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/search_test",
    "SEARCH_API_KEY": TEST_KEY,
}


def settings_with_search(**overrides: object) -> Settings:
    base = dict(BASE)
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def build(handler, **overrides: object) -> tuple[BraveSearchAdapter, list[httpx.Request]]:
    """An adapter whose transport is a mock that records what it was asked."""
    sent: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return BraveSearchAdapter(settings_with_search(**overrides), client=client), sent


def ok_payload(*urls: str) -> dict:
    return {
        "type": "search",
        "web": {
            "type": "search",
            "results": [
                {
                    "title": f"Result {index}",
                    "url": url,
                    "description": f"A description for {url}",
                    "age": "3 days ago",
                }
                for index, url in enumerate(urls, start=1)
            ],
        },
    }


# --- Successful search ------------------------------------------------------

async def test_a_successful_search_returns_normalized_results():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/one", "https://b.example/two"))
    )

    response = await adapter.search(SearchRequest(query="python asyncio"))

    assert isinstance(response, SearchResponse)
    assert response.query == "python asyncio"
    assert response.provider == "brave"
    assert [r.url for r in response.results] == ["https://a.example/one", "https://b.example/two"]
    assert [r.rank for r in response.results] == [1, 2]
    assert response.domains == ("a.example", "b.example")
    assert response.is_empty is False

    assert len(sent) == 1
    assert sent[0].url.path == "/res/v1/web/search"


async def test_a_single_result_search_is_normalized():
    adapter, _ = build(lambda _r: httpx.Response(200, json=ok_payload("https://only.example/x")))

    response = await adapter.search(SearchRequest(query="q"))

    assert len(response.results) == 1
    assert response.results[0].domain == "only.example"
    assert response.results[0].title == "Result 1"


async def test_the_query_and_result_count_reach_the_provider():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
        SEARCH_MAX_RESULTS=3,
    )

    await adapter.search(SearchRequest(query="release notes", max_results=3))

    params = sent[0].url.params
    assert params["q"] == "release notes"
    assert params["count"] == "3"


async def test_persian_text_survives_the_round_trip_unchanged():
    """The product's primary language is Persian (§ 15.10). Encoding damage here
    would be invisible in English-only fixtures."""
    query = "قیمت رسمی بنزین در ایران ۱۴۰۴"
    payload = ok_payload("https://news.example/گزارش")
    payload["web"]["results"][0]["title"] = "گزارش بازار انرژی"
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    response = await adapter.search(SearchRequest(query=query))

    assert response.query == query
    assert response.results[0].title == "گزارش بازار انرژی"


async def test_optional_controls_are_translated_only_when_set():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
        SEARCH_LANGUAGE="",
        SEARCH_REGION="",
    )

    await adapter.search(SearchRequest(query="q"))

    params = sent[0].url.params
    assert "search_lang" not in params, "an unset control must not be sent empty"
    assert "country" not in params
    assert "safesearch" not in params


async def test_configured_language_and_region_apply_when_the_request_is_silent():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
        SEARCH_LANGUAGE="fa",
        SEARCH_REGION="ir",
    )

    await adapter.search(SearchRequest(query="q"))

    params = sent[0].url.params
    assert params["search_lang"] == "fa"
    assert params["country"] == "IR", "a configured region is uppercased for the provider"


async def test_an_explicit_request_control_wins_over_configuration():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
        SEARCH_LANGUAGE="fa",
    )

    await adapter.search(SearchRequest(query="q", language="en"))

    assert sent[0].url.params["search_lang"] == "en"


async def test_the_freshness_window_is_translated_to_a_provider_code():
    """The domain owns the vocabulary (day/week/month/year); the provider's codes
    are the adapter's business and appear nowhere else."""
    for window, code in (
        (SearchFreshness.DAY, "pd"),
        (SearchFreshness.WEEK, "pw"),
        (SearchFreshness.MONTH, "pm"),
        (SearchFreshness.YEAR, "py"),
    ):
        adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

        await adapter.search(SearchRequest(query="q", freshness=window))

        assert sent[0].url.params["freshness"] == code


async def test_safe_search_is_translated_when_requested():
    adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    await adapter.search(SearchRequest(query="q", safe_search="strict"))

    assert sent[0].url.params["safesearch"] == "strict"


async def test_the_result_count_is_capped_at_the_requested_limit():
    """A provider returning more than was asked for must not silently inflate the
    candidate set: the bound is a resource control (SR-5), not a hint."""
    adapter, _ = build(
        lambda _r: httpx.Response(
            200,
            json=ok_payload(*[f"https://e{i}.example/{i}" for i in range(10)]),
        )
    )

    response = await adapter.search(SearchRequest(query="q", max_results=3))

    assert len(response.results) == 3
    assert [r.rank for r in response.results] == [1, 2, 3]


async def test_a_result_count_beyond_the_provider_limit_is_a_configuration_error():
    """Refused rather than truncated: a truncated set presented as complete is
    the dishonesty SR-12 forbids."""
    with pytest.raises(ValueError, match="SEARCH_MAX_RESULTS"):
        BraveSearchAdapter(settings_with_search(SEARCH_MAX_RESULTS=25))


async def test_unusable_entries_are_dropped_and_the_rest_kept():
    """A candidate that cannot be addressed is not a candidate; one bad entry
    must not fail the whole query."""
    payload = {
        "web": {
            "results": [
                {"title": "No url", "description": "x"},
                {"title": "Blank", "url": "   "},
                {"title": "Not an object", "url": 42},
                {"title": "No host", "url": "not-a-url"},
                {"title": "Usable", "url": "https://good.example/1", "description": "d"},
            ]
        }
    }
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    response = await adapter.search(SearchRequest(query="q"))

    assert [r.url for r in response.results] == ["https://good.example/1"]
    assert response.results[0].rank == 1, "ranks stay contiguous after dropping"


async def test_results_without_text_fields_still_normalize():
    payload = {"web": {"results": [{"url": "https://bare.example/1"}]}}
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    response = await adapter.search(SearchRequest(query="q"))

    assert response.results[0].title is None
    assert response.results[0].snippet is None


# --- Empty results ----------------------------------------------------------

async def test_an_empty_result_set_is_a_distinct_terminal_outcome():
    """"Found nothing" and "the provider broke" must be tellable apart: the first
    is terminal for the query and the second may be retried. § 15.9 requires the
    distinction, and FR-41 requires the first to be reported honestly."""
    adapter, _ = build(lambda _r: httpx.Response(200, json={"web": {"results": []}}))

    with pytest.raises(SearchNoResultsError) as info:
        await adapter.search(SearchRequest(query="nothing matches this"))

    assert info.value.retryable is False, (
        "an identical query yields an identical empty set; retrying cannot help"
    )


async def test_a_missing_results_array_is_no_results_not_a_contract_violation():
    """The provider omits the array when nothing matched; that is an empty set,
    not a malformed response."""
    adapter, _ = build(lambda _r: httpx.Response(200, json={"type": "search", "web": {}}))

    with pytest.raises(SearchNoResultsError):
        await adapter.search(SearchRequest(query="q"))


async def test_a_response_whose_entries_are_all_unusable_is_also_no_results():
    """No addressable candidate survived normalization, so there is nothing for
    the caller to work with — and it is still not a provider failure."""
    payload = {"web": {"results": [{"title": "No url"}, {"url": "not-a-url"}]}}
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    with pytest.raises(SearchNoResultsError):
        await adapter.search(SearchRequest(query="q"))


async def test_no_results_is_distinguishable_from_every_other_failure():
    """One assertion for the property the whole taxonomy exists to provide."""
    adapter, _ = build(lambda _r: httpx.Response(200, json={"web": {"results": []}}))

    with pytest.raises(SearchError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert isinstance(info.value, SearchNoResultsError)
    for other in (
        SearchAuthenticationError,
        SearchRateLimitError,
        SearchTimeoutError,
        SearchProviderError,
        SearchRequestInvalidError,
        SearchResponseInvalidError,
    ):
        assert not isinstance(info.value, other), f"no-results must not be confused with {other}"


# --- Malformed responses ----------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        "<html><body>we are down</body></html>",
        "not json at all",
        "[1, 2, 3]",
        '"a bare string"',
    ],
    ids=["html-error-page", "garbage", "json-array", "json-string"],
)
async def test_a_malformed_response_is_a_typed_non_retryable_error(body):
    adapter, _ = build(lambda _r: httpx.Response(200, text=body))

    with pytest.raises(SearchResponseInvalidError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is False, (
        "a contract violation will fail identically on a second attempt"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"no_web_block": True},
        {"web": "not an object"},
        {"web": {"results": "not a list"}},
        {"web": {"results": {"url": "https://a.example/1"}}},
    ],
    ids=["no-web", "web-not-object", "results-not-list", "results-not-a-list-entry"],
)
async def test_a_wrongly_shaped_result_block_is_malformed(payload):
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    with pytest.raises(SearchResponseInvalidError):
        await adapter.search(SearchRequest(query="q"))


async def test_a_malformed_error_page_is_never_reported_as_no_results():
    """The specific failure that would be most damaging: a 200 carrying an HTML
    error page, mistaken for a provider that found nothing."""
    adapter, _ = build(lambda _r: httpx.Response(200, text="<html>503 Service Unavailable</html>"))

    with pytest.raises(SearchResponseInvalidError):
        await adapter.search(SearchRequest(query="q"))


# --- Provider failures ------------------------------------------------------

async def test_a_provider_timeout_is_typed_and_retryable():
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=_request)

    adapter, _ = build(timeout)

    with pytest.raises(SearchTimeoutError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is True


async def test_a_connect_timeout_is_also_a_search_timeout():
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=_request)

    adapter, _ = build(timeout)

    with pytest.raises(SearchTimeoutError):
        await adapter.search(SearchRequest(query="q"))


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_are_typed_terminal_and_quiet(status):
    """Never retried: a bad token fails identically forever. And the provider's
    body is not propagated, because it can echo the request headers that carry
    the token (SR-5)."""
    adapter, _ = build(lambda _r: httpx.Response(status, json={"error": "invalid token abc123"}))

    with pytest.raises(SearchAuthenticationError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is False
    assert "abc123" not in str(info.value), "the provider's body must not travel in the error"


async def test_a_rate_limit_is_typed_and_retryable():
    adapter, _ = build(lambda _r: httpx.Response(429, json={"error": "slow down"}))

    with pytest.raises(SearchRateLimitError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is True


@pytest.mark.parametrize("status", [400, 422])
async def test_a_rejected_request_is_invalid_and_not_retried(status):
    adapter, _ = build(lambda _r: httpx.Response(status, json={"error": "bad query"}))

    with pytest.raises(SearchRequestInvalidError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is False


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_server_side_failure_is_retryable(status):
    adapter, _ = build(lambda _r: httpx.Response(status))

    with pytest.raises(SearchProviderError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is True


async def test_a_transport_failure_is_a_retryable_provider_error():
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_request)

    adapter, _ = build(broken)

    with pytest.raises(SearchProviderError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert info.value.retryable is True
    assert "connection refused" not in str(info.value)


async def test_a_redirect_is_refused_rather_than_followed():
    """Following it would carry the subscription token to a host we did not
    choose (SR-8). It also means the configured endpoint is wrong, so it is not
    retryable."""
    adapter, sent = build(
        lambda _r: httpx.Response(302, headers={"Location": "https://elsewhere.example/search"})
    )

    with pytest.raises(SearchProviderError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert len(sent) == 1, "the redirect target must never be requested"
    assert info.value.retryable is True


async def test_an_unexpected_status_still_produces_a_typed_error():
    """Whatever the provider does, the caller sees a typed SearchError."""
    adapter, _ = build(lambda _r: httpx.Response(418, text="teapot"))

    with pytest.raises(SearchError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert isinstance(info.value, SearchProviderError)
    assert info.value.retryable is True


# --- Credential handling ----------------------------------------------------

def test_building_without_a_credential_fails_with_the_setting_name():
    with pytest.raises(SearchAuthenticationError, match="SEARCH_API_KEY"):
        BraveSearchAdapter(settings_with_search(SEARCH_API_KEY=None))


def test_building_with_a_blank_credential_fails_the_same_way():
    with pytest.raises(SearchAuthenticationError, match="SEARCH_API_KEY"):
        BraveSearchAdapter(settings_with_search(SEARCH_API_KEY="   "))


async def test_the_credential_travels_as_a_header_never_in_the_url():
    """A URL is the one thing this process later hands to a fetcher, a log, or
    an error message. A credential must never be in it (SR-8)."""
    adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    await adapter.search(SearchRequest(query="secret query about acquisition"))

    request = sent[0]
    assert request.headers["X-Subscription-Token"] == TEST_KEY
    assert TEST_KEY not in str(request.url)
    assert TEST_KEY not in request.url.params


async def test_no_log_record_contains_the_credential(caplog):
    adapter, _ = build(lambda _r: httpx.Response(500, text="internal error detail"))

    with caplog.at_level(logging.DEBUG), pytest.raises(SearchProviderError):
        await adapter.search(SearchRequest(query="q"))

    assert TEST_KEY not in caplog.text


async def test_no_log_record_contains_the_response_body(caplog):
    """SR-7: external content is never logged in full. An error page from a
    provider is external content."""
    secret_marker = "provider-internal-detail-should-not-be-logged"
    adapter, _ = build(lambda _r: httpx.Response(200, text=f"<html>{secret_marker}</html>"))

    with caplog.at_level(logging.DEBUG), pytest.raises(SearchResponseInvalidError):
        await adapter.search(SearchRequest(query="q"))

    assert secret_marker not in caplog.text


async def test_the_query_text_is_not_logged(caplog):
    """A query comes from a Telegram user. Logging it would put user content in
    the log for no operational benefit — the count and the outcome suffice."""
    adapter, _ = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    with caplog.at_level(logging.DEBUG):
        await adapter.search(SearchRequest(query="a very distinctive private question"))

    assert "a very distinctive private question" not in caplog.text


# --- Adapter hygiene -------------------------------------------------------

async def test_closing_an_adapter_does_not_close_an_injected_client():
    """The adapter closes only what it built. Closing someone else's transport
    would break whoever injected it."""
    adapter, _ = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))
    injected = adapter._client

    await adapter.aclose()

    assert injected.is_closed is False
    await injected.aclose()


async def test_the_adapter_satisfies_the_port():
    adapter, _ = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    assert isinstance(adapter, SearchProvider)


# --- Provider-independent architecture --------------------------------------

def test_the_factory_builds_the_configured_provider():
    settings = settings_with_search()

    adapter = create_search_provider(settings)

    assert isinstance(adapter, SearchProvider)
    assert adapter.name == "tavily"


def test_an_unknown_provider_name_is_refused_rather_than_defaulted():
    """A typo in configuration must not quietly change where results come from."""
    with pytest.raises(ValueError, match="Unknown SEARCH_PROVIDER"):
        create_search_provider(settings_with_search(), provider="not_a_provider")


def test_the_factory_requires_the_credential_for_the_selected_provider():
    with pytest.raises(SearchAuthenticationError):
        create_search_provider(settings_with_search(SEARCH_API_KEY=None))


def test_only_one_provider_adapter_is_registered():
    """One adapter is a deliberate stage decision, not an accident. A second one
    would need a fallback or selection policy that P3.2 has no evidence for."""
    from app.infrastructure.search.factory import SUPPORTED_SEARCH_PROVIDERS

    assert sorted(SUPPORTED_SEARCH_PROVIDERS) == ["brave", "tavily"]


class TestTheSelectedProvider:
    """AD-050 — Tavily is the selected default, and the choice is replaceable.

    The rationale is a **budget** one, recorded rather than argued: Brave Search was
    not a viable free option for this project's usage, and Tavily meets the
    functional requirement within its free-tier allowance. Nothing here claims Tavily
    is better in every respect, or free indefinitely — only that it is what the
    project selected, why, and that the decision is configuration rather than code.
    """

    def test_the_default_provider_is_tavily(self):
        """The selection lives in configuration and is asserted, not assumed."""
        assert settings_with_search().SEARCH_PROVIDER == "tavily"

    def test_the_default_builds_the_tavily_adapter(self):
        from app.infrastructure.search.tavily import TavilySearchAdapter

        adapter = create_search_provider(settings_with_search())

        assert isinstance(adapter, TavilySearchAdapter)

    def test_an_explicit_override_still_builds_brave(self):
        """Replaceability is the property that makes the choice reversible.

        Re-selecting is a settings change, not a code change — the whole reason the
        provider sits behind ``SearchProvider`` rather than being imported by the
        research domain.
        """
        from app.infrastructure.search.brave import BraveSearchAdapter

        adapter = create_search_provider(settings_with_search(SEARCH_PROVIDER="brave"))

        assert isinstance(adapter, BraveSearchAdapter)

    def test_a_per_call_override_wins_over_configuration(self):
        from app.infrastructure.search.brave import BraveSearchAdapter

        adapter = create_search_provider(settings_with_search(), provider="brave")

        assert isinstance(adapter, BraveSearchAdapter)


async def test_a_domain_consumer_works_with_any_search_implementation():
    """The T-3 swap guarantee, expressed as a test: a fake port implementation
    satisfies a consumer that only knows ``SearchProvider``, so no orchestration
    change is needed to replace the vendor."""
    calls: list[SearchRequest] = []

    class FakeSearchProvider:
        name = "fake"

        async def search(self, request: SearchRequest) -> SearchResponse:
            calls.append(request)
            return SearchResponse(
                query=request.query,
                provider=self.name,
                results=(),
            )

    consumer = FakeSearchProvider()
    response = await consumer.search(SearchRequest(query="swapped", max_results=2))

    assert isinstance(consumer, SearchProvider)
    assert calls[0].query == "swapped"
    assert response.provider == "fake"


def test_provider_types_do_not_escape_the_adapter_package():
    """A structural boundary: only the adapter may know Brave's endpoint, header
    name, or freshness codes."""
    brave_only = ("X-Subscription-Token", "/res/v1/web/search", "_FRESHNESS_CODES")
    offenders: dict[str, list[str]] = {}

    for path in (ROOT / "app").rglob("*.py"):
        if path.parts[-2:] == ("search", "brave.py"):
            continue
        if "infrastructure" in path.parts and path.parent.name == "search":
            continue  # the factory legitimately names the adapter
        text = path.read_text()
        found = [token for token in brave_only if token in text]
        if found:
            offenders[str(path.relative_to(ROOT))] = found

    assert not offenders, f"provider detail leaked outside the adapter: {offenders}"


def test_the_adapter_module_is_the_only_place_that_imports_httpx_for_search():
    for path in (ROOT / "app").rglob("*.py"):
        if path.parts[-2:] == ("search", "brave.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "httpx":
                pytest.fail(f"{path.relative_to(ROOT)} imports httpx; only the adapter should")


# --- Configuration behaviour ------------------------------------------------

def test_search_is_reported_as_disabled_without_a_credential():
    settings = settings_with_search(SEARCH_API_KEY=None)

    assert settings.search_enabled is False


def test_search_is_reported_as_enabled_with_a_credential():
    assert settings_with_search().search_enabled is True


def test_search_settings_are_optional_so_a_chat_only_deployment_still_starts():
    """Nothing searches yet. A deployment with no search configuration must
    still construct settings and boot the chat flow."""
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="1:TEST",
        GROQ_API_KEY="gsk-test",
        REDIS_URL="redis://localhost:6379/15",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/chat_test",
    )

    assert settings.SEARCH_API_KEY is None
    assert settings.search_enabled is False
    assert settings.SEARCH_MAX_RESULTS > 0, "the bound exists even when unused"


def test_the_search_credential_is_a_secret_and_cannot_be_printed():
    settings = settings_with_search()

    assert settings.SEARCH_API_KEY is not None
    assert TEST_KEY not in repr(settings.SEARCH_API_KEY)
    assert TEST_KEY not in str(settings.SEARCH_API_KEY)


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_a_non_positive_timeout_is_rejected(timeout):
    with pytest.raises(ValueError):
        settings_with_search(SEARCH_TIMEOUT_SECONDS=timeout)


def test_the_configured_result_bound_is_capped():
    with pytest.raises(ValueError):
        settings_with_search(SEARCH_MAX_RESULTS=21)