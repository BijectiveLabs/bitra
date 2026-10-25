"""How many requests a minute this process sends one venue, whatever is asking.

A venue's rate limit belongs to the venue, not to each caller. A backfill, a poll and a recorder
that each kept their own count would together exceed it, get refused, and lose data while they
wait. So every request to a venue draws from one bucket, and a venue that says slow down empties
it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from trading_research_agents.market_data.bars import MarketDataError

DEFAULT_PER_MINUTE = 60


class RequestBudget:
    """At most `per_minute` requests a minute, refilled continuously."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute <= 0:
            raise MarketDataError("a request budget is a positive number per minute")
        self.per_minute = per_minute
        self.waited_s = 0.0
        self._rate = per_minute / 60.0
        self._tokens = float(per_minute)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one request's place, waiting for it if the bucket is empty; the wait in seconds."""
        with self._lock:
            self._refill()
            wait = 0.0
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                self._sleep(wait)
                self._refill()
            self._tokens = max(self._tokens - 1.0, 0.0)
            self.waited_s += wait
            return wait

    def pause(self, seconds: float) -> None:
        """The venue asked to slow down: wait, and start again from an empty bucket."""
        with self._lock:
            self._sleep(seconds)
            self.waited_s += seconds
            self._tokens = 0.0
            self._last = self._clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.per_minute), self._tokens + (now - self._last) * self._rate)
        self._last = now


def budget_for(client: Any, *, share: float = 0.5) -> RequestBudget:
    """A budget at `share` of the limit ccxt reports for the venue (its `rateLimit`, in ms)."""
    rate_ms = getattr(client, "rateLimit", None)
    if not isinstance(rate_ms, int | float) or rate_ms <= 0:
        return RequestBudget(DEFAULT_PER_MINUTE)
    return RequestBudget(max(1, int(60_000 / rate_ms * share)))
