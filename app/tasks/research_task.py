"""Celery task entry point.

Thin by design: resolve the process container, call the chat service, deliver
the result, release the concurrency slot. No prompt construction, no SQL, no
provider SDK, no transport management (TR-2, Phase 2 § 18).

**Naming.** The module and task keep their Phase 1 names. The Phase 1
structural test asserts on the literal import
``from app.tasks.research_task import process_research``, and
``DEVELOPMENT_RULES.md`` § 7 forbids renaming an existing application file
without an explicit instruction. The behaviour is now the AI chat flow; the
name is inherited. Recorded as known naming debt in ``docs/PROJECT_PLAN.md``.

Audit references: the original body composed the agent, formatted the reply,
created and closed an event loop, and called Telegram inline (M-4, C-3).

Retry policy (FR-4.4, Phase 2 § 15): a retryable failure is retried a bounded
number of times with backoff and the user is **not** messaged on an attempt that
will be retried. A non-retryable failure, or an exhausted allowance, produces
exactly one terminal notice. Because the whole turn is one database
transaction, a rolled-back attempt leaves no partial state for the retry to
trip over.
"""

from __future__ import annotations

import logging

from app.bot import container as container_module
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.domain.chat import ChatTurn
from app.services.failures import user_message_for

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
def process_research(
    self,
    chat_id: int,
    user_id: int,
    query: str,
    username: str | None = None,
    telegram_message_id: int | None = None,
) -> int:
    """Run one chat turn and deliver the reply to ``chat_id``.

    ``chat_id`` and ``user_id`` are distinct: in a group chat the first is the
    destination and the second is the sender (H-1).
    """
    container = container_module.get_container()
    chat = container.chat
    try:
        if chat is None:  # pragma: no cover - only reachable via a bad test double
            raise RuntimeError("container has no chat service")

        turn = ChatTurn(
            telegram_user_id=user_id,
            chat_id=chat_id,
            text=query,
            username=username,
            telegram_message_id=telegram_message_id,
        )
        # The service is async because persistence is. The task body is
        # synchronous, so it is driven on this process's long-lived loop rather
        # than a new one per task (audit C-3).
        outcome = run_async(chat.handle_message(turn))
        sent = container.delivery.send(chat_id, outcome.text)
        log.info(
            "chat turn completed chat=%s user=%s task=%s conversation=%s chunks=%s "
            "duplicate=%s truncated=%s tokens=%s",
            chat_id, user_id, self.request.id, outcome.conversation_id, sent,
            outcome.duplicate, outcome.truncated_history,
            outcome.prompt_tokens + outcome.completion_tokens,
        )
        return sent
    except Exception as exc:  # bounded retry, then report
        retryable = getattr(exc, "retryable", True)
        will_retry = retryable and self.request.retries < self.max_retries
        log.warning(
            "chat turn failed chat=%s user=%s task=%s attempt=%s/%s "
            "error=%s retryable=%s will_retry=%s",
            chat_id, user_id, self.request.id, self.request.retries,
            self.max_retries, type(exc).__name__, retryable, will_retry,
        )
        if will_retry:
            raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc

        # Terminal: the user gets exactly one failure notice, and it never
        # contains provider text, SQL, or a traceback.
        try:
            container.delivery.send(chat_id, user_message_for(exc))
        except Exception:  # never mask the original failure
            log.error("could not deliver failure notice to chat %s", chat_id)
        raise
    finally:
        container.rate_limiter.release()


def run_async(coro):
    """Drive a coroutine on this process's long-lived event loop."""
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)


def _backoff(retries: int) -> int:
    """Exponential backoff from the configured base, bounded."""
    base = get_settings().CELERY_TASK_RETRY_BACKOFF
    return min(base * (2 ** max(retries - 1, 0)), 300)
