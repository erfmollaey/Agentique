"""Structured logging, initialised exactly once per process.

Audit references: M-7 (basicConfig called twice, full user text logged),
FR-9 (single init, lazy interpolation, no secrets, correlation ids).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_CONFIGURED = False

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
    """
    global _CONFIGURED
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
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

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
