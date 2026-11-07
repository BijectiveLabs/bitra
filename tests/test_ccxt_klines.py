from __future__ import annotations

from typing import Any

import pytest

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.budget import RequestBudget
from trading_research_agents.market_data.venues.ccxt_klines import fetch_kline_rows

HOUR_MS = 3_600_000


# Named as ccxt names it: the rate-limit test reads the class name, as it does for ccxt's own.
class RateLimitExceeded(Exception):  # noqa: N818
    pass


class Candles:
    """Binance USD-M's candle endpoint over a fixed run of hours, a page at a time."""

    id = "binanceusdm"

    def __init__(self, hours: int, refuse_first: bool = False) -> None:
        self.rows = [
            [h * HOUR_MS, "1", "1", "1", "1", "1", h * HOUR_MS + HOUR_MS - 1] for h in range(hours)
        ]
        self.asked: list[dict[str, Any]] = []
        self.refuse_first = refuse_first

    def fapiPublicGetKlines(self, params: dict[str, Any]) -> list[list[Any]]:  # noqa: N802
        self.asked.append(params)
        if self.refuse_first:
            self.refuse_first = False
            raise RateLimitExceeded("slow down")
        start, end, limit = params["startTime"], params["endTime"], params["limit"]
        return [r for r in self.rows if start <= r[0] <= end][:limit]


def budget() -> RequestBudget:
    return RequestBudget(6000, sleep=lambda _: None)


def test_pages_move_forward_until_the_window_is_read() -> None:
    venue = Candles(5)
    rows = fetch_kline_rows(venue, "BTCUSDT", 0, 4 * HOUR_MS, budget=budget(), page=2)
    assert [r[0] for r in rows] == [h * HOUR_MS for h in range(5)]
    assert [p["startTime"] for p in venue.asked] == [0, 2 * HOUR_MS, 4 * HOUR_MS]
    assert all(p["interval"] == "1h" and p["symbol"] == "BTCUSDT" for p in venue.asked)


def test_a_rate_limit_is_waited_out_and_asked_again() -> None:
    venue = Candles(2, refuse_first=True)
    rows = fetch_kline_rows(venue, "BTCUSDT", 0, HOUR_MS, budget=budget(), rate_pause_s=0)
    assert len(rows) == 2 and len(venue.asked) == 2


def test_a_venue_whose_candles_lack_the_taker_split_is_refused() -> None:
    class Bybit:
        id = "bybit"

    with pytest.raises(MarketDataError, match="taker"):
        fetch_kline_rows(Bybit(), "BTCUSDT", 0, HOUR_MS, budget=budget())
