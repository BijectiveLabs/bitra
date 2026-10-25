from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S, midnight_ns

DAY = midnight_ns(date(2025, 7, 1))


def trade(at_ns: int, price: str, qty: str, side: Side, ident: str) -> Trade:
    return Trade(
        ts_ns=at_ns,
        venue="bybit",
        instrument="BTCUSDT",
        price=Decimal(price),
        qty=Decimal(qty),
        side=side,
        trade_id=ident,
    )


def builder(**kw: object) -> FlowBarBuilder:
    fields: dict[str, object] = {
        "venue": "bybit",
        "instrument": "BTCUSDT",
        "source": "archive:bybit",
        "start_ns": DAY,
    }
    fields.update(kw)
    return FlowBarBuilder(**fields)  # type: ignore[arg-type]


def build_all(made: FlowBarBuilder, trades: list[Trade], until_ns: int) -> list[FlowBar]:
    bars: list[FlowBar] = []
    for t in trades:
        bars.extend(made.push(t))
    bars.extend(made.finish(until_ns))
    return bars


def test_trades_become_minute_bars_with_quiet_minutes_between() -> None:
    trades = [
        trade(DAY + 1 * NS_PER_S, "100", "1", Side.BUY, "a"),
        trade(DAY + 30 * NS_PER_S, "102", "2", Side.SELL, "b"),
        trade(DAY + 2 * NS_PER_MINUTE + 5, "101", "0.5", Side.BUY, "c"),
    ]
    bars = build_all(builder(), trades, DAY + 4 * NS_PER_MINUTE)
    assert [b.start_ns for b in bars] == [DAY + i * NS_PER_MINUTE for i in range(4)]
    first = bars[0]
    assert first.ohlc() == (Decimal("100"), Decimal("102"), Decimal("100"), Decimal("102"))
    assert first.taker_buy_base == Decimal("1") and first.taker_sell_base == Decimal("2")
    assert first.volume_quote == Decimal("304") and first.trade_count == 2
    assert bars[1].empty and bars[1].gap is GapState.NONE
    assert bars[2].close == Decimal("101") and bars[3].empty


def test_a_trade_on_the_boundary_opens_the_next_bar() -> None:
    trades = [
        trade(DAY + NS_PER_MINUTE - 1, "100", "1", Side.BUY, "a"),
        trade(DAY + NS_PER_MINUTE, "105", "1", Side.BUY, "b"),
    ]
    bars = build_all(builder(), trades, DAY + 2 * NS_PER_MINUTE)
    assert bars[0].close == Decimal("100") and bars[0].trade_count == 1
    assert bars[1].open == Decimal("105") and bars[1].trade_count == 1


def test_a_duplicate_trade_is_dropped_and_counted() -> None:
    made = builder()
    t = trade(DAY + 1, "100", "1", Side.BUY, "same")
    bars = build_all(made, [t, t], DAY + NS_PER_MINUTE)
    assert bars[0].trade_count == 1 and made.duplicates == 1


def test_large_trades_are_counted_by_their_notional() -> None:
    trades = [
        trade(DAY + 1, "100", "10", Side.BUY, "big"),
        trade(DAY + 2, "100", "1", Side.SELL, "small"),
    ]
    bars = build_all(builder(large_trade_quote=Decimal("1000")), trades, DAY + NS_PER_MINUTE)
    assert bars[0].large_trade_base == Decimal("10")
    assert build_all(builder(), trades, DAY + NS_PER_MINUTE)[0].large_trade_base is None


def test_reordering_inside_the_grace_builds_the_archive_bars() -> None:
    step = 250_000_000
    trades = [
        trade(DAY + i * step, str(100 + i % 7), "0.1", Side.BUY if i % 3 else Side.SELL, f"t{i}")
        for i in range(1, 1200)
    ]
    archive = build_all(builder(), trades, DAY + 6 * NS_PER_MINUTE)
    shuffled: list[Trade] = []
    rng = random.Random(7)  # noqa: S311
    for chunk in range(0, len(trades), 4):
        window = trades[chunk : chunk + 4]
        rng.shuffle(window)
        shuffled.extend(window)
    stream = build_all(builder(grace_ns=2 * NS_PER_S), shuffled, DAY + 6 * NS_PER_MINUTE)
    assert stream == archive


def test_a_trade_after_its_bar_closed_is_late_and_the_bar_is_not_rewritten() -> None:
    made = builder()
    closed = made.push(trade(DAY + 1, "100", "1", Side.BUY, "a"))
    closed += made.push(trade(DAY + NS_PER_MINUTE + 1, "101", "1", Side.BUY, "b"))
    assert len(closed) == 1 and closed[0].trade_count == 1
    assert made.push(trade(DAY + 2, "99", "1", Side.SELL, "late")) == []
    assert made.take_late() == {DAY: 1}
    assert made.take_late() == {}
    assert closed[0].with_late(1).gap is GapState.PARTIAL


def test_finish_refuses_to_drop_trades_beyond_the_end() -> None:
    made = builder()
    made.push(trade(DAY + 2 * NS_PER_MINUTE, "100", "1", Side.BUY, "a"))
    with pytest.raises(MarketDataError, match="at or after the end"):
        made.finish(DAY + NS_PER_MINUTE)


def test_finish_marks_bars_with_the_gap_it_is_given() -> None:
    made = builder()
    made.push(trade(DAY + 1, "100", "1", Side.BUY, "a"))
    bars = made.finish(DAY + 2 * NS_PER_MINUTE, gap=GapState.PARTIAL)
    assert [b.gap for b in bars] == [GapState.PARTIAL, GapState.PARTIAL]


def test_advance_closes_bars_by_time_without_a_trade() -> None:
    made = builder(grace_ns=2 * NS_PER_S)
    made.push(trade(DAY + 1, "100", "1", Side.BUY, "a"))
    assert made.advance(DAY + NS_PER_MINUTE + NS_PER_S) == []
    closed = made.advance(DAY + NS_PER_MINUTE + 2 * NS_PER_S)
    assert [b.start_ns for b in closed] == [DAY]


def test_a_builder_refuses_another_market_and_a_misaligned_start() -> None:
    other = Trade(
        ts_ns=DAY + 1,
        venue="okx",
        instrument="BTCUSDT",
        price=Decimal("1"),
        qty=Decimal("1"),
        side=Side.BUY,
        trade_id="x",
    )
    with pytest.raises(MarketDataError, match="pushed into"):
        builder().push(other)
    with pytest.raises(MarketDataError, match="interval boundary"):
        builder(start_ns=DAY + 1)
    with pytest.raises(MarketDataError, match="positive quote"):
        builder(large_trade_quote=Decimal(0))
