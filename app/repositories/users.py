"""User persistence.

Concurrency is handled by the database, not by an application-level check. Two
processes resolving the same Telegram identity at the same time both attempt an
insert; one succeeds and the other gets a unique violation on
``users.telegram_user_id`` and re-reads the winner. A ``get``-then-``insert``
check without this would create duplicate users under load (Phase 2 § 8).
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConversationModel, MessageModel, UserModel
from app.domain.chat import DeletedUserData, User
from app.domain.errors import PersistenceError

log = logging.getLogger(__name__)


class UserRepositoryImpl:
    """SQLAlchemy implementation of :class:`app.domain.ports.UserRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.telegram_user_id == telegram_user_id)
        )
        model = result.scalar_one_or_none()
        return User.model_validate(model) if model else None

    async def get_or_create(self, telegram_user_id: int, username: str | None) -> User:
        existing = await self.get_by_telegram_id(telegram_user_id)
        if existing is not None:
            return existing

        model = UserModel(telegram_user_id=telegram_user_id, username=username)
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Another process created this identity between the lookup above and
            # this insert. The constraint did its job — there is still exactly
            # one user — but this transaction is now unusable, so the unit of
            # work rolls back and the caller retries. On the retry the lookup
            # finds the winner's row.
            #
            # Marked retryable precisely because a retry is the correct response:
            # the race resolves itself, and retrying cannot create a duplicate
            # because the database refuses it.
            error = PersistenceError("concurrent user creation")
            error.retryable = True
            raise error from exc
        return User.model_validate(model)

    async def delete(self, telegram_user_id: int) -> DeletedUserData:
        """Delete the user and everything the database cascades from it (SR-9).

        Scoped by the requesting user's own platform identity. There is no
        parameter through which a caller could name a different user, so this
        cannot delete another user's rows (SR-4).

        The deletion is a **single statement**. The foreign keys already declare
        ``ON DELETE CASCADE`` from ``conversations`` to ``users`` and from
        ``messages`` to ``conversations``, so one delete removes the whole tree
        atomically. Deleting the children explicitly instead would be three
        statements that could partially apply, and would duplicate a guarantee
        the schema already makes. The cascades are narrow: a conversation can
        only belong to one user, and a message only to one conversation, so
        nothing unrelated is reachable.

        Counts are read *before* the delete, purely to report what happened. They
        are metadata, never message content (SR-5).
        """
        user = await self.get_by_telegram_id(telegram_user_id)
        if user is None:
            # Nothing is stored for this identity. Reported honestly rather than
            # as a failure, and without claiming a deletion that did not happen.
            return DeletedUserData(user_id=0, conversations=0, messages=0, deleted=False)

        conversations = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(ConversationModel)
                    .where(ConversationModel.user_id == user.id)
                )
            ).scalar_one()
        )
        messages = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(MessageModel)
                    .join(ConversationModel, ConversationModel.id == MessageModel.conversation_id)
                    .where(ConversationModel.user_id == user.id)
                )
            ).scalar_one()
        )

        await self._session.execute(delete(UserModel).where(UserModel.id == user.id))

        # Identifiers and counts only. Never content (SR-5).
        log.info(
            "user data deleted telegram_id=%s user_id=%s conversations=%s messages=%s",
            telegram_user_id, user.id, conversations, messages,
        )
        return DeletedUserData(
            user_id=user.id,
            conversations=conversations,
            messages=messages,
            deleted=True,
        )
