"""Engine and session ownership.

The engine is a **process** resource, created once and disposed on shutdown,
exactly like the Telegram HTTP session that audit C-3 was about. Creating an
engine per request would leak connection pools, so nothing here builds one
inside a request.

Two access paths, one implementation:

* ``async with db.session() as session`` — used by the API/poller process, which
  already has a running loop.
* ``db.run(work)`` — used by the Celery worker, whose task bodies are
  synchronous. It drives the coroutine on the process's long-lived loop
  (:mod:`app.infrastructure.asyncio_runtime`), which is the same mechanism
  Phase 1 established for Telegram delivery.

Test isolation lives here too: :func:`resolve_database_url` refuses to hand back
a database whose name does not look like a test database when a test URL was
requested, so a stray ``DATABASE_URL`` cannot silently become the test target
(Phase 2 § 23).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Suffix a database name must carry to be accepted as a test target.
TEST_DB_SUFFIX = "_test"


class DatabaseMisconfiguredError(RuntimeError):
    """The configured database URL is unusable or unsafe for the current use."""


def resolve_database_url(url: str | None, *, require_test: bool = False) -> str:
    """Validate and return a database URL.

    ``require_test=True`` is the test-isolation guard. It requires a name ending
    in ``_test`` so that a misconfigured ``TEST_DATABASE_URL`` pointing at a
    development or production database fails loudly instead of truncating it.
    """
    if not url:
        raise DatabaseMisconfiguredError(
            "No database URL is configured. Set DATABASE_URL (or TEST_DATABASE_URL "
            "for the test suite). See .env.example."
        )
    if "://" not in url:
        raise DatabaseMisconfiguredError(
            f"Database URL is not a valid connection URL: {url!r}"
        )
    if require_test:
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if not name.endswith(TEST_DB_SUFFIX):
            raise DatabaseMisconfiguredError(
                f"Refusing to use {name!r} as a test database: its name must end "
                f"in {TEST_DB_SUFFIX!r}. Point TEST_DATABASE_URL at a dedicated "
                "test database so the suite can never touch real data."
            )
    return url


class Database:
    """Owns one async engine and its session factory for a process."""

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5,
                 max_overflow: int = 5) -> None:
        self._url = url
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._echo = echo
        self._pool_size = pool_size
        self._max_overflow = max_overflow

    @property
    def url(self) -> str:
        return self._url

    @property
    def engine(self) -> AsyncEngine:
        """The engine, created on first use and reused for the process lifetime."""
        if self._engine is None:
            self._engine = create_async_engine(
                self._url,
                echo=self._echo,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                # Recycle below the usual 30-minute server-side idle timeout so a
                # long-idle worker does not hand out a dead connection.
                pool_recycle=1800,
                pool_pre_ping=True,
            )
            log.info("database engine created")
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            self._sessionmaker = async_sessionmaker(
                self.engine, expire_on_commit=False, class_=AsyncSession
            )
        return self._sessionmaker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure.

        One session per unit of work, so a turn's writes are a single
        transaction (Phase 2 § 15).
        """
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def run(self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Run ``work(session)`` to completion from synchronous code.

        For the Celery worker, whose task bodies are synchronous. Uses the
        process's long-lived loop rather than creating one per call, which would
        reintroduce audit C-3 for the database pool.
        """
        from app.infrastructure.asyncio_runtime import run_coroutine

        return run_coroutine(self._run(work))

    async def _run(self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async with self.session() as session:
            return await work(session)

    async def ping(self, timeout: float = 2.0) -> bool:
        """True when the database answers a trivial query.

        Used by ``/ready``. Returns a bool rather than raising so a probe failure
        is reportable, not fatal (FR-7.3).
        """
        from sqlalchemy import text

        try:
            async with self.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.warning("database probe failed: %s", type(exc).__name__)
            return False

    async def dispose(self) -> None:
        """Close every pooled connection. Call once, on process shutdown."""
        if self._engine is None:
            return
        engine, self._engine, self._sessionmaker = self._engine, None, None
        await engine.dispose()
        log.info("database engine disposed")
