"""The cost of a trade that has not happened yet, and the gate that compares it with the claim."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data.schema import Direction, Side
from trading_research_agents.data.sources.base import OrderBook
from trading_research_agents.envelope import load_envelope
from trading_research_agents.envelope.model import Channel
from trading_research_agents.execution.pretrade import (
    Certainty,
    CostComponent,
    PreTradeCost,
    PreTradeError,
    admit,
    book_from_levels,
    horizon_seconds,
    impact_bps,
    interval_seconds,
    pre_trade_cost,
    settlements_in,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def channel() -> Channel:
    envelope = load_envelope(EXAMPLES / "envelope.toml")
    return envelope.cheapest(None)


def book(depth: str = "5") -> OrderBook:
    return book_from_levels(
        bids=[("99.95", depth), ("99.90", depth), ("99.80", "50")],
        asks=[("100.05", depth), ("100.10", depth), ("100.20", "50")],
    )


def test_impact_is_what_the_book_costs_beyond_the_touch() -> None:
    # Size that fits the top level: the touch is the whole cost, so impact is zero.
    assert impact_bps(book(), Decimal("5"), Side.BUY) == 0
    # Twice the top level: half the size pays 100.05, half pays 100.10.
    walked = impact_bps(book(), Decimal("10"), Side.BUY)
    assert walked == pytest.approx(Decimal("2.5"), abs=Decimal("0.01"))
    assert impact_bps(book(), Decimal("10"), Side.SELL) == pytest.approx(
        Decimal("2.5"), abs=Decimal("0.01")
    )
    with pytest.raises(PreTradeError, match="insufficient depth"):
        impact_bps(book(), Decimal("1000"), Side.BUY)


def test_settlements_round_up_because_a_short_horizon_can_still_span_one() -> None:
    assert settlements_in(28_800, 28_800) == 1
    assert settlements_in(60, 28_800) == 1
    assert settlements_in(28_801, 28_800) == 2
    assert horizon_seconds(30, 60) == 1800
    for bad in ((0, 60), (60, 0)):
        with pytest.raises(PreTradeError, match="positive"):
            settlements_in(*bad)
    with pytest.raises(PreTradeError, match="positive"):
        horizon_seconds(0, 60)


def test_every_component_carries_its_certainty_and_the_total_is_their_sum(channel: Channel) -> None:
    cost = pre_trade_cost(
        channel=channel,
        book=book(),
        size=Decimal("10"),
        direction=Direction.LONG,
        horizon_s=1800,
        funding_rate=Decimal("0.0001"),
        settlement_interval_s=28_800,
        latency_slippage_bps=Decimal("1"),
    )
    names = [c.name for c in cost.components]
    assert names == [
        "fee_round_trip",
        "spread_cost",
        "impact",
        "funding_accrual",
        "latency_slippage",
    ]
    by_name = {c.name: c for c in cost.components}
    assert by_name["fee_round_trip"].certainty is Certainty.DECLARED
    assert by_name["spread_cost"].certainty is Certainty.MEASURED
    assert by_name["impact"].certainty is Certainty.COMPUTED
    assert by_name["latency_slippage"].certainty is Certainty.ESTIMATED
    assert "assumed symmetric" in by_name["impact"].source

    # The spread is charged on both legs: a 10 bps spread is 5 bps a side.
    assert by_name["spread_cost"].bps == pytest.approx(Decimal("10"), abs=Decimal("0.01"))
    # One settlement inside half an hour, one basis point, paid by the long.
    assert by_name["funding_accrual"].bps == pytest.approx(Decimal("1"), abs=Decimal("0.0001"))
    assert by_name["latency_slippage"].bps == Decimal("2")
    assert cost.round_trip_bps == sum((c.bps for c in cost.components), Decimal(0))
    assert cost.estimated_bps == Decimal("2")
    record = cost.record()
    assert record["size"] == "10" and record["mid"] == "100.00"
    assert [c["certainty"] for c in record["components"]][-1] == "estimated"

    # A short receives what the long pays, and a receipt at today's rate is never counted against
    # the cost: the rate can turn before the settlement comes.
    short = pre_trade_cost(
        channel=channel,
        book=book(),
        size=Decimal("5"),
        direction=Direction.SHORT,
        horizon_s=1800,
        funding_rate=Decimal("0.0001"),
    )
    received = {c.name: c for c in short.components}["funding_accrual"]
    assert received.bps == 0 and "a receipt of 1.0000 bps is not counted" in received.source
    paying = pre_trade_cost(
        channel=channel,
        book=book(),
        size=Decimal("5"),
        direction=Direction.SHORT,
        horizon_s=1800,
        funding_rate=Decimal("-0.0001"),
    )
    assert {c.name: c.bps for c in paying.components}["funding_accrual"] == Decimal("1")


def test_the_venue_interval_decides_how_many_settlements_a_hold_meets() -> None:
    assert interval_seconds("8h") == 28_800 and interval_seconds("4h") == 14_400
    assert interval_seconds("1h") == 3600 and interval_seconds("30m") == 1800
    # A dated contract settles no funding, and text this cannot read is not guessed at.
    assert interval_seconds("0h") is None and interval_seconds("weekly") is None
    assert interval_seconds("") is None
    # Six hours held: one charge at eight hours, two at four, six at one.
    assert settlements_in(21_600, 28_800) == 1
    assert settlements_in(21_600, 14_400) == 2
    assert settlements_in(21_600, 3600) == 6


def test_funding_and_latency_are_absent_when_nothing_is_known(channel: Channel) -> None:
    cost = pre_trade_cost(
        channel=channel,
        book=book(),
        size=Decimal("5"),
        direction=Direction.LONG,
        horizon_s=600,
    )
    assert [c.name for c in cost.components] == ["fee_round_trip", "spread_cost", "impact"]
    assert cost.estimated_bps == Decimal(0)


def test_a_cost_refuses_to_be_built_from_nonsense(channel: Channel) -> None:
    for size, message in ((Decimal(0), "size is a positive"), (5, "size is a positive")):
        with pytest.raises(PreTradeError, match=message):
            pre_trade_cost(
                channel=channel,
                book=book(),
                size=size,  # type: ignore[arg-type]
                direction=Direction.LONG,
                horizon_s=600,
            )
    with pytest.raises(PreTradeError, match="non-negative"):
        pre_trade_cost(
            channel=channel,
            book=book(),
            size=Decimal("5"),
            direction=Direction.LONG,
            horizon_s=600,
            latency_slippage_bps=Decimal("-1"),
        )
    with pytest.raises(PreTradeError, match="finite Decimal or None"):
        pre_trade_cost(
            channel=channel,
            book=book(),
            size=Decimal("5"),
            direction=Direction.LONG,
            horizon_s=600,
            funding_rate=0.0001,  # type: ignore[arg-type]
        )
    with pytest.raises(PreTradeError, match="no components"):
        PreTradeCost(components=(), size=Decimal("1"), mid=Decimal("100"))
    with pytest.raises(PreTradeError, match="finite Decimal"):
        CostComponent("fee", Decimal("nan"), Certainty.DECLARED, "test")


def test_the_gate_admits_only_a_claim_that_clears_cost_plus_margin(channel: Channel) -> None:
    cost = pre_trade_cost(
        channel=channel,
        book=book(),
        size=Decimal("5"),
        direction=Direction.LONG,
        horizon_s=600,
    )
    total = cost.round_trip_bps
    clears = admit(edge_bps=total + Decimal("1"), cost=cost, margin_bps=Decimal("1"))
    assert clears.admitted and clears.net_bps == Decimal("1")
    assert "clears" in clears.reason and clears.record()["admitted"] is True

    exact = admit(edge_bps=total, cost=cost)
    assert exact.admitted  # equal is enough; the margin is where an operator adds room

    short = admit(edge_bps=total, cost=cost, margin_bps=Decimal("2"))
    assert not short.admitted and "below" in short.reason
    assert short.record()["net_bps"] == "0.0000"

    with pytest.raises(PreTradeError, match="finite Decimal"):
        admit(edge_bps=Decimal("nan"), cost=cost)
    with pytest.raises(PreTradeError, match="non-negative"):
        admit(edge_bps=Decimal("10"), cost=cost, margin_bps=Decimal("-1"))


def test_the_gate_records_where_the_edge_came_from(channel: Channel) -> None:
    cost = pre_trade_cost(
        channel=channel,
        book=book(),
        size=Decimal("5"),
        direction=Direction.LONG,
        horizon_s=600,
    )
    # By default the edge is a declaration, and the record says so, because a reader must never
    # mistake the operator's claim for something this package measured.
    plain = admit(edge_bps=Decimal("100"), cost=cost)
    assert plain.record()["edge_certainty"] == "declared"
    assert plain.record()["edge_source"] == "operator declaration"

    measured = admit(
        edge_bps=Decimal("100"),
        cost=cost,
        edge_certainty=Certainty.MEASURED,
        edge_source="layer 1 on 2025-01-01, n=71",
    )
    assert measured.record()["edge_certainty"] == "measured"
    assert "n=71" in measured.record()["edge_source"]


def test_a_chains_own_cost_is_a_component_with_its_certainty() -> None:
    from trading_research_agents.envelope.model import Channel

    chain = Channel(
        venue="some-dex",
        name="taker-taker",
        entry_fee_bps=Decimal(3),
        exit_fee_bps=Decimal(3),
        gas_quote=Decimal("1.40"),
    )
    book = book_from_levels(
        bids=[("100.00", "5"), ("99.95", "50")], asks=[("100.02", "5"), ("100.07", "50")]
    )
    cost = pre_trade_cost(
        channel=chain, book=book, size=Decimal("1"), direction=Direction.LONG, horizon_s=600
    )
    gas = next(c for c in cost.components if c.name == "gas")

    # 1.40 of quote currency against 100.01 of notional is 139.99 bps, declared by the operator.
    assert gas.certainty is Certainty.DECLARED
    assert gas.bps.quantize(Decimal("0.01")) == Decimal("139.99")
    assert "some-dex/taker-taker" in gas.source
