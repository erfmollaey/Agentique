"""Celery task entry point.

Thin by design: resolve the process container, call the service, release the
concurrency slot. No formatting, no transport, no event-loop management
(TR-2, P1-5).

Audit references: the original body composed the agent, formatted the reply,
created and closed an event loop, and called Telegram inline (M-4, C-3).
"""

from __future__ import annotations

import logging

from app.bot import container as container_module
from app.core.celery_app import celery_app
from app.core.config import get_settings

# Importing the lifecycle module registers the Celery worker signals that give
# this process its long-lived event loop and close the Telegram session on
# shutdown (P0-7, C-3). The import must not be removed: without it the loop fix
# is inert even though the code appears correct.
from app.tasks import lifecycle as _lifecycle  # noqa: F401

log = logging.getLogger(__name__)

# Retry count comes from settings (CELERY_TASK_MAX_RETRIES) via the Celery
# task_annotations in app/core/celery_app.py, so there is one source of truth
# rather than a value duplicated here and there.
@celery_app.task(
    bind=True,
    name="app.tasks.research_task.process_research",
    autoretry_for=(),
)
def process_research(self, chat_id: int, user_id: int, query: str) -> int:
    """Analyse ``query`` and deliver the result to ``chat_id``.

    ``chat_id`` and ``user_id`` are distinct: in a group chat the first is the
    destination and the second is the sender (H-1).

    Retry policy (FR-4.4): retryable failures are retried a bounded number of
    times with backoff and the user is **not** messaged on an attempt that will
    be retried. A non-retryable failure, or an exhausted allowance, produces one
    terminal notice.
    """
    container = container_module.get_container()
    try:
        sent = container.research.execute(chat_id=chat_id, query=query)
        log.info(
            "research completed chat=%s user=%s task=%s chunks=%s",
            chat_id, user_id, self.request.id, sent,
        )
        return sent
    except Exception as exc:  # bounded retry, then report
        retryable = getattr(exc, "retryable", True)
        will_retry = retryable and self.request.retries < self.max_retries
        log.warning(
            "research failed chat=%s user=%s task=%s attempt=%s/%s "
            "error=%s retryable=%s will_retry=%s",
            chat_id, user_id, self.request.id, self.request.retries,
            self.max_retries, type(exc).__name__, retryable, will_retry,
        )
        if will_retry:
            raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc

        # Terminal: the user gets exactly one failure notice.
        try:
            container.research.notify_failure(chat_id, exc)
        except Exception:  # never mask the original failure
            log.error("could not deliver failure notice to chat %s", chat_id)
        raise
    finally:
        container.rate_limiter.release()


def _backoff(retries: int) -> int:
    """Exponential backoff from the configured base, bounded."""
    base = get_settings().CELERY_TASK_RETRY_BACKOFF
    return min(base * (2 ** max(retries - 1, 0)), 300)

