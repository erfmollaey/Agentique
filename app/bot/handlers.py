"""Telegram handlers.

Thin by design: parse the update, delegate, respond. No business logic, no
provider SDK, no string formatting of model output, no event-loop management, no
SQL (TR-2, Phase 2 § 7).

Audit references: H-1 (``from_user.id`` used as ``chat_id``), H-7 (unfiltered
catch-all), H-2 (no error handling), M-8 (no rate limiting), M-7 (full user
text logged), P1-1, P1-2.

Registration order is load-bearing and was a real Phase 1 defect (F-1): the
catch-all must be registered last, or it swallows every text message. Command
handlers come first, then text, then the catch-all.
"""

from __future__ import annotations

import logging

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.bot.container import AppContainer
from app.domain.chat import ChatTurn
from app.services.failures import user_message_for
from app.services.rate_limit import ThrottleDecision

log = logging.getLogger(__name__)

START_TEXT = (
    "👋 Welcome!\n\n"
    "I am an AI assistant. Send me a message and I will reply, and I will "
    "remember this conversation so you can keep talking to me.\n\n"
    "Use /help to see the commands."
)
HELP_TEXT = (
    "Commands:\n"
    "/start — introduction\n"
    "/help — this list\n"
    "/new — start a new conversation\n"
    "/conversations — list your conversations\n"
    "/reset — clear this conversation (asks for confirmation)\n"
    "/delete_account — delete everything stored about you (asks for confirmation)\n\n"
    "Anything that is not a command is sent to me as a message."
)
UNKNOWN_COMMAND_TEXT = (
    "I do not know that command. Send /help to see what I can do, or just "
    "type your question."
)
UNSUPPORTED_TEXT_TEXT = "Only text messages are supported for now."
RATE_LIMITED_TEXT = "Too many requests. Please try again in {seconds}s."
QUEUE_UNAVAILABLE_TEXT = "The processing queue is unavailable. Please try again later."
GLOBAL_CAPACITY_TEXT = "Processing capacity is full. Please try again shortly."
GENERIC_ERROR_TEXT = "Something went wrong. Please try again."
ACKNOWLEDGEMENT_TEXT = "💭 Thinking…"
RESET_CONFIRM_TEXT = (
    "This will delete every message in the current conversation. The "
    "conversation itself is kept. Send /reset confirm to go ahead."
)
DELETE_CONFIRM_TEXT = (
    "⚠️ This permanently deletes everything stored about you: all of your "
    "conversations and every message in them, and your account record. It "
    "cannot be undone.\n\n"
    "Send /delete_account confirm to go ahead, or /delete_account cancel to keep "
    "your data."
)


def _safe_user_id(message: Message) -> int | None:
    """Return the sender's Telegram user id, or ``None`` if absent (L-8)."""
    return message.from_user.id if message.from_user else None


def _turn_from(message: Message) -> ChatTurn | None:
    """Build the service-layer input from an update.

    Returns ``None`` when the update has no usable sender, so a missing
    ``from_user`` cannot become a user row (L-8, SR-1 in Phase 2 § 26).
    """
    user_id = _safe_user_id(message)
    if user_id is None:
        return None
    return ChatTurn(
        telegram_user_id=user_id,
        chat_id=message.chat.id,
        text=(message.text or "").strip(),
        username=message.from_user.username if message.from_user else None,
        telegram_update_id=message.message_id,
        telegram_message_id=message.message_id,
    )


def _throttle_text(decision: ThrottleDecision) -> str:
    if decision.reason == "global_capacity":
        return GLOBAL_CAPACITY_TEXT
    if decision.reason == "daily_quota":
        return RATE_LIMITED_TEXT.format(seconds=decision.retry_after_seconds)
    return RATE_LIMITED_TEXT.format(seconds=decision.retry_after_seconds)


def register_handlers(dispatcher: Dispatcher, container: AppContainer) -> None:
    """Attach every handler to ``dispatcher``."""
    router = Router(name="research-bot")

    async def _run_command(message: Message, coro_factory) -> None:
        """Execute a command through the service and answer with its text.

        Keeps command handlers to three lines each: the service owns the logic,
        the handler owns the transport. The factory is a lambda so the service is
        resolved inside the error handling, and a container built without one
        fails as a user-visible message rather than an AttributeError.
        """
        turn = _turn_from(message)
        if turn is None or container.chat is None:
            await message.answer(UNSUPPORTED_TEXT_TEXT)
            return
        try:
            text = await coro_factory(turn)
        except Exception as exc:
            log.warning("command failed: %s", type(exc).__name__)
            await message.answer(user_message_for(exc))
            return
        await message.answer(text)

    def _chat():
        """The chat service, narrowed for the command factories below."""
        assert container.chat is not None, "container has no chat service"
        return container.chat

    @router.message(CommandStart())
    async def start_command(message: Message) -> None:
        log.info("start command chat=%s user=%s", message.chat.id, _safe_user_id(message))
        await message.answer(START_TEXT)

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        """FR-17: /help lists the available commands."""
        await message.answer(HELP_TEXT)

    @router.message(Command("new"))
    async def new_conversation_command(message: Message) -> None:
        """FR-5: start a new conversation."""
        log.info("new conversation chat=%s user=%s", message.chat.id, _safe_user_id(message))
        await _run_command(message, lambda turn: _chat().start_new_conversation(turn))

    @router.message(Command("conversations"))
    async def list_conversations_command(message: Message) -> None:
        """FR-5: list the user's conversations."""
        await _run_command(message, lambda turn: _chat().list_conversations(turn))

    @router.message(Command("reset"))
    async def reset_conversation_command(message: Message, command: CommandObject) -> None:
        """FR-20: a destructive command requires explicit confirmation."""
        argument = (command.args or "").strip().lower()
        if argument != "confirm":
            await message.answer(RESET_CONFIRM_TEXT)
            return
        log.info("conversation reset confirmed chat=%s user=%s",
                 message.chat.id, _safe_user_id(message))
        await _run_command(message, lambda turn: _chat().reset_conversation(turn))

    @router.message(Command("delete_account"))
    async def delete_account_command(message: Message, command: CommandObject) -> None:
        """SR-9: a user can delete their data, and deletion actually removes it.

        Destructive and irreversible, so it follows the same two-step shape as
        ``/reset``: the bare command explains the consequence and deletes
        nothing. Only the literal ``confirm`` argument acts.

        Deliberately separate from ``/reset``. A reset clears one conversation
        and keeps the user; this removes the user and everything reachable from
        them. They must not be reachable from one another.
        """
        argument = (command.args or "").strip().lower()

        if argument == "cancel":
            log.info("account deletion cancelled chat=%s user=%s",
                     message.chat.id, _safe_user_id(message))
            await message.answer("Cancelled. Nothing was deleted.")
            return

        if argument != "confirm":
            await message.answer(DELETE_CONFIRM_TEXT)
            return

        log.info("account deletion confirmed chat=%s user=%s",
                 message.chat.id, _safe_user_id(message))
        await _run_command(message, lambda turn: _chat().delete_all_user_data(turn))

    # Any command that is not recognised. Registered after the known commands,
    # so those are matched first and never fall through to here.
    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        """FR-18: an unknown command gets a helpful response, not free text."""
        log.info("unknown command in chat=%s", message.chat.id)
        await message.answer(UNKNOWN_COMMAND_TEXT)

    # Text messages. Must be registered before the unfiltered catch-all below,
    # otherwise the catch-all matches every text message and this handler is
    # never reached (audit F-1).
    @router.message(F.text)
    async def chat_message(message: Message) -> None:
        """Enqueue a chat turn.

        The handler does no AI work: it acknowledges, enqueues, and the Celery
        worker runs the chat service (Phase 2 § 18). ``chat_id`` and ``user_id``
        are separate values — in a group the first is the destination and the
        second is the sender (H-1).
        """
        chat_id = message.chat.id
        user_id = _safe_user_id(message)
        query = (message.text or "").strip()

        if user_id is None or not query:
            await message.answer(UNSUPPORTED_TEXT_TEXT)
            return

        # Never log the full user text (M-7, FR-9.3, SR-5). Length and a short
        # prefix are enough to diagnose without storing user content.
        log.info(
            "queued chat chat=%s user=%s chars=%s preview=%r",
            chat_id, user_id, len(query), query[:40],
        )

        decision = container.rate_limiter.check(user_id)
        if not decision.allowed:
            log.info("throttled user=%s reason=%s", user_id, decision.reason)
            await message.answer(_throttle_text(decision))
            return

        await message.answer(ACKNOWLEDGEMENT_TEXT)

        # Imported here, not at module scope: the handler must not create a
        # hard module-level dependency on the task package.
        from app.tasks.research_task import process_research

        try:
            process_research.delay(
                chat_id=chat_id,
                user_id=user_id,
                query=query,
                username=message.from_user.username if message.from_user else None,
                telegram_message_id=message.message_id,
            )
        except Exception as exc:
            # The acknowledgement was already sent, so the user is waiting.
            # Tell them the outcome explicitly (FR-6.3, T-9).
            log.error("failed to enqueue task: %s", type(exc).__name__)
            await message.answer(QUEUE_UNAVAILABLE_TEXT)

    # Unfiltered catch-all. Registered LAST so it only receives updates that
    # no text or command handler claimed (H-7, T-5).
    @router.message()
    async def non_text_message(message: Message) -> None:
        """Non-text content is rejected explicitly, never enqueued (H-7, T-5)."""
        log.info(
            "non-text message in chat=%s, content_type=%s",
            message.chat.id, message.content_type,
        )
        await message.answer(UNSUPPORTED_TEXT_TEXT)

    @router.errors()
    async def on_error(event: Exception) -> bool:
        """Last-resort handler so nothing fails silently (FR-6.1, H-2)."""
        log.exception("unhandled dispatcher error: %s", type(event).__name__)
        update = getattr(event, "update", None)
        message = getattr(update, "message", None) if update else None
        if message is not None:
            try:
                await message.answer(GENERIC_ERROR_TEXT)
            except Exception:
                log.warning("could not deliver error notice to chat %s", message.chat.id)
        return True

    dispatcher.include_router(router)
