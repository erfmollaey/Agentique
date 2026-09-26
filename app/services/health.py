"""Dependency health probes (H-5, P1-3).

Liveness is a local fact. Readiness probes the broker and, as of Phase 2, the
database. Nothing is reported healthy that has not actually been checked
(FR-7.1 - FR-7.4).

Phase 1 deliberately did not probe PostgreSQL, because no database code existed
and claiming a check that did not exist would be false. Phase 2 introduces the
data layer, so the probe is real now.
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
    database_check: Callable[[], bool] | None = None,
) -> HealthReport:
    """Build a readiness report from injected probes.

    The poller is checked first: a stopped poller is the failure the audit
    identified as invisible (H-5), because the old endpoint returned a static
    literal while the bot was dead.

    ``database_check`` is optional so a container built without a database still
    produces a report; when it is supplied, an unreachable database makes the
    process unready, because the chat flow cannot run without it.
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

    if database_check is not None:
        try:
            db_ok = bool(database_check())
        except Exception:
            db_ok = False
        checks["database"] = "ok" if db_ok else "unavailable"
        healthy = healthy and db_ok

    return HealthReport(status="ok" if healthy else "degraded", checks=checks)
