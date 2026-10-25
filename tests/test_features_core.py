from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.features.core import (
    Absent,
    Feature,
    FeatureSpec,
    Inputs,
    LookAheadError,
    closes,
    compute,
    last_z,
    window,
)
from trading_research_agents.market_data.bars import FlowBar, GapState, quiet_bar
from trading_research_agents.market_data.days import midnight_ns

HOUR = 3_600_000_000_000
T0 = midnight_ns(date(2025, 7, 1))


def bar(i: int, close: str = "100") -> FlowBar:
    price = Decimal(close)
    return FlowBar(
        venue="v",
        instrument="BTCUSDT",
        start_ns=T0 + i * HOUR,
        end_ns=T0 + (i + 1) * HOUR,
        open=price,
        high=price,
        low=price,
        close=price,
        volume_base=Decimal(1),
        volume_quote=price,
        taker_buy_base=Decimal(1),
        taker_sell_base=Decimal(0),
        trade_count=1,
        large_trade_base=None,
        late_trades=0,
        gap=GapState.NONE,
        source="s",
    )


def gone(i: int, gap: GapState = GapState.MISSING) -> FlowBar:
    start = T0 + i * HOUR
    return quiet_bar("v", "BTCUSDT", start, start + HOUR, source="s", large_declared=False, gap=gap)


def test_a_window_is_the_last_bars_closing_at_the_decision() -> None:
    bars = [bar(i, str(100 + i)) for i in range(5)]
    got = window(bars, T0 + 5 * HOUR, 3)
    assert not isinstance(got, Absent)
    assert [b.start_ns for b in got] == [T0 + i * HOUR for i in (2, 3, 4)]
    assert closes(got) == [102.0, 103.0, 104.0]


def test_a_window_says_why_it_cannot_be_read() -> None:
    bars = [bar(i) for i in range(5)]
    short = window(bars, T0 + 5 * HOUR, 6)
    assert isinstance(short, Absent) and "this needs 6" in short.reason
    late = window(bars[:4], T0 + 5 * HOUR, 2)
    assert isinstance(late, Absent) and "does not close at the decision time" in late.reason
    unseen = window([*bars[:3], gone(3), bars[4]], T0 + 5 * HOUR, 3)
    assert isinstance(unseen, Absent) and "nobody saw" in unseen.reason
    quiet = window([*bars[:3], gone(3, GapState.NONE), bars[4]], T0 + 5 * HOUR, 3)
    assert isinstance(quiet, Absent) and "no trades" in quiet.reason


def test_inputs_refuse_a_bar_closing_after_the_moment() -> None:
    bars = [bar(i) for i in range(3)]
    with pytest.raises(LookAheadError, match="bars"):
        Inputs(at_ns=T0 + 2 * HOUR, interval_ns=HOUR, bars=bars)
    with pytest.raises(LookAheadError, match="ETHUSDT"):
        Inputs(at_ns=T0 + 2 * HOUR, interval_ns=HOUR, bars=bars[:2], context={"ETHUSDT": bars})


def test_compute_names_every_feature() -> None:
    two = Feature(FeatureSpec("two", "x", 1, frozenset()), lambda inputs: 2.0)
    inputs = Inputs(at_ns=T0 + HOUR, interval_ns=HOUR, bars=[bar(0)])
    assert compute([two], inputs) == {"two": 2.0}


def test_last_z_is_measured_in_the_window_own_spread() -> None:
    assert last_z([1.0, 2.0, 3.0]) == pytest.approx(1.0)
    flat = last_z([5.0, 5.0, 5.0])
    assert isinstance(flat, Absent) and "did not vary" in flat.reason
