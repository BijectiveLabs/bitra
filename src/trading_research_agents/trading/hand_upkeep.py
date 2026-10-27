"""What the order hand keeps in order around a trade: leverage, one-way mode, readiness, a taken
order's fill, settlement, and closing a position.
"""

from __future__ import annotations

from contextlib import suppress
from decimal import Decimal
from typing import Any

from trading_research_agents.trading.hand_reads import HandReads
from trading_research_agents.trading.hand_state import (
    ENDED,
    MAX_BOOK_AGE_S,
    DispatchError,
    leverage_number,
)
from trading_research_agents.trading.venues.accounts import account_refusal
from trading_research_agents.trading.venues.margin import (
    account_settings,
    leverage_params,
)


class HandUpkeep(HandReads):
    def ready(self) -> str:
        """Read the account through the same client an order would go through. No order is sent.

        The point is the path, not the answer: a preflight that proves a key works somewhere else
        proves nothing about the place the order is about to be sent to.
        """
        try:
            skew = self.clock_skew()
            if skew:
                return skew
            market = self.market()
            derivative = not (market.held_as_balance or market.outcome)
            hedged = self.hedged() if derivative else ""
            if hedged:
                return hedged
            setting = account_refusal(self.venue, self.client, derivative=derivative)
            if setting:
                return setting
            self._open_orders()
            self.position()
            self._touch_now()
            if self.book_age_s is not None and self.book_age_s > MAX_BOOK_AGE_S:
                return (
                    f"the venue's book for {self.symbol} is stamped {self.book_age_s:.0f}s ago; "
                    "every order would be refused until it is current"
                )
        except DispatchError as exc:
            return str(exc)
        except Exception as exc:
            return f"{exc.__class__.__name__}: {exc}"
        return ""

    def _taken(self, order_id: str, tries: int = 4) -> dict[str, Any]:
        """What an IOC entry actually filled. Some venues answer a create with only an id."""
        for attempt in range(tries):
            status = self.order_status(order_id) if order_id else None
            state = "" if status is None else str(status.get("status", ""))
            if status is not None and state in ("closed", "filled", *ENDED):
                average = status.get("average")
                if average is None and Decimal(str(status.get("filled") or 0)) > 0:
                    average = self._filled_average(order_id)
                return {
                    "status": state,
                    "filled": str(status.get("filled") or 0),
                    "average": None if average is None else str(average),
                }
            if attempt + 1 < tries:
                self.sleep(0.5)
        return {"note": "the venue had not reported a fill when this record was written"}

    def make_one_way(self) -> str:
        """Set a hedged practice account to one-way, when nothing on this symbol could be hurt.

        What changed, or why it was not changed; empty when there was nothing to do. A position or
        a working order in long/short mode means something only its owner can untangle, and on
        mainnet the setting is the operator's to change at the exchange, never this software's.
        """
        if not self.hedged():
            return ""
        if self.environment == "mainnet":
            return "the account is in long/short mode; set it to one-way at the exchange"
        if self.position() or self._open_orders():
            return (
                f"the account is in long/short mode and holds a position or an order on "
                f"{self.symbol}; flatten it, then run this again"
            )
        found = account_settings(self.venue, self.client)
        try:
            if found is not None:
                found.set_one_way()
            else:
                self.client.set_position_mode(False, self.symbol)
        except Exception as exc:
            return f"the venue would not switch to one-way: {self._answer(exc)}"
        if self.hedged():
            return "the venue accepted the switch to one-way but still reads long/short"
        return "position mode set to one-way on this demo or testnet account"

    def _ensure_leverage(self) -> str:
        """Apply the declared leverage and margin mode at the venue, then confirm them.

        Setting a value a venue already holds is reported as an error by some venues, so success is
        judged by reading the setting back rather than by the reply to the request.
        """
        if self.leverage is None or self.leverage_set:
            return ""
        found = account_settings(self.venue, self.client)
        if found is not None:
            return self._ensure_leverage_on(found, self.leverage)
        # What the venue answered when a setting was refused. Some venues refuse a value they
        # already hold, so this is only said when the read-back shows the setting did not take.
        said: list[str] = []
        set_mode = getattr(self.client, "set_margin_mode", None)
        if set_mode is not None:
            try:
                set_mode(self.margin_mode, self.symbol)
            except Exception as exc:
                said.append(f"margin mode: {self._answer(exc)}")
        set_leverage = getattr(self.client, "set_leverage", None)
        if set_leverage is None:
            return f"{self.venue} cannot set leverage through ccxt; set it at the venue and say so"
        answered = False
        try:
            extra = leverage_params(self.venue)
            if extra:
                set_leverage(leverage_number(self.leverage), self.symbol, extra)
            else:
                set_leverage(leverage_number(self.leverage), self.symbol)
            answered = True
        except Exception as exc:
            said.append(f"leverage: {self._answer(exc)}")
        now, mode = self._leverage_now()
        has = getattr(self.client, "has", {}) or {}
        if now is None and answered and not has.get("fetchLeverage"):
            # Nothing to read it back with, so the venue's acceptance is the confirmation. A
            # reply that is an error still refuses below, and the record says which it was.
            self.leverage_set = self.leverage_by_reply = True
            return ""
        why = f" (the venue said {'; '.join(said)})" if said else ""
        if now is None:
            return (
                f"{self.venue} would not confirm the leverage on {self.symbol}{why}; "
                "nothing was sent"
            )
        if now != self.leverage:
            return (
                f"{self.symbol} is at {now}x at the venue and [execution].leverage is "
                f"{self.leverage}x; it could not be changed{why}, so nothing was sent"
            )
        if mode and mode != self.margin_mode:
            return (
                f"{self.symbol} is in {mode} margin at the venue and [execution].margin_mode is "
                f"{self.margin_mode}; it could not be changed{why}, so nothing was sent"
            )
        self.leverage_set = True
        return ""

    def close_position(self, reason: str) -> dict[str, Any]:
        """Close whatever this account holds on the symbol with a reduce-only market order.

        Exits are never price-limited: a close that fails is worse than one that slips. Reduce-only
        means the order can shrink the position and never open or flip one.
        """
        held = self.position()
        if not held:
            return {"closed": False, "note": "nothing is held"}
        side = "sell" if held > 0 else "buy"
        market = self.market()
        spot = market.held_as_balance or market.outcome
        if market.outcome:
            self.cancel_open_orders()
            return self._sell_outcome(held, reason)
        if spot:
            # The stop and take profit rest as orders that can hold the coins; a sale needs them.
            self.cancel_open_orders()
            held = self._holding(self.market())
            if not held:
                return {"closed": False, "note": "nothing is held once the exits were cancelled"}
        amount = abs(held) / self.contract_size()
        to_step = getattr(self.client, "amount_to_precision", None)
        if to_step is not None:
            # Down to the venue's step, never above what is held: a spot sale cannot be
            # reduce-only, so the step is the guard.
            with suppress(Exception):
                amount = Decimal(str(to_step(self.symbol, float(amount))))
        try:
            order = self.client.create_order(
                self.symbol,
                "market",
                side,
                float(amount),
                None,
                # A close carries an id of its own like every other order. Without one, ccxt fills
                # in its own broker id on some venues (seen on 2026-10-20 on a demo close).
                {"clientOrderId": self.close_id()}
                if spot
                else {
                    **self.order_params(),
                    "reduceOnly": True,
                    "clientOrderId": self.close_id(),
                },
            )
        except Exception as exc:
            return {
                "closed": False,
                "error": self.redact(f"{exc.__class__.__name__}: {exc}")[:300],
                "reason": reason,
            }
        order_id = str(order.get("id", ""))
        state = str(order.get("status") or "")
        average = order.get("average")
        if order_id and state not in ("closed", "filled", *ENDED):
            taken = self._taken(order_id)
            state, average = str(taken.get("status") or state), taken.get("average", average)
        if average is None and order_id:
            average = self._filled_average(order_id)
        # Accepted is not filled. Seen on a demo on 2026-10-17: a reduce-only market close on a
        # stock perpetual whose book sat outside the venue's price band rested unfilled, and the
        # account still held the position. The record says closed only when the account agrees.
        after = self.position()
        if after and (after > 0) == (held > 0):
            if order_id and state not in ("closed", "filled", *ENDED):
                self._cancel(order_id)
            return {
                "closed": False,
                "reason": reason,
                "order_id": order_id,
                "error": (
                    f"the close did not fill: the venue reports {state or 'no status'} and the "
                    f"account still holds {after}; the unfilled close was cancelled, the exits at "
                    "the venue still stand, and the next pass tries again"
                ),
            }
        closed: dict[str, Any] = {
            "closed": True,
            "reason": reason,
            "side": side,
            "size": f"{amount * self.contract_size():f}",
            "order_id": order_id,
            "average": None if average is None else str(average),
        }
        if self.exits == "separate":
            swept = self._sweep_exits()
            closed["exits_cancelled" if isinstance(swept, int) else "exits_left"] = swept
        return closed

    def _settle(self, order_id: str) -> dict[str, Any]:
        """Wait for a resting order to fill, then cancel what is left. One pass, no retries."""
        if not order_id:
            return {"filled": None, "cancelled": False, "waited_s": 0.0}
        started = self.clock()
        try:
            while self.clock() - started < self.limit_timeout_s:
                status = self.order_status(order_id)
                state = "" if status is None else str(status.get("status", ""))
                if status is not None and state in ("closed", "filled"):
                    return {
                        "filled": str(status.get("filled", "")),
                        "average": (
                            None if status.get("average") is None else str(status["average"])
                        ),
                        "cancelled": False,
                        "waited_s": round(self.clock() - started, 3),
                    }
                if status is not None and state in ENDED:
                    return self._ended(status, round(self.clock() - started, 3))
                self.sleep(min(1.0, self.limit_timeout_s))
        except KeyboardInterrupt:
            return self._interrupted(order_id, round(self.clock() - started, 3))
        waited = round(self.clock() - started, 3)
        why = self._cancel(order_id)
        if not why:
            return {
                "cancelled": True,
                "waited_s": waited,
                "note": "it did not fill inside [execution].limit_timeout_s and was cancelled",
            }
        # The cancel did not go through. Either the venue had already ended the order - which is
        # ordinary for a post-only limit - or it is still resting there, and those are very
        # different facts. Asking costs one call and turns a guess into a record.
        standing = self.order_status(order_id)
        state = "" if standing is None else str(standing.get("status", ""))
        if standing is not None and state in (*ENDED, "closed", "filled"):
            return {**self._ended(standing, waited), "cancel_refused": why}
        still_open = state in ("open", "new") or (standing is None)
        return {
            "cancelled": False,
            "waited_s": waited,
            "cancel_refused": why,
            "status": state or None,
            "note": (
                f"the cancel was refused ({why}) and the venue reports {state or 'nothing'}; "
                "an order may still be resting there, and the next cycle on this instrument will "
                "refuse a new entry while it is"
                if still_open
                else f"it did not fill in time; the venue had already ended it ({state})"
            ),
        }
