"""`tra keys --prepare`: a hedged practice account set to one-way, where nothing can be hurt."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_research_agents.trading.dispatch import VenueHand
from trading_research_agents.trading.venues.notes import notes_for


class Account:
    def __init__(self, *, hedged: bool = True, held: float = 0.0, orders: int = 0) -> None:
        self.has = {"fetchPositionMode": True}
        self.is_hedged, self.held, self.orders = hedged, held, orders
        self.switched: list[tuple[bool, str]] = []

    def market(self, symbol: str) -> dict[str, Any]:
        return {"settle": "USDT", "limits": {"amount": {}}}

    def fetch_position_mode(self, symbol: str) -> dict[str, Any]:
        return {"hedged": self.is_hedged}

    def set_position_mode(self, hedged: bool, symbol: str) -> None:
        self.switched.append((hedged, symbol))
        self.is_hedged = hedged

    def fetch_position(self, symbol: str) -> dict[str, Any]:
        return {"contracts": self.held, "side": "long"} if self.held else {}

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return [{"id": str(i)} for i in range(self.orders)]


def hand(client: Account, environment: str = "demo") -> VenueHand:
    return VenueHand(
        venue="acme",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(1000),
        keys=("K", "S"),
        client=client,
        environment=environment,
    )


def test_a_flat_hedged_practice_account_is_set_to_one_way() -> None:
    client = Account()
    said = hand(client).make_one_way()
    assert "set to one-way" in said and client.switched == [(False, "BTC/USDT:USDT")]
    assert hand(client).make_one_way() == ""


def test_mainnet_or_anything_open_is_left_as_it_is() -> None:
    live = Account()
    assert "at the exchange" in hand(live, "mainnet").make_one_way() and live.switched == []
    held = Account(held=0.01)
    assert "flatten it" in hand(held).make_one_way() and held.switched == []
    resting = Account(orders=1)
    assert "flatten it" in hand(resting).make_one_way() and resting.switched == []


def test_every_venue_note_ends_with_the_step_they_all_share() -> None:
    assert notes_for("okx")[0].startswith("make the key inside Demo trading")
    assert notes_for("acme") == notes_for("ACME") and "one-way" in notes_for("acme")[-1]
