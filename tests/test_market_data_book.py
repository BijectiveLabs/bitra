from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.market_data.bars import GapState, MarketDataError
from trading_research_agents.market_data.book import BookBar, BookBarBuilder, BookSample
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S, midnight_ns

T0 = midnight_ns(date(2025, 7, 1))
D = Decimal


def sample(at_s: int, bid: str, ask: str, bid_qty: str = "2", ask_qty: str = "1") -> BookSample:
    b, a = D(bid), D(ask)
    return BookSample(
        at_ns=T0 + at_s * NS_PER_S,
        bids=((b, D(bid_qty)), (b - D("0.5"), D("4")), (b - D("50"), D("9"))),
        asks=((a, D(ask_qty)), (a + D("0.5"), D("3")), (a + D("50"), D("8"))),
    )


def builder() -> BookBarBuilder:
    return BookBarBuilder(venue="bybit", instrument="BTCUSDT", start_ns=T0)


def test_a_minute_of_samples_becomes_one_bar() -> None:
    made = builder()
    assert made.push(sample(0, "999.5", "1000.5")) == []
    assert made.push(sample(30, "1000", "1001", "1", "3")) == []
    (bar,) = made.push(sample(60, "1001", "1002"))
    assert bar.start_ns == T0 and bar.samples == 2 and bar.gap is GapState.NONE
    first = 1.0 / 1000.0 * 1e4
    second = 1.0 / 1000.5 * 1e4
    assert bar.spread_bps_mean == pytest.approx((first + second) / 2)
    assert bar.spread_bps_max == pytest.approx(first)
    # Ten levels each side are all three here: (15 - 12) / 27, then (14 - 14) / 28.
    assert bar.imbalance_top10_mean == pytest.approx((3 / 27 + 0) / 2)
    # Within 10 bps of the mid: the first two levels each side, the third is 50 away.
    assert bar.depth_bid_10bps_sum_base == D("11") and bar.depth_ask_10bps_sum_base == D("10")
    assert bar.depth_bid_10bps_base == D("5.5") and bar.depth_ask_10bps_base == D("5")
    assert bar.mid_close == D("1000.5")


def test_a_minute_without_samples_is_empty_not_zero() -> None:
    made = builder()
    made.push(sample(0, "999.5", "1000.5"))
    bars = made.push(sample(150, "999.5", "1000.5"))
    assert [b.samples for b in bars] == [1, 0]
    quiet = bars[1]
    assert quiet.spread_bps_mean is None and quiet.mid_close is None
    assert quiet.depth_bid_10bps_base is None
    finished = made.finish(T0 + 4 * NS_PER_MINUTE, gap=GapState.PARTIAL)
    assert [b.gap for b in finished] == [GapState.PARTIAL, GapState.PARTIAL]


def test_a_crossed_book_is_refused() -> None:
    with pytest.raises(MarketDataError, match="crossed"):
        sample(0, "1001", "1000")
    with pytest.raises(MarketDataError, match="both sides"):
        BookSample(at_ns=T0, bids=(), asks=((D(1), D(1)),))
    with pytest.raises(MarketDataError, match="samples"):
        BookBar(
            venue="bybit",
            instrument="BTCUSDT",
            start_ns=T0,
            end_ns=T0 + NS_PER_MINUTE,
            samples=0,
            spread_bps_mean=1.0,
            spread_bps_max=None,
            imbalance_top10_mean=None,
            depth_bid_10bps_sum_base=None,
            depth_ask_10bps_sum_base=None,
            mid_close=None,
            gap=GapState.NONE,
        )


def test_a_sample_for_a_closed_minute_is_counted_and_dropped() -> None:
    made = builder()
    made.push(sample(0, "999.5", "1000.5"))
    made.push(sample(61, "999.5", "1000.5"))
    assert made.push(sample(30, "999.5", "1000.5")) == [] and made.late == 1
