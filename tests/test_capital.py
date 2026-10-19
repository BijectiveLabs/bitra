"""Capital, orders and positions on the venue path: margin, leverage, liquidation, time, kill."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.trading.capital import (
    Account,
    CapitalError,
    day_loss_bps,
    liquidation_estimate,
    margin_needed,
    read_account,
    sizing_equity,
)
from trading_research_agents.trading.dispatch import OrderRate, VenueHand
from trading_research_agents.trading.positions import ROUND_TRIPS, Keeper, finished_orders

LONG: dict[str, Any] = {
    "target": "long",
    "size": "0.01",
    "mid": "100000",
    "bid": "99999",
    "ask": "100001",
    "stop_price": "99000",
    "take_price": "102000",
    "at": "2099-01-01T00-00-00Z",  # never refused by the age check, which has its own test
    "instrument": "BTCUSDT",
}


class Venue:
    """A ccxt-shaped account with leverage, balance, tiers and fills."""

    def __init__(
        self,
        *,
        free: str = "10000",
        leverage: str = "10",
        settable: bool = True,
        position: str = "0",
        fills: list[dict[str, Any]] | None = None,
    ) -> None:
        self.has = {"createOrderWithTakeProfitAndStopLoss": True}
        self.free, self.leverage, self.settable = Decimal(free), Decimal(leverage), settable
        self.held = Decimal(position)
        self.orders: list[dict[str, Any]] = []
        self.fills = fills or []
        self.cancelled: list[str] = []

    def market(self, symbol: str) -> dict[str, Any]:
        return {"settle": "USDT", "taker": 0.00055, "limits": {"amount": {"min": None}}}

    def fetch_balance(self) -> dict[str, Any]:
        return {"USDT": {"total": float(self.free), "free": float(self.free), "used": 0.0}}

    def fetch_positions(self) -> list[dict[str, Any]]:
        return [{"settle": "USDT", "unrealizedPnl": 12.5}] if self.held else []

    def fetch_position(self, symbol: str) -> dict[str, Any]:
        side = "long" if self.held > 0 else "short"
        return {"contracts": float(abs(self.held)), "side": side} if self.held else {}

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return []

    def set_margin_mode(self, mode: str, symbol: str) -> None:
        return None

    def set_leverage(self, leverage: float, symbol: str) -> None:
        if not self.settable:
            raise RuntimeError("leverage cannot be changed while a position is open")
        if Decimal(str(leverage)) == self.leverage:
            raise RuntimeError("leverage not modified")
        self.leverage = Decimal(str(leverage))

    def fetch_leverage(self, symbol: str) -> dict[str, Any]:
        return {"longLeverage": float(self.leverage)}

    def fetch_market_leverage_tiers(self, symbol: str) -> list[dict[str, Any]]:
        return [{"maintenanceMarginRate": 0.005}]

    def create_order(
        self,
        symbol: str,
        kind: str,
        side: str,
        amount: float,
        price: Any,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.orders.append(
            {
                "type": kind,
                "side": side,
                "amount": amount,
                "price": price,
                "params": dict(params or {}),
            }
        )
        if (params or {}).get("reduceOnly"):
            self.held = Decimal(0)
        return {"id": f"order-{len(self.orders)}", "status": "closed", "filled": amount}

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self.cancelled.append(order_id)

    def fetch_my_trades(self, symbol: str, since: int | None = None) -> list[dict[str, Any]]:
        return self.fills


def hand(venue: Venue, **overrides: Any) -> VenueHand:
    settings: dict[str, Any] = {
        "venue": "acme",
        "symbol": "BTC/USDT:USDT",
        "instrument": "BTCUSDT",
        "max_notional": Decimal(5000),
        "keys": ("ACME_API_KEY", "ACME_API_SECRET"),
        "client": venue,
        "leverage": Decimal(3),
    }
    settings.update(overrides)
    return VenueHand(**settings)


def test_the_arithmetic_of_capital_is_checkable_by_hand() -> None:
    assert sizing_equity(Decimal(10000), Decimal(44836)) == Decimal(10000)
    assert sizing_equity(Decimal(50000), Decimal(44836)) == Decimal(44836)
    assert sizing_equity(None, Decimal(44836)) == Decimal(44836)
    assert margin_needed(Decimal(1000), Decimal(4), Decimal("5.5")) == Decimal("250.55")
    # Isolated at 10x with a 0.5% maintenance rate: liquidated 9.5% away.
    assert liquidation_estimate(Decimal(100), "buy", Decimal(10), Decimal("0.005")) == Decimal(
        "90.500"
    )
    assert liquidation_estimate(Decimal(100), "sell", Decimal(10), Decimal("0.005")) == Decimal(
        "109.500"
    )
    with pytest.raises(CapitalError):
        margin_needed(Decimal(1), Decimal(0))


def test_equity_counts_what_open_positions_are_worth() -> None:
    account = read_account(Venue(position="0.01"), "USDT")
    assert account == Account("USDT", Decimal(10000), Decimal(10000), Decimal("12.5"))
    assert account.equity == Decimal("10012.5")


def test_the_day_loss_is_measured_from_the_first_reading_of_the_day(tmp_path: Path) -> None:
    morning = datetime(2026, 10, 5, 0, 5, tzinfo=UTC)
    assert day_loss_bps(tmp_path, morning, Decimal(10000)) == Decimal(0)
    assert day_loss_bps(tmp_path, morning + timedelta(hours=6), Decimal(9700)) == Decimal("300.00")
    assert day_loss_bps(tmp_path, morning + timedelta(hours=7), Decimal(10100)) == Decimal(0)
    # A new day starts from its own first reading.
    assert day_loss_bps(tmp_path, morning + timedelta(days=1), Decimal(9700)) == Decimal(0)


def test_leverage_is_applied_and_read_back_before_the_first_order() -> None:
    venue = Venue(leverage="10")
    placed = hand(venue)(LONG)
    assert placed["sent"] is True and venue.leverage == Decimal(3)
    assert placed["capital"]["leverage"] == "3" and placed["capital"]["margin_mode"] == "isolated"
    # A venue that already holds the declared value answers "not modified", and that is fine.
    again = Venue(leverage="3")
    assert hand(again)(LONG)["sent"] is True


def test_a_leverage_the_venue_will_not_take_refuses_the_order() -> None:
    venue = Venue(leverage="10", settable=False)
    placed = hand(venue)(LONG)
    assert placed["sent"] is False and "at 10x at the venue" in placed["refused"]
    assert venue.orders == []


def test_an_order_the_free_margin_cannot_carry_is_refused() -> None:
    # 0.01 BTC at 100000 is 1000 of notional, 333.33 of margin at 3x; half of 500 is 250.
    venue = Venue(free="500")
    placed = hand(venue)(LONG)
    assert placed["sent"] is False and "needs 333.88 USDT of margin" in placed["refused"]


def test_a_stop_past_liquidation_is_refused() -> None:
    # At 80x liquidation is 0.75% away, inside a stop 1% away: a stop past liquidation.
    placed = hand(Venue(leverage="80"), leverage=Decimal(80))(LONG)
    assert placed["sent"] is False and "liquidation is estimated" in placed["refused"]


def test_the_desk_order_rate_is_shared_across_hands() -> None:
    rate = OrderRate(per_minute=1)
    assert hand(Venue(), rate=rate)(LONG)["sent"] is True
    second = hand(Venue(), rate=rate)({**LONG, "instrument": "BTCUSDT"})
    assert second["sent"] is False and "max_orders_per_minute is 1" in second["refused"]


def entry_cycle(root: Path, name: str, *, filled: str = "0.002", horizon: int = 5) -> None:
    where = root / name
    where.mkdir(parents=True)
    intent = {
        "instrument": "BTCUSDT",
        "target": "long",
        "size": "0.002",
        "mid": "85299",
        "horizon_bars": horizon,
        "risk_bps": "1.71",
    }
    (where / "intents.jsonl").write_text(json.dumps(intent) + "\n", encoding="utf-8")
    placed = {
        "sent": True,
        "filled": filled,
        "average": "85298.9",
        "order_id": "entry-1",
        "stop_price": "84446.3",
        "take_price": "87005.2",
    }
    (where / "placed.json").write_text(json.dumps(placed), encoding="utf-8")


def keeper(root: Path, venue: Venue, *, hold_limit: str = "timeout", at: datetime) -> Keeper:
    return Keeper(
        root=root,
        hand_for=lambda name: hand(venue),
        instruments=("BTCUSDT",),
        bar_interval_s=60,
        timeout_bars=240,
        hold_limit=hold_limit,
        clock=lambda: at,
    )


def test_a_position_past_its_hold_limit_is_closed_reduce_only(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-04T17-55-13Z-BTCUSDT")
    venue = Venue(
        position="0.002",
        fills=[
            {"side": "buy", "amount": 0.002, "price": 85298.9, "fee": {"cost": 0.034}},
            {"side": "sell", "amount": 0.002, "price": 86400.0, "fee": {"cost": 0.095}},
        ],
    )
    said = keeper(tmp_path, venue, at=datetime(2026, 10, 5, 3, 0, tzinfo=UTC)).run()
    close = venue.orders[-1]
    assert close["type"] == "market" and close["params"] == {"reduceOnly": True}
    assert close["side"] == "sell" and close["amount"] == 0.002
    assert "past its timeout limit of 4h 00m" in said[0]
    trip = json.loads((tmp_path / ROUND_TRIPS).read_text("utf-8").splitlines()[-1])
    assert trip["closed_by"] == "time" and trip["net"] == "2.0732"
    # Recorded once: the next pass leaves it alone.
    assert "entry-1" in finished_orders(tmp_path)
    assert keeper(tmp_path, venue, at=datetime(2026, 10, 5, 3, 5, tzinfo=UTC)).run() == []


def test_horizon_and_none_change_when_time_closes_a_position(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-05T02-00-00Z-BTCUSDT", horizon=5)
    ten_past = datetime(2026, 10, 5, 2, 10, tzinfo=UTC)
    venue = Venue(position="0.002")
    assert keeper(tmp_path, venue, hold_limit="timeout", at=ten_past).run() == []
    assert keeper(tmp_path, venue, hold_limit="none", at=ten_past).run() == []
    said = keeper(tmp_path, venue, hold_limit="horizon", at=ten_past).run()
    assert "past its horizon limit of 5m" in said[0] and venue.held == 0


def test_a_position_the_venue_closed_is_recorded_with_its_result(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-04T17-55-13Z-BTCUSDT")
    venue = Venue(
        position="0",
        fills=[
            {"side": "buy", "amount": 0.002, "price": 85298.9, "fee": {"cost": 0.034}},
            {"side": "sell", "amount": 0.002, "price": 87005.2, "fee": {"cost": 0.096}},
        ],
    )
    said = keeper(tmp_path, venue, at=datetime(2026, 10, 5, 3, 0, tzinfo=UTC)).run()
    assert "was closed at the venue; exit 87005.2000, net 3.2826 USDT" in said[0]
    assert "(192.4 bps of the entry notional) after 0.1300 USDT fees" in said[0]
    assert venue.orders == []


class ContractVenue(Venue):
    """The same account on a venue where one contract is 0.01 of the base."""

    def market(self, symbol: str) -> dict[str, Any]:
        return {**super().market(symbol), "contractSize": "0.01", "contract": True}


def test_fills_reported_in_contracts_are_written_up_in_the_base(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-04T17-55-13Z-BTCUSDT")
    # 0.2 contracts of 0.01 is the 0.002 the entry holds.
    venue = ContractVenue(
        position="0",
        fills=[
            {"side": "buy", "amount": 0.2, "price": 85298.9, "fee": {"cost": 0.034}},
            {"side": "sell", "amount": 0.2, "price": 87005.2, "fee": {"cost": 0.096}},
        ],
    )
    said = keeper(tmp_path, venue, at=datetime(2026, 10, 5, 3, 0, tzinfo=UTC)).run()
    assert "net 3.2826 USDT" in said[0]


def test_the_kill_switch_flattens_and_cancels(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-05T02-00-00Z-BTCUSDT")
    venue = Venue(position="0.002")
    said = keeper(tmp_path, venue, at=datetime(2026, 10, 5, 2, 1, tzinfo=UTC)).run(kill=True)
    assert venue.held == 0 and "flattened for the kill switch" in said[0]
    journal = (tmp_path / "positions.jsonl").read_text("utf-8")
    assert '"event": "flattened"' in journal


def test_a_round_trip_written_before_its_exit_fills_is_finished_later(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-04T17-55-13Z-BTCUSDT")
    venue = Venue(position="0.002")
    late = keeper(tmp_path, venue, at=datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    late.hand_for = lambda name: hand(venue, sleep=lambda _s: None)
    late.run()
    first = (tmp_path / ROUND_TRIPS).read_text("utf-8").splitlines()
    assert len(first) == 1 and "no exit fills" in first[0]
    # The exit shows up at the venue; the next pass finishes the trip with one more line.
    venue.fills = [{"side": "sell", "amount": 0.002, "price": 86052.2, "fee": {"cost": 0.0947}}]
    said = late.run()
    assert any("round trip completed" in line for line in said)
    rows = (tmp_path / ROUND_TRIPS).read_text("utf-8").splitlines()
    assert len(rows) == 2 and '"net": "1.4119"' in rows[-1]
    assert late.run() == []


def test_the_next_position_s_fills_are_not_counted_against_this_one(tmp_path: Path) -> None:
    entry_cycle(tmp_path, "2026-10-04T17-55-13Z-BTCUSDT")
    venue = Venue(
        position="0",
        fills=[
            {
                "timestamp": 1,
                "side": "buy",
                "amount": 0.002,
                "price": 85298.9,
                "fee": {"cost": 0.034},
            },
            {
                "timestamp": 2,
                "side": "sell",
                "amount": 0.002,
                "price": 86052.2,
                "fee": {"cost": 0.095},
            },
            # The next entry, a minute after the close: its fee is not this trade's.
            {
                "timestamp": 3,
                "side": "buy",
                "amount": 0.002,
                "price": 86073.1,
                "fee": {"cost": 0.095},
            },
        ],
    )
    keeper(tmp_path, venue, at=datetime(2026, 10, 5, 4, 0, tzinfo=UTC)).run()
    trip = json.loads((tmp_path / ROUND_TRIPS).read_text("utf-8").splitlines()[-1])
    assert trip["fees"] == "0.1290" and trip["net"] == "1.3776"


def test_an_entry_never_confirmed_as_filled_is_not_written_up_as_a_trade(tmp_path: Path) -> None:
    where = tmp_path / "2026-10-04T15-30-17Z-SOLUSDT"
    where.mkdir()
    intent = {"instrument": "BTCUSDT", "target": "long", "size": "0.002", "mid": "85000"}
    (where / "intents.jsonl").write_text(json.dumps(intent) + "\n", encoding="utf-8")
    placed = {"sent": True, "filled": None, "cancelled": False, "order_id": "resting-1"}
    (where / "placed.json").write_text(json.dumps(placed), encoding="utf-8")
    flat = Venue(position="0")
    assert keeper(tmp_path, flat, at=datetime(2026, 10, 5, 4, 0, tzinfo=UTC)).run() == []
    assert not (tmp_path / ROUND_TRIPS).exists()
    # Held at the venue, though, it is a position like any other and its hold limit applies.
    held = Venue(position="0.002")
    said = keeper(tmp_path, held, at=datetime(2026, 10, 5, 4, 0, tzinfo=UTC)).run()
    assert "past its timeout limit" in said[0]


def test_a_position_on_an_instrument_no_longer_listed_is_still_looked_after(
    tmp_path: Path,
) -> None:
    # Discovery took BTCUSDT on an earlier pass; this pass lists other names only.
    entry_cycle(tmp_path, "2026-10-04T17-55-13Z-BTCUSDT")
    venue = Venue(position="0.002")
    at = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
    blind = Keeper(
        root=tmp_path,
        hand_for=lambda name: hand(venue),
        instruments=("ZECUSDT",),
        bar_interval_s=60,
        timeout_bars=240,
        clock=lambda: at,
    )
    assert not any("BTCUSDT" in line for line in blind.run())
    following = Keeper(
        root=tmp_path,
        hand_for=lambda name: hand(venue),
        instruments=("ZECUSDT",),
        bar_interval_s=60,
        timeout_bars=240,
        clock=lambda: at,
        follow_records=True,
    )
    assert any("past its timeout limit" in line for line in following.run())


class SeparateVenue(Venue):
    """Takes no stop with the entry, only as orders of their own, as Binance futures do."""

    def __init__(
        self, *, refuse: str = "", resting: list[dict[str, Any]] | None = None, stuck: bool = False
    ) -> None:
        super().__init__()
        self.has = {"createStopLossOrder": True, "createTakeProfitOrder": True}
        self.refuse, self.resting, self.stuck = refuse, list(resting or []), stuck
        self.cancel_params: list[dict[str, Any]] = []

    def create_order(
        self,
        symbol: str,
        kind: str,
        side: str,
        amount: float,
        price: Any,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        said = dict(params or {})
        if self.refuse and self.refuse in said:
            raise RuntimeError("the venue said no")
        if "stopLossPrice" in said or "takeProfitPrice" in said:
            self.orders.append({"type": kind, "side": side, "amount": amount, "params": said})
            return {"id": f"exit-{len(self.orders)}"}
        placed = super().create_order(symbol, kind, side, amount, price, params)
        if not said.get("reduceOnly"):
            self.held = Decimal(str(amount)) * (1 if side == "buy" else -1)
        return placed

    def fetch_open_orders(
        self, symbol: str, since: Any = None, limit: Any = None, params: Any = None
    ) -> list[dict[str, Any]]:
        return list(self.resting) if (params or {}).get("trigger") else []

    def cancel_order(self, order_id: str, symbol: str, params: Any = None) -> None:
        if self.stuck:
            raise RuntimeError("unknown order")
        self.cancelled.append(order_id)
        self.cancel_params.append(dict(params or {}))


def separate(client: SeparateVenue, venue: str = "binance") -> VenueHand:
    # A venue whose resting exits are read: separate exits refuse anywhere else.
    built = hand(client, exits="separate")
    built.venue = venue
    return built


def test_separate_exits_follow_the_fill_as_reduce_only_orders() -> None:
    venue = SeparateVenue()
    placed = separate(venue)(LONG)
    assert placed["sent"] is True and set(placed["exit_orders"]) == {"stop", "take"}
    entry, stop, take = venue.orders[-3:]
    assert "stopLoss" not in entry["params"] and "takeProfit" not in entry["params"]
    assert (stop["side"], stop["amount"], stop["params"]["stopLossPrice"]) == (
        "sell",
        0.01,
        99000.0,
    )
    assert (take["side"], take["params"]["takeProfitPrice"]) == ("sell", 102000.0)
    assert stop["params"]["reduceOnly"] and take["params"]["reduceOnly"]
    assert stop["params"]["clientOrderId"] != take["params"]["clientOrderId"]


def test_a_fill_whose_exit_cannot_be_placed_is_closed_at_once() -> None:
    venue = SeparateVenue(refuse="takeProfitPrice")
    placed = separate(venue)(LONG)
    assert (
        placed["exit_orders"] is None and "the take could not be placed" in placed["exits_refused"]
    )
    # The stop that did go out comes off, and the position goes with a reduce-only close.
    assert placed["closed_unprotected"]["closed"] is True and venue.held == 0
    assert "exit-2" in venue.cancelled
    assert venue.orders[-1]["params"] == {"reduceOnly": True}


def test_an_exit_left_from_an_earlier_position_comes_off_before_the_entry() -> None:
    venue = SeparateVenue(resting=[{"id": "old-take"}])
    placed = separate(venue)(LONG)
    assert placed["sent"] is True and placed["earlier_exits_cancelled"] == 1
    assert venue.cancelled[0] == "old-take" and venue.cancel_params[0] == {"trigger": True}

    stuck = SeparateVenue(resting=[{"id": "old-take"}], stuck=True)
    refused = separate(stuck)(LONG)
    assert refused["sent"] is False and "would not come off" in refused["refused"]
    assert stuck.orders == []


def test_a_venue_that_cannot_attach_points_to_separate_exits() -> None:
    placed = hand(SeparateVenue())(LONG)
    assert placed["sent"] is False and 'exits = "separate"' in placed["refused"]
    # Where resting exits are not read yet, separate exits are refused rather than left to chance.
    unread = separate(SeparateVenue(), venue="acme")(LONG)
    assert unread["sent"] is False and "not read here yet" in unread["refused"]


def test_the_mandate_takes_separate_exits() -> None:
    from trading_research_agents.execution.mandate import Mandate, MandateError

    assert Mandate(exits="separate").exits == "separate"
    with pytest.raises(MandateError, match="separate"):
        Mandate(exits="later")


class NoLeverageRead(Venue):
    """Applies leverage but has no way through ccxt to report it back, as Hyperliquid does."""

    fetch_leverage = None  # type: ignore[assignment]  # the read is absent, not failing


def test_leverage_the_venue_cannot_report_is_confirmed_by_its_reply() -> None:
    venue = NoLeverageRead(leverage="10")
    placed = hand(venue)(LONG)
    assert placed["sent"] is True and venue.leverage == Decimal(3)
    assert placed["leverage_confirmed"] == "by the venue's reply"
    # A reply that is an error confirms nothing, and with nothing to read the order is refused.
    stuck = NoLeverageRead(leverage="10", settable=False)
    refused = hand(stuck)(LONG)
    assert refused["sent"] is False and "would not confirm the leverage" in refused["refused"]


def test_order_status_is_found_in_the_lists_when_no_single_read_exists() -> None:
    class ListsOnly(Venue):
        def __init__(self) -> None:
            super().__init__()
            self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}

        fetch_order = None

        def fetch_closed_orders(self, symbol: str) -> list[dict[str, Any]]:
            return [{"id": "other", "status": "closed"}, {"id": "mine", "status": "closed"}]

    found = hand(ListsOnly()).order_status("mine")
    assert found is not None and found["status"] == "closed"
    assert hand(ListsOnly()).order_status("gone") is None


def test_every_credential_a_venue_signs_with_is_asked_for_by_name() -> None:
    from trading_research_agents.trading.dispatch import credentials_for

    class Orderly:
        requiredCredentials = {"apiKey": True, "secret": True, "accountId": True}  # noqa: N815

    asked = credentials_for("somedex", Orderly())
    assert asked == {
        "apiKey": "SOMEDEX_API_KEY",
        "secret": "SOMEDEX_API_SECRET",
        "accountId": "SOMEDEX_ACCOUNT_ID",
    }


def test_a_margin_mode_the_venue_would_not_change_refuses_and_says_why() -> None:
    class CrossOnly(Venue):
        """A key without position permissions: the mode stays cross, as a Bitget demo key did."""

        def set_margin_mode(self, mode: str, symbol: str) -> None:
            raise RuntimeError("Incorrect permissions, need future pos write permissions")

        def fetch_leverage(self, symbol: str) -> dict[str, Any]:
            return {"longLeverage": float(self.leverage), "marginMode": "cross"}

    placed = hand(CrossOnly(leverage="3"))(LONG)
    assert placed["sent"] is False
    assert "in cross margin at the venue" in placed["refused"]
    assert "need future pos write permissions" in placed["refused"]
