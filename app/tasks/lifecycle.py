"""Celery worker process lifecycle.

Implements TR-1: one event loop per worker process, created when the child
process starts and closed when it exits. This is what makes audit C-3
impossible rather than merely unlikely.

Celery's ``prefork`` pool forks after import, so the child must discard any
loop object inherited from the parent. ``worker_process_init`` runs in the
child and is the correct hook; ``solo`` and ``threads`` pools run in-process
and are handled by the same signals.
"""

from __future__ import annotations

import logging

from celery.signals import worker_process_init, worker_process_shutdown

from app.infrastructure.asyncio_runtime import close_event_loop, get_event_loop, reset_event_loop

log = logging.getLogger(__name__)


@worker_process_init.connect
def _on_worker_process_init(**_kwargs) -> None:
    """Establish this process's long-lived loop."""
    reset_event_loop()
    get_event_loop()
    log.info("worker process initialised with a dedicated event loop")


@worker_process_shutdown.connect
def _on_worker_process_shutdown(**_kwargs) -> None:
    """Release the Telegram session, then close the loop, in that order.

    Order matters: closing the loop first would leave the cached aiohttp
    session unusable and leak its sockets (FR-4.2).
    """
    try:
        from app.bot.container import get_container

        get_container().close()
    except Exception:
        log.warning("error closing application container", exc_info=True)
    finally:
        close_event_loop()
