"""In-memory persistence doubles.

The Phase 1 service tests must not need a database: they exist to prove the
event-loop and delivery invariants, and a live PostgreSQL would add a failure
mode that has nothing to do with what they assert. Phase 2 keeps them that way
and puts the real database under test in the repository and integration modules.

These doubles are not a mock framework. They enforce the same invariants the
schema does — unique Telegram identity, one user message and one assistant reply
per turn, ownership-scoped reads — so a test that passes here is not passing
because a constraint was skipped. Where behaviour legitimately diverges (no
transaction rollback, ordering by insertion rather than by timestamp) that is
called out in the docstring.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from app.domain.chat import (
    Conversation,
    ConversationStatus,
    DeletedUserData,
    MessageRecord,
    User,
)
from app.domain.errors import DuplicateMessageError


class InMemoryUsers:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        return self._store.users_by_telegram.get(telegram_user_id)

    async def get_or_create(self, telegram_user_id: int, username: str | None) -> User:
        existing = self._store.users_by_telegram.get(telegram_user_id)
        if existing is not None:
            return existing
        user = User(
            id=next(self._store.user_ids),
            telegram_user_id=telegram_user_id,
            username=username,
            created_at=datetime.now(tz=UTC),
        )
        self._store.users_by_telegram[telegram_user_id] = user
        self._store.users_by_id[user.id] = user
        return user

    async def delete(self, telegram_user_id: int) -> DeletedUserData:
        """Mirror the real cascade, so isolation is tested here too.

        Counts first, then remove the user and everything reachable from it —
        the same shape as the SQL implementation, which relies on
        ``ON DELETE CASCADE``. Scoped to the one identity.
        """
        user = self._store.users_by_telegram.get(telegram_user_id)
        if user is None:
            return DeletedUserData(user_id=0, conversations=0, messages=0, deleted=False)

        owned = [
            c for c in self._store.conversations.values() if c.user_id == user.id
        ]
        conversation_ids = {c.id for c in owned}
        messages = [
            m for m in self._store.messages if m.conversation_id in conversation_ids
        ]
        result = DeletedUserData(
            user_id=user.id,
            conversations=len(owned),
            messages=len(messages),
            deleted=True,
        )

        self._store.messages = [
            m for m in self._store.messages if m.conversation_id not in conversation_ids
        ]
        for conversation_id in conversation_ids:
            self._store.conversations.pop(conversation_id, None)
        self._store.users_by_telegram.pop(telegram_user_id, None)
        self._store.users_by_id.pop(user.id, None)
        return result



class InMemoryConversations:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def create(self, user_id: int, title: str | None) -> Conversation:
        now = datetime.now(tz=UTC)
        conversation = Conversation(
            id=next(self._store.conversation_ids),
            user_id=user_id,
            title=title,
            status=ConversationStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self._store.conversations[conversation.id] = conversation
        return conversation

    async def get_for_user(self, conversation_id: int, user_id: int) -> Conversation | None:
        conversation = self._store.conversations.get(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return None
        return conversation

    async def get_active(self, user_id: int) -> Conversation | None:
        candidates = [
            c for c in self._store.conversations.values()
            if c.user_id == user_id and c.status == ConversationStatus.ACTIVE
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda c: (c.updated_at, c.id))

    async def list_for_user(self, user_id: int, limit: int = 20):
        owned = [c for c in self._store.conversations.values() if c.user_id == user_id]
        owned.sort(key=lambda c: (c.updated_at, c.id), reverse=True)
        return owned[:limit]

    async def set_status(self, conversation_id: int, status: str) -> None:
        conversation = self._store.conversations.get(conversation_id)
        if conversation is not None:
            self._store.conversations[conversation_id] = conversation.model_copy(
                update={"status": ConversationStatus(status), "updated_at": datetime.now(tz=UTC)}
            )

    async def touch(self, conversation_id: int) -> None:
        conversation = self._store.conversations.get(conversation_id)
        if conversation is not None:
            self._store.conversations[conversation_id] = conversation.model_copy(
                update={"updated_at": datetime.now(tz=UTC)}
            )


class InMemoryMessages:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def append(
        self,
        message: MessageRecord,
        *,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
    ) -> MessageRecord:
        # Same two uniqueness rules the schema enforces, so the idempotency
        # behaviour under test is not an artefact of the double.
        for existing in self._store.messages:
            if existing.turn_id == message.turn_id and existing.role == message.role:
                raise DuplicateMessageError("message already recorded")
        telegram_key = (telegram_chat_id, telegram_message_id)
        if telegram_message_id is not None and telegram_key in self._store.telegram_keys:
            raise DuplicateMessageError("message already recorded")

        stored = message.model_copy(
            update={"id": next(self._store.message_ids), "created_at": datetime.now(tz=UTC)}
        )
        if telegram_message_id is not None:
            self._store.telegram_keys.add(telegram_key)
        self._store.messages.append(stored)
        return stored

    async def history(self, conversation_id: int, limit: int = 50):
        owned = [m for m in self._store.messages if m.conversation_id == conversation_id]
        # Insertion order stands in for (created_at, id); ids are monotonic.
        return owned[-limit:]

    async def get(self, message_id: int) -> MessageRecord | None:
        for message in self._store.messages:
            if message.id == message_id:
                return message
        return None

    async def find_by_turn(
        self, conversation_id: int, turn_id: str, role: str
    ) -> MessageRecord | None:
        for message in self._store.messages:
            if (
                message.conversation_id == conversation_id
                and message.turn_id == turn_id
                and message.role.value == role
            ):
                return message
        return None

    async def delete_for_conversation(self, conversation_id: int) -> int:
        before = len(self._store.messages)
        self._store.messages = [
            m for m in self._store.messages if m.conversation_id != conversation_id
        ]
        return before - len(self._store.messages)


class InMemoryStore:
    """The state every in-memory repository shares."""

    def __init__(self) -> None:
        self.users_by_telegram: dict[int, User] = {}
        self.users_by_id: dict[int, User] = {}
        self.conversations: dict[int, Conversation] = {}
        self.messages: list[MessageRecord] = []
        # Telegram provenance keys already persisted, mirroring
        # uq_messages_telegram_message.
        self.telegram_keys: set[tuple[int | None, int | None]] = set()
        self.user_ids = itertools.count(1)
        self.conversation_ids = itertools.count(1)
        self.message_ids = itertools.count(1)


class InMemoryUnitOfWork:
    """A unit of work over :class:`InMemoryStore`.

    Deliberately has no rollback: the real unit of work commits or rolls back,
    but no service-level test depends on that, and pretending to offer it would
    be a false capability. Tests that need transactional behaviour use the real
    database.
    """

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.users = InMemoryUsers(store)
        self.conversations = InMemoryConversations(store)
        self.messages = InMemoryMessages(store)

    async def __aenter__(self) -> InMemoryUnitOfWork:
        return self

    async def __aexit__(self, *_exc_info) -> None:
        return None


def in_memory_uow_factory(store: InMemoryStore | None = None):
    """Return ``(factory, store)`` so a test can inspect what was written."""
    shared = store or InMemoryStore()

    def factory() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(shared)

    return factory, shared


def user_turn(user_id: int, chat_id: int, text: str, message_id: int):
    """A :class:`~app.domain.chat.ChatTurn` shaped like a Telegram message."""
    from app.domain.chat import ChatTurn

    return ChatTurn(
        telegram_user_id=user_id,
        chat_id=chat_id,
        text=text,
        username=f"user{user_id}",
        telegram_message_id=message_id,
    )
