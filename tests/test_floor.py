from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from trading_research_agent.chain import FloorOutcome, cost_floor
from trading_research_agent.envelope import Envelope
from trading_research_agent.ledger import Axis, Transferability, Verdict


def test_the_documented_case_three_bps_against_nine(envelope: Envelope) -> None:
    # A real, well-powered 3.0 bps drift, dead against the taker round trip it would pay.
    check = cost_floor(envelope, Decimal("3.0"), venue="hyperliquid")
    # The cheapest hyperliquid channel is maker-maker at 3.0, so the check passes on quote...
    assert check.channel.key == "hyperliquid/maker-maker"
    assert check.outcome is FloorOutcome.CLEARED
    assert check.margin_bps == Decimal("0.0")

    taker_only = Envelope(tuple(c for c in envelope.channels if c.name == "taker-taker"))
    check = cost_floor(taker_only, Decimal("3.0"), venue="hyperliquid")
    assert check.outcome is FloorOutcome.STOP
    assert check.margin_bps == Decimal("-6.0")
    assert check.stopped


def test_a_stop_becomes_a_closed_row_on_the_cost_axis(envelope: Envelope, now: datetime) -> None:
    taker_only = Envelope(tuple(c for c in envelope.channels if c.name == "taker-taker"))
    check = cost_floor(taker_only, Decimal("3.0"), venue="hyperliquid")
    entry = check.to_entry(
        entry_id="H-020", subject="informed-flow burst drift", recorded_at=now, envelope=taker_only
    )
    assert entry.verdict is Verdict.CLOSED
    assert entry.layer == 0
    assert entry.axis_of_death == (Axis.COST,)
    assert entry.transferability is Transferability.ENVELOPE_BOUND
    assert entry.reversal_point is not None
    assert entry.reversal_point.parameter == "c_rt"
    assert entry.reversal_point.value == Decimal("3.0")
    assert entry.reversal_point.comparison == "<"
    assert dict(entry.envelope)["hyperliquid/taker-taker/round_trip_bps"] == "9.0"
    expected = "3.0 bps plausible against a 9.0 bps round trip on hyperliquid/taker-taker"
    assert expected in entry.note


def test_passing_layer_zero_is_not_a_verdict(envelope: Envelope, now: datetime) -> None:
    check = cost_floor(envelope, Decimal("20"))
    assert check.outcome is FloorOutcome.CLEARED
    with pytest.raises(ValueError, match="nothing to record"):
        check.to_entry(entry_id="H-021", subject="x", recorded_at=now)


def test_equal_to_the_floor_is_not_below_it(envelope: Envelope) -> None:
    check = cost_floor(envelope, Decimal("3.0"))
    assert check.outcome is FloorOutcome.CLEARED
    assert check.margin_bps == 0


def test_measured_cost_is_reported_but_does_not_decide(envelope: Envelope, now: datetime) -> None:
    bybit_taker = Envelope(tuple(c for c in envelope.channels if c.key == "bybit/taker-taker"))
    check = cost_floor(bybit_taker, Decimal("12.0"))
    # Clears the 11.0 quote, does not clear the 15.02 that execution actually cost.
    assert check.outcome is FloorOutcome.CLEARED
    assert check.margin_bps == Decimal("1.0")
    assert check.measured_margin_bps == Decimal("-3.02")
    assert check.clears_measured_cost is False
    assert check.measured_round_trip_bps == Decimal("15.02")

    check = cost_floor(bybit_taker, Decimal("9.0"))
    assert check.stopped
    note = check.to_entry(entry_id="H-022", subject="x", recorded_at=now).note
    assert "(15.02 bps measured)" in note


def test_no_measurement_means_no_opinion(envelope: Envelope) -> None:
    check = cost_floor(envelope, Decimal("5"), venue="hyperliquid")
    assert check.measured_margin_bps is None
    assert check.clears_measured_cost is None


def test_costs_are_never_floats(envelope: Envelope) -> None:
    with pytest.raises(TypeError, match="never floats"):
        cost_floor(envelope, 3.0)  # type: ignore[arg-type]


def test_an_unbounded_magnitude_is_refused_not_guessed(envelope: Envelope) -> None:
    with pytest.raises(ValueError, match="bound the plausible magnitude"):
        cost_floor(envelope, Decimal("0"))
    with pytest.raises(ValueError, match="bound the plausible magnitude"):
        cost_floor(envelope, Decimal("-1"))
