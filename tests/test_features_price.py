from __future__ import annotations

import math
import random
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.agents.technical_analyst.tools import features as v1_features
from trading_research_agents.execution.regime import RegimeRule, label
from trading_research_agents.features.core import Absent, Inputs
from trading_research_agents.features.price import (
    atr_bps,
    mom_z,
    position_in_range,
    range_vol_bps,
    regime_of,
    ret_bps,
    trend_strength_of,
    variance_ratio_of,
    vol_bps,
    vwap_gap_bps,
    window_move_bps,
    zscore,
)
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.days import midnight_ns
from trading_research_agents.strategy.core import Bar

HOUR = 3_600_000_000_000
T0 = midnight_ns(date(2025, 7, 1))


def bars_of(
    closes: list[Decimal],
    *,
    highs: list[Decimal] | None = None,
    lows: list[Decimal] | None = None,
    volumes: list[Decimal] | None = None,
) -> list[FlowBar]:
    out = []
    for i, close in enumerate(closes):
        volume = volumes[i] if volumes else Decimal(1)
        out.append(
            FlowBar(
                venue="v",
                instrument="BTCUSDT",
                start_ns=T0 + i * HOUR,
                end_ns=T0 + (i + 1) * HOUR,
                open=close,
                high=highs[i] if highs else close,
                low=lows[i] if lows else close,
                close=close,
                volume_base=volume,
                volume_quote=volume * close,
                taker_buy_base=volume,
                taker_sell_base=Decimal(0),
                trade_count=1,
                large_trade_base=None,
                late_trades=0,
                gap=GapState.NONE,
                source="s",
            )
        )
    return out


def inputs_of(bars: list[FlowBar]) -> Inputs:
    return Inputs(at_ns=bars[-1].end_ns, interval_ns=HOUR, bars=bars)


def zigzag(k: float, a: float) -> list[Decimal]:
    """97 closes whose one-bar log returns alternate k + a and k - a."""
    price = 100.0
    out = [Decimal(repr(price))]
    for i in range(1, 97):
        price *= math.exp(k + (a if i % 2 else -a))
        out.append(Decimal(repr(price)))
    return out


def test_returns_are_in_basis_points() -> None:
    bars = bars_of([Decimal(100), Decimal(101)])
    assert ret_bps(1)(inputs_of(bars)) == pytest.approx(100.0)
    short = ret_bps(4)(inputs_of(bars))
    assert isinstance(short, Absent) and "this needs 5" in short.reason


def test_volatility_and_momentum_from_log_returns() -> None:
    k, a = 0.001, 0.002
    inputs = inputs_of(bars_of(zigzag(k, a)))
    spread = a * math.sqrt(96 / 95)
    assert vol_bps(inputs) == pytest.approx(spread * 1e4, rel=1e-9)
    assert mom_z(4)(inputs) == pytest.approx(4 * k / (spread * 2), rel=1e-9)
    assert mom_z(24)(inputs) == pytest.approx(24 * k / (spread * math.sqrt(24)), rel=1e-9)


def test_momentum_without_movement_is_absent() -> None:
    inputs = inputs_of(bars_of([Decimal(100)] * 97))
    flat = mom_z(4)(inputs)
    assert isinstance(flat, Absent) and "did not move" in flat.reason


def test_parkinson_range_volatility() -> None:
    b = 0.004
    closes = [Decimal(100)] * 96
    highs = [Decimal(repr(100 * math.exp(b / 2)))] * 96
    lows = [Decimal(repr(100 * math.exp(-b / 2)))] * 96
    inputs = inputs_of(bars_of(closes, highs=highs, lows=lows))
    assert range_vol_bps(inputs) == pytest.approx(b / (2 * math.sqrt(math.log(2))) * 1e4, rel=1e-6)


def test_atr_is_wilder_over_the_close() -> None:
    bars = bars_of([Decimal(100)] * 96, highs=[Decimal(101)] * 96, lows=[Decimal(99)] * 96)
    assert atr_bps(inputs_of(bars)) == pytest.approx(200.0)


def test_path_figures_are_the_v1_figures() -> None:
    rng = random.Random(11)  # noqa: S311
    price = Decimal("100.00")
    closes, volumes = [], []
    for _ in range(60):
        price += Decimal(rng.choice(("-0.40", "-0.10", "0.20", "0.30", "-0.20", "0.50")))
        closes.append(price)
        volumes.append(Decimal(rng.choice(("0.5", "1.0", "2.5", "4.0"))))
    highs = [c + Decimal("0.35") for c in closes]
    lows = [c - Decimal("0.35") for c in closes]
    ours = bars_of(closes, highs=highs, lows=lows, volumes=volumes)
    theirs = [
        Bar(b.start_ns, b.end_ns, b.open, b.high, b.low, b.close, b.volume_base, b.trade_count)
        for b in ours
    ]
    v1 = v1_features(theirs)
    assert v1 is not None
    inputs = inputs_of(ours)
    assert zscore(inputs) == v1.zscore
    assert vwap_gap_bps(inputs) == v1.vwap_gap_bps
    assert position_in_range(inputs) == v1.position_in_range
    assert window_move_bps(inputs) == v1.window_move_bps
    assert trend_strength_of(inputs) == v1.trend_strength
    assert variance_ratio_of(inputs) == v1.variance_ratio
    assert regime_of(inputs) == label(v1.trend_strength, v1.window_move_bps, RegimeRule())
