"""What a venue can do is asked of the library, not promised.

"ccxt supports a hundred venues" is true and not useful. A spot exchange has no funding rate, some
venues cannot attach a stop to an entry, and a few will not say what position you hold - and each of
those is the difference between a cycle that runs and one that refuses. These tests pin the reading
of that, and the words an operator is given when a venue cannot do something.
"""

from __future__ import annotations

from typing import Any

import pytest

from trading_research_agents.data.sources.capability import (
    CapabilityError,
    capability_of,
)


class Exchange:
    """ccxt's base class: every switch is defined here, and here they only refuse."""

    def __init__(self, has: dict[str, bool], *, testnet: bool = True) -> None:
        self.has = has
        self.urls = {"test": {"public": "https://testnet"}} if testnet else {}

    def enable_demo_trading(self, on: bool) -> None:
        raise NotImplementedError("NotSupported")


class DemoVenue(Exchange):
    """A venue that implements its demo switch itself, as Bybit does."""

    def enable_demo_trading(self, on: bool) -> None:
        return None


PERPETUAL = {
    "fetchOHLCV": True,
    "fetchOrderBook": True,
    "fetchFundingRate": True,
    "fetchOpenInterest": True,
    "createOrder": True,
    "createOrderWithTakeProfitAndStopLoss": True,
    "fetchPosition": True,
    "fetchOpenOrders": True,
    "cancelOrder": True,
}


def test_a_full_perpetual_venue_can_do_everything_a_cycle_needs() -> None:
    able = capability_of("bybit", client=DemoVenue(PERPETUAL))

    assert able.serves_data == ("funding", "ohlcv", "open_interest", "orderbook")
    assert able.can_send and able.can_attach_exits and able.guards
    assert able.demo and able.testnet and able.practice == "demo and testnet"
    assert able.level == "full" and "venue mode ready" in able.verdict()


def test_a_venue_that_cannot_attach_exits_is_told_what_that_costs() -> None:
    able = capability_of(
        "binance", client=Exchange({**PERPETUAL, "createOrderWithTakeProfitAndStopLoss": False})
    )

    assert able.can_send and not able.can_attach_exits
    assert '[execution].exits = "none"' in able.verdict()


def test_a_spot_venue_says_which_kinds_it_cannot_serve() -> None:
    spot = {
        k: v for k, v in PERPETUAL.items() if k not in ("fetchFundingRate", "fetchOpenInterest")
    }
    able = capability_of(
        "kraken", client=Exchange({**spot, "fetchFundingRate": False, "fetchOpenInterest": False})
    )

    assert able.serves_data == ("ohlcv", "orderbook")
    assert able.data["funding"] is False and able.data["open_interest"] is False
    # Declaring a kind it cannot serve is the mistake this is here to prevent.
    asked = capability_of(
        "kraken",
        client=Exchange({**spot, "fetchFundingRate": False, "fetchOpenInterest": False}),
        kinds=("ohlcv", "funding"),
    )
    assert asked.kinds_missing == ("funding",)


def test_a_venue_that_takes_no_orders_is_data_only() -> None:
    able = capability_of("somefeed", client=Exchange({**PERPETUAL, "createOrder": False}))

    assert not able.can_send and "data only" in able.verdict()


def test_missing_account_reads_are_named_rather_than_discovered_live() -> None:
    able = capability_of(
        "thin", client=Exchange({**PERPETUAL, "fetchPosition": False, "fetchOpenOrders": False})
    )

    assert able.can_send and able.can_attach_exits and not able.guards
    assert able.level == "data" and "the position or the open orders" in able.verdict()


def test_a_venue_with_no_test_environment_says_so() -> None:
    able = capability_of("nodemo", client=Exchange(PERPETUAL, testnet=False))
    # The inherited demo switch only refuses, so it does not count as a demo environment.
    assert able.demo is False and able.testnet is False and able.practice == "none"


def test_a_venue_the_library_does_not_know_is_refused_by_name() -> None:
    with pytest.raises(CapabilityError, match="no exchange named 'not-a-venue'"):
        capability_of("not-a-venue")


def test_the_record_is_what_the_json_output_carries() -> None:
    record: dict[str, Any] = capability_of("bybit", client=Exchange(PERPETUAL)).record()
    assert set(record) == {"venue", "data", "execution", "demo", "testnet", "level", "verdict"}
    assert record["execution"]["attached_exits"] is True


def test_a_venue_that_takes_exits_as_their_own_orders_is_told_to_place_them_separately() -> None:
    separate = {
        **PERPETUAL,
        "createOrderWithTakeProfitAndStopLoss": False,
        "createStopLossOrder": True,
        "createTakeProfitOrder": True,
    }
    able = capability_of("acme", client=Exchange(separate))
    assert able.level == "exits-none" and able.can_place_exits
    assert 'exits = "separate"' in able.verdict()
    bare = capability_of("acme", client=Exchange({**separate, "createTakeProfitOrder": False}))
    assert not bare.can_place_exits and 'exits = "none"' in bare.verdict()
