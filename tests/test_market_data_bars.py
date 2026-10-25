from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.market_data.bars import (
    FlowBar,
    GapState,
    MarketDataError,
    quiet_bar,
    worst,
)
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, day_of, midnight_ns

START = midnight_ns(date(2025, 7, 1))


def traded(**kw: object) -> FlowBar:
    fields: dict[str, object] = {
        "venue": "bybit",
        "instrument": "BTCUSDT",
        "start_ns": START,
        "end_ns": START + NS_PER_MINUTE,
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "close": Decimal("101"),
        "volume_base": Decimal("3"),
        "volume_quote": Decimal("302"),
        "taker_buy_base": Decimal("2"),
        "taker_sell_base": Decimal("1"),
        "trade_count": 3,
        "large_trade_base": None,
        "late_trades": 0,
        "gap": GapState.NONE,
        "source": "archive:bybit",
    }
    fields.update(kw)
    return FlowBar(**fields)  # type: ignore[arg-type]


def test_days_are_exact_in_nanoseconds() -> None:
    assert midnight_ns(date(1970, 1, 2)) == DAY_NS
    assert day_of(midnight_ns(date(2025, 7, 1)) + DAY_NS - 1) == date(2025, 7, 1)
    assert day_of(midnight_ns(date(2025, 7, 1)) + DAY_NS) == date(2025, 7, 2)


def test_a_traded_bar_reads_its_vwap_and_delta() -> None:
    bar = traded()
    assert not bar.empty
    assert bar.vwap == Decimal("302") / Decimal("3")
    assert bar.delta_base == Decimal("1")
    assert bar.ohlc() == (Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"))


def test_taker_sides_must_add_up_to_the_volume() -> None:
    with pytest.raises(MarketDataError, match="do not add up"):
        traded(taker_buy_base=Decimal("2.5"))


def test_prices_must_be_consistent() -> None:
    with pytest.raises(MarketDataError, match="inconsistent"):
        traded(high=Decimal("100.5"))


def test_a_bar_with_trades_cannot_be_missing() -> None:
    with pytest.raises(MarketDataError, match="nobody saw"):
        traded(gap=GapState.MISSING)


def test_an_empty_bar_carries_no_prices() -> None:
    quiet = quiet_bar(
        "bybit", "BTCUSDT", START, START + NS_PER_MINUTE, source="s", large_declared=True
    )
    assert quiet.empty and quiet.vwap is None and quiet.large_trade_base == Decimal(0)
    with pytest.raises(MarketDataError, match="no prices"):
        traded(trade_count=0)
    with pytest.raises(MarketDataError, match="empty bar has no prices"):
        quiet.ohlc()


def test_a_missing_bar_has_no_large_trade_figure() -> None:
    gone = quiet_bar(
        "bybit",
        "BTCUSDT",
        START,
        START + NS_PER_MINUTE,
        source="s",
        large_declared=True,
        gap=GapState.MISSING,
    )
    assert gone.large_trade_base is None and gone.gap is GapState.MISSING


def test_large_trade_volume_lies_inside_the_volume() -> None:
    assert traded(large_trade_base=Decimal("3")).large_trade_base == Decimal("3")
    with pytest.raises(MarketDataError, match="large-trade"):
        traded(large_trade_base=Decimal("3.1"))


def test_late_trades_mark_a_bar_partial_without_changing_it() -> None:
    bar = traded().with_late(2)
    assert bar.late_trades == 2 and bar.gap is GapState.PARTIAL
    assert bar.volume_base == Decimal("3")
    assert traded().with_late(0) == traded()


def test_worst_orders_the_states() -> None:
    assert worst([]) is GapState.NONE
    assert worst([GapState.NONE, GapState.PARTIAL]) is GapState.PARTIAL
    assert worst([GapState.PARTIAL, GapState.MISSING, GapState.NONE]) is GapState.MISSING


def test_a_bar_names_its_market_and_runs_forward() -> None:
    with pytest.raises(MarketDataError, match="names its venue"):
        traded(venue=" ")
    with pytest.raises(MarketDataError, match="ends after it starts"):
        traded(end_ns=START)
