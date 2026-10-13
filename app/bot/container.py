"""Process composition root.

Builds the object graph for one process and hands it to the consumer, instead
of exposing module-level singletons (P1-6, M-4).

This is also what breaks the original circular dependency:
``app/bot/handlers.py`` imported from ``app/tasks/``, and
``app/tasks/research_task.py`` imported ``bot`` from ``app/bot/dispatcher.py``.
Neither module imports the other any more; both receive what they need.

Phase 2 adds the persistence graph. The engine is a process resource, created
here and disposed on shutdown, for the same reason the Telegram session is: a
per-request engine would leak a connection pool.

Phase 3 (P3.9-B, B3) adds Research dependency wiring: search provider, fetch
client, extractor, planner, evidence adapters, synthesizer, and the research
unit-of-work factory. These are constructed explicitly here so the composition
is testable and the shared dependencies (LLM provider, Database, UnitOfWork)
are reused rather than re-created.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from aiogram import Bot, Dispatcher

from app.core.config import Settings, get_settings
from app.db.session import Database, resolve_database_url
from app.db.unit_of_work import sqlalchemy_uow_factory
from app.infrastructure.llm import create_llm_client
from app.infrastructure.telegram import create_bot, create_dispatcher
from app.services.chat import ChatService
from app.services.context import ContextBuilder
from app.services.delivery import DeliveryService
from app.services.rate_limit import RateLimiter
from app.services.research.orchestrator import ResearchOrchestrator


@dataclass
class AppContainer:
    """Everything one process needs, constructed once."""

    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    llm: object
    delivery: DeliveryService
    rate_limiter: RateLimiter
    # Phase 2. Optional so a Phase 1 style test double can still build a
    # container; the real builder always supplies both.
    database: Database | None = None
    chat: ChatService | None = None

    # Phase 3 (P3.9-B, B3). Research dependencies, constructed explicitly
    # through the factories in ``app.infrastructure.*``. Each factory takes
    # the shared ``llm`` (never constructs its own) and ``settings``.
    # Optional so existing tests that inject their own ``AppContainer`` are
    # unaffected; the real builder below always supplies them.
    search_provider: object | None = None
    fetch_client: object | None = None
    extractor: object | None = None
    planner: object | None = None
    source_summarizer: object | None = None
    claim_extractor: object | None = None
    synthesizer: object | None = None
    research_uow_factory: Callable[[], Any] | None = None
    # P3.9-D: the research pipeline and the intake path. ``research`` is the
    # orchestrator, typed as the pipeline entry point so a task body can call
    # ``execute`` and mypy checks it against the real signature rather than an
    # ``object``. Optional so existing tests that build their own ``AppContainer``
    # are unaffected; the real builder below always supplies them.
    research: ResearchOrchestrator | None = None
    intake: Any = None
    research_rate_limiter: Any = None

    def close(self) -> None:
        """Release network resources from sync code (Celery worker)."""
        self.delivery.close()

    async def aclose(self) -> None:
        """Release network resources from async code (ASGI lifespan)."""
        await self.delivery.aclose()
        if self.database is not None:
            await self.database.dispose()


def build_container(settings: Settings | None = None) -> AppContainer:
    """Construct the object graph for the current process."""
    settings = settings or get_settings()

    bot = create_bot(settings)
    dispatcher = create_dispatcher()
    llm = create_llm_client(settings)
    delivery = DeliveryService(bot, settings)
    rate_limiter = RateLimiter(settings)

    database = Database(
        resolve_database_url(settings.DATABASE_URL),
        echo=settings.DATABASE_ECHO,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_POOL_MAX_OVERFLOW,
    )
    chat = ChatService(
        settings=settings,
        llm=llm,
        uow_factory=sqlalchemy_uow_factory(database),
        context=ContextBuilder(settings),
    )

    # Phase 3 (P3.9-B, B3): Research dependency wiring.
    # Each factory receives the shared LLM provider; none constructs its own.
    # The search provider and fetch client own their HTTP transports; the
    # extractor, planner, evidence adapters, and synthesizer do not.
    #
    # FR-9/SR-5: the four research adapters make several provider calls per run and
    # nothing measured what they cost. They receive the shared provider behind
    # `UsageRecordingLLM`, which forwards every reply untouched and records only the
    # usage the provider reported, into whichever run is executing. `ChatService`
    # keeps the raw provider above and its own per-turn accounting — the two ledgers
    # must not be mixed.
    from app.services.research.usage import UsageRecordingLLM

    research_llm = UsageRecordingLLM(llm)
    search_provider = _create_search_provider(settings)
    fetch_client = _create_fetch_client(settings)
    extractor = _create_extractor(settings)
    planner = _create_planner(settings, research_llm)
    source_summarizer = _create_source_summarizer(settings, research_llm)
    claim_extractor = _create_claim_extractor(settings, research_llm)
    synthesizer = _create_synthesizer(settings, research_llm)
    research_uow_factory = sqlalchemy_uow_factory(database)
    research, intake, research_rate_limiter = _build_research_pipeline(
        settings,
        research_llm,
        research_uow_factory,
        provider=search_provider,
        fetcher=fetch_client,
        extractor=extractor,
        summarizer=source_summarizer,
        claim_extractor=claim_extractor,
        synthesizer=synthesizer,
    )

    return AppContainer(
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        llm=llm,
        delivery=delivery,
        rate_limiter=rate_limiter,
        database=database,
        chat=chat,
        search_provider=search_provider,
        fetch_client=fetch_client,
        extractor=extractor,
        planner=planner,
        source_summarizer=source_summarizer,
        claim_extractor=claim_extractor,
        synthesizer=synthesizer,
        research_uow_factory=research_uow_factory,
        research=research,
        intake=intake,
        research_rate_limiter=research_rate_limiter,
    )


# --- research pipeline helpers (P3.9-D) --------------------------------------


def _build_research_pipeline(
    settings: Settings,
    llm: object,
    research_uow_factory: Callable[[], Any],
    *,
    provider: Any,
    fetcher: Any,
    extractor: Any,
    summarizer: Any,
    claim_extractor: Any,
    synthesizer: Any,
) -> tuple[Any, Any, Any]:
    """Build the research pipeline, its intake path, and its rate limiter.

    The composition point P3.9-B left open. Every collaborator is the one the
    container already built above — the shared ``llm``, the same
    ``research_uow_factory``, the same provider adapters — so nothing here
    constructs a second client, a second session factory, or a second object graph.

    The three services this wires are the ones the P3.9 audit found had no
    production caller: ``PlanningService``, ``SourceEvidenceService``, and
    ``SynthesisService`` each take the shared provider and the shared uow factory.
    ``SourceChunkService`` is the same, and ``create_deduplicator`` /
    ``create_ranking_policy`` are pure policies with no configuration error path.

    Returns ``(orchestrator, intake, research_rate_limiter)``.
    """
    from app.domain.research.outcome import CooperativeDeadline
    from app.infrastructure.planning import create_planner
    from app.infrastructure.ranking import (
        create_deduplicator,
        create_ranking_policy,
    )
    from app.services.research.evidence import SourceEvidenceService
    from app.services.research.intake import ResearchIntakeService
    from app.services.research.orchestrator import ResearchOrchestrator
    from app.services.research.planning import PlanningService
    from app.services.research.synthesis import SynthesisService

    planner = create_planner(settings, llm=llm)
    chunking = _create_chunk_service(settings, research_uow_factory)
    planning = PlanningService(
        settings=settings, planner=planner, uow_factory=research_uow_factory
    )
    evidence = SourceEvidenceService(
        settings=settings,
        summarizer=summarizer,
        extractor=claim_extractor,
        uow_factory=research_uow_factory,
    )
    synthesis = SynthesisService(
        settings=settings,
        synthesizer=synthesizer,
        uow_factory=research_uow_factory,
    )

    # AD-045: the cooperative deadline comes from configuration and is None when
    # unset, which is the documented state of a value that has never been measured
    # (audit § S.7). It is injected so a test can control time rather than read it.
    deadline = CooperativeDeadline.from_budget(settings.RESEARCH_DEADLINE_SECONDS)
    orchestrator = ResearchOrchestrator(
        settings=settings,
        uow_factory=research_uow_factory,
        planning=planning,
        search_provider=provider,
        fetcher=fetcher,
        extractor=extractor,
        deduplicator=create_deduplicator(settings),
        ranking=create_ranking_policy(settings),
        chunking=chunking,
        evidence=evidence,
        synthesis=synthesis,
        deadline=deadline,
    )
    intake = ResearchIntakeService(
        settings=settings, uow_factory=research_uow_factory
    )
    # AD-047: research gets a dedicated allowance, in a separate limiter instance
    # from chat's, keyed by the same Telegram user id. The numbers are the
    # existing, already-justified settings rather than new ones, because no
    # measured cost per research run exists to justify any other figure (§ S.6.4).
    research_rate_limiter = RateLimiter(settings)
    return orchestrator, intake, research_rate_limiter


def _create_chunk_service(settings: Settings, uow_factory: Callable[[], Any]):
    """Build the passage producer (B1, P3.9-A)."""
    from app.services.research.chunking import SourceChunkService

    return SourceChunkService(uow_factory=uow_factory)


# --- research factory helpers (P3.9-B, B3) ----------------------------------

def _create_search_provider(settings: Settings) -> object:
    """Build the configured search provider.

    Delegates to ``app.infrastructure.search.create_search_provider``. The
    adapter owns its HTTP client; missing credentials raise a typed
    ``SearchAuthenticationError`` at construction time rather than on first
    use, so the application fails closed when search is configured but not
    usable.
    """
    from app.infrastructure.search import create_search_provider
    return create_search_provider(settings)


def _create_fetch_client(settings: Settings) -> object:
    """Build the restricted fetch client (SSRF boundary, AD-031).

    Delegates to ``app.infrastructure.fetch.create_fetch_client``. The client
    owns its transport; it is closed by the container's async shutdown.
    """
    from app.infrastructure.fetch import create_fetch_client
    return create_fetch_client(settings)


def _create_extractor(settings: Settings) -> object:
    """Build the content extractor (P3.4).

    Delegates to ``app.infrastructure.extract.create_extractor``. The
    extractor performs no I/O and owns no network resource.
    """
    from app.infrastructure.extract import create_extractor
    return create_extractor(settings)


def _create_planner(settings: Settings, llm: object) -> object:
    """Build the research planner (P3.6).

    Delegates to ``app.infrastructure.planning.create_planner``. The planner
    receives the shared LLM provider; it never constructs its own.
    """
    from app.infrastructure.planning import create_planner
    return create_planner(settings, llm=llm)


def _create_source_summarizer(settings: Settings, llm: object) -> object:
    """Build the source summarizer (P3.7).

    Delegates to ``app.infrastructure.evidence.create_source_summarizer``.
    Receives the shared LLM provider.
    """
    from app.infrastructure.evidence import create_source_summarizer
    return create_source_summarizer(settings, llm=llm)


def _create_claim_extractor(settings: Settings, llm: object) -> object:
    """Build the claim extractor (P3.7).

    Delegates to ``app.infrastructure.evidence.create_claim_extractor``.
    Receives the shared LLM provider.
    """
    from app.infrastructure.evidence import create_claim_extractor
    return create_claim_extractor(settings, llm=llm)


def _create_synthesizer(settings: Settings, llm: object) -> object:
    """Build the synthesizer (P3.8).

    Delegates to ``app.infrastructure.synthesis.create_synthesizer``.
    Receives the shared LLM provider.
    """
    from app.infrastructure.synthesis import create_synthesizer
    return create_synthesizer(settings, llm=llm)


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    """Process-wide container, built on first use."""
    return build_container()


def reset_container() -> None:
    """Drop the cached container. Test support."""
    get_container.cache_clear()
