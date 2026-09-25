"""Process composition root.

Builds the object graph for one process and hands it to the consumer, instead
of exposing module-level singletons (P1-6, M-4).

This is also what breaks the original circular dependency:
``app/bot/handlers.py`` imported from ``app/tasks/``, and
``app/tasks/research_task.py`` imported ``bot`` from ``app/bot/dispatcher.py``.
Neither module imports the other any more; both receive what they need.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from aiogram import Bot, Dispatcher

from app.core.config import Settings, get_settings
from app.infrastructure.llm import LLMClient, OpenAICompatibleClient
from app.infrastructure.telegram import create_bot, create_dispatcher
from app.services.delivery import DeliveryService
from app.services.rate_limit import RateLimiter
from app.services.research import ResearchService


@dataclass
class AppContainer:
    """Everything one process needs, constructed once."""

    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    llm: LLMClient
    delivery: DeliveryService
    research: ResearchService
    rate_limiter: RateLimiter

    def close(self) -> None:
        """Release network resources from sync code (Celery worker)."""
        self.delivery.close()

    async def aclose(self) -> None:
        """Release network resources from async code (ASGI lifespan)."""
        await self.delivery.aclose()


def build_container(settings: Settings | None = None) -> AppContainer:
    """Construct the object graph for the current process."""
    settings = settings or get_settings()
    bot = create_bot(settings)
    dispatcher = create_dispatcher()
    llm = OpenAICompatibleClient(settings)
    delivery = DeliveryService(bot, settings)
    research = ResearchService(llm=llm, delivery=delivery, settings=settings)
    rate_limiter = RateLimiter(settings)
    return AppContainer(
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        llm=llm,
        delivery=delivery,
        research=research,
        rate_limiter=rate_limiter,
    )


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    """Process-wide container, built on first use."""
    return build_container()


def reset_container() -> None:
    """Drop the cached container. Test support."""
    get_container.cache_clear()
