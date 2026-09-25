"""Per-process asyncio event loop ownership.

This module is the fix for audit C-3.

The original defect
-------------------
``app/tasks/research_task.py`` created a new event loop per task and closed it
at the end, while ``app/bot/dispatcher.py`` held a module-level ``Bot``. aiogram
caches its ``aiohttp.ClientSession`` on the ``AiohttpSession`` object
(``aiogram/client/session/aiohttp.py: create_session``), and that session is
bound to whatever loop was running when it was first created. Closing the loop
per task therefore left the cached session bound to a dead loop, so the second
task in a worker process failed and every task leaked an unclosed session.

The invariant
------------
One event loop per process, created on first use, **never closed between
tasks**, and closed exactly once when the process shuts down. Resources bound
to the loop -- notably the Telegram HTTP session -- stay valid for the whole
process lifetime.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Module-level so it survives across task invocations within one process.
# Reset in the worker child process so a forked child never inherits a loop
# object that belonged to the parent.
_loop: asyncio.AbstractEventLoop | None = None


def get_event_loop() -> asyncio.AbstractEventLoop:
    """Return this process's long-lived loop, creating it on first use.

    Deliberately never closes a loop. A caller that closes the returned loop
    reintroduces C-3.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        log.debug("created long-lived event loop for this process")
    return _loop


def close_event_loop() -> None:
    """Close the process loop. Call once, on shutdown, after closing clients."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
        log.debug("closed process event loop")
    _loop = None


def reset_event_loop() -> None:
    """Drop any inherited loop reference without closing it.

    Used in a Celery worker child right after fork. The parent's loop object
    must not be reused, and closing it from the child would be wrong because
    the parent still owns it.
    """
    global _loop
    _loop = None


def run_coroutine(coro):
    """Run a coroutine to completion on this process's long-lived loop.

    The single entry point for calling async client code from synchronous
    Celery task bodies.
    """
    loop = get_event_loop()
    if loop.is_running():  # pragma: no cover - guards against re-entrancy
        raise RuntimeError(
            "event loop is already running; "
            "call this from a synchronous context only"
        )
    return loop.run_until_complete(coro)
