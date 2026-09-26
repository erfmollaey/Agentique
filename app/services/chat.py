"""Chat orchestration.

The application layer for AI Chat. It owns the decisions — who the user is,
which conversation the turn belongs to, what history the model sees, what gets
persisted, and what the caller should send back — and owns nothing else. It has
no Telegram client, no provider SDK, and no Celery machinery (TR-2).

Flow for one inbound message (Phase 2 § 2, § 8, § 15)::

    resolve or create user        (unique constraint, race-safe)
      -> resolve or create active conversation
      -> COMMIT the user message   (before the provider call, so it cannot vanish)
      -> assemble context          (ordered, budgeted, truncated)
      -> call the provider         (validated reply)
      -> COMMIT the assistant message

Delivery is *not* done here. The transport layer sends :class:`ChatOutcome.text`,
which keeps this service testable without a bot and keeps Telegram specifics out
of the application layer.

Failure boundaries, stated rather than implied (Phase 2 § 15):

* **Two transactions, deliberately.** The user's message is committed on its own
  before the provider is called, so a provider outage cannot lose what the user
  typed. The assistant reply is a second transaction. This means a half-turn —
  a user message with no reply — is a legitimate intermediate state, not
  corruption, and the retry path is written to expect it.
* If the provider fails, no assistant message is fabricated. The task decides
  whether to retry; the user gets one terminal notice when it does not.
* If delivery fails after a successful commit, the assistant message is already
  stored. A redelivered update re-derives the same turn id, finds the stored
  reply, and re-sends it instead of generating a second one.
* This is **not** a distributed transaction. The Telegram send happens outside
  the database transaction and is not rolled back if it fails; the worst case is
  a stored reply the user did not receive, which the next message reveals.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import Settings
from app.domain.chat import (
    ChatOutcome,
    ChatTurn,
    Conversation,
    ConversationStatus,
    MessageRecord,
    Role,
    User,
)
from app.domain.errors import (
    ConversationNotFoundError,
    DuplicateMessageError,
    MessageTooLongError,
    PersistenceError,
    ResearchError,
)
from app.services.context import ContextBuilder
from app.services.failures import user_message_for

log = logging.getLogger(__name__)

# Cap on how much of a user's own first message becomes the conversation title.
_TITLE_MAX_CHARS = 60


def derive_turn_id(chat_id: int | None, message_id: int | None) -> str:
    """A stable turn id for one inbound Telegram message.

    Derived from the platform identifiers rather than generated, so a redelivered
    update and a retried Celery task both compute the same value. That is what
    makes ``uq_messages_turn_id_role`` an effective idempotency boundary instead
    of a uniqueness constraint on random values (Phase 2 § 15, § 16).

    Falls back to a random id when the platform identifiers are absent. That is
    not only a non-Telegram caller: observed during Phase 2 validation, tasks
    enqueued by the *Phase 1* handler carry no ``telegram_message_id``, so a
    backlog of pre-Phase-2 messages produces one turn per redelivery rather than
    deduplicated ones. New traffic always carries the id, so the fallback is a
    migration concern rather than a steady-state one.
    """
    if chat_id is None or message_id is None:
        from app.db.models import new_turn_id

        return new_turn_id()
    digest = hashlib.sha256(f"{chat_id}:{message_id}".encode()).hexdigest()
    return digest[:32]


def stored_text(text: str) -> str:
    """The text to answer with when replaying a half-completed turn.

    Taken from the request rather than re-read from the database: the caller
    holds the authoritative text, and re-reading would risk answering a
    different string than the one that was stored.
    """
    return text


def derive_title(text: str) -> str:
    """A short conversation title from the first user message."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _TITLE_MAX_CHARS:
        return collapsed
    return collapsed[: _TITLE_MAX_CHARS - 1].rstrip() + "…"


class ChatService:
    """Multi-turn AI chat over persisted conversations."""

    def __init__(
        self,
        *,
        settings: Settings,
        llm: Any,
        uow_factory: Callable[[], Any],
        context: ContextBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._uow_factory = uow_factory
        self._context = context or ContextBuilder(settings)

    # --- message path ------------------------------------------------------

    async def handle_message(self, turn: ChatTurn) -> ChatOutcome:
        """Process one inbound message and return what to deliver.

        Raises a typed :class:`~app.domain.errors.ResearchError` on failure. It
        never sends anything and never swallows an error: the caller applies the
        retry policy and owns the single terminal user-facing message.
        """
        text = (turn.text or "").strip()
        if not text:
            raise MessageTooLongError("empty message")
        if len(text) > self._settings.CHAT_MAX_MESSAGE_CHARS:
            log.info(
                "message rejected: %s chars exceeds the %s limit",
                len(text), self._settings.CHAT_MAX_MESSAGE_CHARS,
            )
            raise MessageTooLongError("message too long")

        turn_id = derive_turn_id(turn.chat_id, turn.telegram_message_id)

        try:
            # Transaction 1: the user's message, committed on its own so a
            # provider outage cannot lose what they typed (Phase 2 § 15).
            # A duplicate here aborts this transaction and is handled below.
            async with self._uow_factory() as uow:
                user = await self._resolve_user(uow, turn)
                conversation = await self._resolve_conversation(uow, user, text)
                user_message = await self._insert_user_message(
                    uow, turn, conversation, text, turn_id
                )
                await uow.conversations.touch(conversation.id)

            # Transaction 2: the answer. A failure here cannot roll back the
            # user's message, which is the whole point of the split.
            async with self._uow_factory() as uow:
                return await self._answer_turn(
                    uow, conversation, user_message.id or 0, text, turn_id
                )
        except DuplicateMessageError as exc:
            # A unique constraint fired outside the guarded insert below, which
            # means the same turn is being processed concurrently.
            log.info("duplicate turn rejected by the database: %s", type(exc).__name__)
            return await self._replay_duplicate(turn, text, turn_id)
        except ResearchError:
            raise
        except Exception as exc:
            log.exception("unexpected chat failure for telegram user %s", turn.telegram_user_id)
            raise PersistenceError("chat turn failed") from exc

    async def _resolve_user(self, uow: Any, turn: ChatTurn) -> User:
        user = await uow.users.get_or_create(turn.telegram_user_id, turn.username)
        log.info("user resolved telegram_id=%s db_id=%s", turn.telegram_user_id, user.id)
        return user

    async def _resolve_conversation(
        self, uow: Any, user: User, text: str
    ) -> Conversation:
        conversation = await uow.conversations.get_active(user.id)
        if conversation is not None:
            return conversation
        conversation = await uow.conversations.create(user.id, derive_title(text))
        log.info("conversation created id=%s user_id=%s", conversation.id, user.id)
        return conversation

    async def _answer_turn(
        self,
        uow: Any,
        conversation: Conversation,
        user_message_id: int,
        text: str,
        turn_id: str,
    ) -> ChatOutcome:
        """Assemble context, call the provider, store the reply.

        The user message is already committed, so history must exclude this turn
        or the current message would reach the model twice.
        """
        history = await self._history_excluding(uow, conversation.id, turn_id)
        assembled = self._context.assemble(history, text)

        log.info(
            "llm request started conversation=%s turn=%s history=%s dropped=%s tokens=%s",
            conversation.id, turn_id, assembled.included, assembled.dropped,
            assembled.estimated_tokens,
        )
        reply = self._complete(assembled.messages)
        log.info(
            "llm request completed conversation=%s turn=%s model=%s tokens=%s/%s",
            conversation.id, turn_id, reply.model,
            reply.prompt_tokens, reply.completion_tokens,
        )

        assistant = await self._insert_assistant_message(
            uow, conversation.id, turn_id, reply
        )
        await uow.conversations.touch(conversation.id)

        return ChatOutcome(
            text=reply.text,
            conversation_id=conversation.id,
            user_message_id=user_message_id,
            assistant_message_id=assistant.id,
            truncated_history=assembled.truncated,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            model=reply.model,
        )

    async def _insert_user_message(
        self,
        uow: Any,
        turn: ChatTurn,
        conversation: Conversation,
        text: str,
        turn_id: str,
    ) -> MessageRecord:
        """Store the user's message.

        Raises :class:`DuplicateMessageError` when the turn is already recorded,
        which aborts the enclosing unit of work. The caller resolves that as a
        replay rather than trying to continue in a rolled-back session.
        """
        record = MessageRecord(
            conversation_id=conversation.id, role=Role.USER, content=text, turn_id=turn_id
        )
        stored = await uow.messages.append(
            record,
            telegram_chat_id=turn.chat_id,
            telegram_message_id=turn.telegram_message_id,
        )
        log.info("message persisted role=user id=%s turn=%s", stored.id, turn_id)
        return stored

    async def _insert_assistant_message(
        self, uow: Any, conversation_id: int, turn_id: str, reply: Any
    ) -> MessageRecord:
        record = MessageRecord(
            conversation_id=conversation_id,
            role=Role.ASSISTANT,
            content=reply.text,
            turn_id=turn_id,
            model=reply.model,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )
        stored = await uow.messages.append(record)
        log.info(
            "message persisted role=assistant id=%s turn=%s tokens=%s",
            stored.id, turn_id, stored.prompt_tokens + stored.completion_tokens,
        )
        return stored

    async def _history_excluding(
        self, uow: Any, conversation_id: int, turn_id: str
    ) -> Sequence[MessageRecord]:
        """Prior turns in ascending order, excluding the current turn.

        Over-fetches by two so the extra rows the current turn added are dropped
        without a second query.
        """
        window = self._settings.LLM_CONTEXT_MAX_MESSAGES + 2
        history = await uow.messages.history(conversation_id, limit=window)
        return [m for m in history if m.turn_id != turn_id]

    def _complete(self, messages: Sequence[Any]) -> Any:
        return self._llm.complete(
            messages,
            model=self._settings.LLM_MODEL,
            temperature=self._settings.LLM_TEMPERATURE,
            max_tokens=self._settings.LLM_MAX_TOKENS,
        )

    # --- idempotent replay -------------------------------------------------

    async def _replay_duplicate(self, turn: ChatTurn, text: str, turn_id: str) -> ChatOutcome:
        """Resolve a turn whose user message already exists.

        Two situations, and they are the same code path:

        * **Redelivery.** The turn already has a reply. That reply is returned
          unchanged, so the user gets their answer and no second one is
          generated or stored.
        * **Retry after a provider failure.** The first attempt committed the
          question and then failed. There is no reply, so the turn is answered
          now. A question with no answer is a legitimate intermediate state, not
          corruption.
        """
        async with self._uow_factory() as uow:
            user = await uow.users.get_or_create(turn.telegram_user_id, turn.username)
            conversation = await uow.conversations.get_active(user.id)
            if conversation is None:  # pragma: no cover - the turn implies one
                return self._no_conversation(turn_id)

            stored_reply = await uow.messages.find_by_turn(
                conversation.id, turn_id, Role.ASSISTANT.value
            )
            if stored_reply is not None:
                log.info("replaying stored reply for turn=%s", turn_id)
                stored_question = await uow.messages.find_by_turn(
                    conversation.id, turn_id, Role.USER.value
                )
                return ChatOutcome(
                    text=stored_reply.content,
                    conversation_id=conversation.id,
                    user_message_id=stored_question.id if stored_question else 0,
                    assistant_message_id=stored_reply.id,
                    duplicate=True,
                    model=stored_reply.model,
                )

            log.info("turn %s has a question but no reply; answering it now", turn_id)
            return await self._answer_turn(
                uow, conversation, 0, stored_text(text), turn_id
            )

    def _no_conversation(self, turn_id: str) -> ChatOutcome:
        log.warning("turn %s has no conversation to resolve against", turn_id)
        return ChatOutcome(
            text=user_message_for(DuplicateMessageError("duplicate")),
            conversation_id=0,
            user_message_id=0,
            duplicate=True,
        )

    # --- command path ------------------------------------------------------

    async def start_new_conversation(self, turn: ChatTurn) -> str:
        """Archive the current conversation and open a new one (FR-5)."""
        async with self._uow_factory() as uow:
            user = await uow.users.get_or_create(turn.telegram_user_id, turn.username)
            current = await uow.conversations.get_active(user.id)
            if current is not None:
                await uow.conversations.set_status(
                    current.id, ConversationStatus.ARCHIVED.value
                )
                log.info("conversation archived id=%s", current.id)
            conversation = await uow.conversations.create(user.id, None)
            log.info("conversation created id=%s user_id=%s", conversation.id, user.id)
            return (
                "Started a new conversation. Anything you say now is a fresh start; "
                "the previous one is kept and still listed by /conversations."
            )

    async def list_conversations(self, turn: ChatTurn, limit: int = 10) -> str:
        """List the user's conversations, newest first (FR-5)."""
        async with self._uow_factory() as uow:
            user = await uow.users.get_or_create(turn.telegram_user_id, turn.username)
            conversations = await uow.conversations.list_for_user(user.id, limit=limit)
            if not conversations:
                return "You have no conversations yet. Just send me a message to begin."
            lines = ["Your conversations:"]
            for conversation in conversations:
                marker = "*" if conversation.status == ConversationStatus.ACTIVE else "-"
                title = conversation.title or "(no title)"
                lines.append(f"{marker} #{conversation.id} {title}")
            lines.append("")
            lines.append("* is the current conversation.")
            return "\n".join(lines)

    async def reset_conversation(self, turn: ChatTurn) -> str:
        """Delete the current conversation's messages (FR-6).

        The conversation record and the user record both survive: a reset is not
        a deletion, and it must not orphan the user's identity.
        """
        async with self._uow_factory() as uow:
            user = await uow.users.get_or_create(turn.telegram_user_id, turn.username)
            conversation = await uow.conversations.get_active(user.id)
            if conversation is None:
                raise ConversationNotFoundError("no active conversation")
            removed = await uow.messages.delete_for_conversation(conversation.id)
            log.info(
                "conversation reset id=%s removed=%s user_id=%s",
                conversation.id, removed, user.id,
            )
            return (
                f"Cleared {removed} message(s) from this conversation. "
                "The conversation itself is still here — send /new for a clean start."
            )

    async def delete_all_user_data(self, turn: ChatTurn) -> str:
        """Delete the user's account and every record the schema cascades from it.

        This is SR-9: a user can delete their data, and deletion actually removes
        it. It is **not** reachable from ``/reset`` — ``/reset`` clears one
        conversation and keeps the user, and this removes the user and everything.

        Lives on the chat service because it shares the unit of work and the
        command wiring, not because account management is chat logic. It is
        deliberately one method and one command, not an account subsystem: no
        profile, no settings, no credentials, nothing that belongs to Phase 8.

        Transactional: the whole deletion is a single unit of work, so a failure
        rolls back and nothing is partially removed. The caller never sees a
        success message unless the commit succeeded.

        Ownership-safe: it is scoped to the requesting user's own Telegram
        identity, and that identity is the only argument the repository accepts.
        """
        async with self._uow_factory() as uow:
            result = await uow.users.delete(turn.telegram_user_id)

        if not result.deleted:
            # Honest, and not dressed up as a deletion that did not happen.
            log.info("delete requested for unknown identity; nothing to remove")
            return "There was nothing stored for you, so nothing was deleted."

        return (
            "Done. Everything I had stored for you has been deleted, and I have "
            "forgotten our conversations. Send /start any time to begin again."
        )
