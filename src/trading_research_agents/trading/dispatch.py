"""Sending one admitted intent to a venue, under a mandate, with the key never leaving this layer.

A cycle decides; this sends. They are kept apart on purpose: nothing in `agents/` can reach a venue,
nothing in `execution/` holds a credential, and the only way an order is ever placed is that an
operator wrote a mandate, the ladder stands at its canary tier, and every gate in the cycle passed
first.

The order obeys the channel. A channel named `taker-taker` was priced at the venue's taker fee, so
its entry crosses the book with a limit IOC inside the slippage band; a channel named
`maker-taker` was priced at the maker fee, so its entry rests on the book as a post-only limit and
is cancelled if it has not filled by the operator's timeout. A channel whose name does not say is
refused rather than guessed, because guessing here means paying a fee the gate did not price.

Three habits make this auditable after a failure, which is the only time it matters: every order
carries a client id derived from the cycle, so the same cycle cannot open two positions; the account
is read before anything is sent, so an order is never added to a position this desk did not expect;
and nothing is retried. A retry loop around real capital is how one defect becomes ten orders.

The exits go out attached to the entry rather than after it. A filled position with no stop
is the one state this software must never create, because it is the state that survives our
process dying.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from trading_research_agents.execution.mandate import DEFAULT_MARKETS, MARKETS
from trading_research_agents.trading.hand_state import (
    BPS,
    MAX_BOOK_AGE_S,
    SIDES,
    UNDESCRIBED,
    DispatchError,
    OrderRate,
    _client_id,
    _decision_age_s,
    _drift_bps,
    _slippage_bps,
    credentials_for,
)
from trading_research_agents.trading.hand_upkeep import HandUpkeep
from trading_research_agents.trading.market import Market
from trading_research_agents.trading.venues.accounts import account_refusal
from trading_research_agents.trading.venues.exits import exit_cancel_params
from trading_research_agents.trading.venues.limits import (
    client_id_max,
    client_id_prefix,
)


@dataclass
class _Send:
    """One intent on its way to the venue: what each check has read so far, in the order read."""

    intent: Mapping[str, Any]
    market: Market | None = None
    kind: str = ""
    target: str = ""
    side: str = ""
    derivative: bool = False
    size: Decimal = Decimal(0)
    mid: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    age: float | None = None
    standing: Decimal | None = None
    swept: int = 0
    unit: Decimal = Decimal(1)
    contracts: Decimal = Decimal(0)
    drift: Decimal | None = None
    fresh: dict[str, Any] | None = None
    carried: dict[str, str] = field(default_factory=dict)
    order_id: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    price: Decimal | None = None


def _refused(why: str, **seen: Any) -> dict[str, Any]:
    return {"sent": False, "refused": why, **seen}


@dataclass
class VenueHand(HandUpkeep):
    """What a cycle in venue mode sends with: one venue, one instrument, one cap, one role.

    The client is built here from the environment, so the key is read in one place and never
    travels through a config file, a brief, a prompt or a record. `demo` and `testnet` put the
    client into the venue's own sandbox, which is the only way this should ever be tried first.
    """

    def __call__(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        """Send it, or say why it was not sent. Either way the caller gets a record.

        The checks run in this order because several of them act on the venue - the clock and
        account reads, cancelling this desk's earlier exits, setting leverage, taking a slot in
        the order rate - and each of those is only worth doing once every cheaper check passed.
        """
        send = _Send(intent)
        checks = (
            self._meant_for_this_hand,
            self._market_admits,
            self._sized,
            self._timely,
            self._account_admits,
            self._nothing_resting,
            self._in_contracts,
            self._priced_now,
            self._carried,
            self._order_terms,
        )
        for check in checks:
            refused = check(send)
            if refused is not None:
                return refused
        return self._send(send)

    def _meant_for_this_hand(self, send: _Send) -> dict[str, Any] | None:
        about = str(send.intent.get("instrument", ""))
        if self.instrument and about and about != self.instrument:
            # A desk that watches several instruments builds a hand per instrument. Sending this
            # one under another's symbol is the one mistake no record afterwards can undo.
            return _refused(
                f"this hand sends {self.instrument} as {self.symbol}; the intent is about {about}"
            )
        if self.sent_count:
            # One decision, one order. A hand asked twice is a wiring fault, not a second trade.
            return _refused("this hand has already sent its one order")
        return None

    def _market_admits(self, send: _Send) -> dict[str, Any] | None:
        market = send.market = self.market()
        kind = send.kind = market.label
        if kind not in MARKETS and kind != UNDESCRIBED:
            # A type with no rules here would be priced and protected by another type's rules.
            return _refused(
                f"{self.symbol} is a {kind} market; venue mode trades "
                f"{', '.join(MARKETS)} markets today, and other market types are not built yet"
            )
        if kind not in self.markets and kind != UNDESCRIBED:
            return _refused(
                f"{self.symbol} is a {kind} market and [execution].markets lists "
                f"{list(self.markets)}"
            )
        target = send.target = str(send.intent.get("target", ""))
        if target not in SIDES:
            return _refused(f"target {target!r} is not an order")
        if target == "short" and not market.shortable:
            return _refused(
                f"{self.symbol} is a {kind} market; a short there sells coins the account "
                "does not hold, which needs a borrow this desk does not take"
            )
        return None

    def _sized(self, send: _Send) -> dict[str, Any] | None:
        market = cast(Market, send.market)
        send.derivative = not (market.held_as_balance or market.outcome)
        send.side = SIDES[send.target]
        send.size = Decimal(str(send.intent.get("size", "0")))
        send.mid = Decimal(str(send.intent.get("mid", "0")))
        if send.size <= 0:
            return _refused("size is not positive")
        send.notional = send.size * send.mid
        if send.notional > self.max_notional:
            return _refused(
                f"notional {send.notional} is above [execution].max_notional {self.max_notional}"
            )
        return None

    def _timely(self, send: _Send) -> dict[str, Any] | None:
        age = send.age = _decision_age_s(send.intent, self.clock())
        if age is not None and age > self.max_decision_age_s:
            return _refused(
                f"the decision is {age:.0f}s old, past [execution].max_decision_age_s "
                f"{self.max_decision_age_s:.0f}; its figures are no longer the market's",
                decision_age_s=round(age, 1),
            )
        if not self.skew_checked:
            skew = self.clock_skew()
            if skew:
                return _refused(skew)
            self.skew_checked = True
        return None

    def _account_admits(self, send: _Send) -> dict[str, Any] | None:
        hedged = self.hedged() if send.derivative else ""
        if hedged:
            return _refused(hedged)
        if not self.account_checked:
            setting = account_refusal(self.venue, self.client, derivative=send.derivative)
            if setting:
                return _refused(setting)
            self.account_checked = True
        standing = send.standing = self.position()
        if standing and not self.adopt_open_position:
            # Any open position, not only one pointing the other way. A second entry in the same
            # direction is a larger position than the conviction rule sized, and nothing upstream
            # asked for it.
            way = "the other way" if (standing > 0) != (send.target == "long") else "the same way"
            return _refused(
                f"the account already holds {standing} of {self.symbol} {way}; flatten it, "
                "wait for it to close, or set [execution].adopt_open_position",
                position=str(standing),
            )
        return None

    def _nothing_resting(self, send: _Send) -> dict[str, Any] | None:
        if self.exits == "separate" and not send.derivative:
            return _refused(
                f"{self.symbol} is a {send.kind} market; exits placed after the fill are "
                "reduce-only orders, which a coin balance does not take; use "
                'exits = "attached" here'
            )
        # Only with nothing held: an adopted position keeps the exits that protect it.
        swept = self._sweep_exits() if self.exits == "separate" and not send.standing else 0
        if isinstance(swept, str):
            return _refused(swept)
        send.swept = swept
        resting = self._open_orders()
        if resting:
            return _refused(
                f"{resting} order(s) are still open on {self.symbol}; a second one would be a "
                "second decision on the same position",
                open_orders=resting,
            )
        return None

    def _in_contracts(self, send: _Send) -> dict[str, Any] | None:
        try:
            send.unit = self.contract_size()
        except DispatchError as exc:
            return _refused(str(exc))
        send.contracts = send.size / send.unit
        off_step = self.off_step(send.contracts, send.size, send.unit)
        return _refused(off_step) if off_step else None

    def _priced_now(self, send: _Send) -> dict[str, Any] | None:
        # The snapshot this was priced on is as old as the analysts and validators were slow -
        # half a minute is ordinary. Seen on 2026-10-04: a post-only order rested at that stale
        # touch was on the wrong side of a market that had moved, and the venue rejected it at once.
        now_bid, now_ask = self._touch_now()
        if self.book_age_s is not None and self.book_age_s > MAX_BOOK_AGE_S:
            return _refused(
                f"the venue's book for {self.symbol} is stamped {self.book_age_s:.0f}s ago; "
                "an order is not priced against a book that old"
            )
        drift = send.drift = _drift_bps(send.mid, now_bid, now_ask, send.side)
        if drift is not None and self.max_drift_bps is not None and drift > self.max_drift_bps:
            return _refused(
                f"the book moved {drift:.2f} bps against the trade since it was priced, more "
                f"than [execution].max_drift_bps {self.max_drift_bps}; the decision is stale",
                drift_bps=str(drift.quantize(Decimal("0.01"))),
            )
        send.fresh = {"bid": now_bid, "ask": now_ask} if now_bid and now_ask else None
        return None

    def _carried(self, send: _Send) -> dict[str, Any] | None:
        held_back = self._ensure_leverage() if send.derivative else ""
        if held_back:
            return _refused(held_back)
        market = cast(Market, send.market)
        carried = (
            self._capital(send.notional, send.side, send.mid, send.intent)
            if send.derivative
            else self._cash(send.notional, market, send.size, send.mid)
        )
        if isinstance(carried, str):
            return _refused(carried)
        send.carried = carried
        if self.rate is not None:
            busy = self.rate.admit(self.clock())
            if busy:
                return _refused(busy)
        return None

    def _order_terms(self, send: _Send) -> dict[str, Any] | None:
        market = cast(Market, send.market)
        intent = send.intent
        send.order_id = _client_id(intent, client_id_max(self.venue), client_id_prefix(self.venue))
        # The prediction venues take no client id through ccxt; their orders are matched by the id
        # the venue returns, and one decision still sends one order.
        params = send.params = {} if market.outcome else {"clientOrderId": send.order_id}
        if send.derivative:
            params.update(self.order_params())
        params.update(self.params)
        protective = {} if market.outcome else self._protective(intent, send.target)
        if isinstance(protective, str):
            return _refused(protective)
        params.update(protective)
        touch = send.fresh or intent
        if self.role == "maker":
            send.price = self._resting_price(touch, send.side)
            if send.price is None:
                return _refused("no book side to rest on; the snapshot had none")
            params["postOnly"] = True
            params.setdefault("timeInForce", "GTC")
            return None
        # Never a naked market order: a limit that takes what is there within the band and
        # cancels the rest, so a thin book cannot fill the entry at any price it likes.
        send.price = self._protected_price(touch, send.side)
        if send.price is None:
            return _refused("no book to protect a taker order against")
        params.setdefault("timeInForce", "IOC")
        return None

    def _send(self, send: _Send) -> dict[str, Any]:
        """The one order, then what the venue said about it and what follows it."""
        began = self.clock()
        try:
            order = self.client.create_order(
                self.symbol,
                "limit",
                send.side,
                float(send.contracts),
                float(cast(Decimal, send.price)),
                send.params,
            )
        except Exception as exc:
            raise DispatchError(self.redact(f"{exc.__class__.__name__}: {exc}")) from exc
        send_ms = round((self.clock() - began) * 1000)
        order = self.in_base(order, send.unit)
        self.sent_count += 1
        placed = self._placed(send, order, send_ms)
        if self.role == "maker":
            placed.update(self._settle(str(placed["order_id"])))
        elif placed["filled"] is None or placed["average"] is None:
            # A reply with a fill and no price is not an execution report (seen on 2026-10-20: a
            # venue answered a create with its ids alone), so the venue's own order is read.
            placed.update(self._taken(str(placed["order_id"])))
        placed["slippage_bps"] = _slippage_bps(placed.get("average"), send.mid, send.side)
        if send.swept:
            placed["earlier_exits_cancelled"] = send.swept
        if self.exits == "separate":
            placed.update(
                self._place_exits(placed, send.intent, send.side, send.unit, send.order_id)
            )
        if cast(Market, send.market).outcome:
            placed["exits"] = "bounded"
            placed.update(self._rest_take(placed, send.intent))
        return placed

    def _placed(self, send: _Send, order: Mapping[str, Any], send_ms: int) -> dict[str, Any]:
        intent, unit, drift, age = send.intent, send.unit, send.drift, send.age
        return {
            "sent": True,
            "venue": self.venue,
            "instrument": self.instrument,
            "environment": self.environment,
            "symbol": self.symbol,
            "role": self.role,
            "side": send.side,
            "size": str(send.size),
            **(
                {"contracts": f"{send.contracts:f}", "contract_size": f"{unit:f}"}
                if unit != 1
                else {}
            ),
            "price": None if send.price is None else str(send.price),
            "notional": str(send.notional),
            "client_order_id": send.order_id,
            "exits": self.exits,
            "stop_price": str(intent.get("stop_price", "")) or None,
            "take_price": str(intent.get("take_price", "")) or None,
            "priced_mid": str(send.mid),
            "drift_bps": None if drift is None else str(drift.quantize(Decimal("0.01"))),
            "decision_age_s": None if age is None else round(age, 1),
            "send_ms": send_ms,
            "rested_at": "the book at send time" if send.fresh else "the snapshot's touch",
            "capital": send.carried,
            **({"leverage_confirmed": "by the venue's reply"} if self.leverage_by_reply else {}),
            "order_id": str(order.get("id", "")),
            # A venue that reports no status reports none: "None" is not a status.
            "status": None if order.get("status") is None else str(order["status"]),
            "filled": None if order.get("filled") is None else str(order["filled"]),
            "average": None if order.get("average") is None else str(order["average"]),
        }

    def _place_exits(
        self,
        placed: Mapping[str, Any],
        intent: Mapping[str, Any],
        side: str,
        unit: Decimal,
        order_id: str,
    ) -> dict[str, Any]:
        """The stop and the take profit as reduce-only orders of their own, for what filled.

        The venue cannot take them with the entry, so they follow it at once. A fill they cannot be
        placed for is closed in the same breath: a position with no exit at the venue is the one
        state this desk does not leave behind, even for a cycle.
        """
        try:
            filled = Decimal(str(placed.get("filled") or 0))
        except ArithmeticError:
            filled = Decimal(0)
        if filled <= 0:
            # A fill the venue has not reported yet is still a position if the account says so.
            filled = abs(self.position() or Decimal(0))
        if filled <= 0:
            return {"exit_orders": None}
        amount = float(filled / unit)
        away = "sell" if side == "buy" else "buy"
        legs = (
            ("stop", "stopLossPrice", intent.get("stop_price"), "s"),
            ("take", "takeProfitPrice", intent.get("take_price"), "t"),
        )
        made: dict[str, str] = {}
        for leg, key, level, mark in legs:
            params = {
                **self.order_params(),
                **self.params,
                key: float(Decimal(str(level))),
                "reduceOnly": True,
                "clientOrderId": f"{order_id[: client_id_max(self.venue) - 1]}{mark}",
            }
            try:
                order = self.client.create_order(self.symbol, "market", away, amount, None, params)
            except Exception as exc:
                why = self.redact(f"{exc.__class__.__name__}: {exc}")[:200]
                extra = exit_cancel_params(self.venue)
                for left in made.values():
                    self._cancel(left, extra)
                return {
                    "exit_orders": None,
                    "exits_refused": f"the {leg} could not be placed: {why}",
                    "closed_unprotected": self.close_position(f"no {leg} at the venue: {why}"),
                }
            made[leg] = str(order.get("id", ""))
        return {"exit_orders": made}


def account_fees(
    venue: str,
    *,
    symbol: str,
    env: Mapping[str, str],
    environment: str,
    client: Any = None,
) -> tuple[Decimal, Decimal]:
    """The maker and taker fee in basis points, as this account reports them for one market.

    The fee is the largest figure in the cost gate and it depends on the account's own tier, so it
    is read from the account rather than copied from a fee page. Read only: nothing is sent.
    """
    hand = hand_for(
        venue=venue,
        symbol=symbol,
        max_notional=Decimal(1),
        env=env,
        environment=environment,
        client=client,
    )
    read = getattr(hand.client, "fetch_trading_fee", None)
    if read is None or not (getattr(hand.client, "has", {}) or {}).get("fetchTradingFee", True):
        raise DispatchError(f"{venue} does not report account fees through ccxt")
    try:
        got = read(hand.symbol) or {}
        maker, taker = got.get("maker"), got.get("taker")
        if maker is None or taker is None:
            raise DispatchError(f"{venue} answered without a maker and a taker fee")
        return Decimal(str(maker)) * BPS, Decimal(str(taker)) * BPS
    except DispatchError:
        raise
    except Exception as exc:
        raise DispatchError(hand.redact(f"{exc.__class__.__name__}: {exc}")) from exc


def hand_for(
    *,
    venue: str,
    symbol: str,
    max_notional: Decimal,
    instrument: str = "",
    role: str = "taker",
    exits: str = "attached",
    environment: str = "demo",
    limit_timeout_s: float = 30.0,
    price_offset_bps: Decimal = Decimal(0),
    adopt_open_position: bool = False,
    params: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    client: Any = None,
    max_drift_bps: Decimal | None = None,
    max_decision_age_s: float = 120.0,
    leverage: Decimal | None = None,
    margin_mode: str = "isolated",
    max_slippage_bps: Decimal = Decimal(20),
    max_margin_share: Decimal = Decimal("0.5"),
    liquidation_buffer_bps: Decimal = Decimal(50),
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    rate: OrderRate | None = None,
    ccxt_options: Mapping[str, Any] | None = None,
) -> VenueHand:
    """A hand for `venue`, reading whatever credentials that venue signs with."""
    return VenueHand(
        venue=venue.strip().lower(),
        symbol=symbol,
        max_notional=max_notional,
        instrument=instrument,
        keys=credentials_for(venue, client),
        role=role,
        exits=exits,
        environment=environment,
        limit_timeout_s=limit_timeout_s,
        price_offset_bps=price_offset_bps,
        adopt_open_position=adopt_open_position,
        params=dict(params or {}),
        env=env if env is not None else os.environ,
        client=client,
        max_drift_bps=max_drift_bps,
        max_decision_age_s=max_decision_age_s,
        leverage=leverage,
        margin_mode=margin_mode,
        max_slippage_bps=max_slippage_bps,
        max_margin_share=max_margin_share,
        liquidation_buffer_bps=liquidation_buffer_bps,
        markets=markets,
        rate=rate,
        options={"options": dict(ccxt_options)} if ccxt_options else {},
    )


def market_kind(market: Mapping[str, Any]) -> str:
    """What ccxt says a market is, in the words the refusal uses."""
    return Market.from_ccxt("", "", market).label
