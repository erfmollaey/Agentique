"""Per-user request throttling and a global in-flight cap (M-8, P1-8).

In-process by design. Phase 1 runs a single API process, so an in-memory
counter is sufficient and avoids adding a Redis round-trip to the hot path.

Limitation, recorded deliberately: with more than one API process these
counters stop being global. A shared store becomes necessary before scaling the
API tier horizontally. See the "Phase 1 limitations" note in the README.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.domain.errors import DailyQuotaExceededError, RateLimitedError


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    retry_after_seconds: int = 0
    reason: str = ""


class RateLimiter:
    """Sliding-window per-user throttle, a daily allowance, and a global cap.

    All three live in one limiter on purpose. Phase 2 FR-28 adds a daily
    allowance, and a second limiter would mean two sets of counters to reason
    about for the same decision. The daily allowance reuses the same sliding
    window with a longer span, so it inherits the same in-process limitation
    (AD-012) rather than introducing a second one.
    """

    def __init__(
        self, settings: Settings, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max_requests = settings.RATE_LIMIT_MAX_REQUESTS
        self._window = settings.RATE_LIMIT_WINDOW_SECONDS
        self._max_in_flight = settings.MAX_IN_FLIGHT_REQUESTS
        self._daily_max = settings.RATE_LIMIT_DAILY_MAX_REQUESTS
        self._daily_window = settings.RATE_LIMIT_DAILY_WINDOW_SECONDS
        self._clock = clock
        self._hits: dict[int, deque[float]] = {}
        self._daily: dict[int, deque[float]] = {}
        self._in_flight = 0
        self._lock = threading.Lock()

    def check(self, user_id: int) -> ThrottleDecision:
        """Decide whether ``user_id`` may enqueue a request right now."""
        now = self._clock()
        with self._lock:
            if self._in_flight >= self._max_in_flight:
                return ThrottleDecision(
                    allowed=False,
                    reason="global_capacity",
                    retry_after_seconds=self._window,
                )

            daily = self._prune(self._daily.setdefault(user_id, deque()), now, self._daily_window)
            if len(daily) >= self._daily_max:
                return ThrottleDecision(
                    allowed=False,
                    reason="daily_quota",
                    retry_after_seconds=max(int(self._daily_window - (now - daily[0])) + 1, 1),
                )

            bucket = self._prune(self._hits.setdefault(user_id, deque()), now, self._window)

            if len(bucket) >= self._max_requests:
                retry_after = int(self._window - (now - bucket[0])) + 1
                return ThrottleDecision(
                    allowed=False,
                    reason="user_rate_limit",
                    retry_after_seconds=max(retry_after, 1),
                )

            bucket.append(now)
            daily.append(now)
            self._in_flight += 1
            return ThrottleDecision(allowed=True)

    @staticmethod
    def _prune(bucket: deque[float], now: float, window: float) -> deque[float]:
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        return bucket

    def release(self) -> None:
        """Return a concurrency slot after a request finishes."""
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    def enforce(self, user_id: int) -> None:
        """Raise :class:`RateLimitedError` if the caller is over their limit."""
        decision = self.check(user_id)
        if not decision.allowed:
            error_cls = (
                DailyQuotaExceededError
                if decision.reason == "daily_quota"
                else RateLimitedError
            )
            raise error_cls(
                f"throttled: {decision.reason}, retry in {decision.retry_after_seconds}s"
            )

    def reset(self) -> None:
        """Clear all state. Test support."""
        with self._lock:
            self._hits.clear()
            self._daily.clear()
            self._in_flight = 0
