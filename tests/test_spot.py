"""Spot on the same order path as perpetuals, told apart only by what the market says it is.

The double answers with ccxt 4.5.85's spot shapes: a balance per coin instead of a position, no
leverage, and an amount step it truncates to, the way `amount_to_precision` does.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.trading.dispatch import VenueHand
from trading_research_agents.trading.positions import _fee_in_quote

SPOT_LONG: dict[str, Any] = {
    "target": "long",
    "size": "2",
    "mid": "150",
    "stop_price": "145",
    "take_price": "160",
    "at": "2099-01-01T00-00-00Z",
    "instrument": "SOLUSDT",
    "bid": "149.99",
    "ask": "150.01",
}


class SpotVenue:
    has = {"createOrderWithTakeProfitAndStopLoss": True}

    def __init__(self, sol: str = "0", usdt: str = "1000", resting: int = 0) -> None:
        self.balances = {"SOL": Decimal(sol), "USDT": Decimal(usdt)}
        self.orders: list[dict[str, Any]] = []
        self.open_orders: list[dict[str, Any]] = [{"id": f"exit-{i}"} for i in range(resting)]
        self.cancelled: list[str] = []
        self.options: dict[str, Any] = {}

    def market(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": "SOL/USDT",
            "type": "spot",
            "spot": True,
            "contract": False,
            "base": "SOL",
            "quote": "USDT",
            "taker": 0.001,
            "limits": {"amount": {"min": 0.01}},
        }

    def fetch_balance(self) -> dict[str, Any]:
        return {k: {"free": float(v), "total": float(v)} for k, v in self.balances.items()}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        step = Decimal("0.01")
        return str((Decimal(str(amount)) / step).to_integral_value(rounding="ROUND_DOWN") * step)

    def set_leverage(self, *args: Any) -> None:
        raise AssertionError("a spot order never sets leverage")

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self.open_orders)

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        self.open_orders = [o for o in self.open_orders if o["id"] != order_id]
        return {"id": order_id}

    def create_order(
        self,
        symbol: str,
        kind: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.orders.append(
            {
                "symbol": symbol,
                "type": kind,
                "side": side,
                "amount": amount,
                "price": price,
                "params": dict(params or {}),
            }
        )
        return {"id": f"o-{len(self.orders)}", "status": "closed", "filled": amount}


def hand(client: SpotVenue, **overrides: Any) -> VenueHand:
    settings: dict[str, Any] = {
        "venue": "bybit",
        "symbol": "SOL/USDT",
        "instrument": "SOLUSDT",
        "max_notional": Decimal(1000),
        "keys": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        "client": client,
        "leverage": Decimal(3),
        "markets": ("linear perpetual", "spot"),
    }
    settings.update(overrides)
    return VenueHand(**settings)


def test_a_spot_long_is_paid_in_full_with_its_exits_attached_and_no_leverage() -> None:
    venue = SpotVenue()
    placed = hand(venue)(dict(SPOT_LONG))
    assert placed["sent"] is True, placed
    sent = venue.orders[0]
    assert sent["side"] == "buy" and sent["amount"] == 2.0 and "reduceOnly" not in sent["params"]
    assert sent["params"]["stopLoss"] == {"triggerPrice": 145.0}
    assert sent["params"]["takeProfit"] == {"triggerPrice": 160.0}
    # 300 of notional and a 10 bps fee, against half of 1000 free.
    assert placed["capital"] == {"cash_needed": "300.30", "free_cash": "1000.0", "equity": "1000.0"}
    assert "contracts" not in placed


def test_a_spot_market_is_never_sold_short() -> None:
    venue = SpotVenue()
    placed = hand(venue)({**SPOT_LONG, "target": "short", "stop_price": "155", "take_price": "140"})
    assert placed["sent"] is False and "needs a borrow this desk does not take" in placed["refused"]
    assert venue.orders == []


def test_an_order_the_free_cash_cannot_pay_for_is_refused() -> None:
    venue = SpotVenue(usdt="500")
    placed = hand(venue)(dict(SPOT_LONG))
    assert placed["sent"] is False
    assert (
        "costs 300.30 USDT with its fee and 0.5 of the free 500.00 is 250.00" in placed["refused"]
    )


def test_the_holding_is_the_balance_and_residue_under_the_minimum_is_flat() -> None:
    assert hand(SpotVenue(sol="0.004")).position() == 0
    assert hand(SpotVenue(sol="2.5")).position() == Decimal("2.5")
    held = SpotVenue(sol="2.5")
    placed = hand(held)(dict(SPOT_LONG))
    assert placed["sent"] is False and "already holds 2.5 of SOL/USDT" in placed["refused"]


def test_a_spot_close_frees_the_coins_from_their_exits_and_sells_what_is_held() -> None:
    venue = SpotVenue(sol="1.998", resting=2)
    closed = hand(venue).close_position("held past its limit")
    assert venue.cancelled == ["exit-0", "exit-1"]
    sale = venue.orders[0]
    assert sale["side"] == "sell" and sale["type"] == "market" and sale["params"] == {}
    # Down to the step, never above the balance: 1.998 sells as 1.99.
    assert sale["amount"] == 1.99 and closed["closed"] is True and closed["size"] == "1.99"


def test_the_derivatives_account_checks_do_not_refuse_a_spot_desk() -> None:
    class SpotModeOkx(SpotVenue):
        def privateGetAccountConfig(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
            return {"data": [{"acctLv": "1"}]}

    assert hand(SpotModeOkx(), venue="okx").ready() == ""


@pytest.mark.parametrize(
    ("fee", "expected"),
    [
        ({"cost": 0.002, "currency": "SOL"}, Decimal("0.300")),
        ({"cost": 0.3, "currency": "USDT"}, Decimal("0.3")),
    ],
)
def test_a_fee_charged_in_the_coin_bought_is_counted_in_the_quote(
    fee: dict[str, Any], expected: Decimal
) -> None:
    fill = {"price": 150.0, "amount": 2.0, "fee": fee}
    assert _fee_in_quote(fill, "SOL") == expected


def test_attached_exits_kept_as_algo_orders_are_cancelled_before_a_spot_sale() -> None:
    class OkxSpot(SpotVenue):
        """OKX keeps an attached stop and take profit as one OCO algo order, listed by kind."""

        def __init__(self) -> None:
            super().__init__(sol="0.286")
            self.algos = [{"id": "algo-1"}]
            self.cancel_params: list[dict[str, Any]] = []

        def fetch_open_orders(
            self,
            symbol: str,
            since: int | None = None,
            limit: int | None = None,
            params: dict[str, Any] | None = None,
        ) -> list[dict[str, Any]]:
            if (params or {}).get("ordType") == "oco":
                return list(self.algos)
            return [] if params else list(self.open_orders)

        def cancel_order(
            self, order_id: str, symbol: str, params: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            self.cancelled.append(order_id)
            self.cancel_params.append(dict(params or {}))
            self.algos = [a for a in self.algos if a["id"] != order_id]
            return {"id": order_id}

    venue = OkxSpot()
    closed = hand(venue, venue="okx").close_position("held past its limit")
    assert venue.cancelled == ["algo-1"] and venue.cancel_params == [{"trigger": True}]
    assert closed["closed"] is True and venue.orders[0]["amount"] == 0.28
