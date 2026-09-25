"""Telegram handlers.

Thin by design: parse the update, delegate, respond. No business logic, no
provider SDK, no string formatting of model output, no event-loop management
(TR-2).

Audit references: H-1 (``from_user.id`` used as ``chat_id``), H-7 (unfiltered
catch-all), H-2 (no error handling), M-8 (no rate limiting), M-7 (full user
text logged), P1-1, P1-2.
"""

from __future__ import annotations

import logging

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.container import AppContainer

log = logging.getLogger(__name__)

START_TEXT = (
    "👋 Welcome to the research bot!\n\n"
    "Ask a question and I will break it down into smaller sub-questions."
)
UNKNOWN_COMMAND_TEXT = "Unknown command. Send your question directly, or use /start."
UNSUPPORTED_TEXT_TEXT = "Only text messages are supported for now."
RATE_LIMITED_TEXT = "Too many requests. Please try again in a moment."
QUEUE_UNAVAILABLE_TEXT = "The processing queue is unavailable. Please try again later."
GLOBAL_CAPACITY_TEXT = "Processing capacity is full. Please try again shortly."
GENERIC_ERROR_TEXT = "Something went wrong. Please try again."
ACKNOWLEDGEMENT_TEXT = "⏳ Analysing your question... please wait a moment."


def _safe_user_id(message: Message) -> int | None:
    """Return the sender's Telegram user id, or ``None`` if absent (L-8)."""
    return message.from_user.id if message.from_user else None


def register_handlers(dispatcher: Dispatcher, container: AppContainer) -> None:
    """Attach every handler to ``dispatcher``.

    Registration order matters: ``/start`` is matched before the catch-all, so
    the command never falls through to the text handler.
    """
    router = Router(name="research-bot")

    @router.message(Command("start"))
    async def start_command(message: Message) -> None:
        log.info("start command from chat=%s user=%s", message.chat.id, _safe_user_id(message))
        await message.answer(START_TEXT)

    # Any command that is not /start. Registered after /start, so /start is
    # matched first and never falls through to here.
    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        """Any other command is answered, not treated as free text (FR-3.3, T-7)."""
        log.info("unknown command in chat=%s", message.chat.id)
        await message.answer(UNKNOWN_COMMAND_TEXT)

    # Text messages. Must be registered before the unfiltered catch-all below,
    # otherwise the catch-all matches every text message and this handler is
    # never reached.
    @router.message(F.text)
    async def research_handler(message: Message) -> None:
        """Enqueue a research task for a text message.

        ``chat_id`` and ``user_id`` are passed as separate arguments. They are
        different values in a group chat: ``from_user.id`` is the sender,
        ``chat.id`` is the destination (H-1, P1-1).
        """
        chat_id = message.chat.id
        user_id = _safe_user_id(message)
        query = (message.text or "").strip()

        if user_id is None or not query:
            await message.answer(UNSUPPORTED_TEXT_TEXT)
            return

        # Never log the full user text (M-7, FR-9.3). Length and a short
        # prefix are enough to diagnose without storing user content.
        log.info(
            "queued research chat=%s user=%s chars=%s preview=%r",
            chat_id, user_id, len(query), query[:40],
        )

        decision = container.rate_limiter.check(user_id)
        if not decision.allowed:
            text = GLOBAL_CAPACITY_TEXT if decision.reason == "global_capacity" else RATE_LIMITED_TEXT
            log.info("throttled user=%s reason=%s", user_id, decision.reason)
            await message.answer(text)
            return

        await message.answer(ACKNOWLEDGEMENT_TEXT)

        # Imported here, not at module scope: the handler must not create a
        # hard module-level dependency on the task package.
        from app.tasks.research_task import process_research

        try:
            process_research.delay(chat_id=chat_id, user_id=user_id, query=query)
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
