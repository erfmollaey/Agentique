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
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

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

    return AppContainer(
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        llm=llm,
        delivery=delivery,
        rate_limiter=rate_limiter,
        database=database,
        chat=chat,
    )


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    """Process-wide container, built on first use."""
    return build_container()


def reset_container() -> None:
    """Drop the cached container. Test support."""
    get_container.cache_clear()
