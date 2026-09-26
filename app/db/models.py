"""ORM models for the AI chat MVP.

Three tables, matching the conceptual model in Phase 2 § 4:

    User ──< Conversation ──< Message

Design decisions that affect later phases are recorded as AD-015 … AD-019 in
``docs/PROJECT_PLAN.md``. The two that most constrain Phase 3–5:

* **No ``users.current_conversation_id`` pointer.** The active conversation is
  derived by query. A stored pointer would have to be rewritten when Phase 5
  groups conversations into projects (AD-016).
* **Messages are keyed to a conversation only.** A Phase 5 project association
  is therefore one additive column on ``conversations``, not a rewrite of every
  descendant table (AD-015).

Nothing here stores a secret, and no raw provider payload is retained: only the
model name and the token counts the provider reported (FR-30).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Roles are a closed set. Enforced in the database as well as in the domain, so
# a bad write cannot be made by any path, including a hand-run INSERT.
_ROLE_VALUES = "role IN ('user', 'assistant', 'system')"
_STATUS_VALUES = "status IN ('active', 'archived')"


def new_turn_id() -> str:
    """A turn groups one user message with the assistant reply to it.

    A UUID rather than a database sequence because a turn is created before
    either of its rows exists, and because it must be identical across a Celery
    retry of the same logical turn.
    """
    return uuid.uuid4().hex


class UserModel(Base):
    """A Telegram user.

    Only the platform identity and an optional display name are stored. The
    Telegram id is the natural key and is unique, which is what makes user
    resolution safe under concurrency (Phase 2 § 8): two racing inserts produce
    one row and one unique violation, not two users.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversations: Mapped[list[ConversationModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ConversationModel(Base):
    """A conversation: an ordered group of messages owned by exactly one user."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_STATUS_VALUES, name="status_values"),
        # Retrieval path: "this user's conversations, newest first". Without
        # this, listing conversations is a sequential scan per user.
        Index("ix_conversations_user_id_updated_at", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Reserved for Phase 5 project/collection association. Intentionally not a
    # foreign key: the `projects` table does not exist yet and inventing a
    # dangling reference now would be implementing Phase 5 (AD-015).
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    user: Mapped[UserModel] = relationship(back_populates="conversations")
    messages: Mapped[list[MessageModel]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class MessageModel(Base):
    """One message in a conversation.

    Ordering is ``(created_at, id)``. ``id`` is the tiebreak because two messages
    committed together can share a timestamp, and history reconstruction must be
    stable (Phase 2 § 10).

    ``turn_id`` carries the idempotency boundary. A user message and its
    assistant reply share one turn id, and ``(turn_id, role)`` is unique, so a
    redelivered Telegram update or a retried Celery task cannot create a second
    assistant message for a turn that already has one (Phase 2 § 15, § 16).
    """

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_ROLE_VALUES, name="role_values"),
        # The primary access pattern: history for one conversation in order.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at", "id"),
        # Makes "does this turn already have an assistant reply?" a lookup
        # rather than a scan, which is the idempotency check on retry.
        UniqueConstraint("turn_id", "role", name="uq_messages_turn_id_role"),
        # Telegram redelivery. Unique per chat, so the same update cannot be
        # persisted twice. Nullable: assistant messages have no Telegram id.
        UniqueConstraint(
            "telegram_chat_id", "telegram_message_id", name="uq_messages_telegram_message"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Response metadata, for cost visibility (FR-30). Null on user messages.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Telegram provenance, used only for idempotency. No message payload beyond
    # `content` is retained from the platform.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    conversation: Mapped[ConversationModel] = relationship(back_populates="messages")
