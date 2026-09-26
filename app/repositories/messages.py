"""Message persistence.

Two behaviours here are load-bearing.

**Ordering (Phase 2 § 10).** History is ordered by ``(created_at, id)`` in the
database, not in Python and not by insertion order. Two messages committed in
one transaction can share a timestamp; the ``id`` tiebreak makes reconstruction
deterministic.

**Idempotency (Phase 2 § 15, § 16).** Uniqueness is enforced by the database
(``uq_messages_turn_id_role`` and ``uq_messages_telegram_message``), and a
violation is translated into :class:`DuplicateMessageError`. The caller decides
what a duplicate means for its path; the repository never guesses.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageModel
from app.domain.chat import MessageRecord
from app.domain.errors import DuplicateMessageError

log = logging.getLogger(__name__)


class MessageRepositoryImpl:
    """SQLAlchemy implementation of the message port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        message: MessageRecord,
        *,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
    ) -> MessageRecord:
        model = MessageModel(
            conversation_id=message.conversation_id,
            role=message.role.value,
            content=message.content,
            turn_id=message.turn_id,
            model=message.model,
            prompt_tokens=message.prompt_tokens,
            completion_tokens=message.completion_tokens,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
        )
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Deliberately no savepoint.
            #
            # A savepoint looks like the right tool — roll back only the bad
            # statement and keep the caller's transaction usable — but it does
            # not deliver that here. When the savepoint's parent transaction has
            # not yet issued any SQL, rolling the savepoint back still leaves
            # the Session in a pending-rollback state, and the caller's commit
            # then fails with PendingRollbackError. Verified against
            # SQLAlchemy 2.0.36 + asyncpg rather than assumed.
            #
            # Letting the violation propagate is the honest contract: the
            # enclosing unit of work rolls back, and the caller decides what a
            # duplicate means for its path. Nothing is half-written either way,
            # because a unit of work is the transaction.
            raise DuplicateMessageError("message already recorded") from exc
        return MessageRecord.model_validate(model)

    async def history(
        self, conversation_id: int, limit: int = 50
    ) -> Sequence[MessageRecord]:
        """Up to ``limit`` messages, oldest first.

        Takes the newest ``limit`` rows but returns them ascending, so a long
        conversation is trimmed from the *oldest* end — the end the context
        budget discards anyway.
        """
        result = await self._session.execute(
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.created_at.desc(), MessageModel.id.desc())
            .limit(limit)
        )
        records = [MessageRecord.model_validate(m) for m in result.scalars()]
        records.reverse()
        return records

    async def get(self, message_id: int) -> MessageRecord | None:
        result = await self._session.execute(
            select(MessageModel).where(MessageModel.id == message_id)
        )
        model = result.scalar_one_or_none()
        return MessageRecord.model_validate(model) if model else None

    async def find_by_turn(
        self, conversation_id: int, turn_id: str, role: str
    ) -> MessageRecord | None:
        result = await self._session.execute(
            select(MessageModel).where(
                MessageModel.conversation_id == conversation_id,
                MessageModel.turn_id == turn_id,
                MessageModel.role == role,
            )
        )
        model = result.scalar_one_or_none()
        return MessageRecord.model_validate(model) if model else None

    async def delete_for_conversation(self, conversation_id: int) -> int:
        result = await self._session.execute(
            delete(MessageModel).where(MessageModel.conversation_id == conversation_id)
        )
        return int(result.rowcount or 0)

    async def total_tokens_for_user(self, user_id: int) -> int:
        """Tokens attributed to a user, for the quota decision (FR-28, FR-30)."""
        from app.db.models import ConversationModel

        result = await self._session.execute(
            select(MessageModel.prompt_tokens + MessageModel.completion_tokens)
            .join(ConversationModel, ConversationModel.id == MessageModel.conversation_id)
            .where(ConversationModel.user_id == user_id)
        )
        return sum(int(v) for v in result.scalars())
