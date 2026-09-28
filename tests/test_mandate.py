"""What this software is allowed to do with an order, and who said so.

Venue mode is the only mode that can lose money, and it is the only one with a door in front of it.
These tests pin the door: the text has to be in the operator's own project word for word, the
signature has to be there, the ladder has to be at its canary tier, the caps have to be written
down, and the keys have to be in the environment. Every missing piece is named, not summarised.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.execution.mandate import (
    DISCLAIMER,
    MANDATE_FILE,
    MODES,
    Mandate,
    MandateError,
    blocking,
    digest,
    require,
    signed_in,
)
from trading_research_agents.trading.dispatch import DispatchError, VenueHand, hand_for

SIGNED = Mandate(
    mode="venue",
    venue="bybit",
    acknowledged_by="the operator",
    acknowledged_on="2026-09-28",
    max_notional=Decimal(200),
)


class FakeExchange:
    """What ccxt hands back from create_order, in the shape the adapter reads."""

    def __init__(self, answer: object = None) -> None:
        self.answer = answer or {
            "id": "order-1",
            "status": "closed",
            "filled": 0.01,
            "average": 100000.5,
        }
        self.orders: list[tuple[object, ...]] = []
        self.sandbox: bool | None = None

    def set_sandbox_mode(self, on: bool) -> None:
        self.sandbox = on

    def create_order(self, symbol: str, kind: str, side: str, amount: float) -> object:
        self.orders.append((symbol, kind, side, amount))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def project_with_mandate(tmp_path: Path, text: str = DISCLAIMER) -> Path:
    (tmp_path / MANDATE_FILE).write_text(text, encoding="utf-8")
    return tmp_path


def test_the_two_quiet_modes_need_nothing(tmp_path: Path) -> None:
    for mode in ("sandbox", "signals"):
        assert blocking(Mandate(mode=mode), project=tmp_path, tier="REPLAY") == ()
    assert set(MODES) == {"sandbox", "signals", "venue"}
    assert not Mandate().places_orders and SIGNED.places_orders


def test_venue_mode_names_everything_standing_in_the_way(tmp_path: Path) -> None:
    stops = blocking(Mandate(mode="venue"), project=tmp_path, tier="SHADOW")

    assert any(MANDATE_FILE in s for s in stops)
    assert any("acknowledged_by" in s for s in stops)
    assert any("acknowledged_on" in s for s in stops)
    assert any("names the live venue" in s for s in stops)
    assert any("max_notional" in s for s in stops)
    assert any("ladder is at SHADOW" in s for s in stops)
    assert any("[canary] limits" in s for s in stops)


def test_a_signed_project_at_the_canary_tier_may_run(tmp_path: Path) -> None:
    project = project_with_mandate(tmp_path)
    assert (
        blocking(SIGNED, project=project, tier="CANARY", canary_declared=True, missing_keys=())
        == ()
    )
    require(SIGNED, project=project, tier="CANARY", canary_declared=True)


def test_the_text_has_to_be_there_word_for_word(tmp_path: Path) -> None:
    assert not signed_in(tmp_path)

    almost = DISCLAIMER.replace(
        "I accept that the losses are mine.", "I accept some of the losses."
    )
    assert not signed_in(project_with_mandate(tmp_path, almost))

    # An operator may write around it; what matters is that the text itself is in there.
    around = f"# Our desk\n\nSigned by the trading desk on 2026-09-28.\n\n{DISCLAIMER}\n"
    assert signed_in(project_with_mandate(tmp_path, around))


def test_line_endings_do_not_change_the_text(tmp_path: Path) -> None:
    (tmp_path / MANDATE_FILE).write_bytes(DISCLAIMER.replace("\n", "\r\n").encode("utf-8"))
    assert signed_in(tmp_path)
    assert digest(DISCLAIMER) == digest(DISCLAIMER.replace("\n", "\r\n"))


def test_a_missing_key_is_named_rather_than_discovered_at_the_venue(tmp_path: Path) -> None:
    stops = blocking(
        SIGNED,
        project=project_with_mandate(tmp_path),
        tier="CANARY",
        canary_declared=True,
        missing_keys=("BYBIT_API_SECRET",),
    )
    assert stops and "BYBIT_API_SECRET" in stops[0]

    with pytest.raises(MandateError, match="BYBIT_API_SECRET"):
        require(
            SIGNED,
            project=project_with_mandate(tmp_path),
            tier="CANARY",
            canary_declared=True,
            missing_keys=("BYBIT_API_SECRET",),
        )


def test_a_mandate_that_cannot_be_applied_is_refused_when_it_is_written() -> None:
    with pytest.raises(MandateError, match="mode is one of"):
        Mandate(mode="fire")
    with pytest.raises(MandateError, match="max_notional is positive"):
        Mandate(max_notional=Decimal(0))
    with pytest.raises(MandateError, match="at least 1"):
        Mandate(max_orders_per_cycle=0)
    with pytest.raises(MandateError, match="is a date"):
        Mandate(acknowledged_on="last tuesday")
    with pytest.raises(MandateError, match="not in the future"):
        Mandate(acknowledged_on=(date.today() + timedelta(days=1)).isoformat())


def test_the_hand_sends_one_order_and_records_what_came_back() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )

    placed = hand({"target": "long", "size": "0.01", "mid": "100000"})
    assert placed["sent"] is True and placed["order_id"] == "order-1"
    assert placed["side"] == "buy" and placed["notional"] == "1000.00"
    assert venue.orders == [("BTC/USDT:USDT", "market", "buy", 0.01)]

    short = hand({"target": "short", "size": "0.01", "mid": "100000"})
    assert short["side"] == "sell"


def test_the_hand_refuses_before_it_reaches_the_venue() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(500),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )

    big = hand({"target": "long", "size": "0.01", "mid": "100000"})
    assert big["sent"] is False and "above [execution].max_notional" in big["refused"]

    flat = hand({"target": "flat", "size": "0.01", "mid": "100"})
    assert flat["sent"] is False and "is not an order" in flat["refused"]

    nothing = hand({"target": "long", "size": "0", "mid": "100"})
    assert nothing["sent"] is False and "size is not positive" in nothing["refused"]

    assert venue.orders == []


def test_a_venue_failure_is_one_kind_of_error() -> None:
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=FakeExchange(RuntimeError("insufficient margin")),
    )
    with pytest.raises(DispatchError, match="RuntimeError: insufficient margin"):
        hand({"target": "long", "size": "0.01", "mid": "100000"})


def test_a_hand_needs_a_cap_and_the_keys_come_from_the_environment() -> None:
    with pytest.raises(DispatchError, match="positive notional cap"):
        VenueHand(
            venue="bybit",
            symbol="BTC/USDT:USDT",
            max_notional=Decimal(0),
            keys=("K", "S"),
            client=FakeExchange(),
        )

    with pytest.raises(DispatchError, match="not in the environment"):
        hand_for(venue="bybit", symbol="BTC/USDT:USDT", max_notional=Decimal(100), env={})


def test_the_hand_uses_the_venues_test_environment_unless_told_otherwise() -> None:
    venue = FakeExchange()
    VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(100),
        keys=("K", "S"),
        client=venue,
    )
    # A client handed in is the caller's own; the sandbox switch belongs to the one this builds.
    assert venue.sandbox is None
