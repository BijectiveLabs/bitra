from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError, quiet_bar
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S, midnight_ns
from trading_research_agents.market_data.resample import resample

DAY = midnight_ns(date(2025, 7, 1))
FIVE = 5 * NS_PER_MINUTE


def trades() -> list[Trade]:
    out = []
    for i in range(1, 40):
        out.append(
            Trade(
                ts_ns=DAY + i * 17 * NS_PER_S,
                venue="bybit",
                instrument="BTCUSDT",
                price=Decimal(100 + (i * 7) % 11),
                qty=Decimal("0.25") * (1 + i % 3),
                side=Side.BUY if i % 2 else Side.SELL,
                trade_id=f"t{i}",
            )
        )
    return out


def build(interval_ns: int, until_ns: int) -> list[FlowBar]:
    made = FlowBarBuilder(
        venue="bybit",
        instrument="BTCUSDT",
        source="archive:bybit",
        start_ns=DAY,
        interval_ns=interval_ns,
        large_trade_quote=Decimal("50"),
    )
    bars: list[FlowBar] = []
    for t in trades():
        if t.ts_ns < until_ns:
            bars.extend(made.push(t))
    bars.extend(made.finish(until_ns))
    return bars


def test_resampled_minutes_equal_bars_built_at_five_minutes() -> None:
    minutes = build(NS_PER_MINUTE, DAY + 15 * NS_PER_MINUTE)
    assert resample(minutes, FIVE) == build(FIVE, DAY + 15 * NS_PER_MINUTE)


def test_a_missing_minute_makes_the_coarse_bar_partial() -> None:
    minutes = build(NS_PER_MINUTE, DAY + 5 * NS_PER_MINUTE)
    minutes[4] = quiet_bar(
        "bybit",
        "BTCUSDT",
        minutes[4].start_ns,
        minutes[4].end_ns,
        source="archive:bybit",
        large_declared=True,
        gap=GapState.MISSING,
    )
    (bar,) = resample(minutes, FIVE)
    assert bar.gap is GapState.PARTIAL


def test_a_minute_absent_from_the_input_counts_as_missing() -> None:
    minutes = build(NS_PER_MINUTE, DAY + 5 * NS_PER_MINUTE)
    (bar,) = resample(minutes[:3], FIVE)
    assert bar.gap is GapState.PARTIAL


def test_nothing_seen_is_missing() -> None:
    gone = [
        quiet_bar(
            "bybit",
            "BTCUSDT",
            DAY + i * NS_PER_MINUTE,
            DAY + (i + 1) * NS_PER_MINUTE,
            source="none",
            large_declared=False,
            gap=GapState.MISSING,
        )
        for i in range(5)
    ]
    (bar,) = resample(gone, FIVE)
    assert bar.gap is GapState.MISSING and bar.empty and bar.large_trade_base is None


def test_late_trades_add_up() -> None:
    minutes = build(NS_PER_MINUTE, DAY + 5 * NS_PER_MINUTE)
    minutes[1] = minutes[1].with_late(2)
    (bar,) = resample(minutes, FIVE)
    assert bar.late_trades == 2 and bar.gap is GapState.PARTIAL


def test_input_must_be_one_market_in_order() -> None:
    minutes = build(NS_PER_MINUTE, DAY + 5 * NS_PER_MINUTE)
    with pytest.raises(MarketDataError, match="in order"):
        resample([minutes[1], minutes[0]], FIVE)
    with pytest.raises(MarketDataError, match="one market"):
        resample([minutes[0], replace(minutes[1], instrument="ETHUSDT")], FIVE)
    with pytest.raises(MarketDataError, match="whole number"):
        resample(minutes, 90 * NS_PER_S)
    assert resample([], FIVE) == []
