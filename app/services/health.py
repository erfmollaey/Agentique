"""Dependency health probes (H-5, P1-3).

Liveness is a local fact. Readiness probes the broker. Nothing is reported
healthy that has not actually been checked (FR-7.1 - FR-7.4).

PostgreSQL is deliberately not probed: no database code exists in Phase 1, so
claiming a database check would be false (M-2).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from app.domain.schemas import HealthReport

log = logging.getLogger(__name__)


class BrokerConnection(Protocol):
    """The slice of a Celery/kombu connection a readiness probe needs."""

    def ensure_connection(self, max_retries: int = 0, timeout: float | None = None) -> None: ...

    def release(self) -> None: ...


def check_broker(
    connection_factory: Callable[[], BrokerConnection], timeout: float
) -> tuple[bool, str]:
    """Verify the Celery broker accepts a connection.

    Returns ``(ok, detail)``. ``detail`` never contains credentials.
    """
    try:
        connection = connection_factory()
        try:
            connection.ensure_connection(max_retries=0, timeout=timeout)
        finally:
            connection.release()
        return True, "ok"
    except Exception as exc:
        return False, type(exc).__name__


def readiness_report(
    broker_check: Callable[[], tuple[bool, str]],
    poller_alive: Callable[[], bool],
) -> HealthReport:
    """Build a readiness report from injected probes.

    The poller is checked first: a stopped poller is the failure the audit
    identified as invisible (H-5), because the old endpoint returned a static
    literal while the bot was dead.
    """
    checks: dict[str, str] = {}

    poller_ok = False
    try:
        poller_ok = bool(poller_alive())
        checks["telegram_poller"] = "ok" if poller_ok else "stopped"
    except Exception:
        checks["telegram_poller"] = "error"

    broker_ok, broker_detail = broker_check()
    checks["broker"] = broker_detail if broker_ok else f"unavailable ({broker_detail})"

    healthy = poller_ok and broker_ok
    return HealthReport(status="ok" if healthy else "degraded", checks=checks)
