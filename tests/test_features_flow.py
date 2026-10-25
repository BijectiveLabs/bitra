from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.features.core import Absent, Inputs
from trading_research_agents.features.flow import (
    delta_z_24,
    large_trade_share_24,
    taker_imbalance,
    trade_intensity_z_24,
)
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.days import midnight_ns

HOUR = 3_600_000_000_000
T0 = midnight_ns(date(2025, 7, 1))


def flow_bar(i: int, buy: str, sell: str, trades: int, large: str | None = None) -> FlowBar:
    volume = Decimal(buy) + Decimal(sell)
    return FlowBar(
        venue="v",
        instrument="BTCUSDT",
        start_ns=T0 + i * HOUR,
        end_ns=T0 + (i + 1) * HOUR,
        open=Decimal(100),
        high=Decimal(100),
        low=Decimal(100),
        close=Decimal(100),
        volume_base=volume,
        volume_quote=volume * 100,
        taker_buy_base=Decimal(buy),
        taker_sell_base=Decimal(sell),
        trade_count=trades,
        large_trade_base=None if large is None else Decimal(large),
        late_trades=0,
        gap=GapState.NONE,
        source="s",
    )


def day(large: str | None = "0.5") -> list[FlowBar]:
    return [flow_bar(i, str(1 + i % 3), str(2 - i % 2), 10 + i, large) for i in range(24)]


def inputs_of(bars: list[FlowBar]) -> Inputs:
    return Inputs(at_ns=bars[-1].end_ns, interval_ns=HOUR, bars=bars)


def test_taker_imbalance_is_net_taking_over_volume() -> None:
    bars = day()
    last = bars[-4:]
    buy = sum(b.taker_buy_base for b in last)
    sell = sum(b.taker_sell_base for b in last)
    expected = float((buy - sell) / (buy + sell))
    assert taker_imbalance(4)(inputs_of(bars)) == pytest.approx(expected)


def test_delta_and_intensity_are_z_scores_of_the_last_bar() -> None:
    bars = day()
    deltas = [float(b.delta_base) for b in bars]
    counts = [float(b.trade_count) for b in bars]
    z_delta = (deltas[-1] - statistics.mean(deltas)) / statistics.stdev(deltas)
    z_count = (counts[-1] - statistics.mean(counts)) / statistics.stdev(counts)
    assert delta_z_24(inputs_of(bars)) == pytest.approx(z_delta)
    assert trade_intensity_z_24(inputs_of(bars)) == pytest.approx(z_count)


def test_large_trade_share_needs_a_declared_threshold() -> None:
    bars = day()
    volume = sum(b.volume_base for b in bars)
    assert large_trade_share_24(inputs_of(bars)) == pytest.approx(float(Decimal(12) / volume))
    undeclared = large_trade_share_24(inputs_of(day(large=None)))
    assert isinstance(undeclared, Absent) and "threshold" in undeclared.reason
