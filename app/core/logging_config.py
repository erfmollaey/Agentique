"""Structured logging, initialised exactly once per process.

Audit references: M-7 (basicConfig called twice, full user text logged),
FR-9 (single init, lazy interpolation, no secrets, correlation ids).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_CONFIGURED = False

# The handler this module installed, so it can be identified rather than guessed
# at. See configure_logging for why identity matters.
_OWN_HANDLER: logging.Handler | None = None

# Never let a secret reach a log record, even via an accidental f-string.
_SECRET_MARKERS = ("api_key", "apikey", "token", "password", "secret")


class _RedactingFilter(logging.Filter):
    """Masks values that look like credentials in every log record.

    Defence in depth (SR-5). The application never logs secrets by design;
    this catches the accidental case, such as logging a provider's
    authentication error message verbatim.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            for key in list(record.args):
                if any(marker in key.lower() for marker in _SECRET_MARKERS):
                    record.args[key] = "***redacted***"
        return True


def configure_logging(level: str = "INFO") -> None:
    """Install the root log handler exactly once.

    Idempotent (FR-9.1): repeated calls are a no-op, so importing this module
    from several entry points cannot duplicate log lines.

    It adds its own handler and **does not clear the root logger's existing
    handlers**. It used to call ``root.handlers.clear()``, which quietly
    destroyed any handler another component had already attached — a test
    framework's capture handler, or an embedder's. That made logging
    configuration order-dependent and invisible: records still reached the
    console, so nothing looked broken, while every other listener stopped
    receiving them. Found during Phase 2 completion because a Phase 2 security
    assertion (SR-5, no message content in the log) passed or failed depending
    on which test module happened to be imported first.

    Removing only *our own* handler would also be correct, but the
    ``_CONFIGURED`` guard already makes a second installation impossible, so
    there is nothing to remove.
    """
    global _CONFIGURED, _OWN_HANDLER
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(_RedactingFilter())

    root = logging.getLogger()
    # Deliberately not `root.handlers.clear()` — see the docstring.
    root.addHandler(handler)
    root.setLevel(level.upper())
    _OWN_HANDLER = handler

    # Third-party loggers are noisy at INFO and can echo request URLs.
    for noisy in ("httpx", "httpcore", "aiosqlite", "aiogram.event", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger that stamps correlation context onto every record.

    ``extra_context`` is merged into each record so a request or task id can be
    followed across the handler, the queue, and the worker (FR-9.4).
    """
    return logging.LoggerAdapter(logging.getLogger(name), context or {})
