from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.data.schema import Direction
from trading_research_agents.envelope.model import Channel
from trading_research_agents.evaluation.history import MinuteTape
from trading_research_agents.evaluation.policy import TradeIdea
from trading_research_agents.evaluation.settle import (
    Costs,
    Settlement,
    Unsettled,
    funding_schedule,
    settle,
)
from trading_research_agents.execution.cost import CostError, FundingSchedule
from trading_research_agents.market_data.bars import FlowBar, GapState, quiet_bar
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series

T0 = midnight_ns(date(2025, 7, 1))
M = NS_PER_MINUTE
COSTS = Costs(
    Channel(
        venue="bybit",
        name="taker-taker",
        entry_fee_bps=Decimal("5.5"),
        exit_fee_bps=Decimal("5.5"),
    ),
    spread_bps=Decimal("1"),
    slippage_bps=Decimal("0.5"),
)


def minute(i: int, o: str, h: str, lo: str, c: str) -> FlowBar:
    return FlowBar(
        venue="bybit",
        instrument="BTCUSDT",
        start_ns=T0 + i * M,
        end_ns=T0 + (i + 1) * M,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume_base=Decimal(1),
        volume_quote=Decimal(c),
        taker_buy_base=Decimal(1),
        taker_sell_base=Decimal(0),
        trade_count=1,
        large_trade_base=None,
        late_trades=0,
        gap=GapState.NONE,
        source="s",
    )


def still(i: int, gap: GapState = GapState.NONE) -> FlowBar:
    return quiet_bar(
        "bybit", "BTCUSDT", T0 + i * M, T0 + (i + 1) * M, source="s", large_declared=False, gap=gap
    )


def flat(count: int, start: int = 0) -> list[FlowBar]:
    return [minute(start + i, "100", "100.1", "99.9", "100") for i in range(count)]


def idea(
    side: Direction = Direction.LONG, stop: str | None = "30", take: str | None = "50"
) -> TradeIdea:
    return TradeIdea(
        side,
        "test",
        None if stop is None else Decimal(stop),
        None if take is None else Decimal(take),
        3,
        "r",
    )


def run(
    tape: list[FlowBar], made: TradeIdea, funding: object = None, hold: int = 3
) -> Settlement | Unsettled:
    return settle(
        row="row",
        instrument="BTCUSDT",
        idea=made,
        decided_ns=T0,
        hold_ns=hold * M,
        tape=MinuteTape(tape),
        costs=COSTS,
        funding=funding,  # type: ignore[arg-type]
    )


def test_a_take_is_filled_at_its_level_net_of_every_cost() -> None:
    tape = [*flat(1), minute(1, "100", "100.6", "99.9", "100.4"), *flat(5, 2)]
    got = run(tape, idea())
    assert isinstance(got, Settlement) and got.ended == "take"
    assert got.gross_bps == Decimal(50) and got.exit_price == Decimal("100.5")
    assert got.cost.round_trip_bps == Decimal("12.5") and got.net_bps == Decimal("37.5")
    assert got.r_multiple == Decimal("37.5") / Decimal("42.5")
    assert (
        got.exit_ns == T0 + 2 * M
        and got.record()["cost"]["certainty"]["funding"] == "none declared"
    )


def test_a_minute_that_reaches_both_levels_is_the_stop() -> None:
    tape = [*flat(1), minute(1, "100", "100.6", "99.6", "100"), *flat(5, 2)]
    got = run(tape, idea())
    assert isinstance(got, Settlement) and got.ended == "stop" and got.gross_bps == Decimal(-30)


def test_without_a_touch_the_idea_leaves_at_the_time_limit() -> None:
    tape = [*flat(2), minute(2, "100", "100.2", "100", "100.2"), *flat(5, 3)]
    got = run(tape, idea())
    assert isinstance(got, Settlement) and got.ended == "time"
    assert got.gross_bps == Decimal(20) and got.exit_ns == T0 + 3 * M


def test_a_short_takes_below_the_entry() -> None:
    tape = [*flat(1), minute(1, "100", "100.1", "99.4", "99.6"), *flat(5, 2)]
    got = run(tape, idea(Direction.SHORT))
    assert isinstance(got, Settlement) and got.ended == "take" and got.exit_price == Decimal("99.5")


def test_entry_waits_for_the_first_traded_minute() -> None:
    tape = [still(0), minute(1, "101", "101", "101", "101"), *flat(6, 2)]
    got = run(tape, idea(stop=None, take=None))
    assert (
        isinstance(got, Settlement) and got.entry_ns == T0 + M and got.entry_price == Decimal(101)
    )


def test_what_the_archive_cannot_say_is_unsettled() -> None:
    quiet = run([still(i) for i in range(8)], idea())
    assert isinstance(quiet, Unsettled) and "no trade within 5 minutes" in quiet.reason
    unseen = run([*flat(2), still(2, GapState.MISSING), *flat(5, 3)], idea())
    assert isinstance(unseen, Unsettled) and "not seen" in unseen.reason
    short = run(flat(2), idea())
    assert isinstance(short, Unsettled) and "archive ends" in short.reason


def test_funding_is_accrued_over_the_hold() -> None:
    rates = {T0 + k * 60 * NS_PER_S: Decimal("0.0001") for k in (1, 2, 3)}
    schedule = FundingSchedule(interval_s=60, rates=rates)
    got = run(flat(6), idea(stop=None, take=None), funding=schedule)
    assert isinstance(got, Settlement) and got.cost.funding_accrual_bps == Decimal(3)
    assert got.record()["cost"]["certainty"]["funding"] == "measured"


def test_a_settlement_without_a_rate_leaves_the_idea_unsettled() -> None:
    schedule = FundingSchedule(interval_s=60, rates={T0 + 60 * NS_PER_S: Decimal("0.0001")})
    got = run(flat(6), idea(stop=None, take=None), funding=schedule)
    assert isinstance(got, Unsettled) and got.reason.startswith("funding:")
    unknown = run(flat(6), idea(), funding="fewer than two funding settlements")
    assert isinstance(unknown, Unsettled) and "fewer than two" in unknown.reason


def test_the_schedule_is_read_from_the_archive() -> None:
    hour = 3600 * NS_PER_S
    points = [
        DerivPoint(Series.FUNDING, T0 + k * 8 * hour, 0, Decimal("0.0001")) for k in (1, 2, 4)
    ]
    schedule = funding_schedule(points)
    assert isinstance(schedule, FundingSchedule) and schedule.interval_s == 8 * 3600
    assert schedule.settlements_spanned(T0 + 8 * hour, T0 + 16 * hour) == (T0 + 16 * hour,)
    assert isinstance(funding_schedule(points[:1]), str)


def test_declared_costs_are_not_negative() -> None:
    with pytest.raises(CostError, match="spread_bps"):
        Costs(COSTS.channel, spread_bps=Decimal(-1), slippage_bps=Decimal(0))
