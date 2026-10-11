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

_soft, _hard = (
    _settings.CELERY_TASK_SOFT_TIME_LIMIT,
    _settings.CELERY_TASK_TIME_LIMIT,
)
if _soft >= _hard:
    # Enforced here, at the one place both values are consumed together, rather
    # than at the moment it is needed. A soft limit at or above the hard limit is
    # silently inert: the hard limit kills the worker first, no
    # SoftTimeLimitExceeded is ever raised inside the coroutine, and every run
    # dies with the run left `running` and no terminal notice — which is exactly
    # the state a graceful shutdown exists to avoid.
    raise ValueError(
        "CELERY_TASK_SOFT_TIME_LIMIT "
        f"({_soft}s) must be strictly below CELERY_TASK_TIME_LIMIT ({_hard}s); "
        "the soft limit is what lets a run shut down cleanly, and it cannot fire "
        "if the hard limit kills the worker first."
    )

celery_app = Celery(
    "research_bot",
    broker=_settings.REDIS_URL,
    backend=_settings.REDIS_URL,
    include=[
        "app.tasks.research_task",
        "app.tasks.research_pipeline_task",
        "app.tasks.lifecycle",
    ],
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
    #
    # The soft limit is the mechanism and the hard limit is the backstop, in that
    # order on purpose: the soft limit raises inside the awaiting frame where the
    # task can still mark the run failed and tell the user, and the hard limit
    # only exists for a process that cannot even be interrupted. See the
    # constraint above for why the two must not be equal.
    task_time_limit=_hard,
    task_soft_time_limit=_soft,
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
        },
        # P3.9-D: the research pipeline task. Same allowance, same backoff, and
        # deliberately nothing else — no raised time limit, no second queue, and no
        # ``task_acks_late`` change, because AD-045 put the deadline in the
        # coroutine precisely so the inherited limits stay backstops rather than the
        # mechanism (§ S.3.2, B5-C4: chat's limits are untouched).
        "app.tasks.research_task.run_research": {
            "max_retries": _settings.CELERY_TASK_MAX_RETRIES,
            "retry_backoff": True,
            "retry_backoff_max": 300,
            "retry_jitter": True,
        },
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
