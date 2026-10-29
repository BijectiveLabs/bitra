"""What this software is allowed to do with an order, and who said so.

Venue mode is the only mode that can lose money, and it is the only one with a door in front of it.
These tests pin the door: the text has to be in the operator's own project word for word, the
signature has to be there, the ladder has to be at its canary tier, the caps have to be written
down, and the keys have to be in the environment. Every missing piece is named, not summarised.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from approvals import approved
from trading_research_agents.execution.mandate import (
    DISCLAIMER,
    MANDATE_FILE,
    MODES,
    Mandate,
    MandateError,
    blocking,
    digest,
    signed_in,
)
from trading_research_agents.trading.dispatch import VenueHand, hand_for
from trading_research_agents.trading.hand_state import DispatchError

# What a cycle hands the venue: the size the conviction rule chose, the touch it priced on, and the
# exits code computed from [exit]. Everything the hand needs, and nothing a model wrote.
# A cycle id the age check never refuses, however long the suite runs; the age has its own test.
NOW_ID = "2099-01-01T00-00-00Z"
LONG: dict[str, Any] = {
    "target": "long",
    "size": "0.01",
    "mid": "100000",
    "stop_price": "99000",
    "take_price": "102000",
    "at": NOW_ID,
    "instrument": "BTCUSDT",
    "bid": "99999",
    "ask": "100001",
}

SIGNED = Mandate(
    mode="venue",
    venue="bybit",
    acknowledged_by="the operator",
    acknowledged_on="2026-09-28",
    max_notional=Decimal(200),
    leverage=Decimal(3),
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
    # A demo or testnet API is where the ladder's evidence is earned, so it does not wait on it.
    practice = blocking(Mandate(mode="venue"), project=tmp_path, tier="SHADOW", practice=True)
    assert not any("ladder" in s for s in practice)


def test_a_signed_project_at_the_canary_tier_may_run(tmp_path: Path) -> None:
    project = project_with_mandate(tmp_path)
    assert blocking(SIGNED, project=project, tier="CANARY", missing_keys=()) == ()


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
        missing_keys=("BYBIT_API_SECRET",),
    )
    assert stops and "BYBIT_API_SECRET" in stops[0]


def test_a_mandate_that_cannot_be_applied_is_refused_when_it_is_written() -> None:
    with pytest.raises(MandateError, match="mode is one of"):
        Mandate(mode="fire")
    with pytest.raises(MandateError, match="max_notional is positive"):
        Mandate(max_notional=Decimal(0))
    for count in (0, 3):
        with pytest.raises(MandateError, match="one decision sends at most one order"):
            Mandate(max_orders_per_cycle=count)
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

    placed = hand(approved(LONG))
    assert placed["sent"] is True and placed["order_id"] == "order-1"
    assert placed["side"] == "buy" and placed["notional"] == "1000.00" and placed["role"] == "taker"
    sent = venue.orders[0]
    # A taker entry is a limit IOC 20 bps through the ask, never a naked market order.
    assert sent["type"] == "limit" and sent["side"] == "buy" and sent["price"] == 100201.002
    assert sent["params"]["timeInForce"] == "IOC"
    # The exits go out with the entry, so a fill is never left unattended.
    assert sent["params"]["stopLoss"] == {"triggerPrice": 99000.0}
    assert sent["params"]["takeProfit"] == {"triggerPrice": 102000.0}
    assert placed["stop_price"] == "99000" and placed["exits"] == "attached"
    # One client id per cycle and instrument: the same cycle cannot open two positions.
    assert sent["params"]["clientOrderId"] == "tra" + NOW_ID.replace("-", "") + "BTCUSDT"
    assert placed["client_order_id"] == sent["params"]["clientOrderId"]

    # One decision, one order: the same hand asked again refuses.
    assert "already sent" in hand(approved(LONG))["refused"]

    # A short's exits sit the other way round, and the hand checks that they do.
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )
    short = hand(
        approved({**LONG, "target": "short", "stop_price": "101000", "take_price": "98000"})
    )
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

    placed = hand(approved({**LONG, "bid": "99999", "ask": "100001"}))
    sent = venue.orders[0]
    assert sent["type"] == "limit" and sent["price"] == 99999.0
    assert sent["params"]["postOnly"] is True and sent["params"]["timeInForce"] == "GTC"
    assert placed["filled"] == "0.01" and placed["cancelled"] is False


def test_a_resting_order_that_does_not_fill_is_cancelled_rather_than_chased() -> None:
    venue = FakeExchange()
    # The approval and decision age (twice: before the venue reads and just before the send) and
    # the send are read first, then the wait for the fill.
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 40.0, 40.0])
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

    placed = hand(approved({**LONG, "bid": "99999"}))
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
    refused = hand(approved({k: v for k, v in LONG.items() if k not in ("bid", "ask")}))

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
    hand(approved({**LONG, "bid": "100000"}))

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

    refused = hand(approved(LONG))
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
    assert adopting(approved(LONG))["sent"] is True


def test_a_hand_that_cannot_read_the_account_does_not_send() -> None:
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=FakeExchange(position=RuntimeError("permission denied")),
    )
    with pytest.raises(DispatchError, match="cannot read the account"):
        hand(approved(LONG))


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

    big = hand(approved(LONG))
    assert big["sent"] is False and "above [execution].max_notional" in big["refused"]

    flat = hand(approved({**LONG, "target": "flat"}))
    assert flat["sent"] is False and "is not an order" in flat["refused"]

    nothing = hand(approved({**LONG, "size": "0"}))
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
    bare = hand(approved({**LONG, "stop_price": "", "take_price": ""}))

    assert bare["sent"] is False and "no stop and take profit" in bare["refused"]
    assert venue.orders == []

    backwards = hand(approved({**LONG, "stop_price": "103000"}))
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
    refused = hand(approved(LONG))

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
    placed = hand(approved(LONG))

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
    refused = hand(approved(LONG))

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
    refused = hand(approved(LONG))

    assert refused["sent"] is False and "still open" in refused["refused"]
    assert venue.orders == []


class ExchangeError(Exception):
    """ccxt's root of every answer in which the venue said no."""


class InsufficientFunds(ExchangeError):  # noqa: N818 - named as ccxt names it
    pass


class DuplicateOrderId(ExchangeError):  # noqa: N818 - named as ccxt names it
    pass


class NetworkError(Exception):
    """ccxt's root of every failure in which the venue's answer never arrived."""


class RequestTimeout(NetworkError):  # noqa: N818 - named as ccxt names it
    pass


def sending_hand(venue: FakeExchange) -> VenueHand:
    return VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
        sleep=lambda s: None,
    )


def test_a_venue_that_says_no_is_an_error_and_nothing_is_read_back() -> None:
    venue = FakeExchange(InsufficientFunds("insufficient margin"))
    hand = sending_hand(venue)
    with pytest.raises(DispatchError, match="InsufficientFunds: insufficient margin"):
        hand(approved(LONG))
    assert len(venue.orders) == 1


class Slow(FakeExchange):
    """Takes the order, then the answer is lost on the way back."""

    def __init__(self, error: Exception, *, lands: bool) -> None:
        super().__init__(error)
        self.lands = lands
        self.reads = 0

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        if not self.orders:
            return []
        self.reads += 1
        if not self.lands:
            return []
        sent = self.orders[-1]["params"]["clientOrderId"]
        return [
            {
                "id": "order-9",
                "clientOrderId": sent,
                "status": "closed",
                "filled": 0.01,
                "average": 100000.5,
            }
        ]


def test_a_timeout_is_not_a_failure_and_the_order_is_found_by_its_client_id() -> None:
    venue = Slow(RequestTimeout("bybit did not answer in 10s"), lands=True)
    hand = sending_hand(venue)
    placed = hand(approved(LONG))
    assert placed["sent"] is True and placed["order_id"] == "order-9"
    assert placed["recovered"].startswith("RequestTimeout: bybit did not answer")
    assert len(venue.orders) == 1
    assert hand(approved(LONG))["refused"] == "this hand has already sent its one order"


def test_an_order_not_found_after_a_timeout_is_unknown_never_failed_and_never_resent() -> None:
    venue = Slow(RequestTimeout("bybit did not answer in 10s"), lands=False)
    hand = sending_hand(venue)
    placed = hand(approved(LONG))
    assert placed["sent"] is None and placed["status"] == "UNKNOWN"
    assert placed["client_order_id"] == venue.orders[0]["params"]["clientOrderId"]
    assert "reconciled" in placed["unknown"] and venue.reads == 3
    assert len(venue.orders) == 1 and hand.sent_count == 1


def test_an_unclassified_error_and_a_duplicate_id_are_read_back_not_trusted() -> None:
    for error in (RuntimeError("socket closed"), DuplicateOrderId("order link id exists")):
        venue = Slow(error, lands=True)
        assert sending_hand(venue)(approved(LONG))["sent"] is True


def test_a_hand_needs_a_cap_and_the_keys_come_from_the_environment() -> None:
    with pytest.raises(DispatchError, match="positive notional cap"):
        VenueHand(
            venue="bybit",
            symbol="BTC/USDT:USDT",
            max_notional=Decimal(0),
            keys=("K", "S"),
            client=FakeExchange(),
        )

    with pytest.raises(DispatchError, match="not in the shell and not in the"):
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


class SteppedExchange(FakeExchange):
    """ccxt's own rounding: amount_to_precision truncates to the market's step."""

    def __init__(self, step: str, least: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.step, self.least = Decimal(step), least
        self.loaded = False

    def load_markets(self) -> dict[str, Any]:
        self.loaded = True
        return {}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        steps = (Decimal(str(amount)) / self.step).to_integral_value(rounding="ROUND_DOWN")
        return str(steps * self.step)

    def market(self, symbol: str) -> dict[str, Any]:
        return {"limits": {"amount": {"min": self.least}}}


def _stepped_hand(venue: FakeExchange) -> VenueHand:
    return VenueHand(
        venue="bybit",
        symbol="ETH/USDT:USDT",
        instrument="ETHUSDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )


def test_a_size_off_the_venues_step_is_refused_rather_than_quietly_rounded() -> None:
    venue = SteppedExchange("0.01")
    placed = _stepped_hand(venue)(
        approved({**LONG, "instrument": "ETHUSDT", "size": "0.036", "mid": "2700"})
    )
    # ccxt would have sent 0.03: a different trade from the one priced and written down.
    assert placed["sent"] is False and "would send 0.03" in placed["refused"]
    assert venue.loaded and venue.orders == []


def test_a_size_under_the_venues_minimum_is_refused() -> None:
    venue = SteppedExchange("0.001", least="0.01")
    placed = _stepped_hand(venue)(
        approved({**LONG, "instrument": "ETHUSDT", "size": "0.005", "mid": "2700"})
    )
    assert placed["sent"] is False and "minimum of 0.01" in placed["refused"]
    assert venue.orders == []


def test_a_size_on_the_venues_step_goes_out_as_it_is() -> None:
    venue = SteppedExchange("0.01", least="0.01")
    placed = _stepped_hand(venue)(
        approved({**LONG, "instrument": "ETHUSDT", "size": "0.04", "mid": "2700"})
    )
    assert placed["sent"] is True and venue.orders[0]["amount"] == 0.04


class ContractVenue(SteppedExchange):
    """A venue whose contract is a fraction of the base, the way ccxt reports one."""

    def __init__(self, unit: str, step: str = "1", **kwargs: Any) -> None:
        super().__init__(step, **kwargs)
        self.unit = unit

    def market(self, symbol: str) -> dict[str, Any]:
        return {"contractSize": self.unit, "contract": True, "limits": {"amount": {"min": "1"}}}


def _contract_hand(venue: FakeExchange) -> VenueHand:
    return VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        instrument="BTCUSDT",
        max_notional=Decimal(5000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )


def test_a_size_in_the_base_is_sent_as_contracts() -> None:
    # 0.04 BTC on a venue where one contract is 0.01 BTC is four contracts, not 0.04 of one.
    venue = ContractVenue("0.01", answer={"id": "order-1", "status": "closed", "filled": 4})
    placed = _contract_hand(venue)(approved({**LONG, "size": "0.04"}))
    assert placed["sent"] is True and venue.orders[0]["amount"] == 4.0
    assert (
        placed["size"] == "0.04"
        and placed["contracts"] == "4"
        and placed["contract_size"] == "0.01"
    )
    # What the venue says it filled comes back in the base, which is what the record holds.
    assert Decimal(placed["filled"]) == Decimal("0.04")


def test_a_size_that_is_not_a_whole_number_of_contracts_is_refused() -> None:
    venue = ContractVenue("0.01")
    placed = _contract_hand(venue)(approved({**LONG, "size": "0.045"}))
    assert placed["sent"] is False and "contracts of 0.01" in placed["refused"]
    assert venue.orders == []


def test_a_contract_larger_than_the_base_unit_is_not_a_million_times_the_size() -> None:
    # A token whose contract is ten million of it: 20 million tokens is two contracts.
    venue = ContractVenue("10000000", answer={"id": "order-1", "status": "closed", "filled": 2})
    placed = _contract_hand(venue)(approved({**LONG, "size": "20000000", "mid": "0.00001"}))
    assert placed["sent"] is True and venue.orders[0]["amount"] == 2.0


def test_the_position_a_venue_reports_in_contracts_is_read_in_the_base() -> None:
    venue = ContractVenue("0.01", position={"contracts": 4, "side": "short"})
    assert _contract_hand(venue).position() == Decimal("-0.04")


def test_a_venue_that_will_not_say_its_contract_size_is_not_guessed() -> None:
    class Silent(ContractVenue):
        def market(self, symbol: str) -> dict[str, Any]:
            return {"contract": True, "limits": {"amount": {"min": None}}}

    venue = Silent("1")
    placed = _contract_hand(venue)(approved({**LONG, "size": "0.04"}))
    assert placed["sent"] is False and "does not say how large one" in placed["refused"]
    assert venue.orders == []


def test_the_environment_orders_go_to_is_declared_where_orders_are() -> None:
    from trading_research_agents.trading.config_venue import _mandate

    raw = {"execution": {"mode": "venue", "venue": "okx", "environment": "testnet"}}
    assert _mandate(raw, "okx/taker-taker").environment == "testnet"
    assert _mandate({"execution": {"mode": "venue"}}, "bybit/taker-taker").environment == ""
    with pytest.raises(Exception, match="environment"):
        _mandate({"execution": {"environment": "paper"}}, "bybit/taker-taker")


def test_the_account_fees_are_read_in_basis_points() -> None:
    from trading_research_agents.trading.dispatch import account_fees

    class Fees(FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self.has["fetchTradingFee"] = True

        def fetch_trading_fee(self, symbol: str) -> dict[str, Any]:
            return {"maker": 0.0002, "taker": 0.0005}

    maker, taker = account_fees(
        "bybit", symbol="BTC/USDT:USDT", env={}, environment="demo", client=Fees()
    )
    assert maker == Decimal("2") and taker == Decimal("5")


def test_a_blank_fee_says_which_line_and_what_to_write() -> None:
    from trading_research_agents.envelope.load import parse_envelope
    from trading_research_agents.envelope.model import EnvelopeError

    text = chr(10).join(
        [
            "[[channel]]",
            'venue = "okx"',
            'name = "taker-taker"',
            "entry_fee_bps =   # your fee",
            "exit_fee_bps = 5",
        ]
    )
    with pytest.raises(EnvelopeError, match=r"line 4: entry_fee_bps has no value"):
        parse_envelope(text)


def test_the_setup_for_a_venue_without_a_live_plugin_has_no_live_table() -> None:
    from trading_research_agents.cli.venue_info import venue_setup
    from trading_research_agents.data.sources.capability import capability_of
    from trading_research_agents.envelope.load import parse_envelope

    able = capability_of("okx")
    text = venue_setup(
        able,
        ["OKX_API_KEY"],
        fees=(Decimal("2.0"), Decimal("5.0")),
        fees_note="read from your testnet API for BTC/USDT:USDT",
    )
    assert "[live]" not in text and 'environment = "testnet"' in text
    assert "size_step" not in text.split("# 3.")[1].split("# 4.")[0].replace("[run].size_step", "")
    channels = text.split("[[channel]]", 1)[1].split("# 2.")[0]
    envelope = parse_envelope("[[channel]]" + channels)
    assert [c.entry_fee_bps for c in envelope.channels] == [Decimal("5"), Decimal("2")]


def test_a_venue_that_raises_for_no_position_is_read_as_flat() -> None:
    class NullResponse(Exception):  # noqa: N818 - ccxt's own class name is what is matched
        pass

    class Raises(FakeExchange):
        def fetch_position(self, symbol: str) -> object:
            raise NullResponse("fetchPosition() could not find a position for " + symbol)

    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=Raises(),
    )
    assert hand.position() == Decimal(0)

    class Down(FakeExchange):
        def fetch_position(self, symbol: str) -> object:
            raise TimeoutError("venue did not answer")

    broken = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=Down(),
    )
    with pytest.raises(DispatchError, match="cannot read the account"):
        broken.position()


def test_the_client_order_id_is_letters_and_digits_within_thirty_two() -> None:
    from trading_research_agents.trading.hand_state import _client_id

    made = _client_id({"at": "2026-10-07T16-30-47Z-1000PEPEUSDT", "instrument": "1000PEPEUSDT"})
    assert made == "tra20261007T163047Z1000PEPEUSDT"[:32] and made.isalnum() and len(made) <= 32
    assert _client_id({"at": "2026-09-28T02-55-40Z", "instrument": "BTCUSDT"}).isalnum()


def test_the_declared_margin_mode_is_the_clients_default_for_every_call() -> None:
    class WithOptions(FakeExchange):
        options: dict[str, Any] = {}

    venue = WithOptions()
    VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(100),
        keys=("K", "S"),
        client=venue,
        margin_mode="isolated",
    )
    assert venue.options["defaultMarginMode"] == "isolated"


def test_an_account_in_long_short_mode_is_refused_before_anything_is_sent() -> None:
    class Hedged(FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self.has["fetchPositionMode"] = True

        def fetch_position_mode(self, symbol: str) -> dict[str, Any]:
            return {"hedged": True}

    venue = Hedged()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=venue,
    )
    placed = hand(approved(LONG))
    assert placed["sent"] is False and "long/short position mode" in placed["refused"]
    assert venue.orders == [] and "long/short position mode" in hand.ready()


def test_an_interrupt_while_an_order_rests_takes_it_off_the_book_first() -> None:
    venue = FakeExchange()

    def interrupted(_s: float) -> None:
        raise KeyboardInterrupt

    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        role="maker",
        client=venue,
        clock=lambda: 0.0,
        sleep=interrupted,
    )
    placed = hand(approved({**LONG, "bid": "99999"}))
    assert placed["interrupted"] is True and placed["cancelled"] is True
    assert venue.cancelled == ["order-1"] and "cancelled before the process left" in placed["note"]


def test_a_clock_out_of_step_with_the_venue_refuses_before_the_first_order() -> None:
    class Skewed(FakeExchange):
        def fetch_time(self) -> int:
            return 1_000_000_000_000 - 2_600

    venue = Skewed()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
        clock=lambda: 1_000_000_000.0,
    )
    placed = hand(approved(LONG))
    assert placed["sent"] is False and "+2600 ms" in placed["refused"] and venue.orders == []
    assert "+2600 ms" in hand.ready()


def test_an_order_is_found_by_state_when_the_plain_lookup_refuses() -> None:
    class PerState(FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self.has = {**self.has, "fetchOpenOrder": True, "fetchClosedOrder": True}

        def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            raise RuntimeError("can only access an order if it is in last 500 orders")

        def fetch_open_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            raise RuntimeError("order not found among open orders")

        def fetch_closed_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            return {"status": "closed", "filled": 0.01, "average": 99999.0}

    venue = PerState()
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
    placed = hand(approved({**LONG, "bid": "99999"}))
    assert placed["filled"] == "0.01" and placed["cancelled"] is False and venue.cancelled == []


class Moved(FakeExchange):
    """The book half a minute after the snapshot the cycle priced on."""

    def __init__(self, bid: str, ask: str) -> None:
        super().__init__(statuses=[{"status": "closed", "filled": 0.01, "average": float(bid)}])
        self.book = {"bids": [[float(bid), 1.0]], "asks": [[float(ask), 1.0]]}

    def fetch_order_book(self, symbol: str) -> dict[str, Any]:
        return self.book


def _maker(venue: FakeExchange, **overrides: Any) -> VenueHand:
    settings: dict[str, Any] = {
        "venue": "bybit",
        "symbol": "BTC/USDT:USDT",
        "max_notional": Decimal(2000),
        "keys": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        "role": "maker",
        "client": venue,
        "clock": lambda: 0.0,
        "sleep": lambda _s: None,
    }
    settings.update(overrides)
    return VenueHand(**settings)


def test_a_resting_order_sits_at_the_book_as_it_is_now_not_as_it_was() -> None:
    venue = Moved("100040", "100042")
    placed = _maker(venue)(approved({**LONG, "bid": "99999", "ask": "100001"}))
    # It rests at today's bid, and the record says how far the mid moved and where it rested.
    assert venue.orders[0]["price"] == 100040.0
    assert placed["rested_at"] == "the book at send time" and placed["drift_bps"] == "4.10"


def test_a_decision_the_market_has_left_behind_is_refused() -> None:
    venue = Moved("100150", "100152")
    placed = _maker(venue, max_drift_bps=Decimal(10))(
        approved({**LONG, "bid": "99999", "ask": "100001"})
    )
    assert placed["sent"] is False and "moved 15.10 bps against the trade" in placed["refused"]
    assert venue.orders == []
    # A move in the trade's favour is not drift against it.
    kinder = Moved("99850", "99852")
    assert (
        _maker(kinder, max_drift_bps=Decimal(10))(approved({**LONG, "bid": "99999"}))["sent"]
        is True
    )


def test_venue_mode_sends_only_the_market_types_built_and_listed() -> None:
    from trading_research_agents.trading.dispatch import market_kind

    class SpotExchange(SteppedExchange):
        def market(self, symbol: str) -> dict[str, Any]:
            return {"spot": True, "type": "spot", "limits": {"amount": {"min": self.least}}}

    class OptionExchange(SteppedExchange):
        def market(self, symbol: str) -> dict[str, Any]:
            return {"option": True, "type": "option", "contract": True, "contractSize": 1}

    order = {**LONG, "instrument": "ETHUSDT", "size": "0.03", "mid": "2700"}
    # Spot is built, but a desk sends to it only when it lists it.
    placed = _stepped_hand(SpotExchange("0.01"))(approved(order))
    assert placed["sent"] is False
    assert (
        "is a spot market and [execution].markets lists ['linear perpetual']" in placed["refused"]
    )
    unbuilt = _stepped_hand(OptionExchange("0.01"))(approved(order))
    assert (
        unbuilt["sent"] is False and "is a option market; venue mode trades" in unbuilt["refused"]
    )
    assert "other market types are not built yet" in unbuilt["refused"]
    assert market_kind({"swap": True, "linear": True}) == "linear perpetual"
    assert market_kind({"swap": True, "inverse": True}) == "inverse perpetual"
    assert market_kind({"option": True}) == "option"
    assert market_kind({"future": True}) == "dated future"
    assert market_kind({}) == "unknown"


def test_the_clock_is_judged_on_the_quickest_reading_not_the_first() -> None:
    class Slow(FakeExchange):
        def __init__(self) -> None:
            super().__init__()
            self.times = iter([1621.0, 1205.0, 2205.0])

        def fetch_time(self) -> float:
            return next(self.times)

    ticks = iter([0.0, 0.842, 1.0, 1.21, 2.0, 2.21])
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=Slow(),
        clock=lambda: next(ticks),
    )
    # The first reading carries a handshake and looks 1200 ms off; the two quick ones agree at 100.
    assert hand.clock_skew() == ""


def test_an_okx_account_in_spot_mode_is_refused_before_anything_is_sent() -> None:
    class SpotMode(FakeExchange):
        def privateGetAccountConfig(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
            return {"data": [{"acctLv": "1", "posMode": "net_mode"}]}

    venue = SpotMode()
    hand = VenueHand(
        venue="okx",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=venue,
    )
    placed = hand(approved(LONG))
    assert placed["sent"] is False and "Spot mode" in placed["refused"]
    assert venue.orders == [] and "Spot mode" in hand.ready()

    class Futures(SpotMode):
        def privateGetAccountConfig(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
            return {"data": [{"acctLv": "2"}]}

    assert VenueHand(
        venue="okx",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=Futures(),
    )(approved(LONG))["sent"]


def test_a_bitget_unified_key_sent_to_the_classic_api_is_refused_before_anything_is_sent() -> None:
    class Unified(FakeExchange):
        grants = ["uamw", "uatw"]
        options = {"uta": False}

        def privateSpotGetV2SpotAccountInfo(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
            return {"data": {"authorities": self.grants}}

    venue = Unified()
    hand = VenueHand(
        venue="bitget",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=venue,
    )
    assert "unified trading account" in hand.ready()
    placed = hand(approved(LONG))
    assert placed["sent"] is False and "unified trading account" in placed["refused"]
    assert venue.orders == []

    class Classic(Unified):
        # Any grant outside the unified set means the key was not made for that account alone.
        grants = ["uamw", "futures"]

    assert VenueHand(
        venue="bitget",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=Classic(),
    )(approved(LONG))["sent"]


def test_a_decision_older_than_its_bound_is_not_sent_and_a_fresh_one_is_measured() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=venue,
        max_decision_age_s=60.0,
    )
    old = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H-%M-%SZ")
    stale = hand(approved({**LONG, "at": old}))
    assert stale["sent"] is False and "past [execution].max_decision_age_s 60" in stale["refused"]
    assert venue.orders == []

    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    placed = hand(approved({**LONG, "at": fresh}))
    assert placed["sent"] is True and placed["send_ms"] >= 0
    assert placed["decision_age_s"] is not None and placed["decision_age_s"] < 60
    # Filled at 100000.5 against a mid of 100000: half a basis point paid on a buy.
    assert placed["slippage_bps"] == "0.05"


def test_a_taker_order_with_no_book_to_protect_it_is_not_sent() -> None:
    venue = FakeExchange()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=venue,
    )
    refused = hand(approved({k: v for k, v in LONG.items() if k not in ("bid", "ask")}))

    assert refused["sent"] is False and "no book to protect" in refused["refused"]
    assert venue.orders == []


def test_the_hand_sends_nothing_the_risk_gate_did_not_sign() -> None:
    from trading_research_agents.risk import RiskLimits, RiskState, evaluate

    venue = FakeExchange()
    hand = sending_hand(venue)
    assert "no approval" in hand(LONG)["refused"]
    signed = approved(LONG)
    assert "does not match" in hand({**signed, "size": "0.02"})["refused"]
    assert "does not match" in hand({**signed, "instrument": "ETHUSDT"})["refused"]
    late = evaluate(LONG, limits=RiskLimits(valid_for_s=1), state=RiskState(None, 0), now=0.0)
    assert "expired" in hand(late.approved_intent(LONG))["refused"]
    assert venue.orders == []
    assert hand(signed)["sent"] is True and len(venue.orders) == 1


def test_a_read_on_a_connection_left_idle_is_repeated_once_on_a_fresh_one() -> None:
    class Session:
        closed = 0

        def close(self) -> None:
            self.closed += 1

    class Idle(FakeExchange):
        def __init__(self, fails: int, error: Exception) -> None:
            super().__init__(position={"contracts": 0.01, "side": "long"})
            self.session = Session()
            self.fails = fails
            self.error = error
            self.reads = 0

        def fetch_position(self, symbol: str) -> object:
            self.reads += 1
            if self.reads <= self.fails:
                raise self.error
            return self.position

    def hand(client: Idle) -> VenueHand:
        return VenueHand(
            venue="bybit",
            symbol="BTC/USDT:USDT",
            max_notional=Decimal(2000),
            keys=("K", "S"),
            client=client,
        )

    stale = Idle(1, RequestTimeout("read timed out after 10 s"))
    assert hand(stale).position() == Decimal("0.01")
    assert stale.reads == 2 and stale.session.closed == 1
    # Twice unanswered is a venue that is down, not a stale connection: no third read.
    down = Idle(2, RequestTimeout("read timed out after 10 s"))
    with pytest.raises(DispatchError, match="RequestTimeout"):
        hand(down).position()
    assert down.reads == 2
    # A venue that answered no is never asked again.
    refused = Idle(1, InsufficientFunds("not this"))
    with pytest.raises(DispatchError, match="InsufficientFunds"):
        hand(refused).position()
    assert refused.reads == 1


def test_a_decision_that_aged_during_the_venue_reads_is_not_sent() -> None:
    now = [datetime.now(UTC).timestamp()]

    class Slow(FakeExchange):
        def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
            # A slow read: the checks before it passed, and the decision ages while it answers.
            now[0] += 90
            return super().fetch_open_orders(symbol)

    venue = Slow()
    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        client=venue,
        max_decision_age_s=60.0,
        clock=lambda: now[0],
    )
    fresh = datetime.fromtimestamp(now[0], UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    late = hand(approved({**LONG, "at": fresh}))
    assert late["sent"] is False
    assert "old before it could be sent, past [execution].max_decision_age_s 60" in late["refused"]
    assert venue.orders == []


def test_the_readiness_check_never_prints_a_key() -> None:
    class Leaky(FakeExchange):
        def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
            raise RuntimeError("GET /v5/order/realtime?api_key=AKIDSECRET1234 failed")

    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(2000),
        keys=("K", "S"),
        env={"K": "AKIDSECRET1234", "S": "S" * 16},
        client=Leaky(),
    )
    said = hand.ready()
    assert "RuntimeError" in said and "AKIDSECRET1234" not in said and "[redacted]" in said
