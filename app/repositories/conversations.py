"""Conversation persistence.

Every read that can reach user data takes the owning ``user_id``. There is no
``get(conversation_id)`` without an owner, so there is no code path that can
return one user's conversation to another (SR-4, T-16). Ownership is a query
predicate, not a check the caller might forget.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConversationModel
from app.domain.chat import Conversation, ConversationStatus


class ConversationRepositoryImpl:
    """SQLAlchemy implementation of the conversation port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: int, title: str | None) -> Conversation:
        model = ConversationModel(user_id=user_id, title=title, status=ConversationStatus.ACTIVE.value)
        self._session.add(model)
        await self._session.flush()
        return Conversation.model_validate(model)

    async def get_for_user(self, conversation_id: int, user_id: int) -> Conversation | None:
        result = await self._session.execute(
            select(ConversationModel).where(
                ConversationModel.id == conversation_id,
                ConversationModel.user_id == user_id,
            )
        )
        model = result.scalar_one_or_none()
        return Conversation.model_validate(model) if model else None

    async def get_active(self, user_id: int) -> Conversation | None:
        """The user's current conversation: the most recently updated active one.

        Derived by query rather than read from a pointer on the user row. See
        AD-016 for why a stored pointer would need rewriting in Phase 5.
        """
        result = await self._session.execute(
            select(ConversationModel)
            .where(
                ConversationModel.user_id == user_id,
                ConversationModel.status == ConversationStatus.ACTIVE.value,
            )
            .order_by(ConversationModel.updated_at.desc(), ConversationModel.id.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return Conversation.model_validate(model) if model else None

    async def list_for_user(self, user_id: int, limit: int = 20) -> Sequence[Conversation]:
        result = await self._session.execute(
            select(ConversationModel)
            .where(ConversationModel.user_id == user_id)
            .order_by(ConversationModel.updated_at.desc(), ConversationModel.id.desc())
            .limit(limit)
        )
        return [Conversation.model_validate(m) for m in result.scalars()]

    async def count_for_user(self, user_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(ConversationModel)
            .where(ConversationModel.user_id == user_id)
        )
        return int(result.scalar_one())

    async def set_status(self, conversation_id: int, status: str) -> None:
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(status=status, updated_at=func.now())
        )

    async def touch(self, conversation_id: int) -> None:
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(updated_at=func.now())
        )
