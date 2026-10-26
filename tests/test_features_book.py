from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.features.book import book_imbalance, depth_ratio_10bps, spread_bps
from trading_research_agents.features.core import Absent, Inputs, LookAheadError
from trading_research_agents.features.derivatives import liq_imbalance_24
from trading_research_agents.market_data.bars import GapState
from trading_research_agents.market_data.book import BookBar, resample_book
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series, detail_of

T0 = midnight_ns(date(2025, 7, 1))
HOUR = 60 * NS_PER_MINUTE
D = Decimal


def minute(i: int, samples: int, spread: float, imbalance: float, bid: str, ask: str) -> BookBar:
    empty = samples == 0
    return BookBar(
        venue="bybit",
        instrument="BTCUSDT",
        start_ns=T0 + i * NS_PER_MINUTE,
        end_ns=T0 + (i + 1) * NS_PER_MINUTE,
        samples=samples,
        spread_bps_mean=None if empty else spread,
        spread_bps_max=None if empty else spread * 2,
        imbalance_top10_mean=None if empty else imbalance,
        depth_bid_10bps_sum_base=None if empty else D(bid),
        depth_ask_10bps_sum_base=None if empty else D(ask),
        mid_close=None if empty else D("100.05") + i,
        gap=GapState.NONE,
    )


def test_book_minutes_resample_weighted_by_their_samples() -> None:
    minutes = [minute(0, 60, 1.0, 0.5, "60", "30"), minute(1, 20, 3.0, -0.5, "40", "20")]
    minutes += [minute(i, 0, 0, 0, "0", "0") for i in range(2, 60)]
    (hour,) = resample_book(minutes, HOUR)
    assert hour.samples == 80 and hour.spread_bps_mean == pytest.approx(1.5)
    assert hour.spread_bps_max == pytest.approx(6.0)
    assert hour.imbalance_top10_mean == pytest.approx(0.25)
    assert hour.depth_bid_10bps_sum_base == D("100") and hour.mid_close == D("101.05")


def test_book_features_read_the_last_closed_book_bar() -> None:
    bars = [minute(0, 60, 1.5, 0.2, "120", "60")]
    inputs = Inputs(at_ns=T0 + NS_PER_MINUTE, interval_ns=NS_PER_MINUTE, bars=[], book=bars)
    assert spread_bps(inputs) == 1.5 and book_imbalance(inputs) == 0.2
    assert depth_ratio_10bps(inputs) == pytest.approx(2.0)


def test_without_a_book_they_say_so() -> None:
    nothing = Inputs(at_ns=T0 + NS_PER_MINUTE, interval_ns=NS_PER_MINUTE, bars=[])
    found = spread_bps(nothing)
    assert isinstance(found, Absent) and "no book was recorded" in found.reason
    quiet = Inputs(
        at_ns=T0 + NS_PER_MINUTE,
        interval_ns=NS_PER_MINUTE,
        bars=[],
        book=[minute(0, 0, 0, 0, "0", "0")],
    )
    unseen = book_imbalance(quiet)
    assert isinstance(unseen, Absent) and "no sample" in unseen.reason
    with pytest.raises(LookAheadError, match="book"):
        Inputs(at_ns=T0, interval_ns=NS_PER_MINUTE, bars=[], book=[minute(0, 1, 1, 0, "1", "1")])


def liquidation(minutes_ago: int, qty: str, liquidated: str) -> DerivPoint:
    at = T0 + 24 * HOUR - minutes_ago * NS_PER_MINUTE
    return DerivPoint(Series.LIQUIDATION, at, at, D(qty), detail_of(liquidated=liquidated))


def test_liquidation_imbalance_by_side() -> None:
    events = [
        liquidation(5, "3", "long"),
        liquidation(30, "1", "short"),
        liquidation(25 * 60, "100", "short"),
    ]
    inputs = Inputs(at_ns=T0 + 24 * HOUR, interval_ns=HOUR, bars=[], events=events)
    assert liq_imbalance_24(inputs) == pytest.approx(0.5)
    unknown = [
        DerivPoint(Series.LIQUIDATION, T0, T0, D(1), detail_of(side="buy")),
    ]
    raw = Inputs(at_ns=T0 + HOUR, interval_ns=HOUR, bars=[], events=unknown)
    found = liq_imbalance_24(raw)
    assert isinstance(found, Absent) and "which side" in found.reason
