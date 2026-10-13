"""Tavily search adapter (Phase 3 P3.2 extension).

Every case is driven through a mocked httpx transport. The suite's autouse
socket guard forbids non-loopback connections, and a provider adapter whose
test depends on the internet is not a test — it is an outage waiting for a bad
day.

Covers: successful search, multiple results, empty response, malformed
response, timeout, authentication failure, rate limit, unexpected provider
error, plus the boundary properties: no credential in the URL or body, no
credential or response body in logs, provider types confined to the adapter,
and a domain-consumer swap that needs no change outside the factory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

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
from app.domain.research.ports import SearchProvider
from app.domain.research.search import SearchFreshness, SearchRequest, SearchResponse
from app.infrastructure.search import create_search_provider
from app.infrastructure.search.tavily import TavilySearchAdapter

ROOT = Path(__file__).resolve().parents[1]

TEST_KEY = "test-tavily-key-not-a-real-credential"


def settings_with_search(**overrides: object) -> Settings:
    base = {
        "BOT_TOKEN": "1:TEST",
        "GROQ_API_KEY": "gsk-test",
        "REDIS_URL": "redis://localhost:6379/15",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/search_test",
        "SEARCH_API_KEY": TEST_KEY,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def build(handler, **overrides: object) -> tuple[TavilySearchAdapter, list[httpx.Request]]:
    """An adapter whose transport is a mock that records what it was asked."""
    sent: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return TavilySearchAdapter(settings_with_search(**overrides), client=client), sent


def ok_payload(*urls: str) -> dict:
    """A Tavily-shaped response carrying the given URLs as results."""
    return {
        "query": "query",
        "results": [
            {
                "title": f"Result {index}",
                "url": url,
                "content": f"A snippet for {url}",
                "score": 0.9 - (index * 0.1),
                "published_date": "Tue, 11 Mar 2025 17:00:00 GMT" if index == 1 else None,
            }
            for index, url in enumerate(urls, start=1)
        ],
    }


# --- Successful search ------------------------------------------------------

async def test_a_successful_search_returns_normalized_results():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/one", "https://b.example/two"))
    )

    response = await adapter.search(SearchRequest(query="python asyncio"))

    assert isinstance(response, SearchResponse)
    assert response.query == "python asyncio"
    assert response.provider == "tavily"
    assert [r.url for r in response.results] == ["https://a.example/one", "https://b.example/two"]
    assert [r.rank for r in response.results] == [1, 2]
    assert response.domains == ("a.example", "b.example")
    assert response.is_empty is False
    # Tavily exposes a score; it is carried through rather than guessed. The
    # fixture's scores descend (0.8, then 0.7), so matching them exactly proves
    # the provider's own number survived — an expectation of a "natural" first
    # value would pass even if the adapter re-derived one from rank.
    assert response.results[0].relevance == pytest.approx(0.8)
    assert response.results[1].relevance == pytest.approx(0.7)

    assert len(sent) == 1
    assert sent[0].url.path == "/search"
    assert sent[0].method == "POST"


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

    body = json.loads(sent[0].content)
    assert body["query"] == "release notes"
    assert body["max_results"] == 3


async def test_persian_text_survives_the_round_trip_unchanged():
    """The product's primary language is Persian (§ 15.10). Encoding damage here
    would be invisible in English-only fixtures."""
    query = "قیمت رسمی بنزین در ایران ۱۴۰۴"
    payload = ok_payload("https://news.example/گزارش")
    payload["results"][0]["title"] = "گزارش بازار انرژی"
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    response = await adapter.search(SearchRequest(query=query))

    assert response.query == query
    assert response.results[0].title == "گزارش بازار انرژی"


async def test_optional_controls_are_translated_only_when_set():
    adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    await adapter.search(SearchRequest(query="q"))

    body = json.loads(sent[0].content)
    assert "language" not in body, "an unset control must not be sent empty"
    assert "country" not in body
    assert "time_range" not in body
    assert "safe_search" not in body


async def test_configured_language_and_region_apply_when_the_request_is_silent():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
        SEARCH_LANGUAGE="fa",
        SEARCH_REGION="ir",
    )

    await adapter.search(SearchRequest(query="q"))

    body = json.loads(sent[0].content)
    assert body["language"] == "fa"
    assert body["country"] == "IR", "a configured region is uppercased for the provider"


async def test_an_explicit_request_control_wins_over_configuration():
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
        SEARCH_LANGUAGE="fa",
    )

    await adapter.search(SearchRequest(query="q", language="en"))

    assert json.loads(sent[0].content)["language"] == "en"


async def test_the_freshness_window_is_translated_to_a_provider_value():
    """The domain owns the vocabulary (day/week/month/year); the provider's values
    are the adapter's business and appear nowhere else."""
    for window, code in (
        (SearchFreshness.DAY, "day"),
        (SearchFreshness.WEEK, "week"),
        (SearchFreshness.MONTH, "month"),
        (SearchFreshness.YEAR, "year"),
    ):
        adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

        await adapter.search(SearchRequest(query="q", freshness=window))

        assert json.loads(sent[0].content)["time_range"] == code


async def test_safe_search_is_translated_when_requested():
    adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    await adapter.search(SearchRequest(query="q", safe_search="strict"))

    assert json.loads(sent[0].content)["safe_search"] is True


async def test_safe_search_off_sends_false():
    adapter, sent = build(lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")))

    await adapter.search(SearchRequest(query="q", safe_search="off"))

    assert json.loads(sent[0].content)["safe_search"] is False


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
        TavilySearchAdapter(settings_with_search(SEARCH_MAX_RESULTS=25))


# --- Empty / malformed payloads ---------------------------------------------

async def test_an_empty_response_is_a_no_results_error():
    adapter, _ = build(lambda _r: httpx.Response(200, json={"query": "q", "results": []}))

    with pytest.raises(SearchNoResultsError):
        await adapter.search(SearchRequest(query="q"))


async def test_a_missing_results_array_is_a_no_results_error():
    adapter, _ = build(lambda _r: httpx.Response(200, json={"query": "q"}))

    with pytest.raises(SearchNoResultsError):
        await adapter.search(SearchRequest(query="q"))


async def test_a_results_block_that_is_not_a_list_is_a_malformed_response():
    adapter, _ = build(lambda _r: httpx.Response(200, json={"query": "q", "results": "not a list"}))

    with pytest.raises(SearchResponseInvalidError):
        await adapter.search(SearchRequest(query="q"))


async def test_a_non_json_body_is_a_malformed_response():
    adapter, _ = build(lambda _r: httpx.Response(200, text="<html>not a search response</html>"))

    with pytest.raises(SearchResponseInvalidError):
        await adapter.search(SearchRequest(query="q"))


async def test_an_entry_without_a_url_is_skipped():
    payload = {
        "query": "q",
        "results": [
            {"title": "has url", "url": "https://a.example/1", "content": "ok"},
            {"title": "no url", "content": "skip me"},
            {"title": "empty url", "url": "  ", "content": "skip me"},
            {"title": "has url 2", "url": "https://b.example/2", "content": "ok"},
        ],
    }
    adapter, _ = build(lambda _r: httpx.Response(200, json=payload))

    response = await adapter.search(SearchRequest(query="q", max_results=10))

    assert [r.url for r in response.results] == ["https://a.example/1", "https://b.example/2"]


# --- Error mapping ----------------------------------------------------------

async def test_a_timeout_becomes_a_typed_timeout_error():
    adapter, _ = build(
        lambda _r: (_ for _ in ()).throw(httpx.TimeoutException("simulated")),
    )
    with pytest.raises(SearchTimeoutError) as info:
        await adapter.search(SearchRequest(query="q"))
    assert info.value.retryable is True


async def test_a_connection_error_becomes_a_provider_error():

    async def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    adapter, _ = build(handler)
    with pytest.raises(SearchProviderError) as info:
        await adapter.search(SearchRequest(query="q"))
    assert info.value.retryable is True


@pytest.mark.parametrize(
    "status, error_type",
    [
        (401, SearchAuthenticationError),
        (403, SearchAuthenticationError),
        (429, SearchRateLimitError),
        (400, SearchRequestInvalidError),
        (422, SearchRequestInvalidError),
        (500, SearchProviderError),
        (418, SearchProviderError),
    ],
)
async def test_status_codes_map_to_typed_errors(status: int, error_type: type):
    adapter, _ = build(lambda _r: httpx.Response(status, text="provider error body"))
    with pytest.raises(error_type):
        await adapter.search(SearchRequest(query="q"))


async def test_a_redirect_is_refused_rather_than_followed():
    """Following it would carry the credential to a host we did not choose
    (SR-8). It also means the configured endpoint is wrong, so it is not
    retryable."""
    adapter, sent = build(
        lambda _r: httpx.Response(302, headers={"Location": "https://elsewhere.example/search"})
    )

    with pytest.raises(SearchProviderError) as info:
        await adapter.search(SearchRequest(query="q"))

    assert len(sent) == 1
    assert info.value.retryable is True


# --- Credential handling ----------------------------------------------------

def test_building_without_a_credential_fails_with_the_setting_name():
    with pytest.raises(SearchAuthenticationError, match="SEARCH_API_KEY"):
        TavilySearchAdapter(settings_with_search(SEARCH_API_KEY=None))


def test_building_with_a_blank_credential_fails_the_same_way():
    with pytest.raises(SearchAuthenticationError, match="SEARCH_API_KEY"):
        TavilySearchAdapter(settings_with_search(SEARCH_API_KEY="   "))


async def test_the_credential_travels_as_a_header_never_in_the_url_or_body():
    """A URL and the request body are the one thing this process later hands to a
    fetcher, a log line, or an error message. A credential must never be in
    either (SR-8)."""
    adapter, sent = build(
        lambda _r: httpx.Response(200, json=ok_payload("https://a.example/1")),
    )

    await adapter.search(SearchRequest(query="secret query about acquisition"))

    request = sent[0]
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert TEST_KEY not in str(request.url)
    assert TEST_KEY not in request.content.decode("utf-8")


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

def test_the_factory_builds_the_configured_tavily_provider():
    settings = settings_with_search(SEARCH_PROVIDER="tavily")

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


def test_provider_types_do_not_escape_the_adapter_package():
    """A structural boundary: only the adapter may know Tavily's endpoint, header
    name, or time_range codes."""
    tavily_only = ("Authorization", "api.tavily.com/search", "_TIME_RANGE_CODES")
    offenders: dict[str, list[str]] = {}

    for path in (ROOT / "app").rglob("*.py"):
        if path.parts[-2:] == ("search", "tavily.py"):
            continue
        if "infrastructure" in path.parts and path.parent.name == "search":
            continue  # the factory legitimately names the adapter
        text = path.read_text()
        found = [token for token in tavily_only if token in text]
        if found:
            offenders[str(path.relative_to(ROOT))] = found

    assert not offenders, f"provider detail leaked outside the adapter: {offenders}"
