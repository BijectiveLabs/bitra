from __future__ import annotations

from decimal import Decimal
from pathlib import Path

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


def test_a_channel_says_how_it_gets_in_and_out() -> None:
    from trading_research_agents.envelope.model import Channel

    maker = Channel(
        venue="bybit", name="maker-taker", entry_fee_bps=Decimal(2), exit_fee_bps=Decimal("5.5")
    )
    assert maker.roles == ("maker", "taker")
    assert maker.entry_role == "maker" and maker.exit_role == "taker"

    taker = Channel(
        venue="bybit", name="TAKER-TAKER", entry_fee_bps=Decimal(5), exit_fee_bps=Decimal(5)
    )
    assert taker.entry_role == "taker"

    # A name that does not say is not guessed at, because the fee beside it depends on the answer.
    odd = Channel(venue="dex", name="flat", entry_fee_bps=Decimal(1), exit_fee_bps=Decimal(1))
    assert odd.roles == ("undeclared", "undeclared")


def test_a_channel_may_carry_the_chains_own_cost(tmp_path: Path) -> None:
    from trading_research_agents.envelope import load_envelope
    from trading_research_agents.envelope.model import Channel, EnvelopeError

    (tmp_path / "envelope.toml").write_text(
        "[[channel]]\n"
        'venue = "some-dex"\n'
        'name = "taker-taker"\n'
        "entry_fee_bps = 3\n"
        "exit_fee_bps = 3\n"
        "gas_quote = 1.40\n",
        encoding="utf-8",
    )
    (channel,) = load_envelope(tmp_path / "envelope.toml").channels
    assert channel.gas_quote == Decimal("1.40")

    with pytest.raises(EnvelopeError, match="gas_quote"):
        Channel(
            venue="d",
            name="taker-taker",
            entry_fee_bps=Decimal(1),
            exit_fee_bps=Decimal(1),
            gas_quote=Decimal(-1),
        )


SPOT_PRICED = """
[[channel]]
venue = "bybit"
name = "taker-taker"
entry_fee_bps = 5.5
exit_fee_bps = 5.5
measured_round_trip_bps = 15.02

[channel.spot]
entry_fee_bps = 10
exit_fee_bps = 10
"""


def test_a_channel_carries_the_spot_pairs_fees_beside_the_perpetuals() -> None:
    channel = parse_envelope(SPOT_PRICED).channels[0]
    assert channel.round_trip_bps == Decimal("11.0") and channel.key == "bybit/taker-taker"
    spot = channel.for_market("spot")
    assert spot.round_trip_bps == Decimal(20) and spot.key == channel.key
    # Measured on the perpetual, so not carried over to a market it was not measured on.
    assert spot.market == "spot" and spot.measured_round_trip_bps is None
    plain = parse_envelope(SPOT_PRICED.split("[channel.spot]")[0]).channels[0]
    assert plain.for_market("spot") is plain


def test_fees_for_a_market_type_without_its_own_are_refused() -> None:
    with pytest.raises(EnvelopeError, match="declares entry_fee_bps and exit_fee_bps"):
        parse_envelope(SPOT_PRICED.replace("exit_fee_bps = 10\n", ""))
    with pytest.raises(EnvelopeError, match="not a market type with fees of its own"):
        Channel(
            "v",
            "taker-taker",
            Decimal(1),
            Decimal(1),
            market_fees=(("option", Decimal(1), Decimal(1)),),
        )
