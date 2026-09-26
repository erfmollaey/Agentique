"""Unit of work: one transaction per logical operation.

The service layer must not know that a session exists, and repositories must not
outlive their transaction. The unit of work is the seam: it hands out
repositories already bound to a single :class:`AsyncSession`, and commits or
rolls back once at the end.

A chat turn is therefore one transaction, which is what makes Phase 2 § 15
tractable: the user message and its assistant reply are committed together, so a
crash between them cannot leave half a turn behind.
"""

from __future__ import annotations

import logging
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.repositories.conversations import ConversationRepositoryImpl
from app.repositories.messages import MessageRepositoryImpl
from app.repositories.users import UserRepositoryImpl

log = logging.getLogger(__name__)


class SqlAlchemyUnitOfWork:
    """Repositories bound to one session, with a single commit boundary."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._database.sessionmaker()
        self.users = UserRepositoryImpl(self._session)
        self.conversations = ConversationRepositoryImpl(self._session)
        self.messages = MessageRepositoryImpl(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session, self._session = self._session, None
        if session is None:  # pragma: no cover - defensive
            return
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        except Exception:
            log.exception("unit of work failed to finish cleanly")
            raise
        finally:
            await session.close()


def sqlalchemy_uow_factory(database: Database):
    """Return a zero-argument factory producing :class:`SqlAlchemyUnitOfWork`."""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(database)

    return factory
