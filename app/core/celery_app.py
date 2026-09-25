"""Celery application and reliability configuration.

Audit references: H-3 (no time limits, no retry policy, no prefetch tuning),
M-9 (result backend equals broker, results never read, keys accumulate),
P0-12 (bounded retry with backoff), P0-13 (task time limits), P1-9.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import Settings, get_settings
from app.core.logging_config import configure_logging

configure_logging()

_settings: Settings = get_settings()

celery_app = Celery(
    "research_bot",
    broker=_settings.REDIS_URL,
    backend=_settings.REDIS_URL,
    include=["app.tasks.research_task", "app.tasks.lifecycle"],
)

celery_app.conf.update(
    # --- Serialisation -----------------------------------------------------
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Tehran",
    enable_utc=True,
    # --- Result retention (M-9) --------------------------------------------
    # Nothing reads task results, so storing them grows Redis without bound.
    task_ignore_result=True,
    result_expires=3600,
    # --- Reliability (H-3, P0-13) -----------------------------------------
    # A hung LLM call cannot occupy a worker forever.
    task_time_limit=_settings.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=_settings.CELERY_TASK_SOFT_TIME_LIMIT,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # A lost task is redelivered, so a worker lost mid-task must not be retried
    # forever: Celery's own default applies when a task never acks.
    task_annotations={
        "app.tasks.research_task.process_research": {
            "max_retries": _settings.CELERY_TASK_MAX_RETRIES,
            "retry_backoff": True,
            "retry_backoff_max": 300,
            "retry_jitter": True,
        }
    },
    # --- Broker behaviour --------------------------------------------------
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,  # retry until the broker recovers
    task_default_queue="research",
    # --- Worker behaviour (P1-9) -------------------------------------------
    worker_prefetch_multiplier=_settings.CELERY_WORKER_PREFETCH_MULTIPLIER,
    task_send_sent_event=True,
    worker_hijack_root_logger=False,  # app owns logging configuration
)


def get_celery_app() -> Celery:
    """Return the configured Celery application."""
    return celery_app
