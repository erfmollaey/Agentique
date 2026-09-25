"""Telegram dispatcher construction.

The module-level ``bot`` and ``dp`` singletons that lived here were the enabler
of audit C-3: a shared ``Bot`` cached an HTTP session bound to whichever event
loop happened to be running, while the Celery task created and closed a loop
per invocation. A process-wide ``Bot`` plus a per-task loop is exactly the
combination that breaks on the second task.

They are gone. Construction lives in :mod:`app.infrastructure.telegram` and is
performed once per process by :mod:`app.bot.container`.

This module is retained as an import path for existing callers and now exposes
only the factory functions.
"""

from __future__ import annotations

from app.infrastructure.telegram import create_bot, create_dispatcher

__all__ = ["create_bot", "create_dispatcher"]
