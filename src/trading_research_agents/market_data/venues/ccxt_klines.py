"""Hour candles from a venue whose candles carry the taker split and the trade count.

Binance USD-M's `/fapi/v1/klines` does (taker buy base volume and number of trades, columns 9 and
8; verified on 2026-11-07 through ccxt 4.5.91's implicit `fapiPublicGetKlines`). ccxt's unified
`fetch_ohlcv` drops those columns, so the raw endpoint is read. A venue without such candles is
refused: the arena's flow features would otherwise read a split nobody measured.

Reads are idempotent, so a request refused for the rate limit is asked again after a pause, a
bounded number of times. Nothing here places an order or holds a key.
"""

from __future__ import annotations

from typing import Any

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.ports import BudgetStore
from trading_research_agents.market_data.venues.ccxt_backfill import RATE_LIMITED

HOUR_MS = 3_600_000
PAGE = 1500
MAX_PAGES = 10_000
ENDPOINTS = {"binanceusdm": "fapiPublicGetKlines"}
DEFAULT_VENUE = "binanceusdm"


def fetch_kline_rows(
    client: Any,
    symbol_id: str,
    start_ms: int,
    end_ms: int,
    *,
    budget: BudgetStore,
    page: int = PAGE,
    attempts: int = 3,
    rate_pause_s: float = 30.0,
) -> list[list[Any]]:
    """Every hour candle opening in [start_ms, end_ms], oldest first, as the venue sent it."""
    endpoint = ENDPOINTS.get(getattr(client, "id", ""))
    if endpoint is None:
        raise MarketDataError(
            f"{getattr(client, 'id', client)!r} candles do not carry the taker split and trade "
            f"count; venues that do: {sorted(ENDPOINTS)}"
        )
    read = getattr(client, endpoint)
    rows: list[list[Any]] = []
    cursor = start_ms
    for _ in range(MAX_PAGES):
        params = {
            "symbol": symbol_id,
            "interval": "1h",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": page,
        }
        got = _call(read, params, budget, attempts, rate_pause_s)
        rows.extend(r for r in got if start_ms <= int(r[0]) <= end_ms)
        if not got or int(got[-1][0]) + HOUR_MS > end_ms:
            return rows
        cursor = int(got[-1][0]) + HOUR_MS
    raise MarketDataError(f"more than {MAX_PAGES} pages; the venue is not moving forward")


def _call(
    read: Any, params: dict[str, Any], budget: BudgetStore, attempts: int, pause_s: float
) -> list[list[Any]]:
    failure: Exception | None = None
    for attempt in range(1, attempts + 1):
        budget.acquire()
        try:
            return list(read(params))
        except Exception as exc:
            failure = exc
            limited = any(kind.__name__ in RATE_LIMITED for kind in type(exc).__mro__)
            if attempt < attempts and limited:
                budget.pause(pause_s)
                continue
            break
    raise MarketDataError(f"{failure.__class__.__name__}: {failure}") from failure
