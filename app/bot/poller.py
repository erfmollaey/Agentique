"""Standalone Telegram poller.

The process that owns the bot's inbound stream, separate from the ASGI process
that serves HTTP. Composed as the ``bot`` service; run it with::

    python -m app.bot.poller

**Why a process rather than a flag on the API.** The original topology had
``uvicorn app.main:app`` doing both jobs, and that is fine for one process. It
stops being fine the moment a second process appears: Telegram's ``getUpdates``
delivers each update to whichever consumer asks last, so two pollers do not
give a redundancy, they give messages dropped at random and a bot that looks
flaky. Splitting the roles is what makes the failure legible — ``api`` answers
health checks, ``bot`` owns the update stream, and each can be restarted without
disturbing the other.

**Heartbeat.** This process serves no port, so the usual HTTP probe cannot tell a
live poller from a wedged one. It writes a timestamp to
``BOT_HEARTBEAT_PATH`` every ``BOT_HEARTBEAT_INTERVAL_SECONDS`` and the
healthcheck reads it (see ``app/bot/heartbeat.py``). A heartbeat that goes stale
means the event loop has stopped, which is the failure that actually matters and
is invisible to `docker ps`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from app.bot.container import build_container
from app.bot.handlers import register_handlers
from app.core.config import get_settings
from app.core.logging_config import configure_logging

configure_logging()
log = logging.getLogger(__name__)


async def _heartbeat(path: str, interval: float) -> None:
    """Record that the event loop is still turning.

    Written inside the same loop as the poller on purpose. A separate process
    writing this file would prove the healthcheck can read a file, not that the
    bot is alive.
    """
    target = Path(path)
    while True:
        target.write_text(str(time.time()), encoding="ascii")
        await asyncio.sleep(interval)


async def _run() -> None:
    settings = get_settings()
    container = build_container(settings)
    register_handlers(container.dispatcher, container)

    heartbeat = asyncio.create_task(
        _heartbeat(settings.BOT_HEARTBEAT_PATH, settings.BOT_HEARTBEAT_INTERVAL_SECONDS),
        name="bot-heartbeat",
    )
    log.info(
        "starting standalone telegram poller (heartbeat=%s every %ss)",
        settings.BOT_HEARTBEAT_PATH,
        settings.BOT_HEARTBEAT_INTERVAL_SECONDS,
    )
    try:
        # handle_signals=False: aiogram would otherwise install its own SIGTERM
        # handler and stop the loop, which is the server's job in the ASGI
        # process and nobody's job here — this one runs until it is stopped.
        await container.dispatcher.start_polling(
            container.bot,
            allowed_updates=container.dispatcher.resolve_used_update_types(),
            handle_signals=False,
        )
    except asyncio.CancelledError:
        log.info("telegram polling cancelled")
        raise
    except Exception:
        # Logged and re-raised: a dead poller with a healthy container is the
        # original H-2 defect, and silence here is how it comes back.
        log.exception("telegram polling stopped unexpectedly")
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("bot stopped by keyboard interrupt")
    except Exception:
        # The poller has already logged the cause. Re-raising would print a
        # traceback that adds nothing; the exit status is what the supervisor
        # and the healthcheck react to.
        log.exception("standalone poller exited")
        raise SystemExit(1) from None
    finally:
        # The Telegram session is process state; leaving it open leaks sockets.
        from app.bot.container import get_container

        with contextlib.suppress(Exception):
            get_container().close()


if __name__ == "__main__":
    main()
