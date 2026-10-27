"""Binance futures through ccxt's `binance` class, shaped as its demo API answered on 2026-10-20."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from approvals import approved
from test_mandate import LONG, FakeExchange
from trading_research_agents.trading.capital import read_account
from trading_research_agents.trading.dispatch import VenueHand
from trading_research_agents.trading.hand_state import DispatchError, leverage_number
from trading_research_agents.trading.venues.margin import route


class NotSupported(Exception):  # noqa: N818 - ccxt's own class name
    """ccxt's own class name, which the hand reads."""


class Futures:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.id = "binance"
        self.has: dict[str, Any] = {}
        self.options: dict[str, Any] = {"defaultType": "spot"}
        self.rows = rows or []

    def market(self, symbol: str) -> dict[str, Any]:
        return {"settle": "USDT", "limits": {"amount": {}}}

    def fetch_position(self, symbol: str) -> dict[str, Any]:
        raise NotSupported("binance fetchPosition() supports option markets only")

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return list(self.rows)

    def fetch_balance(self) -> dict[str, Any]:
        # ccxt's parse came back empty; the venue's own rows were in `info`.
        assets = [{"asset": "USDT", "walletBalance": "5000.0", "availableBalance": "4800.0"}]
        return {"info": {"assets": assets}, "free": {}, "total": {}}


def hand(client: Futures) -> VenueHand:
    return VenueHand(
        venue="binance",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(1000),
        keys=("K", "S"),
        client=client,
    )


def test_a_futures_symbol_sends_the_client_to_the_futures_api() -> None:
    usdm, coinm, spot = Futures(), Futures(), Futures()
    route("binance", usdm, "BTC/USDT:USDT")
    route("binance", coinm, "BTC/USD:BTC")
    route("binance", spot, "BTC/USDT")
    assert usdm.options["defaultType"] == "future"
    assert coinm.options["defaultType"] == "delivery"
    assert spot.options["defaultType"] == "spot"
    chosen = Futures()
    chosen.options["defaultType"] = "swap"
    route("binance", chosen, "BTC/USDT:USDT")
    assert chosen.options["defaultType"] == "swap"


def test_the_position_is_read_from_the_list_when_the_single_read_is_not_there() -> None:
    assert hand(Futures()).position() == Decimal(0)
    held = Futures([{"symbol": "BTC/USDT:USDT", "contracts": 0.0015, "side": "short"}])
    assert hand(held).position() == Decimal("-0.0015")
    two = Futures([{"symbol": "BTC/USDT:USDT", "contracts": 1, "side": "long"}] * 2)
    with pytest.raises(DispatchError, match="2 positions"):
        hand(two).position()


def test_a_balance_ccxt_did_not_parse_is_read_from_the_venues_own_rows() -> None:
    account = read_account(Futures(), "USDT")
    assert account.wallet == Decimal("5000.0") and account.free == Decimal("4800.0")


def test_a_whole_leverage_is_sent_as_an_integer() -> None:
    assert leverage_number(Decimal(3)) == 3 and isinstance(leverage_number(Decimal("3.0")), int)
    assert leverage_number(Decimal("2.5")) == 2.5


def test_an_order_is_not_priced_against_a_book_the_venue_stamped_hours_ago() -> None:
    class Stale(FakeExchange):
        def fetch_order_book(self, symbol: str) -> dict[str, Any]:
            # As the demo API served it: a book 3.2 hours old.
            stamp = (time.time() - 11_507) * 1000
            return {"bids": [[99999, 1]], "asks": [[100001, 1]], "timestamp": stamp}

    venue = Stale()
    placed = VenueHand(
        venue="acme",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=venue,
    )(approved(LONG))
    assert placed["sent"] is False and "stamped 11507s ago" in placed["refused"]
    assert venue.orders == []
