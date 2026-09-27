from __future__ import annotations

from decimal import Decimal

import pytest

from trading_research_agents.envelope import Channel, Envelope, EnvelopeError, parse_envelope


def test_example_envelope_reproduces_the_published_schedule(envelope: Envelope) -> None:
    by_key = {c.key: c for c in envelope.channels}
    assert by_key["bybit/taker-taker"].round_trip_bps == Decimal("11.0")
    assert by_key["bybit/maker-taker"].round_trip_bps == Decimal("7.5")
    assert by_key["hyperliquid/taker-taker"].round_trip_bps == Decimal("9.0")
    assert by_key["hyperliquid/maker-taker"].round_trip_bps == Decimal("6.0")
    assert by_key["hyperliquid/maker-maker"].round_trip_bps == Decimal("3.0")
    assert by_key["bybit/taker-taker"].measured_round_trip_bps == Decimal("15.02")
    assert envelope.order_latency_ms == Decimal("5.5")
    assert envelope.venues() == ("bybit", "hyperliquid")


def test_toml_numbers_arrive_as_exact_decimals() -> None:
    env = parse_envelope('[[channel]]\nvenue="v"\nname="n"\nentry_fee_bps=0.1\nexit_fee_bps=0.2\n')
    c = env.channels[0]
    assert isinstance(c.entry_fee_bps, Decimal)
    assert str(c.round_trip_bps) == "0.3"


def test_integers_and_strings_are_accepted_as_numbers() -> None:
    env = parse_envelope('[[channel]]\nvenue="v"\nname="n"\nentry_fee_bps=5\nexit_fee_bps="5.5"\n')
    assert env.channels[0].round_trip_bps == Decimal("10.5")


def test_cheapest_overall_and_per_venue(envelope: Envelope) -> None:
    assert envelope.cheapest().key == "hyperliquid/maker-maker"
    assert envelope.cheapest("bybit").key == "bybit/maker-taker"
    with pytest.raises(EnvelopeError, match="no channel"):
        envelope.cheapest("okx")


def test_duplicate_channels_are_refused() -> None:
    c = Channel("v", "n", Decimal("1"), Decimal("1"))
    with pytest.raises(EnvelopeError, match="duplicate"):
        Envelope((c, c))


def test_an_envelope_declares_at_least_one_channel() -> None:
    with pytest.raises(EnvelopeError, match="at least one"):
        Envelope(())
    with pytest.raises(EnvelopeError, match="no \\[\\[channel\\]\\]"):
        parse_envelope("[envelope]\norder_latency_ms = 5\n")


def test_negative_and_non_decimal_fees_are_refused() -> None:
    with pytest.raises(EnvelopeError, match="negative"):
        Channel("v", "n", Decimal("-1"), Decimal("1"))
    with pytest.raises(EnvelopeError, match="Decimal"):
        Channel("v", "n", 1.0, Decimal("1"))  # type: ignore[arg-type]


def test_missing_fields_and_bad_values_name_their_location() -> None:
    with pytest.raises(EnvelopeError, match=r"channel\[0\] is missing 'exit_fee_bps'"):
        parse_envelope('[[channel]]\nvenue="v"\nname="n"\nentry_fee_bps=1\n')
    with pytest.raises(EnvelopeError, match=r"channel\[0\]\.entry_fee_bps is not a number"):
        parse_envelope('[[channel]]\nvenue="v"\nname="n"\nentry_fee_bps="abc"\nexit_fee_bps=1\n')
    with pytest.raises(EnvelopeError, match="must be a number"):
        parse_envelope('[[channel]]\nvenue="v"\nname="n"\nentry_fee_bps=true\nexit_fee_bps=1\n')
    with pytest.raises(EnvelopeError, match="not valid TOML"):
        parse_envelope("[[channel\n")


def test_snapshot_is_flat_strings_for_the_ledger(envelope: Envelope) -> None:
    snap = dict(envelope.snapshot())
    assert snap["bybit/taker-taker/round_trip_bps"] == "11.0"
    assert snap["bybit/taker-taker/measured_round_trip_bps"] == "15.02"
    assert snap["order_latency_ms"] == "5.5"
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in envelope.snapshot())
