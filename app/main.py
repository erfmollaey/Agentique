"""ASGI application.

Owns the process lifecycle for the API/poller: start polling on startup, stop
it cleanly on shutdown, and expose honest health endpoints.

Audit references: H-5 (``/health`` was a static literal), H-6 (no graceful
shutdown, deprecated ``on_event``), H-2 (poller failure logged then ignored).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from app.bot.container import AppContainer, build_container
from app.bot.handlers import register_handlers
from app.core.config import get_settings
from app.core.logging_config import configure_logging
from app.services.health import check_broker, readiness_report

configure_logging()
log = logging.getLogger(__name__)

SETTINGS = get_settings()

# Reference to the running poller so readiness can report on it, and so
# shutdown can stop it (FR-7.4, FR-3.7).
_poller_task: asyncio.Task | None = None


async def _poll(container: AppContainer) -> None:
    """Run Telegram long polling until cancelled.

    ``handle_signals=False`` is required. aiogram otherwise installs its own
    SIGINT/SIGTERM handlers which call ``loop.stop()`` on the running loop.
    Under uvicorn that stops the server's loop out from under it, so the ASGI
    shutdown never completes and the process hangs. The server owns process
    signals; the lifespan owns cleanup (P1-4, FR-3.7, H-6).
    """
    log.info("starting Telegram long polling")
    try:
        await container.dispatcher.start_polling(
            container.bot,
            allowed_updates=container.dispatcher.resolve_used_update_types(),
            handle_signals=False,
        )
    except asyncio.CancelledError:
        log.info("Telegram polling cancelled")
        raise
    except Exception:
        # Previously this was logged and then the bot was silently dead while
        # /health still returned ok (H-5, H-2).
        log.exception("Telegram polling stopped unexpectedly")
        raise
        raise


def _poller_alive() -> bool:
    return _poller_task is not None and not _poller_task.done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the poller on startup; stop it and close clients on shutdown."""
    global _poller_task

    container = build_container(SETTINGS)
    register_handlers(container.dispatcher, container)
    app.state.container = container

    _poller_task = asyncio.create_task(_poll(container), name="telegram-poller")
    log.info("application started (environment=%s)", SETTINGS.ENVIRONMENT)

    try:
        yield
    finally:
        if _poller_task is not None:
            _poller_task.cancel()
            # A cancelled poller raises CancelledError; a poller that already
            # failed raises whatever it failed with. Neither should prevent
            # shutdown, and neither should be reported as a shutdown failure.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _poller_task
        # Async close: the lifespan is already inside the running loop.
        await container.aclose()
        log.info("application stopped cleanly")


app = FastAPI(
    title="Research Bot Core",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness: is this process running?

    Deliberately cheap and local. It must not claim any dependency is healthy,
    because it does not check one.
    """
    return {"status": "ok", "service": "fastapi"}


@app.get("/ready", tags=["health"])
async def ready(response: Response) -> dict[str, object]:
    """Readiness: can this process actually serve requests?

    Probes the poller and the broker. Returns 503 when either is unavailable,
    so a dead bot cannot report healthy (FR-7.2, FR-7.3, T-11).
    """
    from app.core.celery_app import celery_app

    def broker_check() -> tuple[bool, str]:
        return check_broker(
            lambda: celery_app.connection(),
            SETTINGS.HEALTH_PROBE_TIMEOUT_SECONDS,
        )

    report = readiness_report(broker_check, _poller_alive)
    if not report.healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": report.status, "checks": report.checks}
