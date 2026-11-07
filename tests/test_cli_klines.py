from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.cli.klines import data_klines
from trading_research_agents.cli.main import HANDLERS
from trading_research_agents.cli.parser import build_parser
from trading_research_agents.market_data.days import midnight_ns
from trading_research_agents.market_data.klines import read_klines

pytest.importorskip("pyarrow")

DAY = date(2025, 7, 1)
HOUR_MS = 3_600_000
DAY_MS = midnight_ns(DAY) // 1_000_000


class Venue:
    """Binance USD-M's public candles and funding over one day, as ccxt hands them over."""

    id = "binanceusdm"
    rateLimit = 50  # noqa: N815
    has = {"fetchFundingRateHistory": True}

    def __init__(self) -> None:
        market = {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "swap": True}
        self.markets = {"BTC/USDT:USDT": market}
        self.markets_by_id = {"BTCUSDT": [market]}

    def load_markets(self) -> dict[str, Any]:
        return self.markets

    def fapiPublicGetKlines(self, params: dict[str, Any]) -> list[list[Any]]:  # noqa: N802
        rows = []
        for h in range(24):
            start = DAY_MS + h * HOUR_MS
            if params["startTime"] <= start <= params["endTime"]:
                rows.append(
                    [start, "100", "101", "99", "100.5", "2", start + HOUR_MS - 1, "201", 9, "1.2"]
                )
        return rows[: params["limit"]]

    def fetch_funding_rate_history(self, symbol: str, since: int, limit: int) -> list[Any]:
        assert symbol == "BTC/USDT:USDT"
        stamps = [DAY_MS + h * HOUR_MS + 2 for h in (0, 8, 16)]
        return [{"timestamp": t, "fundingRate": 0.0001} for t in stamps if t >= since][:limit]


def args_for(tmp: Path, *extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        ["data", "klines", "BTCUSDT", "2025-07-01", "--to", "2025-07-01", "--out", str(tmp), *extra]
    )


def test_the_command_is_wired() -> None:
    assert ("data", "klines") in HANDLERS


def test_a_day_of_candles_and_funding_is_stored_and_reads_back(tmp_path: Path) -> None:
    payload, lines, code = data_klines(args_for(tmp_path), client_for=lambda _: Venue())
    assert code == 0 and payload["rows"] == 24 and payload["missing_rows"] == 0
    assert payload["funding_rows"] == 3
    stored = read_klines(tmp_path, "binanceusdm", "BTCUSDT")
    assert len(stored.bars) == 24 and stored.bars[0].taker_buy_base == Decimal("1.2")
    assert [p.as_of_ns % 1_000_000_000 for p in stored.points] == [0, 0, 0]
    assert any("24 hours" in line for line in lines)


def test_a_venue_without_the_taker_split_is_named(tmp_path: Path) -> None:
    class Bybit(Venue):
        id = "bybit"

    with pytest.raises(ValueError, match="taker"):
        data_klines(args_for(tmp_path, "--venue", "bybit"), client_for=lambda _: Bybit())
