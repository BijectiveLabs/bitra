"""Derivatives history from a venue through ccxt: funding, open interest, mark, index, long/short.

Every request draws from the venue's budget, every page must move forward in time, and a series
the venue does not offer is reported as unsupported rather than returned empty, so an absent
figure downstream always says why it is absent.

Reads are idempotent, so a request the venue refused for its rate limit is asked again after the
pause it asked for, a bounded number of times. Nothing here places an order or holds a key.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.budget import RequestBudget
from trading_research_agents.market_data.deriv import DerivPoint, Series, detail_of

NS_PER_MS = 1_000_000
CANDLE_MS = 60_000
MAX_PAGES = 10_000
RATE_LIMITED = ("RateLimitExceeded", "DDoSProtection")
SUPPORT: Mapping[Series, str] = {
    Series.FUNDING: "fetchFundingRateHistory",
    Series.OPEN_INTEREST: "fetchOpenInterestHistory",
    Series.MARK: "fetchMarkOHLCV",
    Series.INDEX: "fetchIndexOHLCV",
    Series.LONG_SHORT: "fetchLongShortRatioHistory",
}
# The archive venues whose trades are a perpetual's, and the ccxt name of that market's venue.
# Binance's public archive is spot, which has no funding or open interest to backfill.
DERIV_VENUES: Mapping[str, str] = {"bybit": "bybit"}
# Bybit's names for the account-ratio periods; ccxt passes a timeframe through unchanged, and Bybit
# answers "5m" with nothing (verified through ccxt 4.5.85 on 2026-10-26).
BYBIT_PERIODS = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}
TIMEFRAME_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "1d": 86_400_000}

# One row as the pages read it: the venue's row time, the time the value was known, the value or
# None when the row carried none, and the point's detail.
Row = tuple[int, int, Decimal | None, tuple[tuple[str, str], ...]]
Fetch = Callable[[int], list[Row]]


def supported_series(client: Any) -> dict[Series, bool]:
    """Which series the venue's REST interface offers, as ccxt states; emulated is not offered."""
    has = getattr(client, "has", None) or {}
    return {series: has.get(key) is True for series, key in SUPPORT.items()}


@dataclass(frozen=True)
class Backfilled:
    series: Series
    points: tuple[DerivPoint, ...]
    pages: int
    dropped: int
    supported: bool


class CcxtBackfill:
    def __init__(
        self,
        client: Any,
        *,
        budget: RequestBudget,
        clock: Callable[[], int] = time.time_ns,
        page: int = 200,
        attempts: int = 3,
        rate_pause_s: float = 30.0,
    ) -> None:
        if page <= 0 or attempts <= 0:
            raise MarketDataError("a page and the attempts are positive")
        self.client = client
        self.budget = budget
        self.clock = clock
        self.page = page
        self.attempts = attempts
        self.rate_pause_s = rate_pause_s

    def history(
        self, series: Series, symbol: str, start_ms: int, end_ms: int, *, timeframe: str = "5m"
    ) -> Backfilled:
        """Every point of `series` that became known in [start_ms, end_ms], oldest first.

        A bucket is known at its end, so the window is asked from one bucket before its start:
        otherwise the bucket that closes on the first instant would belong to no window at all.
        It is asked from one millisecond earlier still, because some venues read `since` as
        "after"; anything extra that arrives is outside the window and left out.
        """
        if end_ms < start_ms:
            raise MarketDataError("the end is before the start")
        if not supported_series(self.client).get(series, False):
            return Backfilled(series, (), 0, 0, supported=False)
        fetch = self._fetcher(series, symbol, timeframe)
        lead_ms = _lead_ms(series, timeframe)
        rows, pages, dropped = self._pages(fetch, start_ms - lead_ms - 1, start_ms, end_ms)
        received = self.clock()
        points = tuple(
            DerivPoint(series, as_of * NS_PER_MS, received, value, detail)
            for _, as_of, value, detail in rows
            if value is not None
        )
        return Backfilled(series, points, pages, dropped, supported=True)

    def _fetcher(self, series: Series, symbol: str, timeframe: str) -> Fetch:
        c, n = self.client, self.page
        if series is Series.FUNDING:
            return lambda since: [
                _funding(r) for r in c.fetch_funding_rate_history(symbol, since, n)
            ]
        if series is Series.MARK:
            return lambda since: [_candle(r) for r in c.fetch_mark_ohlcv(symbol, "1m", since, n)]
        if series is Series.INDEX:
            return lambda since: [_candle(r) for r in c.fetch_index_ohlcv(symbol, "1m", since, n)]
        span = TIMEFRAME_MS.get(timeframe)
        if span is None:
            raise MarketDataError(f"timeframe {timeframe!r} is not one of {sorted(TIMEFRAME_MS)}")
        if series is Series.OPEN_INTEREST:
            return lambda since: [
                _interest(r, span, timeframe)
                for r in c.fetch_open_interest_history(symbol, timeframe, since, n)
            ]
        period = BYBIT_PERIODS.get(timeframe) if getattr(c, "id", "") == "bybit" else None
        if period is not None:
            # Bybit ignores `since` and answers a window with its newest buckets, so each request
            # is a window of exactly one page, which keeps forward paging exact.
            def windowed(since: int) -> list[Row]:
                window = {"startTime": since, "endTime": since + n * span - 1}
                rows = c.fetch_long_short_ratio_history(symbol, period, None, n, window)
                return [_ratio(r, span, timeframe) for r in rows]

            return windowed
        return lambda since: [
            _ratio(r, span, timeframe)
            for r in c.fetch_long_short_ratio_history(symbol, timeframe, since, n)
        ]

    def _pages(
        self, fetch: Fetch, since_ms: int, start_ms: int, end_ms: int
    ) -> tuple[list[Row], int, int]:
        found: dict[int, Row] = {}
        dropped = 0
        cursor = since_ms
        for pages in range(1, MAX_PAGES + 1):
            rows = self._call(fetch, cursor)
            stamps = [row[0] for row in rows]
            for row in rows:
                if not start_ms <= row[1] <= end_ms:
                    continue
                if row[2] is None:
                    dropped += 1
                    continue
                found[row[1]] = row
            # A page that does not reach past the cursor means the venue has nothing newer, or is
            # ignoring `since`; either way asking again would return the same page.
            if not stamps or max(stamps) < cursor or max(stamps) >= end_ms:
                return [found[at] for at in sorted(found)], pages, dropped
            cursor = max(stamps) + 1
        raise MarketDataError(f"more than {MAX_PAGES} pages; the venue is not moving forward")

    def _call(self, fetch: Fetch, cursor: int) -> list[Row]:
        failure: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            self.budget.acquire()
            try:
                return fetch(cursor)
            except Exception as exc:
                failure = exc
                if attempt < self.attempts and _rate_limited(exc):
                    self.budget.pause(self.rate_pause_s)
                    continue
                break
        raise MarketDataError(f"{failure.__class__.__name__}: {failure}") from failure


def _lead_ms(series: Series, timeframe: str) -> int:
    """How long after its row time a value of `series` becomes known."""
    if series is Series.FUNDING:
        return 0
    if series in (Series.MARK, Series.INDEX):
        return CANDLE_MS
    return TIMEFRAME_MS[timeframe]


def _rate_limited(exc: Exception) -> bool:
    return any(kind.__name__ in RATE_LIMITED for kind in type(exc).__mro__)


def _value(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _funding(row: Mapping[str, Any]) -> Row:
    at = int(row["timestamp"])
    return at, at, _value(row.get("fundingRate")), detail_of(settled="true")


def _candle(row: list[Any]) -> Row:
    at = int(row[0])
    return at, at + CANDLE_MS, _value(row[4]), ()


def _interest(row: Mapping[str, Any], span: int, timeframe: str) -> Row:
    at = int(row["timestamp"])
    amount = _value(row.get("openInterestAmount"))
    if amount is not None:
        return at, at + span, amount, detail_of(measured="amount", timeframe=timeframe)
    value = _value(row.get("openInterestValue"))
    return at, at + span, value, detail_of(measured="value", timeframe=timeframe)


def _ratio(row: Mapping[str, Any], span: int, timeframe: str) -> Row:
    at = int(row["timestamp"])
    return at, at + span, _value(row.get("longShortRatio")), detail_of(timeframe=timeframe)
