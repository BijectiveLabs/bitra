"""A unified trading account: the leverage kept per margin mode, read back from where it is kept.

The shapes are Bitget's v3 answers as read on its demo API on 2026-10-20 through ccxt 4.5.85.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_research_agents.trading.dispatch import VenueHand
from trading_research_agents.trading.venues.accounts import account_refusal
from trading_research_agents.trading.venues.margin import BitgetUnified, account_settings, route

LONG: dict[str, Any] = {
    "target": "long",
    "size": "0.01",
    "mid": "100000",
    "bid": "99999",
    "ask": "100001",
    "stop_price": "99000",
    "take_price": "102000",
    "at": "2099-01-01T00-00-00Z",
    "instrument": "BTCUSDT",
}


class Unified:
    """A Bitget client on the unified account: settings rows per symbol and per margin mode."""

    def __init__(self, *, hold: str = "one_way_mode", settable: bool = True) -> None:
        self.has = {"createOrderWithTakeProfitAndStopLoss": True, "fetchLeverage": True}
        self.options: dict[str, Any] = {"uta": True}
        self.hold, self.settable = hold, settable
        self.rows = [{"symbol": "BTCUSDT", "marginMode": "crossed", "leverage": "10"}]
        self.orders: list[dict[str, Any]] = []
        self.held = Decimal(0)
        self.grants = ["uamw", "uatw"]

    def market(self, symbol: str) -> dict[str, Any]:
        return {"id": "BTCUSDT", "settle": "USDT", "taker": 0.0006, "limits": {"amount": {}}}

    def privateUtaGetV3AccountSettings(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
        return {"data": {"holdMode": self.hold, "symbolConfigList": [dict(r) for r in self.rows]}}

    def privateSpotGetV2SpotAccountInfo(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
        return {"data": {"authorities": self.grants}}

    def set_leverage(self, leverage: float, symbol: str, params: dict[str, Any]) -> None:
        if not self.settable:
            raise RuntimeError("leverage cannot be changed while a position is open")
        mode = params["marginMode"]
        self.rows = [r for r in self.rows if r["marginMode"] != mode]
        self.rows.append({"symbol": "BTCUSDT", "marginMode": mode, "leverage": f"{leverage:g}"})

    # ccxt sends these two to the classic futures API, which a unified key cannot reach.
    def set_margin_mode(self, mode: str, symbol: str) -> None:
        raise RuntimeError("40014 need future pos write permissions")

    def fetch_leverage(self, symbol: str) -> dict[str, Any]:
        raise RuntimeError("40014 need future pos read permissions")

    def fetch_balance(self) -> dict[str, Any]:
        return {"USDT": {"total": 50000.0, "free": 50000.0, "used": 0.0}}

    def fetch_positions(self) -> list[dict[str, Any]]:
        return []

    def fetch_position(self, symbol: str) -> dict[str, Any]:
        return {"contracts": float(abs(self.held)), "side": "long"} if self.held else {}

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return []

    def create_order(
        self, symbol: str, kind: str, side: str, amount: float, price: Any, params: Any = None
    ) -> dict[str, Any]:
        self.orders.append({"side": side, "params": dict(params or {})})
        self.held = Decimal(0) if (params or {}).get("reduceOnly") else Decimal(str(amount))
        return {"id": f"o{len(self.orders)}", "status": "closed", "filled": amount, "average": 1}


def hand(client: Unified, **overrides: Any) -> VenueHand:
    settings: dict[str, Any] = {
        "venue": "bitget",
        "symbol": "BTC/USDT:USDT",
        "instrument": "BTCUSDT",
        "max_notional": Decimal(5000),
        "keys": ("BITGET_API_KEY", "BITGET_API_SECRET"),
        "client": client,
        "leverage": Decimal(3),
    }
    settings.update(overrides)
    return VenueHand(**settings)


def test_the_declared_mode_is_set_with_the_leverage_and_read_from_its_own_row() -> None:
    client = Unified()
    placed = hand(client)(LONG)
    assert placed["sent"] is True
    # The crossed row still says 10x; the isolated row the order trades in says 3x.
    assert {r["marginMode"]: r["leverage"] for r in client.rows} == {
        "crossed": "10",
        "isolated": "3",
    }
    assert client.orders[0]["params"]["marginMode"] == "isolated"
    assert placed["capital"]["leverage"] == "3"


def test_a_leverage_that_will_not_change_is_refused_with_the_venues_words() -> None:
    client = Unified(settable=False)
    placed = hand(client)(LONG)
    assert placed["sent"] is False and client.orders == []
    assert "would not confirm the leverage on BTC/USDT:USDT in isolated margin" in placed["refused"]
    assert "position is open" in placed["refused"]


def test_hedge_mode_is_read_from_the_account_and_refused() -> None:
    client = Unified(hold="hedge_mode")
    assert "long/short position mode" in hand(client).ready()
    assert hand(client)(LONG)["sent"] is False
    assert hand(Unified()).hedged() == ""


def test_every_order_on_the_account_names_the_margin_mode_close_included() -> None:
    client = Unified()
    worker = hand(client, margin_mode="cross")
    assert worker(LONG)["sent"] is True
    closed = worker.close_position("time")
    assert closed["closed"] is True
    assert [o["params"]["marginMode"] for o in client.orders] == ["crossed", "crossed"]
    assert client.orders[1]["params"]["reduceOnly"] is True


def test_a_unified_key_is_routed_and_an_operator_setting_is_not_overruled() -> None:
    client = Unified()
    client.options = {"uta": None}
    route("bitget", client)
    assert client.options["uta"] is True and isinstance(
        account_settings("bitget", client), BitgetUnified
    )
    classic = Unified()
    classic.options, classic.grants = {"uta": None}, ["uamw", "futures"]
    route("bitget", classic)
    assert classic.options["uta"] is None and account_settings("bitget", classic) is None
    forced = Unified()
    forced.options = {"uta": False}
    route("bitget", forced)
    assert forced.options["uta"] is False
    # Sent to the classic API on purpose, a unified key cannot set anything there: refused.
    assert "uta = false" in account_refusal("bitget", forced)
    assert account_refusal("bitget", Unified()) == ""
    assert account_settings("bybit", Unified()) is None


def test_a_fee_ccxt_signs_as_a_rebate_is_counted_as_the_venue_charged_it() -> None:
    from trading_research_agents.trading.round_trip import _fee_in_quote

    # As read on 2026-10-20: Bitget charged six basis points; ccxt said -0.0732375.
    fill = {
        "takerOrMaker": "taker",
        "price": 81375.0,
        "fee": {"currency": "USDT", "cost": -0.0732375},
        "info": {"execId": "1", "feeDetail": [{"feeCoin": "USDT", "fee": "0.0732375"}]},
    }
    assert _fee_in_quote(fill, "BTC", "bitget") == Decimal("0.0732375")
    # Anywhere else, a taker is never paid to take: the sign is read the wrong way.
    assert _fee_in_quote({**fill, "info": {}}, "BTC", "acme") == Decimal("0.0732375")
    maker = {"takerOrMaker": "maker", "fee": {"currency": "USDT", "cost": -0.01}}
    assert _fee_in_quote(maker, "BTC", "acme") == Decimal("-0.01")


def test_a_fill_reported_without_a_price_is_priced_from_its_own_fills() -> None:
    class Bare(Unified):
        def create_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            placed = super().create_order(*args, **kwargs)
            return {**placed, "average": None}

        def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            return {"id": order_id, "status": "closed", "filled": 0.01, "average": None}

        def fetch_my_trades(self, symbol: str, *args: Any) -> list[dict[str, Any]]:
            return [
                {"order": "o1", "amount": 0.004, "price": 100001.0},
                {"order": "o1", "amount": 0.006, "price": 100002.0},
                {"order": "other", "amount": 1.0, "price": 1.0},
            ]

    placed = hand(Bare())(LONG)
    assert placed["average"] == "100001.6" and placed["slippage_bps"] == "0.16"
