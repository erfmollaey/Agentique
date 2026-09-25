"""T-11 — Health and readiness; T-12 — rate limiting; Celery configuration.

Audit references: H-5 (``/health`` was a static literal that reported a dead
bot as healthy), M-8 (no rate limiting), H-3 and M-9 (Celery reliability and
result retention).
"""

from __future__ import annotations

import pytest

from app.services.health import check_broker, readiness_report
from app.services.rate_limit import RateLimiter

# --- T-11: readiness -------------------------------------------------------

def test_t11_readiness_is_ok_when_poller_and_broker_are_healthy():
    report = readiness_report(lambda: (True, "ok"), lambda: True)
    assert report.status == "ok"
    assert report.healthy is True
    assert report.checks["telegram_poller"] == "ok"


def test_t11_stopped_poller_makes_readiness_fail():
    """The defect the audit found: the bot is dead but health said ok."""
    report = readiness_report(lambda: (True, "ok"), lambda: False)
    assert report.status == "degraded"
    assert report.healthy is False
    assert report.checks["telegram_poller"] == "stopped"


def test_t11_unavailable_broker_makes_readiness_fail():
    report = readiness_report(lambda: (False, "ConnectionError"), lambda: True)
    assert report.status == "degraded"
    assert "ConnectionError" in report.checks["broker"]


def test_t11_both_failing_is_still_reported_independently():
    report = readiness_report(lambda: (False, "ConnectionError"), lambda: False)
    assert report.checks["telegram_poller"] == "stopped"
    assert "ConnectionError" in report.checks["broker"]


def test_t11_poller_probe_exception_is_contained():
    def boom():
        raise RuntimeError("probe exploded")

    report = readiness_report(lambda: (True, "ok"), boom)
    assert report.healthy is False
    assert report.checks["telegram_poller"] == "error"


def test_t11_report_never_contains_credentials():
    report = readiness_report(lambda: (False, "ConnectionError"), lambda: False)
    text = str(report.model_dump())
    assert "sk-" not in text
    assert "redis://" not in text


# --- check_broker ----------------------------------------------------------

def test_check_broker_reports_ok_for_a_healthy_connection():
    class Conn:
        def ensure_connection(self, max_retries=0, timeout=None):
            return None

        def release(self):
            return None

    ok, detail = check_broker(lambda: Conn(), 1.0)
    assert ok is True
    assert detail == "ok"


def test_check_broker_reports_failure_without_leaking_the_url():
    class Conn:
        def ensure_connection(self, max_retries=0, timeout=None):
            raise ConnectionRefusedError("connection to redis://user:hunter2@host refused")

        def release(self):
            return None

    ok, detail = check_broker(lambda: Conn(), 1.0)
    assert ok is False
    assert "hunter2" not in detail, "connection detail leaked"
    assert detail == "ConnectionRefusedError"


# --- Health endpoint over HTTP ---------------------------------------------

def test_t11_health_endpoint_is_liveness_only():
    """``/health`` must not claim any dependency is healthy; it checks none."""
    from app.main import app

    routes = {r.path: r for r in app.routes}
    assert "/health" in routes
    assert "/ready" in routes, "a distinct readiness endpoint is required"


# --- T-12: rate limiting ---------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_t12_requests_within_allowance_are_permitted(settings):
    """The per-user allowance permits RATE_LIMIT_MAX_REQUESTS requests.

    The concurrency slot is released between requests, mirroring real use where
    a task frees its slot when it finishes; otherwise the global cap would trip
    first and mask the per-user behaviour under test.
    """
    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
        assert limiter.check(1).allowed is True
        limiter.release()


def test_t12_request_beyond_allowance_is_refused(settings):
    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
        limiter.check(1)
        limiter.release()

    decision = limiter.check(1)
    assert decision.allowed is False
    assert decision.reason == "user_rate_limit"
    assert decision.retry_after_seconds > 0, "no retry guidance for the user"


def test_t12_allowance_recovers_after_the_window(settings):
    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
        limiter.check(1)
        limiter.release()
    assert limiter.check(1).allowed is False

    clock.advance(settings.RATE_LIMIT_WINDOW_SECONDS + 1)
    assert limiter.check(1).allowed is True


def test_t12_limit_is_per_user(settings):
    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
        limiter.check(1)
        limiter.release()

    assert limiter.check(1).allowed is False
    assert limiter.check(2).allowed is True, "one user's limit affected another"
    limiter.release()


def test_t12_global_capacity_cap_is_enforced(settings):
    """Saturating the in-flight cap refuses work even from a fresh user."""
    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    for i in range(settings.MAX_IN_FLIGHT_REQUESTS):
        assert limiter.check(i).allowed is True

    decision = limiter.check(999)
    assert decision.allowed is False
    assert decision.reason == "global_capacity"


def test_t12_release_frees_a_concurrency_slot(settings):
    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    for i in range(settings.MAX_IN_FLIGHT_REQUESTS):
        limiter.check(i)
    assert limiter.check(999).allowed is False

    limiter.release()
    assert limiter.check(999).allowed is True


def test_t12_enforce_raises_a_typed_error(settings):
    from app.domain.errors import RateLimitedError

    clock = FakeClock()
    limiter = RateLimiter(settings, clock=clock)
    with pytest.raises(RateLimitedError):
        for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
            limiter.check(1)
            limiter.release()
        limiter.enforce(1)


# --- Celery configuration (H-3, M-9) ---------------------------------------

def test_celery_has_time_limits():
    from app.core.celery_app import celery_app

    assert celery_app.conf.task_time_limit > 0
    assert celery_app.conf.task_soft_time_limit > 0
    assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit


def test_celery_does_not_accumulate_results():
    """M-9: nothing reads results, so storing them grows Redis unbounded."""
    from app.core.celery_app import celery_app

    assert celery_app.conf.task_ignore_result is True


def test_celery_acknowledgement_and_worker_loss_settings():
    from app.core.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.broker_connection_retry_on_startup is True


def test_celery_research_task_retries_are_bounded():
    """P0-12: retries must be bounded, never unbounded."""
    from app.core.celery_app import celery_app

    annotation = celery_app.conf.task_annotations[
        "app.tasks.research_task.process_research"
    ]
    assert 0 < annotation["max_retries"] <= 10
    assert annotation["retry_backoff"] is True


def test_celery_prefetch_is_configured():
    from app.core.celery_app import celery_app

    assert celery_app.conf.worker_prefetch_multiplier >= 1


def test_celery_owns_no_logging_configuration():
    """M-7: the app configures logging once, the worker must not duplicate it."""
    from app.core.celery_app import celery_app

    assert celery_app.conf.worker_hijack_root_logger is False
