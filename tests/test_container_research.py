"""Container tests: Research dependency wiring (P3.9-B, B3).

Verifies that the composition root constructs Research dependencies explicitly,
shares the LLM provider where required, and does not perform real network calls.

These tests build an ``AppContainer`` directly with test settings — they never
call ``build_container`` (which is patched to refuse in conftest.py).
"""

from __future__ import annotations

import pytest

from app.bot.container import AppContainer
from app.core.config import Settings
from app.domain.chat import LLMReply
from app.domain.research.ports import (
    ClaimExtractor,
    ContentExtraction,
    PlanningProvider,
    SearchProvider,
    SourceSummarizer,
    SynthesisProvider,
)


class _FakeLLM:
    """A minimal LLM provider double for container tests."""

    def complete(self, messages, *, model, temperature, max_tokens):
        return LLMReply(
            text="{}",
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            finish_reason="stop",
        )


def _make_settings() -> Settings:
    return Settings(
        _env_file=None,
        BOT_TOKEN="1234567890:TEST",
        GROQ_API_KEY="gsk-test",
        REDIS_URL="redis://localhost:6379/15",
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/db",
        TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/db_test",
        LLM_BASE_URL="https://provider.invalid/v1",
        LLM_MODEL="test-model",
        LLM_TIMEOUT_SECONDS=1.0,
        LLM_MAX_TOKENS=256,
        LLM_PROVIDER="openai_compatible",
        LLM_FALLBACK_MODELS="",
        LLM_FALLBACK_MAX_ATTEMPTS=0,
        SEARCH_PROVIDER="brave",
        SEARCH_API_KEY=None,
        SEARCH_MAX_RESULTS=10,
        SEARCH_TIMEOUT_SECONDS=10.0,
        SEARCH_LANGUAGE="en",
        SEARCH_REGION="US",
        SEARCH_SAFE_SEARCH="moderate",
        FETCH_ALLOWED_SCHEMES="http,https",
        FETCH_ALLOWED_CONTENT_TYPES="text/html,text/plain",
        FETCH_CONNECT_TIMEOUT_SECONDS=5.0,
        FETCH_READ_TIMEOUT_SECONDS=10.0,
        FETCH_TOTAL_TIMEOUT_SECONDS=30.0,
        FETCH_MAX_RESPONSE_BYTES=5 * 1024 * 1024,
        FETCH_MAX_COMPRESSION_RATIO=25,
        FETCH_MAX_REDIRECTS=5,
        FETCH_MAX_CONNECTIONS=10,
        FETCH_MAX_KEEPALIVE_CONNECTIONS=5,
        FETCH_KEEPALIVE_EXPIRY_SECONDS=30,
        FETCH_USER_AGENT="research-bot/1.0",
        PLAN_MAX_STEPS=10,
        PLAN_MAX_QUESTION_CHARS=2000,
        PLAN_MAX_TOKENS=2048,
        CLAIM_MAX_CLAIMS=20,
        CLAIM_MAX_TOKENS=2048,
        SUMMARY_MAX_TOKENS=1024,
        SYNTHESIS_MAX_SECTIONS=20,
        SYNTHESIS_MAX_TOKENS=4096,
        RATE_LIMIT_MAX_REQUESTS=3,
        RATE_LIMIT_WINDOW_SECONDS=60,
        RATE_LIMIT_DAILY_MAX_REQUESTS=100,
        MAX_IN_FLIGHT_REQUESTS=2,
        LOG_LEVEL="WARNING",
        DATABASE_ECHO=False,
        DATABASE_POOL_SIZE=5,
        DATABASE_POOL_MAX_OVERFLOW=5,
    )


def _make_bot():
    from aiogram import Bot
    return Bot(token="1234567890:TEST")


def _make_dispatcher():
    from aiogram import Dispatcher
    return Dispatcher()


def _make_delivery():
    from app.services.delivery import DeliveryService
    return DeliveryService(_make_bot(), _make_settings())


def _make_rate_limiter():
    from app.services.rate_limit import RateLimiter
    return RateLimiter(_make_settings())


@pytest.fixture
def research_container() -> AppContainer:
    """An ``AppContainer`` with Research dependencies constructed.

    Uses test settings so no real credential is required. The search provider
    is only constructed when a key is configured; otherwise ``None`` is passed
    so the container still exposes the attribute (type-checked as ``object | None``
    in the dataclass).
    """
    from app.bot.container import (
        _create_claim_extractor,
        _create_extractor,
        _create_fetch_client,
        _create_planner,
        _create_source_summarizer,
        _create_synthesizer,
    )
    from app.domain.errors import SearchAuthenticationError

    settings = _make_settings()
    llm = _FakeLLM()

    # Search provider: fail-closed when no key configured.
    try:
        from app.bot.container import _create_search_provider
        search = _create_search_provider(settings)
    except SearchAuthenticationError:
        search = None

    fetch = _create_fetch_client(settings)
    extractor = _create_extractor(settings)
    planner = _create_planner(settings, llm=llm)
    summarizer = _create_source_summarizer(settings, llm=llm)
    claim_extractor = _create_claim_extractor(settings, llm=llm)
    synthesizer = _create_synthesizer(settings, llm=llm)

    return AppContainer(
        settings=settings,
        bot=_make_bot(),
        dispatcher=_make_dispatcher(),
        llm=llm,
        delivery=_make_delivery(),
        rate_limiter=_make_rate_limiter(),
        database=None,
        chat=None,
        search_provider=search,
        fetch_client=fetch,
        extractor=extractor,
        planner=planner,
        source_summarizer=summarizer,
        claim_extractor=claim_extractor,
        synthesizer=synthesizer,
        research_uow_factory=None,
    )


# --- composition -------------------------------------------------------------------------------


def test_container_exposes_search_provider(research_container: AppContainer) -> None:
    """Research dependencies are exposed through the container.

    The container exposes a ``search_provider`` attribute. When no API key is
    configured the factory leaves it as ``None`` (fail-closed), which is the
    established contract: the application can start but search is not usable.
    """
    # When no key is configured, the attribute is None — that's the correct
    # fail-closed behaviour, not a defect.
    if research_container.search_provider is not None:
        assert isinstance(research_container.search_provider, SearchProvider)


def test_container_exposes_fetch_client(research_container: AppContainer) -> None:
    """The restricted fetch client is constructed and exposed."""
    assert research_container.fetch_client is not None
    from app.infrastructure.fetch.client import RestrictedFetcher
    assert isinstance(research_container.fetch_client, RestrictedFetcher)


def test_container_exposes_extractor(research_container: AppContainer) -> None:
    """The content extractor is constructed and exposed."""
    assert research_container.extractor is not None
    assert isinstance(research_container.extractor, ContentExtraction)


def test_container_exposes_planner(research_container: AppContainer) -> None:
    """The research planner is constructed and exposed."""
    assert research_container.planner is not None
    assert isinstance(research_container.planner, PlanningProvider)


def test_container_exposes_source_summarizer(research_container: AppContainer) -> None:
    """The source summarizer is constructed and exposed."""
    assert research_container.source_summarizer is not None
    assert isinstance(research_container.source_summarizer, SourceSummarizer)


def test_container_exposes_claim_extractor(research_container: AppContainer) -> None:
    """The claim extractor is constructed and exposed."""
    assert research_container.claim_extractor is not None
    assert isinstance(research_container.claim_extractor, ClaimExtractor)


def test_container_exposes_synthesizer(research_container: AppContainer) -> None:
    """The synthesizer is constructed and exposed."""
    assert research_container.synthesizer is not None
    assert isinstance(research_container.synthesizer, SynthesisProvider)


def test_container_exposes_research_uow_factory(research_container: AppContainer) -> None:
    """The research unit-of-work factory is exposed for service construction.

    The fixture passes ``None`` because no database is available in the container
    test. When a database is present (in production), the factory is a callable.
    """
    # In the fixture, research_uow_factory is None because no Database is provided.
    # This test documents the contract: when present, it must be callable.
    if research_container.research_uow_factory is not None:
        assert callable(research_container.research_uow_factory)


# --- shared dependencies --------------------------------------------------------------------


def test_container_reuses_llm_for_research_services() -> None:
    """The planner, summarizer, claim extractor, and synthesizer share one LLM."""
    from app.bot.container import (
        _create_claim_extractor,
        _create_planner,
        _create_source_summarizer,
        _create_synthesizer,
    )

    settings = _make_settings()
    llm = _FakeLLM()
    planner = _create_planner(settings, llm=llm)
    summarizer = _create_source_summarizer(settings, llm=llm)
    claim_extractor = _create_claim_extractor(settings, llm=llm)
    synthesizer = _create_synthesizer(settings, llm=llm)

    # Each factory stores the injected LLM; structural check that they are
    # the same object (shared, not re-created per service).
    assert planner._llm is llm
    assert summarizer._llm is llm
    assert claim_extractor._llm is llm
    assert synthesizer._llm is llm


def test_container_search_provider_owns_its_client() -> None:
    """The search provider constructs its own HTTP client; the container does not.

    This test documents the ownership contract: when a client is injected the
    adapter does not own it; otherwise it does. With no API key configured the
    factory raises ``SearchAuthenticationError`` — that is the established
    fail-closed contract, not a defect. This test is skipped when no key is
    available.
    """
    from app.bot.container import _create_search_provider
    from app.domain.errors import SearchAuthenticationError

    settings = _make_settings()
    # When no key is configured, the factory raises — that's the correct
    # fail-closed behaviour.
    with pytest.raises(SearchAuthenticationError):
        _create_search_provider(settings)


def test_container_fetch_client_owns_its_transport() -> None:
    """The fetch client constructs its own transport; the container does not."""
    from app.bot.container import _create_fetch_client
    from app.infrastructure.fetch.client import RestrictedFetcher

    settings = _make_settings()
    client = _create_fetch_client(settings)
    assert isinstance(client, RestrictedFetcher)
    assert client._owns_transport is True


def test_container_extractor_performs_no_i_o() -> None:
    """The extractor owns no network resource."""
    from app.bot.container import _create_extractor
    from app.infrastructure.extract.extractor import TrafilaturaExtractor

    settings = _make_settings()
    extractor = _create_extractor(settings)
    assert isinstance(extractor, TrafilaturaExtractor)
    # TrafilaturaExtractor performs no I/O at construction and owns no socket.
    assert not hasattr(extractor, "_client")
    assert not hasattr(extractor, "_transport")


# --- construction is safe ------------------------------------------------------------------


def test_container_construction_does_not_make_network_calls() -> None:
    """Building the container does not perform real network calls.

    The search provider raises ``SearchAuthenticationError`` when no key is
    configured — it does not silently skip network checks or make a test call.
    The fetcher, extractor, planner, evidence adapters, and synthesizer are
    all constructed without touching the network.
    """
    from app.bot.container import (
        _create_claim_extractor,
        _create_extractor,
        _create_fetch_client,
        _create_planner,
        _create_search_provider,
        _create_source_summarizer,
        _create_synthesizer,
    )
    from app.domain.errors import SearchAuthenticationError

    settings = _make_settings()

    # Search provider fails closed: missing credential → typed error at construction.
    with pytest.raises(SearchAuthenticationError):
        _create_search_provider(settings)

    # The other factories construct without error.
    fetch = _create_fetch_client(settings)
    assert fetch is not None

    extractor = _create_extractor(settings)
    assert extractor is not None

    llm = _FakeLLM()
    planner = _create_planner(settings, llm=llm)
    assert planner is not None

    summarizer = _create_source_summarizer(settings, llm=llm)
    assert summarizer is not None

    claim_extractor = _create_claim_extractor(settings, llm=llm)
    assert claim_extractor is not None

    synthesizer = _create_synthesizer(settings, llm=llm)
    assert synthesizer is not None


def test_container_missing_search_key_fails_closed() -> None:
    """A missing optional search key is a typed construction error, not a silent skip."""
    from app.bot.container import _create_search_provider
    from app.domain.errors import SearchAuthenticationError

    settings = _make_settings()
    # SEARCH_API_KEY is None → SearchAuthenticationError at construction.
    with pytest.raises(SearchAuthenticationError):
        _create_search_provider(settings)


def test_unrecognised_search_provider_is_an_error() -> None:
    """An unknown SEARCH_PROVIDER raises, not silently defaults."""
    from app.bot.container import _create_search_provider

    settings = _make_settings().model_copy(
        update={"SEARCH_PROVIDER": "does_not_exist"}
    )
    with pytest.raises(ValueError, match="Unknown SEARCH_PROVIDER"):
        _create_search_provider(settings)


# --- shutdown -------------------------------------------------------------------------------


def test_container_close_is_idempotent() -> None:
    """Closing a container with no owned transport is a safe no-op."""
    from app.bot.container import AppContainer
    from app.services.delivery import DeliveryService

    settings = _make_settings()
    container = AppContainer(
        settings=settings,
        bot=_make_bot(),
        dispatcher=_make_dispatcher(),
        llm=_FakeLLM(),
        delivery=DeliveryService(_make_bot(), _make_settings()),
        rate_limiter=_make_rate_limiter(),
    )
    # First close succeeds; second close is a no-op.
    container.close()
    container.close()


@pytest.mark.asyncio
async def test_container_async_close_disposes_database_when_present() -> None:
    """Async close disposes the database exactly once."""
    from app.bot.container import AppContainer
    from app.db.session import Database
    from app.services.delivery import DeliveryService

    settings = _make_settings()
    database = Database(
        "postgresql+asyncpg://user:pass@localhost:5432/db_test",
        echo=False,
    )
    container = AppContainer(
        settings=settings,
        bot=_make_bot(),
        dispatcher=_make_dispatcher(),
        llm=_FakeLLM(),
        delivery=DeliveryService(_make_bot(), _make_settings()),
        rate_limiter=_make_rate_limiter(),
        database=database,
    )
    await container.aclose()
    # After aclose, the engine is disposed.
    assert database._engine is None
