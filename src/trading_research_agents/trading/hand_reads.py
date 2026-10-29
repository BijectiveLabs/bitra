"""What the order hand reads from the venue before and after it sends: the account, capital, open
orders, the position, an order's status, and cancelling what was left.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from decimal import Decimal
from typing import Any

from trading_research_agents.trading.capital import (
    CapitalError,
    liquidation_estimate,
    maintenance_rate,
    margin_needed,
    read_account,
)
from trading_research_agents.trading.hand_state import BPS, EXITS, READ_BACKS, ROLES, DispatchError
from trading_research_agents.trading.hand_terms import HandTerms
from trading_research_agents.trading.market import Market, MarketError
from trading_research_agents.trading.venues.exits import exit_cancel_params, exit_reads
from trading_research_agents.trading.venues.limits import (
    price_band,
)
from trading_research_agents.trading.venues.margin import (
    AccountSettings,
)


class HandReads(HandTerms):
    def __post_init__(self) -> None:
        if self.max_notional <= 0:
            raise DispatchError("a venue hand needs a positive notional cap")
        if self.role not in ROLES:
            raise DispatchError(
                f"the channel does not say whether entry is maker or taker; name it "
                f"<entry>-<exit> in the envelope, one of {list(ROLES)}"
            )
        if self.limit_timeout_s <= 0:
            raise DispatchError("[execution].limit_timeout_s is positive")
        if self.exits not in EXITS:
            raise DispatchError(f"[execution].exits is one of {list(EXITS)}")
        if self.client is None:
            self.client = self._build()
        options = getattr(self.client, "options", None)
        if isinstance(options, dict):
            # ccxt reads the margin mode for orders, leverage and its read-back from here when a
            # call does not name one. Left out, a venue that defaults to cross margin
            # would trade cross while the operator declared isolated.
            options.setdefault("defaultMarginMode", self.margin_mode)

    def _rest_take(self, placed: Mapping[str, Any], intent: Mapping[str, Any]) -> dict[str, Any]:
        """An outcome's exit: what it filled, offered at the take price, resting at the venue.

        An outcome share cannot lose more than it cost, so the stake is the stop; the take profit
        is the price the analyst's probability says the share is worth, left on the book so it
        outlives this process the way an attached exit does.
        """
        filled = Decimal(str(placed.get("filled") or 0))
        take = intent.get("take_price")
        if filled <= 0 or not take:
            return {"take_order": None}
        try:
            order = self.client.create_order(
                self.symbol,
                "limit",
                "sell",
                float(filled),
                float(Decimal(str(take))),
                {**self.params, "timeInForce": "GTC"},
            )
        except Exception as exc:
            return {
                "take_order": None,
                "take_refused": self.redact(f"{exc.__class__.__name__}: {exc}")[:200],
            }
        return {"take_order": str(order.get("id", "")), "take_size": f"{filled:f}"}

    def contract_size(self) -> Decimal:
        """How much of the base one contract is. 1 for a venue that quotes in the base itself.

        Orders and positions are in contracts, and this package sizes in the base. One venue's
        Bitcoin contract is 0.01 of a coin, another's is 0.0001, and a meme coin's can be ten
        million tokens, so sending the base figure as an amount would be a hundred times too small
        on one and a million times too large on another.
        """
        try:
            return self.market().unit()
        except MarketError as exc:
            raise DispatchError(str(exc)) from exc

    def off_step(self, contracts: Decimal, size: Decimal, unit: Decimal) -> str:
        """Empty when the venue takes this size as it is; otherwise why it would not.

        ccxt rounds an amount to the market's precision before sending it. That is helpful
        everywhere except here: the cycle priced, risked and recorded one size, and a quietly
        rounded order is a different trade from the one on disk. So a size off the venue's own step,
        or under its minimum, is refused with both figures rather than adjusted. The step and the
        minimum are in contracts, so the size is judged as contracts.
        """
        to_step = getattr(self.client, "amount_to_precision", None)
        if self.market().outcome:
            to_step = getattr(self.client, "amount_to_prediction_precision", None)
        if to_step is None:
            return ""
        shown = f"{size} ({contracts} contracts of {unit})" if unit != 1 else f"{size}"
        try:
            load = getattr(self.client, "load_markets", None)
            if load is not None:
                load()
            stepped = Decimal(str(to_step(self.symbol, float(contracts))))
            market = self.client.market(self.symbol) if hasattr(self.client, "market") else {}
        except Exception as exc:
            said = self.redact(f"{exc.__class__.__name__}: {exc}")[:200]
            return f"{self.venue} will not take {shown} of {self.symbol}: {said}"
        if stepped != contracts:
            return (
                f"{shown} is off {self.venue}'s step for {self.symbol}, which would send "
                f"{stepped}; set [run].size_step for this instrument to the venue's increment"
            )
        limits = (market or {}).get("limits") or {}
        least = (limits.get("amount") or {}).get("min")
        if least is not None and contracts < Decimal(str(least)):
            return f"{shown} is under {self.venue}'s minimum of {least} for {self.symbol}"
        return ""

    def _sweep_exits(self) -> int | str:
        """Take off exits an earlier position left resting, before a new position can meet them.

        Called with nothing held, so anything found is reduce-only and belongs to a position that
        is gone. A take profit sized for that one would close part of the next. One that will not
        come off is a refusal, never a guess.
        """
        fetch = getattr(self.client, "fetch_open_orders", None)
        reads = exit_reads(self.venue)
        if fetch is None or not reads:
            return 0
        extra = exit_cancel_params(self.venue)
        gone = 0
        for read in reads:
            try:
                found = list(fetch(self.symbol, None, None, dict(read)) or [])
            except Exception as exc:
                return self.redact(
                    f"cannot read the resting exits: {exc.__class__.__name__}: {exc}"
                )
            for order in found:
                why = self._cancel(str(order.get("id", "")), extra)
                if why:
                    return (
                        f"an exit left from an earlier position on {self.symbol} is still resting "
                        f"and would not come off ({why}); a new entry would meet it"
                    )
                gone += 1
        return gone

    def _open_orders(self) -> int:
        """How many orders still work on this instrument. A hand that cannot ask does not guess."""
        fetch = getattr(self.client, "fetch_open_orders", None)
        if fetch is None:
            return 0
        try:
            return len(list(fetch(self.symbol)))
        except Exception as exc:
            raise DispatchError(
                self.redact(f"cannot read the open orders: {exc.__class__.__name__}: {exc}")
            ) from exc

    def find_sent(self, client_id: str) -> tuple[dict[str, Any] | None, list[str]]:
        """The order this hand sent, found among the venue's orders by its client id, and the
        order lists the venue would not show.

        Read a few times, a second apart: a venue that lost the answer to a create may take a
        moment to list the order it accepted. A list that cannot be read is not a list without the
        order, so its name is returned and the caller keeps the order UNKNOWN.
        """
        unread: list[str] = []
        if not client_id:
            return None, unread
        for attempt in range(READ_BACKS):
            if attempt:
                self.sleep(1.0)
            for read in ("fetch_open_orders", "fetch_closed_orders", "fetch_canceled_orders"):
                found = self._listed(read, client_id, unread)
                if found is not None:
                    return found, unread
        return None, unread

    def _listed(self, read: str, client_id: str, unread: list[str]) -> dict[str, Any] | None:
        fetch = getattr(self.client, read, None)
        if fetch is None:
            return None
        try:
            rows = fetch(self.symbol) or []
        except Exception as exc:
            unread.append(f"{read} ({exc.__class__.__name__})")
            return None
        for row in rows:
            if str(row.get("clientOrderId") or "") == client_id:
                return dict(row)
        return None

    def position(self) -> Decimal | None:
        """What the account already holds, or nothing when the venue will not say.

        A hand that cannot read the account does not send: an order into a position nobody can see
        is the one mistake that cannot be undone by reading the record afterwards.
        """
        market = self.market()
        if market.held_as_balance:
            return self._holding(market)
        if market.outcome:
            return self._shares()
        fetch = getattr(self.client, "fetch_position", None)
        if fetch is None:
            return None
        try:
            held = self._read_once_more_if_stale(fetch)
        except Exception as exc:
            if type(exc).__name__ == "NotSupported":
                # Seen on 2026-10-20: a venue that reads one position only for options through
                # ccxt reads its futures from the whole list, filtered here.
                held = self._position_in_list()
                if held is None:
                    return None
            elif type(exc).__name__ == "NullResponse":
                # The venue answered, with no position in it. Some adapters say that by raising
                # (seen through ccxt 4.5.84, 2026-10-08) rather than returning an empty one;
                # it is a read that succeeded and found the account flat, not a failed read.
                return Decimal(0)
            else:
                raise DispatchError(
                    self.redact(f"cannot read the account: {exc.__class__.__name__}: {exc}")
                ) from exc
        if not held:
            return Decimal(0)
        contracts = held.get("contracts")
        if contracts is None:
            return Decimal(0)
        signed = Decimal(str(contracts)) * self.contract_size()
        return -signed if str(held.get("side", "")).lower() == "short" else signed

    def _read_once_more_if_stale(self, fetch: Any) -> Any:
        """The position read, repeated once on a fresh connection when the first never answered.

        A pooled connection left idle past the venue's keep-alive can hang until the client's
        timeout (seen on 2026-10-26: four reads in ten after fifteen idle minutes). A read changes
        nothing at the venue, so it is safe to repeat; an answer the venue gave is never repeated.
        """
        try:
            return fetch(self.symbol)
        except Exception as exc:
            if not _unanswered(exc):
                raise
            session = getattr(self.client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                close()
            return fetch(self.symbol)

    def _position_in_list(self) -> Mapping[str, Any] | None:
        """This symbol's row of the venue's position list: empty when flat, None when unreadable."""
        fetch = getattr(self.client, "fetch_positions", None)
        if fetch is None:
            return None
        try:
            rows = list(fetch([self.symbol]) or [])
        except Exception as exc:
            raise DispatchError(
                self.redact(f"cannot read the positions: {exc.__class__.__name__}: {exc}")
            ) from exc
        mine = [r for r in rows if r.get("symbol") == self.symbol and r.get("contracts")]
        if len(mine) > 1:
            raise DispatchError(
                f"the venue reports {len(mine)} positions on {self.symbol}; this desk holds one"
            )
        return mine[0] if mine else {}

    def _shares(self) -> Decimal:
        """The shares of this outcome the account holds, as the venue's positions report them.

        A venue that books a binary market as one position reports the NO side as a short on the
        YES outcome, so a NO handle is also matched against its YES sibling held short.
        """
        try:
            rows = list(self.client.fetch_positions() or [])
        except Exception as exc:
            raise DispatchError(
                self.redact(f"cannot read the positions: {exc.__class__.__name__}: {exc}")
            ) from exc
        handle = self.symbol
        sibling = handle[: -len(":NO")] + ":YES" if handle.upper().endswith(":NO") else ""
        held = Decimal(0)
        for row in rows:
            name = str(row.get("outcome") or "")
            side = str(row.get("side") or "long").lower()
            if row.get("resolved"):
                continue
            if (name == handle and side == "long") or (
                sibling and name == sibling and side == "short"
            ):
                held += Decimal(str(row.get("contracts") or 0))
        return held

    def _holding(self, market: Market) -> Decimal:
        """The base coin the account holds; under the venue's minimum order it is nothing.

        A spot desk's position is coins in the account, and the account is the desk's. An amount
        too small to sell is fee residue, not a position, and not a reason to refuse an entry.
        """
        try:
            balance = self.client.fetch_balance()
        except Exception as exc:
            raise DispatchError(
                self.redact(f"cannot read the account: {exc.__class__.__name__}: {exc}")
            ) from exc
        row = balance.get(market.base) or {}
        total = Decimal(str(row.get("total") or 0))
        if total <= 0 or (market.amount_min is not None and total < market.amount_min):
            return Decimal(0)
        return total

    def _ensure_leverage_on(self, found: AccountSettings, leverage: Decimal) -> str:
        """The same confirmation on an account whose leverage is kept per margin mode."""
        said = ""
        try:
            found.apply(self.symbol, leverage, self.margin_mode)
        except Exception as exc:
            said = f" (the venue said {self._answer(exc)})"
        try:
            now = found.leverage(self.symbol, self.margin_mode)
        except Exception as exc:
            now, said = None, said or f" (the read said {self._answer(exc)})"
        if now is None:
            return (
                f"{self.venue} would not confirm the leverage on {self.symbol} in "
                f"{self.margin_mode} margin{said}; nothing was sent"
            )
        if now != leverage:
            shown = now.to_integral_value() if now == now.to_integral_value() else now
            return (
                f"{self.symbol} is at {shown}x in {self.margin_mode} margin at the venue and "
                f"[execution].leverage is {leverage}x; it could not be changed{said}, so nothing "
                "was sent"
            )
        self.leverage_set = True
        return ""

    def _answer(self, exc: Exception) -> str:
        return self.redact(f"{exc.__class__.__name__}: {exc}")[:160]

    def _capital(
        self, notional: Decimal, side: str, mid: Decimal, intent: Mapping[str, Any]
    ) -> dict[str, str] | str:
        """Is there margin for this order, and does liquidation sit safely beyond the stop?"""
        if self.leverage is None:
            return {}
        market = self.market()
        currency = market.settlement
        if not currency:
            return f"the settlement currency of {self.symbol} is unknown; nothing was sent"
        try:
            account = read_account(self.client, currency)
        except CapitalError as exc:
            return self.redact(str(exc))
        fee_bps = (market.taker or Decimal(0)) * BPS
        needed = margin_needed(notional, self.leverage, fee_bps)
        allowed = account.free * self.max_margin_share
        if needed > allowed:
            return (
                f"this order needs {needed:.2f} {currency} of margin at {self.leverage}x and "
                f"{self.max_margin_share} of the free {account.free:.2f} is {allowed:.2f}; "
                "nothing was sent"
            )
        record = {
            "margin_needed": str(needed.quantize(Decimal("0.01"))),
            "free_margin": str(account.free),
            "equity": str(account.equity),
            "leverage": str(self.leverage),
            "margin_mode": self.margin_mode,
        }
        stop = intent.get("stop_price")
        if not stop:
            return record
        maintenance = maintenance_rate(self.client, self.symbol)
        liquidation = liquidation_estimate(mid, side, self.leverage, maintenance)
        stop_price = Decimal(str(stop))
        buffer = stop_price * self.liquidation_buffer_bps / BPS
        unsafe = (
            liquidation >= stop_price - buffer
            if side == "buy"
            else liquidation <= stop_price + buffer
        )
        record["liquidation_estimate"] = str(liquidation.quantize(Decimal("0.01")))
        if unsafe:
            return (
                f"at {self.leverage}x liquidation is estimated at {liquidation:.2f}, inside "
                f"the stop at {stop_price} plus {self.liquidation_buffer_bps} bps; a stop past "
                "liquidation is not a stop, so lower [execution].leverage"
            )
        return record

    def _cash(
        self, notional: Decimal, market: Market, size: Decimal, price: Decimal
    ) -> dict[str, str] | str:
        """Is there quote currency for this order? A spot order is paid in full, fee included."""
        currency = market.quote
        if not currency:
            return f"the quote currency of {self.symbol} is unknown; nothing was sent"
        try:
            account = read_account(self.client, currency)
        except CapitalError as exc:
            return self.redact(str(exc))
        fee = self._fee(notional, market, size, price)
        needed = notional + fee
        allowed = account.free * self.max_margin_share
        if needed > allowed:
            return (
                f"this order costs {needed:.2f} {currency} with its fee and "
                f"{self.max_margin_share} of the free {account.free:.2f} is {allowed:.2f}; "
                "nothing was sent"
            )
        return {
            "cash_needed": str(needed.quantize(Decimal("0.01"))),
            "free_cash": str(account.free),
            "equity": str(account.equity),
        }

    def account(self) -> Any:
        """The account in this symbol's settlement currency, read through this hand's client."""
        currency = self.market().settlement
        if not currency:
            raise CapitalError(f"the settlement currency of {self.symbol} is unknown")
        return read_account(self.client, currency)

    def _protected_price(self, touch: Mapping[str, Any], side: str) -> Decimal | None:
        """The far edge of the band a taker order may fill within, on the venue's tick."""
        raw = touch.get("ask") if side == "buy" else touch.get("bid")
        if raw is None:
            return None
        edge = Decimal(str(raw))
        step = edge * self.max_slippage_bps / BPS
        price = edge + step if side == "buy" else edge - step
        # Held inside the venue's own band where it publishes one: an order priced past it is
        # refused whole, and the band's edge is never a worse price than the slippage bound.
        high, low = price_band(self.venue, self.client, self.symbol)
        market = self.market()
        # An outcome share trades between the venue's own floor and ceiling, never at 0 or 1.
        high = market.price_max if high is None and market.outcome else high
        low = market.price_min if low is None and market.outcome else low
        if side == "buy" and high is not None and price > high:
            price = high
        elif side == "sell" and low is not None and price < low:
            price = low
        to_tick = getattr(self.client, "price_to_precision", None)
        if market.outcome:
            to_tick = getattr(self.client, "price_to_prediction_precision", None)
        if to_tick is not None:
            with suppress(Exception):
                price = Decimal(str(to_tick(self.symbol, float(price))))
        return price

    def _sell_outcome(self, held: Decimal, reason: str) -> dict[str, Any]:
        """Sell an outcome's shares now: these venues take no market order, so a limit at the
        venue's floor, immediate or cancel, which is a market order that cannot rest."""
        floor = self.market().price_min or Decimal("0.01")
        try:
            order = self.client.create_order(
                self.symbol,
                "limit",
                "sell",
                float(held),
                float(floor),
                {**self.params, "timeInForce": "IOC"},
            )
        except Exception as exc:
            return {
                "closed": False,
                "error": self.redact(f"{exc.__class__.__name__}: {exc}")[:300],
                "reason": reason,
            }
        order_id = str(order.get("id", ""))
        after = self._shares()
        if after > 0:
            return {
                "closed": False,
                "reason": reason,
                "order_id": order_id,
                "error": (
                    f"the sale did not fill: the account still holds {after} shares; the next pass "
                    "tries again"
                ),
            }
        return {
            "closed": True,
            "reason": reason,
            "side": "sell",
            "size": f"{held:f}",
            "order_id": order_id,
            "average": None if order.get("average") is None else str(order["average"]),
        }

    def cancel_open_orders(self) -> int:
        """Cancel every working order on the symbol, attached exits included. How many went."""
        fetch = getattr(self.client, "fetch_open_orders", None)
        if fetch is None:
            return 0
        gone = 0
        with suppress(Exception):
            for order in fetch(self.symbol) or []:
                if not self._cancel(str(order.get("id", ""))):
                    gone += 1
        extra = exit_cancel_params(self.venue)
        for read in exit_reads(self.venue):
            # One kind the venue will not list is not a reason to skip the others.
            with suppress(Exception):
                for order in fetch(self.symbol, None, None, dict(read)) or []:
                    if not self._cancel(str(order.get("id", "")), extra):
                        gone += 1
        return gone

    def _interrupted(self, order_id: str, waited: float) -> dict[str, Any]:
        """The operator stopped the process while an order rested. Take it off the book first.

        Leaving here without a cancel would leave a post-only order working with nobody waiting on
        it, so the cancel is sent before anything else and the record says how it went. The
        caller sees `interrupted` and ends the run once the record is on disk.
        """
        why = self._cancel(order_id)
        return {
            "interrupted": True,
            "cancelled": not why,
            "cancel_refused": why or None,
            "waited_s": waited,
            "note": (
                "interrupted while the order rested; it was cancelled before the process left"
                if not why
                else f"interrupted while the order rested and the cancel was refused ({why}); "
                "check the venue, and the next cycle will refuse while an order works"
            ),
        }

    def order_status(self, order_id: str) -> Mapping[str, Any] | None:
        """What the venue says became of an order, or None when it will not say.

        Seen on 2026-10-04: one venue's fetch_order through ccxt refuses unless told the order is
        among the last 500, and that refusal was read as "no answer" for the whole wait. A venue
        that answers per state is asked per state: open first, then closed (which includes
        cancelled and rejected).
        """
        has = getattr(self.client, "has", {}) or {}
        ways: list[str] = []
        if has.get("fetchOpenOrder") and has.get("fetchClosedOrder"):
            ways += ["fetch_open_order", "fetch_closed_order"]
        ways.append("fetch_order")
        for way in ways:
            fetch = getattr(self.client, way, None)
            if fetch is None:
                continue
            found = None
            # Not found in this state, or a read this venue will not do: try the next way.
            with suppress(Exception):
                found = fetch(order_id, self.symbol)
            if found:
                return self.in_base(found, self.contract_size())
        # No single-order read answered: look for it in the open list, then the closed one.
        for listed, way in (
            ("fetchOpenOrders", "fetch_open_orders"),
            ("fetchClosedOrders", "fetch_closed_orders"),
        ):
            fetch = getattr(self.client, way, None)
            if fetch is None or not has.get(listed):
                continue
            with suppress(Exception):
                for order in fetch(self.symbol) or []:
                    if str(order.get("id", "")) == order_id:
                        return self.in_base(order, self.contract_size())
        return None

    def _cancel(self, order_id: str, params: Mapping[str, Any] | None = None) -> str:
        """Empty when the venue took the cancel; otherwise what it said, in its own words."""
        cancel = getattr(self.client, "cancel_order", None)
        if cancel is None:
            return "this venue has no cancel through ccxt"
        try:
            if params:
                cancel(order_id, self.symbol, dict(params))
            else:
                cancel(order_id, self.symbol)
        except Exception as exc:
            return self.redact(f"{exc.__class__.__name__}: {exc}")[:200]
        return ""


def _unanswered(exc: BaseException) -> bool:
    """A failure in which no answer arrived, as opposed to one the venue gave."""
    names = {kind.__name__ for kind in type(exc).__mro__}
    return bool(names & {"NetworkError", "TimeoutError", "ConnectionError"})
