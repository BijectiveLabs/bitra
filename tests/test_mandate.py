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
from typing import Any

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

# What a cycle hands the venue: the size the conviction rule chose, the touch it priced on, and the
# exits code computed from [exit]. Everything the hand needs, and nothing a model wrote.
LONG: dict[str, Any] = {
    "target": "long",
    "size": "0.01",
    "mid": "100000",
    "stop_price": "99000",
    "take_price": "102000",
    "at": "2026-09-28T02-55-40Z",
    "instrument": "BTCUSDT",
}

SIGNED = Mandate(
    mode="venue",
    venue="bybit",
    acknowledged_by="the operator",
    acknowledged_on="2026-09-28",
    max_notional=Decimal(200),
)


class FakeExchange:
    """ccxt 4.5.84's own shapes: create_order, fetch_order, cancel_order, fetch_position."""

    def __init__(
        self,
        answer: object = None,
        *,
        position: object = None,
        statuses: list[dict[str, Any]] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        attaches: bool = True,
    ) -> None:
        self.has = {"createOrderWithTakeProfitAndStopLoss": attaches}
        self.open_orders = open_orders or []
        self.answer = answer or {
            "id": "order-1",
            "status": "closed",
            "filled": 0.01,
            "average": 100000.5,
        }
        self.position = position
        self.statuses = statuses or []
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.sandbox: bool | None = None

    def set_sandbox_mode(self, on: bool) -> None:
        self.sandbox = on

    def create_order(
        self,
        symbol: str,
        kind: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> object:
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
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self.open_orders)

    def fetch_position(self, symbol: str) -> object:
        if isinstance(self.position, Exception):
            raise self.position
        return self.position

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self.statuses.pop(0) if self.statuses else {"status": "open"}

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "canceled"}


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

    placed = hand(LONG)
    assert placed["sent"] is True and placed["order_id"] == "order-1"
    assert placed["side"] == "buy" and placed["notional"] == "1000.00" and placed["role"] == "taker"
    sent = venue.orders[0]
    assert sent["type"] == "market" and sent["side"] == "buy" and sent["price"] is None
    # The exits go out with the entry, so a fill is never left unattended.
    assert sent["params"]["stopLoss"] == {"triggerPrice": 99000.0}
    assert sent["params"]["takeProfit"] == {"triggerPrice": 102000.0}
    assert placed["stop_price"] == "99000" and placed["exits"] == "attached"
    # One client id per cycle and instrument: the same cycle cannot open two positions.
    assert sent["params"]["clientOrderId"] == "tra-2026-09-28T02-55-40Z-BTCUSDT"
    assert placed["client_order_id"] == sent["params"]["clientOrderId"]

    # A short's exits sit the other way round, and the hand checks that they do.
    short = hand({**LONG, "target": "short", "stop_price": "101000", "take_price": "98000"})
    assert short["side"] == "sell"
    assert venue.orders[-1]["params"]["stopLoss"] == {"triggerPrice": 101000.0}


def test_a_maker_channel_rests_on_the_book_and_never_crosses_it() -> None:
    venue = FakeExchange(statuses=[{"status": "closed", "filled": 0.01, "average": 99999.0}])
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        role="maker",
        client=venue,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )

    placed = hand({**LONG, "bid": "99999", "ask": "100001"})
    sent = venue.orders[0]
    assert sent["type"] == "limit" and sent["price"] == 99999.0
    assert sent["params"]["postOnly"] is True and sent["params"]["timeInForce"] == "GTC"
    assert placed["filled"] == "0.01" and placed["cancelled"] is False


def test_a_resting_order_that_does_not_fill_is_cancelled_rather_than_chased() -> None:
    venue = FakeExchange()
    ticks = iter([0.0, 0.0, 10.0, 40.0, 40.0])
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        role="maker",
        limit_timeout_s=30.0,
        client=venue,
        clock=lambda: next(ticks),
        sleep=lambda _s: None,
    )

    placed = hand({**LONG, "bid": "99999"})
    assert placed["cancelled"] is True and venue.cancelled == ["order-1"]
    assert "did not fill" in placed["note"]


def test_a_maker_order_with_no_book_side_is_not_sent() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        role="maker",
        client=venue,
    )
    refused = hand(LONG)

    assert refused["sent"] is False and "no book side" in refused["refused"]
    assert venue.orders == []


def test_an_offset_moves_a_resting_order_away_from_the_touch() -> None:
    venue = FakeExchange(statuses=[{"status": "closed", "filled": 0.01}])
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        role="maker",
        price_offset_bps=Decimal(10),
        client=venue,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )
    hand({**LONG, "bid": "100000"})

    # Ten basis points below the bid for a buy: further from a fill, further from crossing.
    assert venue.orders[0]["price"] == 99900.0


def test_an_order_is_not_added_to_a_position_the_desk_did_not_expect() -> None:
    venue = FakeExchange(position={"contracts": 0.5, "side": "short"})
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )

    refused = hand(LONG)
    assert refused["sent"] is False and "the other way" in refused["refused"]
    assert venue.orders == []

    adopting = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        adopt_open_position=True,
        client=venue,
    )
    assert adopting(LONG)["sent"] is True


def test_a_hand_that_cannot_read_the_account_does_not_send() -> None:
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=FakeExchange(position=RuntimeError("permission denied")),
    )
    with pytest.raises(DispatchError, match="cannot read the account"):
        hand(LONG)


def test_a_channel_that_does_not_say_maker_or_taker_is_refused() -> None:
    with pytest.raises(DispatchError, match="maker or taker"):
        VenueHand(
            venue="bybit",
            symbol="BTC/USDT:USDT",
            max_notional=Decimal(100),
            keys=("K", "S"),
            role="undeclared",
            client=FakeExchange(),
        )


def test_the_hand_refuses_before_it_reaches_the_venue() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(500),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )

    big = hand(LONG)
    assert big["sent"] is False and "above [execution].max_notional" in big["refused"]

    flat = hand({**LONG, "target": "flat"})
    assert flat["sent"] is False and "is not an order" in flat["refused"]

    nothing = hand({**LONG, "size": "0"})
    assert nothing["sent"] is False and "size is not positive" in nothing["refused"]

    assert venue.orders == []


def test_nothing_is_sent_without_an_exit_at_the_venue() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )
    bare = hand({**LONG, "stop_price": "", "take_price": ""})

    assert bare["sent"] is False and "no stop and take profit" in bare["refused"]
    assert venue.orders == []

    backwards = hand({**LONG, "stop_price": "103000"})
    assert backwards["sent"] is False and "wrong way round" in backwards["refused"]
    assert venue.orders == []


def test_a_venue_that_cannot_attach_them_is_refused_rather_than_trusted() -> None:
    venue = FakeExchange(attaches=False)
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )
    refused = hand(LONG)

    assert refused["sent"] is False and "cannot take a stop and a take profit" in refused["refused"]
    assert venue.orders == []


def test_an_operator_may_carry_the_exits_themselves_and_it_is_recorded() -> None:
    venue = FakeExchange(attaches=False)
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        exits="none",
        client=venue,
    )
    placed = hand(LONG)

    assert placed["sent"] is True and placed["exits"] == "none"
    assert "stopLoss" not in venue.orders[0]["params"]

    with pytest.raises(DispatchError, match=r"\[execution\].exits is one of"):
        VenueHand(
            venue="bybit",
            symbol="BTC/USDT:USDT",
            max_notional=Decimal(100),
            keys=("K", "S"),
            exits="maybe",
            client=venue,
        )


def test_a_second_entry_on_an_open_position_is_refused_either_way() -> None:
    same = FakeExchange(position={"contracts": 0.5, "side": "long"})
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=same,
    )
    refused = hand(LONG)

    # Adding to a position is a bigger position than the conviction rule sized for.
    assert refused["sent"] is False and "the same way" in refused["refused"]
    assert same.orders == []


def test_an_order_still_working_stops_a_second_decision_on_the_same_position() -> None:
    venue = FakeExchange(open_orders=[{"id": "resting-1"}])
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )
    refused = hand(LONG)

    assert refused["sent"] is False and "still open" in refused["refused"]
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
        hand(LONG)


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
