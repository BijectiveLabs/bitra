from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.data import Side
from trading_research_agents.envelope import Channel, Envelope
from trading_research_agents.execution import (
    BookError,
    CostError,
    Direction,
    Fill,
    FillError,
    FillMode,
    FundingSchedule,
    IntendedOrder,
    Level,
    Print,
    Snapshot,
    effective_cost,
    entry_after_latency,
    funding_accrual_bps,
    half_spread_bps,
    markout_bps,
    markout_table,
    pair_leg_fee_bps,
    passive_fill,
    realised_cost,
    spread_bps,
    walk_the_book,
)

S = 1_000_000_000
EIGHT_HOURS = 8 * 3600
SETTLEMENT = EIGHT_HOURS * S  # the first settlement after the epoch, in ns


def bybit_taker(envelope: Envelope) -> Channel:
    return next(c for c in envelope.channels if c.key == "bybit/taker-taker")


# Funding: signed, and both directions pinned.


def test_funding_is_zero_when_no_settlement_is_spanned() -> None:
    schedule = FundingSchedule(interval_s=EIGHT_HOURS, rates={})
    assert funding_accrual_bps(Direction.LONG, schedule, 0, 100 * S) == 0


def test_long_pays_and_short_receives_the_same_settlement() -> None:
    schedule = FundingSchedule(interval_s=EIGHT_HOURS, rates={SETTLEMENT: Decimal("0.0001")})
    entry, exit_ = SETTLEMENT - 1000 * S, SETTLEMENT + 1200 * S
    assert funding_accrual_bps(Direction.LONG, schedule, entry, exit_) == Decimal("1.0000")
    assert funding_accrual_bps(Direction.SHORT, schedule, entry, exit_) == Decimal("-1.0000")


def test_funding_sums_every_spanned_settlement() -> None:
    schedule = FundingSchedule(
        interval_s=EIGHT_HOURS,
        rates={SETTLEMENT: Decimal("0.0001"), SETTLEMENT + EIGHT_HOURS * S: Decimal("-0.0002")},
    )
    out = funding_accrual_bps(
        Direction.LONG, schedule, SETTLEMENT - 1000 * S, SETTLEMENT + EIGHT_HOURS * S + 100 * S
    )
    assert out == Decimal("-1.0000")


def test_a_position_opened_exactly_at_settlement_was_not_open_for_it() -> None:
    schedule = FundingSchedule(interval_s=EIGHT_HOURS, rates={})
    assert schedule.settlements_spanned(SETTLEMENT, SETTLEMENT + 10 * S) == ()
    assert schedule.settlements_spanned(SETTLEMENT - 1, SETTLEMENT) == (SETTLEMENT,)


def test_a_spanned_settlement_without_a_rate_is_refused_not_zeroed() -> None:
    schedule = FundingSchedule(interval_s=EIGHT_HOURS, rates={})
    with pytest.raises(CostError, match="missing rate is not zero"):
        funding_accrual_bps(Direction.LONG, schedule, SETTLEMENT - S, SETTLEMENT + S)
    with pytest.raises(CostError, match="not a finite Decimal"):
        FundingSchedule(interval_s=EIGHT_HOURS, rates={SETTLEMENT: Decimal("NaN")})
    with pytest.raises(CostError, match="interval must be positive"):
        FundingSchedule(interval_s=0, rates={})
    with pytest.raises(CostError, match="exit is before entry"):
        schedule.settlements_spanned(10, 0)


# Fees and the identity.


def test_the_hedged_pair_transacts_more_than_two_legs() -> None:
    assert pair_leg_fee_bps(Decimal("5.5"), Decimal("1.4")) == Decimal("13.20")
    assert pair_leg_fee_bps(Decimal("5.5"), Decimal("0")) == Decimal("5.5")
    with pytest.raises(CostError, match="non-negative"):
        pair_leg_fee_bps(Decimal("5.5"), Decimal("-1"))


def test_realised_cost_keeps_the_four_terms_apart(envelope: Envelope) -> None:
    schedule = FundingSchedule(interval_s=EIGHT_HOURS, rates={SETTLEMENT: Decimal("0.0001")})
    breakdown = realised_cost(
        channel=bybit_taker(envelope),
        direction=Direction.LONG,
        entry_ts_ns=SETTLEMENT - S,
        exit_ts_ns=SETTLEMENT + S,
        spread_cost_bps=Decimal("3.0"),
        slippage_bps=Decimal("1.5"),
        funding=schedule,
    )
    assert breakdown.fee_round_trip_bps == Decimal("11.0")
    assert breakdown.spread_cost_bps == Decimal("3.0")
    assert breakdown.slippage_bps == Decimal("1.5")
    assert breakdown.funding_accrual_bps == Decimal("1.0000")
    assert breakdown.round_trip_bps == Decimal("16.5000")

    hedged = realised_cost(
        channel=bybit_taker(envelope),
        direction=Direction.SHORT,
        entry_ts_ns=0,
        exit_ts_ns=S,
        spread_cost_bps=Decimal("3.0"),
        slippage_bps=Decimal("0"),
        funding=FundingSchedule(interval_s=EIGHT_HOURS, rates={}),
        hedge_beta=Decimal("1.4"),
    )
    assert hedged.fee_round_trip_bps == Decimal("26.40")


def test_realised_cost_refuses_a_negative_or_missing_measured_term(envelope: Envelope) -> None:
    schedule = FundingSchedule(interval_s=EIGHT_HOURS, rates={})
    with pytest.raises(CostError, match="cannot be negative"):
        realised_cost(
            channel=bybit_taker(envelope),
            direction=Direction.LONG,
            entry_ts_ns=0,
            exit_ts_ns=S,
            spread_cost_bps=Decimal("3.0"),
            slippage_bps=Decimal("-1"),
            funding=schedule,
        )
    with pytest.raises(CostError, match="finite Decimal"):
        realised_cost(
            channel=bybit_taker(envelope),
            direction=Direction.LONG,
            entry_ts_ns=0,
            exit_ts_ns=S,
            spread_cost_bps=3.0,  # type: ignore[arg-type]
            slippage_bps=Decimal("0"),
            funding=schedule,
        )


# The book.


def test_spread_is_relative_to_mid_and_a_crossed_book_is_refused() -> None:
    assert spread_bps(Decimal("99"), Decimal("101")) == Decimal("200")
    assert half_spread_bps(Decimal("99"), Decimal("101")) == Decimal("100")
    assert spread_bps(Decimal("100"), Decimal("100")) == 0
    with pytest.raises(BookError, match="crossed"):
        spread_bps(Decimal("101"), Decimal("99"))
    with pytest.raises(BookError, match="positive finite"):
        spread_bps(Decimal("0"), Decimal("1"))


def test_walk_the_book_prices_against_depth_and_refuses_to_extrapolate() -> None:
    asks = (Level(Decimal("100"), Decimal("1")), Level(Decimal("101"), Decimal("2")))
    out = walk_the_book(asks, Decimal("2"), Side.BUY, mid=Decimal("99.5"))
    assert out.vwap == Decimal("100.5")
    assert out.levels_consumed == 2
    # (100.5 - 99.5) / 99.5 * 1e4 = 100.5025... bps, positive: paying above mid is a cost.
    assert out.slippage_bps.quantize(Decimal("0.01")) == Decimal("100.50")

    bids = (Level(Decimal("99"), Decimal("1")), Level(Decimal("98"), Decimal("1")))
    sell = walk_the_book(bids, Decimal("2"), Side.SELL, mid=Decimal("99.5"))
    assert sell.vwap == Decimal("98.5")
    assert sell.slippage_bps > 0

    with pytest.raises(BookError, match="insufficient depth"):
        walk_the_book(asks, Decimal("4"), Side.BUY, mid=Decimal("99.5"))
    with pytest.raises(BookError, match="not ordered"):
        walk_the_book(asks[::-1], Decimal("1"), Side.BUY, mid=Decimal("99.5"))
    with pytest.raises(BookError, match="empty"):
        walk_the_book((), Decimal("1"), Side.BUY, mid=Decimal("99.5"))
    with pytest.raises(BookError, match="size must be"):
        walk_the_book(asks, Decimal("0"), Side.BUY, mid=Decimal("99.5"))


def test_snapshot_refuses_stale_reads_and_crossed_books() -> None:
    snap = Snapshot(
        ts_ns=10 * S,
        bids=(Level(Decimal("99"), Decimal("5")),),
        asks=(Level(Decimal("101"), Decimal("5")),),
    )
    assert snap.mid == Decimal("100")
    assert snap.half_spread_bps == Decimal("100")
    out = snap.walk(Decimal("1"), Side.BUY, at_ts_ns=11 * S, max_age_ns=2 * S)
    assert out.vwap == Decimal("101")
    with pytest.raises(BookError, match="stale"):
        snap.walk(Decimal("1"), Side.BUY, at_ts_ns=15 * S, max_age_ns=2 * S)
    with pytest.raises(BookError, match="before the snapshot"):
        snap.walk(Decimal("1"), Side.BUY, at_ts_ns=9 * S, max_age_ns=2 * S)
    with pytest.raises(BookError, match="crossed"):
        Snapshot(
            ts_ns=0,
            bids=(Level(Decimal("102"), Decimal("1")),),
            asks=(Level(Decimal("101"), Decimal("1")),),
        )


# Fills.


def prints(*rows: tuple[int, str]) -> list[Print]:
    return [Print(ts * S, Decimal(px)) for ts, px in rows]


def test_a_sweep_through_the_level_is_a_fill_even_without_a_print_at_it() -> None:
    # The old matcher counted only prints exactly at the resting level and missed this sweep.
    series = prints((1, "100.5"), (2, "99.0"))
    assert (
        passive_fill(series, limit=Decimal("100"), side=Side.BUY, t0_ns=0, budget_ns=5 * S) == 2 * S
    )
    assert (
        passive_fill(
            series,
            limit=Decimal("100"),
            side=Side.BUY,
            t0_ns=0,
            budget_ns=5 * S,
            mode=FillMode.THROUGH,
        )
        == 2 * S
    )


def test_a_print_exactly_at_the_level_counts_only_for_the_upper_bound() -> None:
    series = prints((1, "100"), (3, "100.7"))
    assert passive_fill(series, limit=Decimal("100"), side=Side.BUY, t0_ns=0, budget_ns=5 * S) == S
    assert (
        passive_fill(
            series,
            limit=Decimal("100"),
            side=Side.BUY,
            t0_ns=0,
            budget_ns=5 * S,
            mode=FillMode.THROUGH,
        )
        is None
    )
    sell = prints((1, "100"), (2, "100.2"))
    assert passive_fill(sell, limit=Decimal("100"), side=Side.SELL, t0_ns=0, budget_ns=5 * S) == S
    assert (
        passive_fill(
            sell,
            limit=Decimal("100"),
            side=Side.SELL,
            t0_ns=0,
            budget_ns=5 * S,
            mode=FillMode.THROUGH,
        )
        == 2 * S
    )


def test_prints_before_placement_and_after_the_budget_do_not_fill() -> None:
    series = prints((0, "99"), (10, "99"))
    assert (
        passive_fill(series, limit=Decimal("100"), side=Side.BUY, t0_ns=1 * S, budget_ns=5 * S)
        is None
    )
    with pytest.raises(FillError, match="time order"):
        passive_fill(series[::-1], limit=Decimal("100"), side=Side.BUY, t0_ns=0, budget_ns=S)
    with pytest.raises(FillError, match="budget must be positive"):
        passive_fill(series, limit=Decimal("100"), side=Side.BUY, t0_ns=0, budget_ns=0)


def test_entry_fills_at_the_tick_after_the_latency_not_the_trigger_tick() -> None:
    series = prints((0, "100"), (1, "100.1"), (2, "100.2"))
    fill = entry_after_latency(series, signal_ts_ns=0, latency_ns=int(0.5 * S))
    assert fill.ts_ns == S and fill.price == Decimal("100.1")
    with pytest.raises(FillError, match="does not cover"):
        entry_after_latency(series, signal_ts_ns=2 * S, latency_ns=S)


def test_effective_cost_blends_by_the_measured_fill_rate_and_charges_drift() -> None:
    series = prints((0, "100"), (1, "99"), (10, "100"), (11, "102"), (20, "100"))
    orders = [
        IntendedOrder(0, Side.BUY, Decimal("99.5")),  # fills at t=1
        IntendedOrder(10 * S, Side.BUY, Decimal("99.5")),  # never fills; price drifts to 102
    ]
    out = effective_cost(
        orders, series, budget_ns=5 * S, c_passive_bps=Decimal("4.0"), c_cross_bps=Decimal("15.0")
    )
    assert out.n_filled == 1 and out.n_unfilled == 1
    assert out.p_fill == Decimal("0.5")
    assert out.drift_unfilled_bps == Decimal("200")  # (102 - 100) / 100 * 1e4, adverse for a buyer
    assert out.c_effective_bps == Decimal("0.5") * 4 + Decimal("0.5") * (15 + 200)

    with pytest.raises(FillError, match="not computable"):
        effective_cost(
            [IntendedOrder(30 * S, Side.BUY, Decimal("99.5"))],
            series,
            budget_ns=5 * S,
            c_passive_bps=Decimal("4.0"),
            c_cross_bps=Decimal("15.0"),
        )
    with pytest.raises(FillError, match="no intended orders"):
        effective_cost(
            [], series, budget_ns=S, c_passive_bps=Decimal("4"), c_cross_bps=Decimal("15")
        )


# Markout.


def test_markout_is_signed_so_that_adverse_is_negative() -> None:
    assert markout_bps(Decimal("100"), Side.BUY, Decimal("99.9")) == Decimal("-10")
    assert markout_bps(Decimal("100"), Side.SELL, Decimal("99.9")) == Decimal("10")
    with pytest.raises(FillError, match="positive finite"):
        markout_bps(Decimal("0"), Side.BUY, Decimal("1"))


def test_markout_table_sits_beside_the_half_spread_it_is_measured_against() -> None:
    fills = [Fill(0, Decimal("100"), Side.BUY), Fill(10 * S, Decimal("100"), Side.BUY)]
    mids = prints((0, "100"), (60, "99.9947"), (70, "99.9947"))
    rows = markout_table(fills, mids, horizons_ns=[60 * S], half_spread_bps=Decimal("0.14"))
    assert rows[0].n == 2 and rows[0].markout_bps == Decimal("-0.53")
    assert rows[0].half_spread_bps == Decimal("0.14")
    assert not rows[0].survives  # the market takes back about four times the spread earned
    with pytest.raises(FillError, match="no mid on record"):
        markout_table(fills, mids, horizons_ns=[3600 * S], half_spread_bps=Decimal("0.14"))
    with pytest.raises(FillError, match="horizons must be positive"):
        markout_table(fills, mids, horizons_ns=[0], half_spread_bps=Decimal("0.14"))


def test_a_hand_refuses_an_intent_about_another_instrument() -> None:
    """A desk that watches several instruments builds a hand per instrument; the guard is here."""
    from trading_research_agents.trading.dispatch import hand_for

    sent: list[dict[str, Any]] = []

    class Client:
        def create_order(self, **kwargs: object) -> dict[str, Any]:
            sent.append(dict(kwargs))
            return {"id": "1", "status": "closed", "filled": 0.01, "average": 4600.0}

    hand = hand_for(
        venue="somevenue",
        symbol="ETH/USDT:USDT",
        instrument="ETHUSDT",
        max_notional=Decimal("200"),
        environment="mainnet",
        env={"SOMEVENUE_API_KEY": "k", "SOMEVENUE_API_SECRET": "s"},
        client=Client(),
    )
    wrong = hand({"instrument": "BTCUSDT", "target": "long", "size": "0.01", "mid": "60000"})
    assert not wrong["sent"] and "the intent is about BTCUSDT" in wrong["refused"]
    assert not sent


def test_a_hand_reaches_the_environment_it_was_told_and_refuses_to_guess() -> None:
    """Demo and testnet are two places with two accounts; a key for one is rejected by the other."""
    from trading_research_agents.trading.dispatch import DispatchError, VenueHand, hand_for

    class Client:
        def __init__(self) -> None:
            self.demo = False
            self.sandbox = False

        def enable_demo_trading(self, on: bool) -> None:
            self.demo = on

        def set_sandbox_mode(self, on: bool) -> None:
            self.sandbox = on

    def built(environment: str, client: object) -> object:
        hand = hand_for(
            venue="somevenue",
            symbol="ETH/USDT:USDT",
            instrument="ETHUSDT",
            max_notional=Decimal("200"),
            environment=environment,
            env={"SOMEVENUE_API_KEY": "k", "SOMEVENUE_API_SECRET": "s"},
            client=client,
        )
        assert isinstance(hand, VenueHand)
        hand.to_environment(client)
        return client

    demo = Client()
    built("demo", demo)
    assert demo.demo and not demo.sandbox

    testnet = Client()
    built("testnet", testnet)
    assert testnet.sandbox and not testnet.demo

    live = Client()
    built("mainnet", live)
    assert not live.demo and not live.sandbox

    class OnlySandbox:
        def set_sandbox_mode(self, on: bool) -> None:
            pass

    # A client with no way into the environment asked for is refused, never sent somewhere close.
    with pytest.raises(DispatchError, match="has no demo environment"):
        built("demo", OnlySandbox())
    with pytest.raises(DispatchError, match="is not one of"):
        built("paper", Client())


def test_a_cancel_the_venue_refuses_is_recorded_as_what_it_is() -> None:
    """A note that says "cancelled" beside a flag that says otherwise is the worst of both."""
    from trading_research_agents.trading.dispatch import hand_for

    class Client:
        has = {"createOrderWithTakeProfitAndStopLoss": True}

        def __init__(self, state: str) -> None:
            self.state = state

        def create_order(self, *args: object, **kwargs: object) -> dict[str, Any]:
            return {"id": "1", "status": "open"}

        def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            return {"status": self.state, "filled": 0}

        def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            raise RuntimeError("order not exists or too late to cancel")

    def send(state: str) -> dict[str, Any]:
        hand = hand_for(
            venue="somevenue",
            symbol="ETH/USDT:USDT",
            instrument="ETHUSDT",
            max_notional=Decimal("200"),
            role="maker",
            environment="mainnet",
            limit_timeout_s=0.01,
            env={"SOMEVENUE_API_KEY": "k", "SOMEVENUE_API_SECRET": "s"},
            client=Client(state),
        )
        return dict(
            hand(
                {
                    "instrument": "ETHUSDT",
                    "target": "long",
                    "size": "0.01",
                    "mid": "2700",
                    "bid": "2699",
                    "ask": "2701",
                    "stop_price": "2650",
                    "take_price": "2800",
                }
            )
        )

    # The venue had already ended it: ordinary for a post-only limit, seen while waiting, and said
    # as such - nothing to cancel, and nothing held from it.
    gone = send("canceled")
    assert gone["sent"] and not gone["cancelled"] and gone["status"] == "canceled"
    assert "ended the order itself (canceled) with 0 filled" in gone["note"]
    assert "cancel_refused" not in gone

    # Still open and the cancel was refused: that is an order resting at a venue, said loudly.
    resting = send("open")
    assert not resting["cancelled"] and "may still be resting" in resting["note"]
