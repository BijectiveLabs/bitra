from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.market_data.archive import ArchiveError
from trading_research_agents.market_data.bars import GapState, MarketDataError
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series
from trading_research_agents.market_data.klines import (
    HOUR_NS,
    bars_from_klines,
    funding_on_the_second,
    read_klines,
    write_klines,
)

T0 = midnight_ns(date(2025, 7, 1))
T0_MS = T0 // 1_000_000
HOUR_MS = 3_600_000


def row(i: int, close: str = "100.5", trades: int = 40, buy: str = "6.000") -> list[object]:
    start = T0_MS + i * HOUR_MS
    return [
        start,
        "100.0",
        "101.0",
        "99.0",
        close,
        "10.000",
        start + HOUR_MS - 1,
        "1005.0",
        trades,
        buy,
        "603.0",
        "0",
    ]


def test_a_candle_carries_its_taker_split_and_trade_count() -> None:
    bars = bars_from_klines([row(0)], venue="binanceusdm", instrument="BTCUSDT")
    bar = bars[0]
    assert (bar.start_ns, bar.end_ns) == (T0, T0 + HOUR_NS)
    assert bar.ohlc() == (Decimal("100.0"), Decimal("101.0"), Decimal("99.0"), Decimal("100.5"))
    assert bar.taker_buy_base == Decimal("6.000") and bar.taker_sell_base == Decimal("4.000")
    assert bar.trade_count == 40 and bar.volume_quote == Decimal("1005.0")
    assert bar.large_trade_base is None and bar.source == "klines:binanceusdm"
    assert bar.gap is GapState.NONE


def test_an_hour_the_venue_did_not_return_is_a_missing_bar() -> None:
    bars = bars_from_klines([row(0), row(2)], venue="binanceusdm", instrument="BTCUSDT")
    assert [b.start_ns for b in bars] == [T0, T0 + HOUR_NS, T0 + 2 * HOUR_NS]
    assert bars[1].gap is GapState.MISSING and bars[1].empty


def test_an_hour_without_trades_is_quiet_not_missing() -> None:
    bars = bars_from_klines([row(0, trades=0)], venue="binanceusdm", instrument="BTCUSDT")
    assert bars[0].empty and bars[0].gap is GapState.NONE


def test_candles_out_of_order_or_off_the_hour_are_refused() -> None:
    with pytest.raises(MarketDataError, match="in order"):
        bars_from_klines([row(1), row(0)], venue="binanceusdm", instrument="BTCUSDT")
    shifted = row(0)
    shifted[0] = T0_MS + 60_000
    with pytest.raises(MarketDataError, match="on the hour"):
        bars_from_klines([shifted], venue="binanceusdm", instrument="BTCUSDT")


def test_funding_stamped_a_few_milliseconds_late_is_read_at_its_second() -> None:
    late = DerivPoint(Series.FUNDING, T0 + 2_000_000, 1, Decimal("0.0001"))
    on_time = funding_on_the_second([late])
    assert on_time[0].as_of_ns == T0 and on_time[0].value == Decimal("0.0001")


def test_klines_and_funding_read_back_as_written(tmp_path: Path) -> None:
    bars = bars_from_klines([row(i) for i in range(3)], venue="binanceusdm", instrument="BTCUSDT")
    points = [DerivPoint(Series.FUNDING, T0 + 8 * HOUR_NS, 5, Decimal("0.00012"))]
    manifest = write_klines(tmp_path, bars, points)
    assert manifest["rows"] == 3 and manifest["funding_rows"] == 1
    got = read_klines(tmp_path, "binanceusdm", "BTCUSDT")
    assert got.bars == tuple(bars) and got.points == tuple(points)
    assert set(got.sha256) == {"bars", "funding"}


def test_a_changed_kline_file_is_refused(tmp_path: Path) -> None:
    bars = bars_from_klines([row(0)], venue="binanceusdm", instrument="BTCUSDT")
    write_klines(tmp_path, bars, [])
    data = next(tmp_path.rglob("1h.parquet"))
    data.write_bytes(data.read_bytes() + b"x")
    with pytest.raises(ArchiveError, match="differs from its manifest"):
        read_klines(tmp_path, "binanceusdm", "BTCUSDT")


def test_minutes_are_not_klines() -> None:
    minute = row(0)
    minute[6] = T0_MS + NS_PER_MINUTE // 1_000_000 - 1
    with pytest.raises(MarketDataError, match="one hour"):
        bars_from_klines([minute], venue="binanceusdm", instrument="BTCUSDT")
