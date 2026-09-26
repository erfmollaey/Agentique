"""Ports the application layer depends on.

Every outward dependency of the chat flow is declared here as a ``Protocol``:
persistence, the AI provider, and token estimation. The service layer imports
only these, never a concrete driver or SDK.

This is what makes three Phase 2 requirements hold at once:

* FR-12 — the application depends on an abstraction, not a provider SDK.
* T-5/T-6 — a fake provider can be substituted, and swapping the provider
  implementation requires no change to the service layer.
* T-16/SR-4 — ownership is expressed in the repository interface
  (``get_for_user`` takes the owner), so a conversation can only ever be read
  through the user who owns it.

The Phase 1 ``LLMClient`` protocol in :mod:`app.infrastructure.llm` is retained
for the query-decomposition path used by the audit's regression tests. The chat
path uses :class:`LLMProvider` here.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, runtime_checkable

from app.domain.chat import (
    Conversation,
    DeletedUserData,
    LLMReply,
    MessageRecord,
    User,
)
from app.domain.chat import (
    Message as ChatMessage,
)


@runtime_checkable
class UserRepository(Protocol):
    """Persistence for Telegram users."""

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Return the user, or ``None``. Never creates."""
        ...

    async def get_or_create(self, telegram_user_id: int, username: str | None) -> User:
        """Return the existing user or create one.

        Must be safe under concurrency: the uniqueness of
        ``users.telegram_user_id`` is enforced by the database, so two racing
        callers converge on one row rather than creating two.
        """
        ...

    async def delete(self, telegram_user_id: int) -> DeletedUserData:
        """Delete the user and everything the schema cascades from it (SR-9).

        Takes the requesting user's own platform identity and nothing else, so
        there is no argument a caller could use to name a different user.
        """
        ...


@runtime_checkable
class ConversationRepository(Protocol):
    """Persistence for conversations."""

    async def create(self, user_id: int, title: str | None) -> Conversation: ...

    async def get_for_user(self, conversation_id: int, user_id: int) -> Conversation | None:
        """Return the conversation only if ``user_id`` owns it (SR-4, T-16)."""
        ...

    async def get_active(self, user_id: int) -> Conversation | None:
        """The user's current conversation, or ``None``.

        Defined as "the most recently updated active conversation" rather than a
        pointer stored on the user row. See AD-016: a stored pointer would have
        to be rewritten when Phase 5 groups conversations into projects.
        """
        ...

    async def list_for_user(self, user_id: int, limit: int) -> Sequence[Conversation]: ...

    async def set_status(self, conversation_id: int, status: str) -> None: ...

    async def touch(self, conversation_id: int) -> None:
        """Mark the conversation as the most recently active."""
        ...


@runtime_checkable
class MessageRepository(Protocol):
    """Persistence for messages."""

    async def append(self, message: MessageRecord) -> MessageRecord:
        """Insert one message.

        Raises :class:`app.domain.errors.DuplicateMessageError` when a unique
        constraint is violated, which is how update redelivery is detected
        (idempotency, Phase 2 § 16). The database, not an application-level
        pre-check, is the authority.
        """
        ...

    async def history(self, conversation_id: int, limit: int) -> Sequence[MessageRecord]:
        """Return up to ``limit`` messages in ascending chronological order.

        Ordering is by ``(created_at, id)``. Relying on insertion order alone is
        not safe: two messages committed in the same transaction can share a
        timestamp, and the tiebreak must be deterministic (Phase 2 § 10).
        """
        ...

    async def find_by_turn(self, conversation_id: int, turn_id: str, role: str) -> MessageRecord | None:
        """Return an existing message for a turn/role pair, if any."""
        ...

    async def delete_for_conversation(self, conversation_id: int) -> int:
        """Delete every message in a conversation. Returns the row count."""
        ...


@runtime_checkable
class TokenEstimator(Protocol):
    """Turns text into an approximate token count.

    Isolated behind an interface so a real tokenizer can replace the estimate
    without touching any caller (Phase 2 § 6, "Context assembly").
    """

    def count(self, text: str) -> int: ...

    def count_messages(self, messages: Sequence[ChatMessage]) -> int: ...


@runtime_checkable
class LLMProvider(Protocol):
    """The AI provider, as the application needs it (FR-12, FR-15).

    Returns a validated domain object. A raw provider payload must never reach
    persistence or Telegram delivery.
    """

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMReply: ...


class UnitOfWork(Protocol):
    """Repositories sharing one transaction.

    The service layer never sees a session. It asks for a unit of work, uses the
    repositories, and lets the context manager decide the commit boundary.
    """

    users: UserRepository
    conversations: ConversationRepository
    messages: MessageRepository

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
