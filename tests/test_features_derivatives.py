from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.features.core import Absent, Inputs
from trading_research_agents.features.derivatives import (
    basis_bps,
    funding_rate,
    funding_z_30d,
    long_short_ratio,
    oi_change_pct,
    oi_price_state,
)
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.days import midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series, Timeline

HOUR = 3_600_000_000_000
T0 = midnight_ns(date(2025, 7, 10))
AT = T0 + 5 * HOUR


def bars(closes: list[str]) -> list[FlowBar]:
    start = AT - len(closes) * HOUR
    out = []
    for i, text in enumerate(closes):
        price = Decimal(text)
        out.append(
            FlowBar(
                venue="v",
                instrument="BTCUSDT",
                start_ns=start + i * HOUR,
                end_ns=start + (i + 1) * HOUR,
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
        )
    return out


def line(series: Series, points: list[tuple[int, str]]) -> Timeline:
    return Timeline(series, [DerivPoint(series, at, at, Decimal(v)) for at, v in points])


def inputs_of(deriv: dict[Series, Timeline], closes: list[str] | None = None) -> Inputs:
    return Inputs(at_ns=AT, interval_ns=HOUR, bars=bars(closes or ["100"] * 5), deriv=deriv)


def test_funding_is_the_last_settled_rate_and_goes_stale() -> None:
    funding = line(Series.FUNDING, [(AT - HOUR, "0.0001")])
    assert funding_rate(inputs_of({Series.FUNDING: funding})) == Decimal("0.0001")
    old = line(Series.FUNDING, [(AT - 10 * HOUR, "0.0001")])
    stale = funding_rate(inputs_of({Series.FUNDING: old}))
    assert isinstance(stale, Absent) and "stale" in stale.reason
    none = funding_rate(inputs_of({}))
    assert isinstance(none, Absent) and "no funding history" in none.reason


def test_funding_z_reads_the_last_thirty_days() -> None:
    rates = [f"0.000{i % 4 + 1}" for i in range(12)]
    points = [(AT - (11 - i) * 8 * HOUR, rate) for i, rate in enumerate(rates)]
    values = [float(Decimal(r)) for r in rates]
    expected = (values[-1] - statistics.mean(values)) / statistics.stdev(values)
    found = funding_z_30d(inputs_of({Series.FUNDING: line(Series.FUNDING, points)}))
    assert found == pytest.approx(expected)
    few = funding_z_30d(inputs_of({Series.FUNDING: line(Series.FUNDING, points[-5:])}))
    assert isinstance(few, Absent) and "this needs 10" in few.reason


def test_basis_is_mark_over_index() -> None:
    deriv = {
        Series.MARK: line(Series.MARK, [(AT, "101")]),
        Series.INDEX: line(Series.INDEX, [(AT, "100")]),
    }
    assert basis_bps(inputs_of(deriv)) == pytest.approx(100.0)


def test_open_interest_change_and_state() -> None:
    rising = line(Series.OPEN_INTEREST, [(AT - 4 * HOUR, "100"), (AT, "110")])
    falling = line(Series.OPEN_INTEREST, [(AT - 4 * HOUR, "100"), (AT, "90")])
    still = line(Series.OPEN_INTEREST, [(AT - 4 * HOUR, "100"), (AT, "100.1")])
    up, down = ["100", "101", "102", "103", "104"], ["104", "103", "102", "101", "100"]
    assert oi_change_pct(4)(inputs_of({Series.OPEN_INTEREST: rising})) == pytest.approx(10.0)
    assert oi_price_state(inputs_of({Series.OPEN_INTEREST: rising}, up)) == "new_longs"
    assert oi_price_state(inputs_of({Series.OPEN_INTEREST: rising}, down)) == "new_shorts"
    assert oi_price_state(inputs_of({Series.OPEN_INTEREST: falling}, up)) == "short_covering"
    assert oi_price_state(inputs_of({Series.OPEN_INTEREST: falling}, down)) == "long_liquidation"
    assert oi_price_state(inputs_of({Series.OPEN_INTEREST: still}, up)) == "flat"


def test_long_short_ratio_is_read_as_published() -> None:
    ratio = line(Series.LONG_SHORT, [(AT - HOUR, "1.87")])
    assert long_short_ratio(inputs_of({Series.LONG_SHORT: ratio})) == Decimal("1.87")
