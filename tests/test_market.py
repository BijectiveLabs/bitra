"""One market shape for every venue, and an order path that gives the same answer every time.

The shapes below are the ones ccxt 4.5.85 returns from `market()`, cut to the fields `Market`
reads. The determinism test pins the perpetual order byte for byte, so moving market mechanics
behind `Market` cannot change what is sent without a test saying so.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from approvals import approved
from trading_research_agents.trading.dispatch import VenueHand, market_kind
from trading_research_agents.trading.market import Market, MarketError

LINEAR_SWAP = {
    "symbol": "BTC/USDT:USDT",
    "type": "swap",
    "swap": True,
    "contract": True,
    "linear": True,
    "inverse": False,
    "base": "BTC",
    "quote": "USDT",
    "settle": "USDT",
    "contractSize": 0.001,
    "taker": 0.00055,
    "maker": 0.0002,
    "limits": {"amount": {"min": 1}},
}
INVERSE_SWAP = {
    **LINEAR_SWAP,
    "symbol": "BTC/USD:BTC",
    "linear": False,
    "inverse": True,
    "quote": "USD",
    "settle": "BTC",
    "contractSize": 1,
}
SPOT = {
    "symbol": "BTC/USDT",
    "type": "spot",
    "spot": True,
    "contract": False,
    "base": "BTC",
    "quote": "USDT",
    "settle": None,
    "contractSize": None,
    "taker": 0.001,
}
DATED = {
    **LINEAR_SWAP,
    "symbol": "BTC/USDT:USDT-261225",
    "type": "future",
    "swap": False,
    "future": True,
    "expiry": 1798185600000,
}
OPTION = {
    **LINEAR_SWAP,
    "symbol": "BTC/USDT:USDT-261225-120000-C",
    "type": "option",
    "swap": False,
    "option": True,
}
OUTCOME = {
    "symbol": "WORLDCUP2026:YES",
    "type": "prediction",
    "contract": False,
    "quote": "USDC",
    "settle": "USDC",
}


def test_every_market_type_is_one_shape_with_its_own_facts() -> None:
    labels = {
        "linear perpetual": LINEAR_SWAP,
        "inverse perpetual": INVERSE_SWAP,
        "spot": SPOT,
        "dated future": DATED,
        "option": OPTION,
        "prediction": OUTCOME,
    }
    for label, shape in labels.items():
        market = Market.from_ccxt("venue", str(shape["symbol"]), shape)
        assert market.label == label
        assert market_kind(shape) == label
    assert market_kind({}) == "unknown"

    perpetual = Market.from_ccxt("venue", "BTC/USDT:USDT", LINEAR_SWAP)
    assert perpetual.margined and perpetual.settlement == "USDT"
    assert perpetual.unit() == Decimal("0.001") and perpetual.taker == Decimal("0.00055")
    assert perpetual.amount_min == Decimal(1)

    spot = Market.from_ccxt("venue", "BTC/USDT", SPOT)
    assert not spot.margined and spot.unit() == Decimal(1) and spot.settlement == "USDT"
    assert Market.from_ccxt("venue", "X", INVERSE_SWAP).settlement == "BTC"
    assert Market.from_ccxt("venue", "X", DATED).expiry == 1798185600000


def test_the_flags_decide_when_an_adapter_leaves_the_type_blank() -> None:
    assert market_kind({"swap": True, "linear": True}) == "linear perpetual"
    assert market_kind({"future": True}) == "dated future"
    assert market_kind({"spot": True}) == "spot"
    # Described, but with neither a type nor a flag: the hand still judges size and account.
    assert market_kind({"contractSize": "0.01", "contract": True}) == "unknown"


def test_a_contract_market_without_a_usable_size_is_refused_by_name() -> None:
    with pytest.raises(MarketError, match="does not say how large one BTC/USDT:USDT contract"):
        Market.from_ccxt("venue", "BTC/USDT:USDT", {**LINEAR_SWAP, "contractSize": None}).unit()
    for bad in ("0", "-1", "NaN", "lots"):
        with pytest.raises(MarketError, match=f"contract size of '{bad}'"):
            Market.from_ccxt("venue", "BTC/USDT:USDT", {**LINEAR_SWAP, "contractSize": bad}).unit()


class Recorded:
    """A venue that answers the same way every time and keeps every call it was given."""

    has = {"createOrderWithTakeProfitAndStopLoss": True}

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def market(self, symbol: str) -> dict[str, Any]:
        return dict(LINEAR_SWAP)

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return str(amount)

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.1f}"

    def fetch_order_book(self, symbol: str) -> dict[str, Any]:
        return {"bids": [[100000.0, 1]], "asks": [[100001.0, 1]]}

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return []

    def fetch_position(self, symbol: str) -> None:
        return None

    def create_order(self, *args: Any) -> dict[str, Any]:
        self.calls.append(args)
        return {"id": "order-1", "status": "closed", "filled": 10, "average": 100001.0}


INTENT = {
    "target": "long",
    "size": "0.01",
    "mid": "100000.5",
    "stop_price": "99000",
    "take_price": "102000",
    "at": "2099-01-01T00-00-00Z",
    "instrument": "BTCUSDT",
    "bid": "100000",
    "ask": "100001",
}


def _send() -> tuple[dict[str, Any], tuple[Any, ...]]:
    venue = Recorded()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        instrument="BTCUSDT",
        max_notional=Decimal(5000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
        clock=lambda: 4070908800.0,
    )
    placed = hand(approved(dict(INTENT)))
    return placed, venue.calls[0]


def test_the_same_decision_on_the_same_market_sends_the_same_order() -> None:
    first, sent = _send()
    second, again = _send()
    assert first == second and sent == again
    assert sent == (
        "BTC/USDT:USDT",
        "limit",
        "buy",
        10.0,
        100201.0,
        {
            "clientOrderId": "tra20990101T000000ZBTCUSDT",
            "stopLoss": {"triggerPrice": 99000.0},
            "takeProfit": {"triggerPrice": 102000.0},
            "timeInForce": "IOC",
        },
    )
    assert first["contracts"] == "10" and first["contract_size"] == "0.001"
    assert first["filled"] == "0.010" and first["slippage_bps"] == "0.05"
    assert first["send_ms"] == 0 and first["decision_age_s"] == 0.0


def test_the_market_types_a_desk_sends_to_are_one_setting() -> None:
    from trading_research_agents.trading.config_values import ConfigError
    from trading_research_agents.trading.config_venue import _mandate

    def read(execution: dict[str, Any]) -> tuple[str, ...]:
        return _mandate({"execution": execution}, "bybit/taker-taker").markets

    assert read({}) == ("linear perpetual",)
    assert read({"markets": ["linear perpetual", "linear perpetual"]}) == ("linear perpetual",)
    assert read({"markets": ["linear perpetual", "spot"]}) == ("linear perpetual", "spot")
    with pytest.raises(ConfigError, match=r"\['option'\] not built yet"):
        read({"markets": ["spot", "option"]})
    with pytest.raises(ConfigError, match="at least one market type"):
        read({"markets": []})
    with pytest.raises(ConfigError, match="is a list of market types"):
        read({"markets": "linear perpetual"})


def test_a_built_market_type_the_desk_did_not_list_is_refused() -> None:
    venue = Recorded()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        instrument="BTCUSDT",
        max_notional=Decimal(5000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
        clock=lambda: 4070908800.0,
        markets=(),
    )
    placed = hand(approved(dict(INTENT)))
    assert placed["sent"] is False and "[execution].markets lists []" in placed["refused"]
    assert venue.calls == []


def test_a_round_trip_is_counted_in_the_markets_settlement_currency() -> None:
    from trading_research_agents.trading.round_trip import _settlement

    class Described:
        symbol = "BTC/USD:BTC"

        def market(self) -> Market:
            return Market.from_ccxt("venue", self.symbol, INVERSE_SWAP)

    class Bare:
        symbol = "XRP/USDT:USDT"

    assert _settlement(Described()) == "BTC"
    assert _settlement(Bare()) == "USDT"
