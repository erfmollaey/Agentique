"""Chat domain types.

Pure types with no I/O. These are the contract between persistence, the AI
provider, and Telegram delivery (Phase 2 § 6, FR-15).

Naming note: :class:`Message` would collide with ``aiogram.types.Message``
inside the modules that handle both, so the persisted record is
:class:`MessageRecord` and :class:`Message` is reserved for the provider-facing
message shape. The database table is still called ``messages``.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class Role(enum.StrEnum):
    """Who produced a message."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

    @classmethod
    def coerce(cls, value: object) -> Role:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown role: {value!r}") from exc


class ConversationStatus(enum.StrEnum):
    """Lifecycle state of a conversation.

    ``ARCHIVED`` is what ``/new`` moves the previous conversation to. Nothing is
    deleted, so ``/conversations`` can still list it and Phase 5 can attach
    project metadata to it.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


class Message(BaseModel):
    """One turn of provider-facing conversation content."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str

    @field_validator("content", mode="before")
    @classmethod
    def _coerce_content(cls, value: object) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    @field_validator("role", mode="before")
    @classmethod
    def _coerce_role(cls, value: object) -> Role:
        return Role.coerce(value)


class LLMReply(BaseModel):
    """A validated provider response (FR-15).

    Provider output is parsed into this before it is persisted or rendered, so a
    malformed or empty response fails here rather than three layers downstream
    (audit H-4, M-10).
    """

    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class User(BaseModel):
    """A Telegram user known to the system.

    Only the platform identity and a display name are stored. No Telegram data
    beyond what identity resolution needs (Phase 2 § 5).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_user_id: int
    username: str | None = None
    created_at: datetime | None = None


class Conversation(BaseModel):
    """A conversation grouping an ordered sequence of messages."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str | None = None
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MessageRecord(BaseModel):
    """A persisted message."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    conversation_id: int
    role: Role
    content: str
    turn_id: str
    created_at: datetime | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatTurn(BaseModel):
    """Everything the chat service needs to process one inbound message.

    Assembled by the transport layer from a Telegram update. The service never
    sees aiogram types.
    """

    telegram_user_id: int
    chat_id: int
    text: str
    username: str | None = None
    # Telegram's own identifiers, used for idempotency. Optional because a
    # non-Telegram caller (a test, or a future transport) may not have them.
    telegram_update_id: int | None = None
    telegram_message_id: int | None = None


class DeletedUserData(BaseModel):
    """What a user-data deletion actually removed (SR-9).

    Counts are read before the delete so the outcome can be reported truthfully.
    ``deleted=False`` means the identity had nothing stored, which is reported as
    such rather than dressed up as a successful deletion.
    """

    model_config = ConfigDict(frozen=True)

    user_id: int
    conversations: int
    messages: int
    deleted: bool


class ChatOutcome(BaseModel):
    """What the chat service decided, for the caller to deliver.

    The service does not send anything itself: Telegram delivery belongs to the
    transport layer (TR-2). This keeps the service testable without a bot.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    conversation_id: int
    user_message_id: int
    assistant_message_id: int | None = None
    duplicate: bool = False
    truncated_history: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
