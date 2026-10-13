"""Search domain contract (Phase 3 P3.2).

Covers the provider-independent half: request validation, result normalization,
the ``SearchResult`` ≠ ``Source`` distinction, and the structural guarantee that
no provider or HTTP type reaches this layer.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.research import search as search_module
from app.domain.research.ports import SearchProvider
from app.domain.research.search import (
    MAX_QUERY_CHARS,
    MAX_RESULTS_LIMIT,
    SafeSearchLevel,
    SearchFreshness,
    SearchRequest,
    SearchResponse,
    SearchResult,
    domain_of,
)

ROOT = Path(__file__).resolve().parents[1]
DOMAIN_DIR = ROOT / "app" / "domain"

# --- Valid requests ---------------------------------------------------------

def test_a_plain_query_is_a_valid_request():
    request = SearchRequest(query="python asyncio task groups")
    assert request.query == "python asyncio task groups"
    assert 1 <= request.max_results <= MAX_RESULTS_LIMIT
    assert request.language is None
    assert request.country is None
    assert request.freshness is None


def test_whitespace_in_a_query_is_collapsed():
    """Collapse, do not reject: the pipeline builds queries from user questions."""
    assert SearchRequest(query="  postgresql   18\n\tjson ").query == "postgresql 18 json"


def test_optional_controls_are_accepted():
    request = SearchRequest(
        query="قیمت نفت",
        max_results=3,
        language="fa",
        country="IR",
        freshness=SearchFreshness.WEEK,
        safe_search=SafeSearchLevel.STRICT,
    )
    assert request.language == "fa"
    assert request.country == "IR"
    assert request.freshness is SearchFreshness.WEEK
    assert request.safe_search is SafeSearchLevel.STRICT


def test_language_and_country_are_normalized():
    request = SearchRequest(query="x", language="  FA ", country="ir")
    assert request.language == "fa", "a language tag is lowercased"
    assert request.country == "IR", "a country code is uppercased"


def test_blank_optional_controls_become_unset():
    """An empty string is how configuration expresses 'no hint'; it must not
    become an empty provider parameter."""
    request = SearchRequest(query="x", language="   ", country="")
    assert request.language is None
    assert request.country is None


def test_enums_are_coerced_from_strings():
    request = SearchRequest(query="x", freshness="month", safe_search="STRICT")
    assert request.freshness is SearchFreshness.MONTH
    assert request.safe_search is SafeSearchLevel.STRICT


# --- Invalid requests -------------------------------------------------------

@pytest.mark.parametrize("query", ["", "   ", "\n\t "])
def test_an_empty_query_is_rejected(query):
    with pytest.raises(ValidationError, match="non-empty"):
        SearchRequest(query=query)


def test_an_over_long_query_is_rejected():
    with pytest.raises(ValidationError, match="at most"):
        SearchRequest(query="x" * (MAX_QUERY_CHARS + 1))


def test_a_non_string_query_is_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(query=42)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1, MAX_RESULTS_LIMIT + 1])
def test_result_count_is_bounded(limit):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", max_results=limit)


@pytest.mark.parametrize("language", ["english language name with spaces", "fa;drop", "en/GB"])
def test_a_malformed_language_tag_is_rejected(language):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", language=language)


@pytest.mark.parametrize("country", ["IRN", "i", "1R"])
def test_a_malformed_country_code_is_rejected(country):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", country=country)


def test_an_unknown_freshness_value_is_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(query="x", freshness="decade")


def test_an_unknown_safe_search_level_is_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(query="x", safe_search="paranoid")


def test_a_request_is_immutable():
    """Search results are untrusted input travelling through the pipeline; a
    stage that could mutate the request in place could mutate what another
    stage believes was validated."""
    request = SearchRequest(query="x")
    with pytest.raises(ValidationError):
        request.query = "something else"  # type: ignore[misc]


# --- Result normalization ---------------------------------------------------

def test_a_result_derives_its_domain_from_the_url():
    result = SearchResult.from_provider_fields(
        url="https://Docs.PostgreSQL.example:5432/notes/release-18.html",
        rank=1,
        title="Release notes",
        snippet="What changed",
    )
    assert result.domain == "docs.postgresql.example"
    assert result.rank == 1


def test_a_trailing_dot_and_case_in_the_host_are_normalized():
    assert domain_of("https://Example.COM./a") == "example.com"


@pytest.mark.parametrize("url", ["", "   ", "not-a-url", "https://", "/relative/path"])
def test_a_url_with_no_usable_host_is_rejected(url):
    with pytest.raises(ValueError):
        SearchResult.from_provider_fields(url=url, rank=1)


def test_absent_and_blank_text_fields_become_none():
    """Providers disagree on whether "no title" is absent, null, or empty.
    A later stage must not have to tell the three apart."""
    result = SearchResult.from_provider_fields(
        url="https://example.com/a", rank=1, title="", snippet="   "
    )
    assert result.title is None
    assert result.snippet is None


def test_whitespace_in_title_and_snippet_is_collapsed():
    result = SearchResult.from_provider_fields(
        url="https://example.com/a", rank=1, title="A  title", snippet="line\n\nbreak"
    )
    assert result.title == "A title"
    assert result.snippet == "line break"


def test_a_naive_publication_timestamp_is_read_as_utc():
    """Interpreted rather than discarded, so an undated-but-real date is not
    lost, and explicitly UTC so no later stage guesses a zone."""
    result = SearchResult.from_provider_fields(
        url="https://example.com/a", rank=1, published_at=datetime(2026, 1, 2, 3, 4)
    )
    assert result.published_at is not None
    assert result.published_at.tzinfo is not None
    assert result.published_at == datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def test_an_unusable_relevance_value_is_dropped_rather_than_guessed():
    result = SearchResult.from_provider_fields(
        url="https://example.com/a", rank=1, relevance="high"
    )
    assert result.relevance is None


def test_a_result_requires_a_one_based_position():
    with pytest.raises(ValidationError, match="1-based"):
        SearchResult(url="https://example.com/a", domain="example.com", rank=0)


# --- The response -----------------------------------------------------------

def _result(url: str, rank: int) -> SearchResult:
    return SearchResult.from_provider_fields(url=url, rank=rank)


def test_an_empty_response_is_valid_and_declares_itself_empty():
    response = SearchResponse(query="x", provider="brave")
    assert response.results == ()
    assert response.is_empty is True


def test_response_reports_distinct_domains_in_first_appearance_order():
    response = SearchResponse(
        query="x",
        provider="brave",
        results=(
            _result("https://a.example/1", 1),
            _result("https://b.example/2", 2),
            _result("https://a.example/3", 3),
        ),
    )
    assert response.domains == ("a.example", "b.example")
    assert response.is_empty is False


def test_two_results_may_not_claim_the_same_position():
    with pytest.raises(ValidationError, match="distinct position"):
        SearchResponse(
            query="x",
            provider="brave",
            results=(_result("https://a.example/1", 1), _result("https://b.example/2", 1)),
        )


def test_a_response_records_when_it_was_retrieved_and_who_answered():
    response = SearchResponse(query="x", provider="brave")
    assert response.retrieved_at.tzinfo is not None, "retrieval time must be unambiguous"
    assert response.provider == "brave", "the provider is recorded for reproducibility"


# --- Boundary: a search result is not a source, and not evidence -------------

def test_a_search_result_is_not_a_fetched_source():
    """The distinction the whole stage rests on: a search result is a URL and a
    snippet a provider wrote, for a page nobody in this process has read. A
    ``Source`` is the persisted record, born PENDING, and a ``SourceChunk`` is
    text extracted at P3.4. Neither exists at this stage, and nothing here may
    imply that a result was verified, fetched, or read."""
    from app.domain.research.types import Source, SourceStatus

    result = SearchResult.from_provider_fields(
        url="https://example.com/a", rank=1, snippet="a provider-written summary"
    )
    source = Source(run_id=1, url=result.url, domain=result.domain)

    assert source.status is SourceStatus.PENDING, "a source starts unfetched"
    assert source.content_hash is None, "nothing has been fetched, so nothing is hashed"
    assert not hasattr(result, "status")
    assert not hasattr(result, "content_hash")
    assert not hasattr(result, "text")


def test_the_search_contract_defines_no_fetching_concept():
    """There is deliberately no FetchedSource/SourceChunk type here. Fetching is
    P3.3 and its output contract is its own stage's to define; adding one here
    would let a stage start treating a search result as retrieved evidence."""
    names = {name for name in dir(search_module) if not name.startswith("_")}
    assert "FetchedSource" not in names
    assert "SourceChunk" not in names


# --- Boundary: no provider or HTTP types in the domain ----------------------

def test_the_search_port_is_runtime_checkable():
    class Fake:
        async def search(self, request: SearchRequest) -> SearchResponse:
            return SearchResponse(query=request.query, provider="fake")

    assert isinstance(Fake(), SearchProvider)
    assert not isinstance(object(), SearchProvider)


def test_the_domain_layer_imports_no_provider_or_http_module():
    """Structural, not behavioural: the dependency direction is domain →
    SearchPort → adapter → provider API, never domain → provider SDK."""
    forbidden = {"httpx", "openai", "tavily", "brave", "requests", "urllib3", "aiohttp"}
    offenders: dict[str, set[str]] = {}
    for path in DOMAIN_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module.split(".")[0])
        leaked = found & forbidden
        if leaked:
            offenders[str(path.relative_to(ROOT))] = leaked
    assert not offenders, f"infrastructure types reached the domain: {offenders}"


def test_the_domain_does_not_import_its_own_adapters():
    """The adapter must not be reachable from domain code, by any spelling.

    Checked against the parsed imports rather than the file text, because a
    docstring may legitimately *name* ``app.infrastructure`` while explaining why
    domain code does not import from it.
    """
    offenders: dict[str, set[str]] = {}
    for path in DOMAIN_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    continue
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
                found.add(module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
        leaked = {name for name in found if name.startswith("app.infrastructure")}
        if leaked:
            offenders[str(path.relative_to(ROOT))] = leaked
    assert not offenders, f"domain code imports the infrastructure layer: {offenders}"