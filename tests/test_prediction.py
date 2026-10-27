"""Prediction market outcomes, on the same order path and cycle as every other market.

The shapes are ccxt 4.5.85's prediction namespace as Kalshi's demo returned them on 2026-10-17: a
market of `type` prediction with prices limited to [0.01, 0.99], outcomes addressed by a handle
such as `MARKET:YES`, positions that book a NO holding as a short on the YES outcome, and
settlements that pay 1 or 0 a share.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from approvals import approved
from test_cycle import NOW, Stub, candles, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.data.sources import Fetched, SourceChain
from trading_research_agents.data.sources.ccxt_client import SyncBridge, exchange_class
from trading_research_agents.envelope import parse_envelope
from trading_research_agents.execution.outcome import (
    OutcomeError,
    fee_per_share,
    outcome_edge,
)
from trading_research_agents.execution.pretrade import book_from_levels
from trading_research_agents.trading.dispatch import VenueHand
from trading_research_agents.trading.market import Market
from trading_research_agents.trading.round_trip import _settled

HANDLE = "FED_CUTS_IN_DECEMBER:YES"
NO_HANDLE = "FED_CUTS_IN_DECEMBER:NO"
MARKET: dict[str, Any] = {
    "id": "KXFED-26DEC-CUT",
    "symbol": None,
    "type": "prediction",
    "prediction": True,
    "contract": False,
    "base": "USD",
    "quote": "USD",
    "expiry": 1798416000000,
    "taker": 0.07,
    "maker": 0.0,
    "limits": {"amount": {"min": 1}, "price": {"min": 0.01, "max": 0.99}},
    "info": {"title": "Will the Fed cut rates in December?"},
    "outcomes": [
        {"outcome": HANDLE, "outcomeId": "KXFED-26DEC-CUT", "label": "YES"},
        {"outcome": NO_HANDLE, "outcomeId": "KXFED-26DEC-CUT-NO", "label": "NO"},
    ],
}


def test_a_venue_name_resolves_in_either_namespace() -> None:
    pytest.importorskip("ccxt")
    assert exchange_class("bybit").__module__.startswith("ccxt.bybit")
    assert exchange_class("kalshi").__module__.startswith("ccxt.prediction")
    # Two names live in both: the bare one is the crypto exchange, the prefixed one the venue.
    assert exchange_class("hyperliquid").__module__.startswith("ccxt.hyperliquid")
    assert exchange_class("prediction.hyperliquid").__module__.startswith("ccxt.prediction")
    assert exchange_class("no-such-venue") is None


def test_an_async_client_answers_through_the_bridge_as_if_it_were_synchronous() -> None:
    class Async:
        has = {"fetchBalance": True}

        def __init__(self) -> None:
            self.markets: dict[str, Any] = {}
            self.closed = False

        async def fetch_balance(self) -> dict[str, Any]:
            await asyncio.sleep(0)
            return {"USD": {"free": 100.0}}

        async def close(self) -> None:
            self.closed = True

    inner = Async()
    bridge = SyncBridge(inner)
    assert bridge.fetch_balance() == {"USD": {"free": 100.0}}
    assert bridge.has is inner.has
    bridge.markets = {"x": {}}
    assert inner.markets == {"x": {}}
    bridge.close()
    assert inner.closed


def test_an_outcome_is_one_more_market_with_its_own_facts() -> None:
    outcome = Market.from_ccxt(
        "kalshi", HANDLE, {**MARKET, "symbol": HANDLE, "outcome": MARKET["outcomes"][0]}
    )
    assert outcome.label == "prediction" and outcome.outcome and not outcome.shortable
    assert outcome.price_min == Decimal("0.01") and outcome.price_max == Decimal("0.99")
    assert outcome.question == "Will the Fed cut rates in December?"
    assert outcome.outcome_label == "YES" and outcome.unit() == 1


def test_an_outcome_is_worth_buying_only_past_its_ask_and_fee() -> None:
    # The coefficient fee: 0.07 x p x (1 - p), 1.75 cents a share at 0.5 and nothing near the ends.
    assert fee_per_share(Decimal("0.5"), coefficient=Decimal("0.07")) == Decimal("0.0175")
    assert fee_per_share(Decimal("0.4"), fee_bps=Decimal(100)) == Decimal("0.004")
    good = outcome_edge(Decimal("0.55"), Decimal("0.40"), coefficient=Decimal("0.07"))
    assert good.admitted and good.fee == Decimal("0.01680") and good.value == Decimal("0.13320")
    flat = outcome_edge(Decimal("0.41"), Decimal("0.40"), coefficient=Decimal("0.07"))
    assert not flat.admitted and "nothing to buy" in flat.reason
    thin = outcome_edge(Decimal("0.43"), Decimal("0.40"), margin_bps=Decimal(1000))
    assert not thin.admitted and "the margin is 1000 bps" in thin.reason
    with pytest.raises(OutcomeError, match="between 0 and 1"):
        fee_per_share(Decimal(1))


def test_a_channel_says_how_a_prediction_venue_charges() -> None:
    text = (
        '[[channel]]\nvenue = "kalshi"\nname = "taker-taker"\nentry_fee_bps = 0\n'
        "exit_fee_bps = 0\n\n[channel.prediction]\nfee_coefficient = 0.07\n"
    )
    assert parse_envelope(text).channels[0].outcome_fees() == (Decimal("0.07"), None)
    flat = text.replace("fee_coefficient = 0.07", "entry_fee_bps = 0\nexit_fee_bps = 0")
    assert parse_envelope(flat).channels[0].outcome_fees() == (None, Decimal(0))
    bare = text.split("[channel.prediction]")[0]
    assert parse_envelope(bare).channels[0].outcome_fees() is None


class Kalshi:
    """A prediction venue: outcomes by handle, a coefficient fee, positions per market."""

    has = {"fetchSettlements": True}

    def __init__(self, positions: list[dict[str, Any]] | None = None, resting: int = 0) -> None:
        self.markets = {MARKET["id"]: MARKET}
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.positions = positions or []
        self.resting = [{"id": f"take-{i}"} for i in range(resting)]

    def load_markets(self) -> dict[str, Any]:
        return self.markets

    def market(self, symbol: str) -> dict[str, Any]:
        raise KeyError(symbol)  # ccxt's BadSymbol: a handle is not a market symbol

    def outcome(self, handle: str) -> dict[str, Any]:
        return next(o for o in MARKET["outcomes"] if o["outcome"] == handle)

    def fetch_balance(self) -> dict[str, Any]:
        return {"USD": {"free": 1000.0, "total": 1000.0}}

    def calculate_fee(
        self, symbol: str, kind: str, side: str, amount: float, price: float
    ) -> dict[str, Any]:
        return {"cost": 0.07 * amount * price * (1 - price)}

    def fetch_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self.resting)

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        self.resting = [o for o in self.resting if o["id"] != order_id]
        return {"id": order_id}

    def fetch_settlements(self, outcome: str, since: int) -> list[dict[str, Any]]:
        return [{"outcome": HANDLE, "amount": 10, "price": 1, "won": True, "timestamp": since + 1}]

    def create_order(self, *args: Any) -> dict[str, Any]:
        symbol, kind, side, amount, price, params = args
        self.orders.append(
            {
                "symbol": symbol,
                "type": kind,
                "side": side,
                "amount": amount,
                "price": price,
                "params": dict(params),
            }
        )
        if side == "buy":
            self.positions = [{"outcome": HANDLE, "side": "long", "contracts": amount}]
            return {"id": "buy-1", "status": "closed", "filled": amount, "average": price}
        if params.get("timeInForce") == "IOC":
            self.positions = []
            return {"id": "sell-1", "status": "closed", "filled": amount}
        return {"id": "take-1", "status": "open", "filled": 0}


def hand(client: Kalshi, handle: str = HANDLE) -> VenueHand:
    return VenueHand(
        venue="kalshi",
        symbol=handle,
        instrument="FEDCUT",
        max_notional=Decimal(100),
        keys=("KALSHI_API_KEY", "KALSHI_PRIVATE_KEY"),
        client=client,
        markets=("prediction",),
        sleep=lambda s: None,
    )


ORDER = {
    "target": "long",
    "size": "10",
    "mid": "0.41",
    "bid": "0.40",
    "ask": "0.42",
    "take_price": "0.55",
    "stop_price": None,
    "at": "2099-01-01T00-00-00Z",
    "instrument": "FEDCUT",
}


def test_an_outcome_is_bought_inside_its_price_limits_and_its_take_rests_at_the_venue() -> None:
    venue = Kalshi()
    placed = hand(venue)(approved(dict(ORDER)))
    assert placed["sent"] is True, placed
    entry, take = venue.orders
    # 20 bps through the 0.42 ask, a limit IOC; no client id, no attached stop: the stake is it.
    assert entry["side"] == "buy" and entry["params"] == {"timeInForce": "IOC"}
    assert entry["price"] == pytest.approx(0.42084) and entry["amount"] == 10.0
    assert placed["exits"] == "bounded" and placed["take_order"] == "take-1"
    assert (
        take["side"] == "sell" and take["price"] == 0.55 and take["params"]["timeInForce"] == "GTC"
    )
    # Paid in full at the mid, with the venue's own fee on top: 10 x 0.07 x 0.41 x 0.59 = 0.17.
    assert placed["capital"]["cash_needed"] == "4.27"


def test_an_outcome_is_never_sold_short() -> None:
    placed = hand(Kalshi())(approved({**ORDER, "target": "short"}))
    assert placed["sent"] is False and "needs a borrow" in placed["refused"]


def test_the_shares_held_are_read_from_the_positions_with_no_as_a_short_on_yes() -> None:
    venue = Kalshi(positions=[{"outcome": HANDLE, "side": "short", "contracts": 7}])
    assert hand(venue, HANDLE).position() == 0
    assert hand(venue, NO_HANDLE).position() == 7
    resolved = Kalshi(
        positions=[{"outcome": HANDLE, "side": "long", "contracts": 5, "resolved": True}]
    )
    assert hand(resolved).position() == 0


def test_a_close_takes_the_resting_exit_off_and_sells_at_the_floor() -> None:
    venue = Kalshi(positions=[{"outcome": HANDLE, "side": "long", "contracts": 10}], resting=1)
    closed = hand(venue).close_position("held past its limit")
    assert venue.cancelled == ["take-0"]
    sale = venue.orders[-1]
    assert (
        sale["side"] == "sell"
        and sale["price"] == 0.01
        and sale["params"] == {"timeInForce": "IOC"}
    )
    assert closed["closed"] is True and closed["size"] == "10"


def test_a_share_held_to_resolution_is_written_up_at_its_payout() -> None:
    settled = _settled(hand(Kalshi()), since=1_000)
    assert settled == {
        "side": "sell",
        "amount": Decimal(10),
        "price": Decimal(1),
        "fee": {"cost": 0},
        "timestamp": 1_001,
    }


class OutcomeBook(Stub):
    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        if kind == "orderbook":
            value: Any = book_from_levels(
                bids=[("0.40", "500"), ("0.39", "900")], asks=[("0.42", "500"), ("0.43", "900")]
            )
        else:
            value = candles(["0.38", "0.39", "0.40", "0.41", "0.41"])
        return Fetched(
            kind=kind,
            instrument=instrument,
            value=value,
            source=self.name,
            tier="free",
            fetched_at=NOW,
            as_of_ns=int(NOW * 1_000_000_000),
            detail={},
        )


def _outcome_cycle(tmp_path: Path, answer: dict[str, Any], **overrides: Any) -> Any:
    envelope = parse_envelope(
        '[[channel]]\nvenue = "kalshi"\nname = "taker-taker"\nentry_fee_bps = 0\n'
        "exit_fee_bps = 0\n\n[channel.prediction]\nfee_coefficient = 0.07\n"
    )
    chains = {
        kind: SourceChain(
            kind=kind, sources=(OutcomeBook(kind, None),), ttl_s=600, clock=lambda: NOW
        )
        for kind in ("ohlcv", "orderbook")
    }
    market = Market.from_ccxt(
        "kalshi", HANDLE, {**MARKET, "symbol": HANDLE, "outcome": MARKET["outcomes"][0]}
    )
    settings: dict[str, Any] = {
        "instrument": "FEDCUT",
        "venue_key": "kalshi/taker-taker",
        "chains": chains,
        "market": market,
        "size": Decimal(20),
        "clock": lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    }
    settings.update(overrides)
    cfg, fake, _ = cycle_config(
        tmp_path, envelope, {"event_analyst": json.dumps(answer)}, **settings
    )
    return run_cycle(cfg), fake


def test_the_event_analyst_decides_and_code_prices_the_share_to_resolution(tmp_path: Path) -> None:
    answer = {
        "probability": 0.55,
        "confidence": 0.8,
        "reason": "the dated note says the committee signalled a cut; the price has not moved",
        "figures_used": [{"figure": "ask", "value": "0.42"}],
    }
    result, _ = _outcome_cycle(tmp_path, answer, max_position_quote=Decimal(5))
    assert result.intent is not None, result.refusals
    intent = result.intent
    assert (
        intent["target"] == "long" and intent["setup"] == "event" and intent["exits"] == "bounded"
    )
    # 20 shares asked, capped to 5 of quote at the 0.42 ask: 11.904 shares, stepped down.
    assert Decimal(intent["size"]) <= Decimal(5) / Decimal("0.42")
    assert intent["take_price"] == "0.55" and intent["stop_price"] is None
    assert result.briefs["outcome"]["admitted"] is True
    # Funding and open interest belong to contracts; they are not asked for.
    assert set(result.snapshot.values) == {"ohlcv", "orderbook"}


def test_a_probability_at_the_price_buys_nothing(tmp_path: Path) -> None:
    answer = {
        "probability": 0.41,
        "confidence": 0.3,
        "reason": "nothing in the brief says more than the price already does",
        "figures_used": [{"figure": "mid", "value": "0.41"}],
    }
    result, _ = _outcome_cycle(tmp_path, answer, min_confidence=0.2)
    assert result.intent is None
    assert any(r.startswith("cost gate: at a probability of 0.41") for r in result.refusals)


def test_a_probability_that_cites_nothing_from_its_brief_is_refused(tmp_path: Path) -> None:
    answer = {
        "probability": 0.9,
        "confidence": 0.9,
        "reason": "certain",
        "figures_used": [{"figure": "poll", "value": "64"}],
    }
    result, _ = _outcome_cycle(tmp_path, answer)
    assert result.intent is None
    assert any("rests on nothing this cycle measured" in r for r in result.refusals)


def test_a_channel_without_outcome_fees_refuses_rather_than_guess_zero(tmp_path: Path) -> None:
    envelope = parse_envelope(
        '[[channel]]\nvenue = "kalshi"\nname = "taker-taker"\nentry_fee_bps = 0\nexit_fee_bps = 0\n'
    )
    market = Market.from_ccxt(
        "kalshi", HANDLE, {**MARKET, "symbol": HANDLE, "outcome": MARKET["outcomes"][0]}
    )
    chains = {
        kind: SourceChain(
            kind=kind, sources=(OutcomeBook(kind, None),), ttl_s=600, clock=lambda: NOW
        )
        for kind in ("ohlcv", "orderbook")
    }
    cfg, fake, _ = cycle_config(
        tmp_path,
        envelope,
        {},
        instrument="FEDCUT",
        venue_key="kalshi/taker-taker",
        chains=chains,
        market=market,
        size=Decimal(20),
    )
    result = run_cycle(cfg)
    assert result.intent is None and fake.calls == []
    assert "declares no fees for a prediction outcome" in str(result.stopped_by)


def test_an_outcome_without_published_volume_is_screened_on_its_move_alone() -> None:
    from trading_research_agents.agents.screen import measured
    from trading_research_agents.execution.regime import RegimeRule
    from trading_research_agents.store.snapshot import Snapshot

    snap = Snapshot(cycle="2026-10-17T12-00-00Z", instrument="FEDCUT")
    snap.put(
        Fetched(
            kind="ohlcv",
            instrument="FEDCUT",
            value=candles(["0.40", "0.41", "0.42"], volume="0"),
            source="ccxt:kalshi",
            tier="free",
            fetched_at=NOW,
            as_of_ns=int(NOW * 1_000_000_000),
            detail={"volume": "not published by the venue"},
        )
    )
    assert measured(snap, 3600, RegimeRule()) == {}
