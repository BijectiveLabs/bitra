from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.budget import RequestBudget, budget_for
from trading_research_agents.market_data.deriv import Series
from trading_research_agents.market_data.venues.ccxt_backfill import (
    CcxtBackfill,
    supported_series,
)

H = 3_600_000


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# Named as ccxt names it: the backfill recognises a rate limit by the class name.
class RateLimitExceeded(Exception):  # noqa: N818
    pass


class FakeVenue:
    """Answers from fixed rows the way a venue does: from `since`, at most `limit` of them."""

    def __init__(self, rows: list[dict[str, Any]], *, ignore_since: bool = False) -> None:
        self.rows = rows
        self.ignore_since = ignore_since
        self.calls: list[int | None] = []
        self.fail_first: Exception | None = None
        self.has = {
            "fetchFundingRateHistory": True,
            "fetchOpenInterestHistory": True,
            "fetchMarkOHLCV": True,
            "fetchIndexOHLCV": False,
            "fetchLongShortRatioHistory": "emulated",
        }
        self.rateLimit = 20

    def _page(self, since: int | None, limit: int | None) -> list[dict[str, Any]]:
        self.calls.append(since)
        if self.fail_first is not None:
            failure, self.fail_first = self.fail_first, None
            raise failure
        rows = (
            self.rows
            if self.ignore_since
            else [r for r in self.rows if r["timestamp"] >= (since or 0)]
        )
        return rows[: limit or len(rows)]

    def fetch_funding_rate_history(
        self, symbol: str, since: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return self._page(since, limit)

    def fetch_open_interest_history(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return self._page(since, limit)

    def fetch_mark_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]:
        return [[r["timestamp"], 1, 1, 1, r["close"], 0] for r in self._page(since, limit)]


def funding_rows(n: int) -> list[dict[str, Any]]:
    return [{"timestamp": H + i * 8 * H, "fundingRate": 0.0001 * (i + 1)} for i in range(n)]


def filler(venue: FakeVenue, page: int = 2) -> tuple[CcxtBackfill, Clock]:
    clock = Clock()
    budget = RequestBudget(60, clock=clock, sleep=clock.sleep)
    return CcxtBackfill(venue, budget=budget, clock=lambda: 1_000_000_000, page=page), clock


def test_funding_is_paged_forward_and_exact() -> None:
    venue = FakeVenue(funding_rows(5))
    got, _ = filler(venue)
    found = got.history(Series.FUNDING, "BTC/USDT:USDT", 0, 100 * H)
    assert [p.value for p in found.points] == [Decimal(str(0.0001 * (i + 1))) for i in range(5)]
    assert found.pages >= 3 and found.supported
    forward = [since or 0 for since in venue.calls]
    assert venue.calls[0] == 0 and all(b > a for a, b in pairwise(forward))


def test_open_interest_is_known_at_the_end_of_its_bucket() -> None:
    venue = FakeVenue([{"timestamp": H, "openInterestAmount": 12.5, "openInterestValue": None}])
    got, _ = filler(venue)
    (point,) = got.history(Series.OPEN_INTEREST, "X", 0, 2 * H, timeframe="5m").points
    assert point.as_of_ns == (H + 300_000) * 1_000_000
    assert point.get("measured") == "amount" and point.get("timeframe") == "5m"


def test_a_mark_candle_is_known_at_its_close() -> None:
    venue = FakeVenue([{"timestamp": H, "close": 93530.5}])
    got, _ = filler(venue)
    (point,) = got.history(Series.MARK, "X", 0, 2 * H).points
    assert point.as_of_ns == (H + 60_000) * 1_000_000 and point.value == Decimal("93530.5")


def test_a_venue_that_ignores_since_does_not_loop() -> None:
    venue = FakeVenue(funding_rows(2), ignore_since=True)
    got, _ = filler(venue, page=2)
    found = got.history(Series.FUNDING, "X", 0, 1000 * H)
    assert len(found.points) == 2 and len(venue.calls) == 2


def test_a_rate_limit_waits_inside_the_budget_then_retries() -> None:
    venue = FakeVenue(funding_rows(1))
    venue.fail_first = RateLimitExceeded("slow down")
    got, clock = filler(venue)
    found = got.history(Series.FUNDING, "X", 0, 10 * H)
    assert len(found.points) == 1 and clock.now >= 30.0


def test_any_other_failure_is_a_market_data_error() -> None:
    venue = FakeVenue(funding_rows(1))
    venue.fail_first = ConnectionError("reset")
    got, _ = filler(venue)
    with pytest.raises(MarketDataError, match="ConnectionError: reset"):
        got.history(Series.FUNDING, "X", 0, 10 * H)


def test_a_series_the_venue_does_not_offer_is_said_so() -> None:
    venue = FakeVenue([])
    assert supported_series(venue)[Series.INDEX] is False
    assert supported_series(venue)[Series.LONG_SHORT] is False
    got, _ = filler(venue)
    found = got.history(Series.INDEX, "X", 0, H)
    assert not found.supported and found.points == () and venue.calls == []


def test_a_row_without_a_value_is_dropped_and_counted() -> None:
    venue = FakeVenue(
        [{"timestamp": H, "fundingRate": None}, {"timestamp": 2 * H, "fundingRate": 0.0001}]
    )
    got, _ = filler(venue, page=10)
    found = got.history(Series.FUNDING, "X", 0, 10 * H)
    assert found.dropped == 1 and len(found.points) == 1


def test_the_budget_spaces_requests_and_starts_empty_after_a_pause() -> None:
    clock = Clock()
    budget = RequestBudget(60, clock=clock, sleep=clock.sleep)
    for _ in range(60):
        assert budget.acquire() == 0.0
    assert budget.acquire() == pytest.approx(1.0)
    budget.pause(5.0)
    assert budget.acquire() == pytest.approx(1.0)
    assert budget_for(FakeVenue([])).per_minute == 1500
    with pytest.raises(MarketDataError, match="positive"):
        RequestBudget(0)
