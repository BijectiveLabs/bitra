from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.history import (
    MarketHistory,
    MinuteTape,
    Views,
    label_of,
    load_history,
)
from trading_research_agents.evaluation.policy import DecisionView, TradeIdea
from trading_research_agents.features.core import LookAheadError
from trading_research_agents.market_data.archive import SourceFile, write_flow_day
from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, midnight_ns

HOUR = 60 * NS_PER_MINUTE
DAY = date(2025, 7, 1)
T0 = midnight_ns(DAY)


def minutes(first: date, days: int, instrument: str = "BTCUSDT", seed: int = 3) -> list[FlowBar]:
    rng = random.Random(seed)  # noqa: S311
    price = 100.0
    start = midnight_ns(first)
    out = []
    for i in range(days * 1440):
        before = price
        price = max(1.0, price + rng.choice((-0.03, -0.01, 0.01, 0.03)))
        o, c = Decimal(f"{before:.2f}"), Decimal(f"{price:.2f}")
        buy = Decimal("0.600") if c >= o else Decimal("0.400")
        out.append(
            FlowBar(
                venue="bybit",
                instrument=instrument,
                start_ns=start + i * NS_PER_MINUTE,
                end_ns=start + (i + 1) * NS_PER_MINUTE,
                open=o,
                high=max(o, c) + Decimal("0.01"),
                low=min(o, c) - Decimal("0.01"),
                close=c,
                volume_base=Decimal("1.000"),
                volume_quote=Decimal("1.000") * c,
                taker_buy_base=buy,
                taker_sell_base=Decimal("1.000") - buy,
                trade_count=3,
                large_trade_base=None,
                late_trades=0,
                gap=GapState.NONE,
                source="archive:bybit",
            )
        )
    return out


def test_a_tape_reads_minutes_by_their_start() -> None:
    tape = MinuteTape(minutes(DAY, 1))
    span = tape.span(T0, T0 + 3 * NS_PER_MINUTE)
    assert span is not None and [m.start_ns for m in span] == [
        T0 + i * NS_PER_MINUTE for i in range(3)
    ]
    assert tape.span(T0 + DAY_NS - NS_PER_MINUTE, T0 + DAY_NS + NS_PER_MINUTE) is None
    with pytest.raises(MarketDataError, match="minute boundaries"):
        tape.span(T0 + 1, T0 + NS_PER_MINUTE)


def test_coarser_bars_are_those_closed_by_the_moment() -> None:
    history = MarketHistory("BTCUSDT", minutes(DAY, 1), [], [HOUR])
    got = history.closed(HOUR, T0 + 5 * HOUR, 3)
    assert [b.end_ns for b in got] == [T0 + h * HOUR for h in (3, 4, 5)]
    assert history.closed(HOUR, T0 + 5 * HOUR + 1, 3) == got
    after = history.next_return(HOUR, T0 + 5 * HOUR)
    sixth, fifth = history.closed(HOUR, T0 + 6 * HOUR, 2)[1], got[-1]
    assert after == pytest.approx(float(sixth.ohlc()[3] / fifth.ohlc()[3] - 1))
    assert label_of(HOUR) == "1h" and label_of(15 * NS_PER_MINUTE) == "15m"


def test_a_view_refuses_a_bar_from_the_future() -> None:
    history = MarketHistory("BTCUSDT", minutes(DAY, 1), [], [HOUR])
    bars = history.closed(HOUR, T0 + 5 * HOUR, 3)
    with pytest.raises(LookAheadError, match="BTCUSDT"):
        DecisionView("BTCUSDT", T0 + 4 * HOUR, "1h", bars, {"1h": {}})


def test_views_carry_both_timeframes_and_the_context() -> None:
    histories = {
        "BTCUSDT": MarketHistory("BTCUSDT", minutes(DAY, 5), [], [HOUR, 4 * HOUR]),
        "ETHUSDT": MarketHistory("ETHUSDT", minutes(DAY, 5, "ETHUSDT", 5), [], [HOUR, 4 * HOUR]),
    }
    views = Views(histories, primary_ns=HOUR, context_ns=4 * HOUR, context=("BTCUSDT",))
    at = T0 + 4 * DAY_NS + 8 * HOUR
    view = views.view("ETHUSDT", at)
    assert set(view.features) == {"1h", "4h"} and set(view.context) == {"BTCUSDT"}
    assert isinstance(view.feature("mom_z_24"), float)
    assert isinstance(view.feature("beta_btc_96"), float)
    assert views.vector("ETHUSDT", HOUR, at) is views.vector("ETHUSDT", HOUR, at)


def test_an_idea_holds_for_a_bar_at_least_and_its_levels_are_positive() -> None:
    with pytest.raises(MarketDataError, match="at least one bar"):
        TradeIdea(Direction.LONG, "p", None, None, 0, "r")
    with pytest.raises(MarketDataError, match="positive"):
        TradeIdea(Direction.LONG, "p", Decimal(-1), None, 1, "r")


def test_load_history_reads_the_archive_with_its_hashes(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    tape = minutes(DAY, 2)
    for k in range(2):
        write_flow_day(
            tape[k * 1440 : (k + 1) * 1440],
            tmp_path,
            day=DAY + timedelta(days=k),
            price_scale=2,
            qty_scale=3,
            sources=(SourceFile("fixture", None),),
        )
    loaded = load_history(tmp_path, "bybit", "BTCUSDT", DAY, DAY + timedelta(days=2), [HOUR])
    assert len(loaded.history.tape.bars) == 3 * 1440
    assert sum(1 for m in loaded.history.tape.bars if m.gap is GapState.MISSING) == 1440
    assert sorted(loaded.manifests) == [
        "market/bybit/BTCUSDT/flow_1m/2025-07-01.parquet",
        "market/bybit/BTCUSDT/flow_1m/2025-07-02.parquet",
    ]


def test_views_read_the_book_and_the_liquidations_when_recorded() -> None:
    from trading_research_agents.market_data.book import BookBar
    from trading_research_agents.market_data.deriv import DerivPoint, Series, detail_of

    tape = minutes(DAY, 5)
    book = [
        BookBar(
            venue="bybit",
            instrument="BTCUSDT",
            start_ns=m.start_ns,
            end_ns=m.end_ns,
            samples=60,
            spread_bps_mean=1.25,
            spread_bps_max=3.0,
            imbalance_top10_mean=0.1,
            depth_bid_10bps_sum_base=Decimal("120.000"),
            depth_ask_10bps_sum_base=Decimal("60.000"),
            mid_close=Decimal("100.005"),
            gap=GapState.NONE,
        )
        for m in tape
    ]
    at = T0 + 4 * DAY_NS + 8 * HOUR
    liquidations = [
        DerivPoint(
            Series.LIQUIDATION, at - HOUR, at - HOUR, Decimal("2"), detail_of(liquidated="long")
        )
    ]
    history = MarketHistory("BTCUSDT", tape, liquidations, [HOUR, 4 * HOUR], book=book)
    views = Views({"BTCUSDT": history}, primary_ns=HOUR, context_ns=4 * HOUR, context=())
    view = views.view("BTCUSDT", at)
    assert view.feature("spread_bps") == pytest.approx(1.25)
    assert view.feature("depth_ratio_10bps") == pytest.approx(2.0)
    assert view.feature("liq_imbalance_24") == pytest.approx(1.0)
