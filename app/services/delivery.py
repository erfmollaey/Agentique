"""Telegram delivery.

Owns the only place that calls ``Bot.send_message`` from a synchronous Celery
task, and routes it through the process's long-lived event loop (P0-7, C-3).
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.core.config import Settings
from app.domain.errors import DeliveryError
from app.infrastructure.asyncio_runtime import get_event_loop, run_coroutine
from app.services.formatting import split_for_telegram

log = logging.getLogger(__name__)


class BotLike(Protocol):
    """The slice of ``aiogram.Bot`` this service needs."""

    async def send_message(self, chat_id: int | str, text: str) -> object: ...


class DeliveryService:
    """Sends rendered messages to a chat, safely and in bounded chunks."""

    def __init__(self, bot: BotLike, settings: Settings) -> None:
        self._bot = bot
        self._settings = settings

    def send(self, chat_id: int | str, text: str) -> int:
        """Send ``text`` to ``chat_id``, splitting if it exceeds the limit.

        Runs the async send on this process's long-lived loop. Safe to call
        repeatedly: the loop and the bot's HTTP session are created once and
        reused for the process lifetime (C-3).

        Returns the number of messages sent.
        """
        limit = min(self._settings.TELEGRAM_MAX_MESSAGE_LENGTH, 4096) - 16
        chunks = split_for_telegram(text, limit) or [text]

        sent = 0
        for chunk in chunks:
            try:
                run_coroutine(self._bot.send_message(chat_id=chat_id, text=chunk))
            except Exception as exc:
                log.error("failed to deliver message to chat %s: %s", chat_id, type(exc).__name__)
                raise DeliveryError("could not deliver message") from exc
            sent += 1
        return sent

    def close(self) -> None:
        """Close the bot's HTTP session from synchronous code.

        Used by the Celery worker, where no loop is running. Must run before
        the process loop is closed (FR-4.2).
        """
        close = getattr(getattr(self._bot, "session", None), "close", None)
        if close is None:
            return
        try:
            loop = get_event_loop()
            if loop.is_closed():
                return
            if loop.is_running():
                # We are inside the loop (the ASGI lifespan calls this). The
                # coroutine cannot be driven with run_until_complete from
                # here; schedule it and let the loop finish it.
                loop.create_task(close())
                return
            run_coroutine(close())
        except Exception:  # shutdown must not raise
            log.warning("error while closing Telegram session", exc_info=True)

    async def aclose(self) -> None:
        """Close the bot's HTTP session from async code.

        Used by the ASGI lifespan, which is already inside the running loop.
        """
        close = getattr(getattr(self._bot, "session", None), "close", None)
        if close is None:
            return
        try:
            await close()
        except Exception:  # shutdown must not raise
            log.warning("error while closing Telegram session", exc_info=True)
