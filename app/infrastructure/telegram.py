"""Telegram client construction.

A factory rather than a module-level singleton (P1-6). The singleton was a
direct cause of C-3: a shared ``Bot`` held an HTTP session bound to whichever
event loop happened to be running. Constructing it in one place, per process,
and passing it to consumers makes ownership explicit.

Audit references: C-3, C-4 (parse mode), M-4 (module-level singletons).
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import Settings

_PARSE_MODES = {
    "HTML": ParseMode.HTML,
    "MARKDOWN": ParseMode.MARKDOWN_V2,
    "MARKDOWN_V2": ParseMode.MARKDOWN_V2,
    "NONE": None,
}


def create_bot(settings: Settings) -> Bot:
    """Build a ``Bot`` from configuration.

    The token is read from settings and never logged or echoed.
    """
    raw_mode = settings.TELEGRAM_PARSE_MODE.upper()
    parse_mode = _PARSE_MODES.get(raw_mode)
    if raw_mode not in _PARSE_MODES:
        raise ValueError(
            f"Unsupported TELEGRAM_PARSE_MODE {settings.TELEGRAM_PARSE_MODE!r}. "
            f"Expected one of: {', '.join(sorted(_PARSE_MODES))}."
        )
    return Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=parse_mode),
    )


def create_dispatcher() -> Dispatcher:
    """Build an empty ``Dispatcher``. Handlers register onto it."""
    return Dispatcher()
