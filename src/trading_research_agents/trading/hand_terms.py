"""The order hand's own terms: its client and environment, the market it sends to, the order
parameters and ids it uses, and what it redacts.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from decimal import Decimal
from typing import Any

from trading_research_agents.data.sources.ccxt_client import (
    SIGNED_DEFAULTS,
    exchange_class,
    market_of,
)
from trading_research_agents.data.sources.ccxt_client import build as build_client
from trading_research_agents.extras import install_hint
from trading_research_agents.trading.hand_state import (
    BPS,
    ENVIRONMENTS,
    MAX_CLOCK_SKEW_MS,
    PLAIN_PAIR,
    DispatchError,
    HandState,
    _namespace_missing,
    key_hint,
)
from trading_research_agents.trading.market import Market
from trading_research_agents.trading.venues.exits import exit_reads
from trading_research_agents.trading.venues.limits import (
    client_id_prefix,
)
from trading_research_agents.trading.venues.margin import (
    account_settings,
    route,
)


class HandTerms(HandState):
    def _build(self) -> Any:
        wanted = self.credentials
        held = {field: self.env.get(name, "") for field, name in wanted.items()}
        missing = [wanted[field] for field, value in held.items() if not value]
        if missing:
            hint = key_hint(missing, tuple(wanted.values()), self.env)
            raise DispatchError(
                f"{' and '.join(missing)} not set: not in the shell and not in the .env beside "
                "strategy.toml, the only two places keys are read from"
                + (f"; {hint}" if hint else "")
            )
        if exchange_class(self.venue) is None:
            if _namespace_missing():  # pragma: no cover - exercised only without the extra
                raise DispatchError(f"ccxt is not installed; {install_hint('ccxt')}")
            raise DispatchError(f"ccxt has no exchange named {self.venue!r}")
        config = {**held, "enableRateLimit": True, **self.options}
        config["options"] = {**SIGNED_DEFAULTS, **dict(config.get("options") or {})}
        client = build_client(self.venue, config)
        self.to_environment(client)
        route(self.venue, client, self.symbol)
        return client

    def to_environment(self, client: Any) -> None:
        """Point the client at the environment the operator declared, or refuse to guess.

        A venue's demo and its testnet are two different places with two different accounts, and a
        key for one is rejected by the other. They are reached through two different switches, so a
        client that has one of them does not necessarily have the other. Anything this cannot route
        exactly is refused rather than sent somewhere close, because the environment nobody checked
        is the one holding real capital.
        """
        if self.environment not in ENVIRONMENTS:
            raise DispatchError(
                f"[live].environment {self.environment!r} is not one of {sorted(ENVIRONMENTS)}"
            )
        if self.environment == "mainnet":
            return
        method = "enable_demo_trading" if self.environment == "demo" else "set_sandbox_mode"
        turn = getattr(client, method, None)
        refusal = (
            f"{self.venue} has no {self.environment} environment through ccxt; name an "
            "environment this venue has, deliberately (`tra venues` lists them)"
        )
        if turn is None:
            raise DispatchError(refusal)
        try:
            turn(True)
        except Exception as exc:
            # ccxt's base class defines both switches and raises NotSupported from the ones a
            # venue does not implement, so the method existing proves nothing.
            raise DispatchError(f"{refusal}: {exc.__class__.__name__}") from exc

    @property
    def credentials(self) -> dict[str, str]:
        """ccxt field -> environment variable, whichever way the keys were given."""
        if isinstance(self.keys, Mapping):
            return dict(self.keys)
        return dict(zip(PLAIN_PAIR, self.keys, strict=True))

    def _filled_average(self, order_id: str) -> str | None:
        """The price an order filled at, from its own fills, where the venue's order omits it.

        Seen on 2026-10-20: Bitget's unified account reports a filled order with no average through
        ccxt, and without one the slippage of the entry cannot be measured.
        """
        fetch = getattr(self.client, "fetch_my_trades", None)
        if fetch is None:
            return None
        try:
            fills = [f for f in fetch(self.symbol) or [] if str(f.get("order") or "") == order_id]
            size = sum((Decimal(str(f["amount"])) for f in fills), Decimal(0))
            paid = sum(
                (Decimal(str(f["amount"])) * Decimal(str(f["price"])) for f in fills), Decimal(0)
            )
        except Exception:
            return None
        if size <= 0:
            return None
        return f"{(paid / size).quantize(Decimal('0.00000001')).normalize():f}"

    def _protective(self, intent: Mapping[str, Any], target: str) -> dict[str, Any] | str:
        """The stop and the take profit, attached to the entry so no fill is left unattended.

        They go out with the entry rather than after it. A position that exists for a second
        without an exit is a position this process can die in the middle of, and the whole point of
        placing them at the venue is that they outlive us.
        """
        if self.exits == "none":
            return {}
        stop, take = intent.get("stop_price"), intent.get("take_price")
        if not stop or not take:
            return (
                "no stop and take profit were computed for this intent; declare "
                "[exit].stop_frac and [exit].take_profit_frac, or say "
                '[execution].exits = "none" and carry the exits yourself'
            )
        low, high = Decimal(str(stop)), Decimal(str(take))
        if (target == "long" and low >= high) or (target == "short" and low <= high):
            return f"the stop {stop} and the take profit {take} are the wrong way round"
        if self.exits == "separate":
            if not self._can_place_exits():
                return (
                    f"{self.venue} cannot take a stop or a take profit as an order of its own "
                    "through ccxt, so exits placed after the fill are not possible there"
                )
            if not exit_reads(self.venue):
                # Exits placed on their own outlive the position they protect when one leg fires.
                # Where they rest is read per venue; one not read yet could meet the next entry.
                return (
                    f"where {self.venue} keeps a stop placed on its own is not read here yet, so "
                    "one left from an earlier position could not be taken off before the next "
                    'entry; use exits = "attached" or "none"'
                )
            return {}
        if not self._can_attach():
            hint = (
                '[execution].exits = "separate" places them as reduce-only orders right after '
                "the fill"
                if self._can_place_exits()
                else '[execution].exits = "none" is for a desk carrying the exits itself'
            )
            return f"{self.venue} cannot take a stop and a take profit with the entry; {hint}"
        return {
            "stopLoss": {"triggerPrice": float(low)},
            "takeProfit": {"triggerPrice": float(high)},
        }

    def close_id(self) -> str:
        """A close's own client id: the venue's prefix, then the moment it was sent."""
        return f"{client_id_prefix(self.venue)}tra{int(self.clock() * 1000)}c"

    def order_params(self) -> dict[str, Any]:
        """What a derivatives order on this account carries beyond ccxt's own fields."""
        found = account_settings(self.venue, self.client)
        return found.order_params(self.margin_mode) if found is not None else {}

    def redact(self, text: str) -> str:
        """A venue's message without this hand's key or secret in it, before it is recorded."""
        for name in self.credentials.values():
            held = self.env.get(name, "")
            if len(held) >= 8:
                text = text.replace(held, "[redacted]")
        return text

    def hedged(self) -> str:
        """Why an account in long/short position mode is refused, or nothing when it is one-way.

        In long/short mode a position is two (a long and a short), every order names which side it
        belongs to, and a reduce-only close means something else. The exits, the position reads and
        the close here are written for one position per instrument, so this does not guess.
        """
        refusal = (
            f"the {self.venue} account is in long/short position mode; set it to one-way (net) "
            "mode, because this desk holds one position per instrument"
        )
        found = account_settings(self.venue, self.client)
        has = getattr(self.client, "has", {}) or {}
        read = getattr(self.client, "fetch_position_mode", None)
        try:
            if found is not None:
                hedged = found.hedged()
            elif read is not None and has.get("fetchPositionMode"):
                hedged = (read(self.symbol) or {}).get("hedged")
            else:
                return ""
        except Exception:
            # Not readable is not the same as hedged; a venue that cannot say refuses at its end.
            return ""
        return refusal if hedged is True else ""

    def clock_skew(self) -> str:
        """Empty when this machine's clock agrees with the venue's; otherwise by how much."""
        fetch = getattr(self.client, "fetch_time", None)
        if fetch is None:
            return ""
        # Three readings, and the one with the shortest round trip is believed. The first request
        # on a fresh connection carries the TLS handshake, which moved the midpoint by 300 ms and
        # more on 2026-10-08 (842 ms for the first round trip, 210 ms for the next ones).
        best: tuple[float, float] | None = None
        try:
            for _ in range(3):
                before = self.clock() * 1000
                venue_ms = float(fetch())
                after = self.clock() * 1000
                if best is None or after - before < best[0]:
                    best = (after - before, (before + after) / 2 - venue_ms)
        except Exception:
            # A venue that will not tell the time is not a reason to refuse; the account read that
            # follows fails loudly if the signature does.
            if best is None:
                return ""
        if best is None:
            return ""
        skew = best[1]
        if abs(skew) <= MAX_CLOCK_SKEW_MS:
            return ""
        return (
            f"this machine's clock is {skew:+.0f} ms from {self.venue}; more than "
            f"{MAX_CLOCK_SKEW_MS} ms and a signature can fail mid-order. Sync the clock first"
        )

    @staticmethod
    def in_base(order: Mapping[str, Any], unit: Decimal) -> dict[str, Any]:
        """A venue's order with its quantities in the base, so nothing downstream sees contracts."""
        out = dict(order)
        if unit != 1:
            for key in ("filled", "amount", "remaining"):
                if out.get(key) is not None:
                    # Fixed-point: Decimal's own str writes 10 contracts of 0.001 as 1E+1.
                    out[key] = f"{Decimal(str(out[key])) * unit:f}"
        return out

    def _can_attach(self) -> bool:
        has = getattr(self.client, "has", {}) or {}
        return bool(has.get("createOrderWithTakeProfitAndStopLoss"))

    def _can_place_exits(self) -> bool:
        has = getattr(self.client, "has", {}) or {}
        return bool(has.get("createStopLossOrder")) and bool(has.get("createTakeProfitOrder"))

    def _leverage_now(self) -> tuple[Decimal | None, str]:
        """The leverage and the margin mode the venue reports for the symbol, as far as it says."""
        fetch = getattr(self.client, "fetch_leverage", None)
        if fetch is None:
            return None, ""
        found: Mapping[str, Any] = {}
        with suppress(Exception):
            found = fetch(self.symbol) or {}
        value = found.get("longLeverage") or found.get("leverage")
        mode = str(found.get("marginMode") or "").lower()
        if value is None:
            # Seen on 2026-10-20: one venue keeps the leverage on the position itself and reads it
            # nowhere else through ccxt, flat or not; the position list carries it with the mode.
            row = self._position_row()
            value, mode = row.get("leverage"), str(row.get("marginMode") or "").lower()
        elif not mode and (getattr(self.client, "has", {}) or {}).get("fetchMarginMode"):
            with suppress(Exception):
                mode = str(self.client.fetch_margin_mode(self.symbol).get("marginMode") or "")
                mode = mode.lower()
        if value is None:
            return None, mode
        try:
            read = Decimal(str(value))
        except ArithmeticError:
            return None, mode
        # Venues answer 10.0 for 10; the record and the refusal should say 10.
        return (read.to_integral_value() if read == read.to_integral_value() else read), mode

    def _position_row(self) -> Mapping[str, Any]:
        """This symbol's row of the position list, flat rows included, or nothing."""
        fetch = getattr(self.client, "fetch_positions", None)
        if fetch is None:
            return {}
        with suppress(Exception):
            for row in fetch([self.symbol]) or []:
                if row.get("symbol") == self.symbol:
                    return dict(row)
        return {}

    def _fee(self, notional: Decimal, market: Market, size: Decimal, price: Decimal) -> Decimal:
        """The fee of buying `size` at `price`: the venue's own calculation for an outcome, since a
        prediction venue's fee is not a flat rate (one charges 0.07 x shares x p x (1 - p))."""
        calculate = getattr(self.client, "calculate_fee", None)
        if market.outcome and calculate is not None:
            with suppress(Exception):
                found = calculate(self.symbol, "limit", "buy", float(size), float(price))
                return Decimal(str(found["cost"]))
        return notional * (market.taker or Decimal(0))

    def market(self) -> Market:
        """This hand's market as the venue describes it through ccxt, in the package's one shape."""
        return Market.from_ccxt(self.venue, self.symbol, self._market())

    def _market(self) -> Mapping[str, Any]:
        with suppress(Exception):
            load = getattr(self.client, "load_markets", None)
            if load is not None:
                load()
        return market_of(self.client, self.symbol)

    def settlement(self, since_ms: int) -> dict[str, Any] | None:
        """How this outcome settled since `since_ms`: shares, the price paid out (1 or 0), when.

        None when the venue reports no settlement for it, or none through ccxt.
        """
        has = getattr(self.client, "has", {}) or {}
        if not has.get("fetchSettlements"):
            return None
        rows = self.client.fetch_settlements(self.symbol, since_ms) or []
        for row in rows:
            if str(row.get("outcome") or "") != self.symbol or row.get("amount") is None:
                continue
            return {
                "amount": Decimal(str(row["amount"])),
                "price": Decimal(str(row.get("price") or 0)),
                "won": bool(row.get("won")),
                "timestamp": row.get("timestamp") or 0,
            }
        return None

    def _touch_now(self) -> tuple[Decimal | None, Decimal | None]:
        """The best bid and ask at this moment, or nothing when the venue will not say."""
        fetch = getattr(self.client, "fetch_order_book", None)
        if fetch is None:
            return None, None
        self.book_age_s = None
        try:
            book = fetch(self.symbol)
            bid = Decimal(str(book["bids"][0][0])) if book.get("bids") else None
            ask = Decimal(str(book["asks"][0][0])) if book.get("asks") else None
            stamp = book.get("timestamp")
            if stamp:
                self.book_age_s = self.clock() - float(stamp) / 1000
        except Exception:
            # Without a fresh book the order rests at the snapshot's touch, as it always did.
            return None, None
        return bid, ask

    def _resting_price(self, intent: Mapping[str, Any], side: str) -> Decimal | None:
        """Where a maker order sits: this snapshot's own touch, moved by the declared offset."""
        touch = intent.get("bid") if side == "buy" else intent.get("ask")
        if touch is None:
            return None
        price = Decimal(str(touch))
        if self.price_offset_bps:
            # Away from the touch is further from a fill and further from crossing. Both legs of
            # that trade-off are the operator's to make, so the sign follows the side.
            step = price * self.price_offset_bps / BPS
            price = price - step if side == "buy" else price + step
        return price

    def _ended(self, status: Mapping[str, Any], waited: float) -> dict[str, Any]:
        """The venue finished the order itself: say how, and how much of it filled."""
        state = str(status.get("status", ""))
        filled = Decimal(str(status.get("filled") or 0))
        return {
            "cancelled": False,
            "status": state,
            "filled": str(filled),
            "average": None if status.get("average") is None else str(status["average"]),
            "waited_s": waited,
            "note": (
                f"the venue ended the order itself ({state}) with {filled} filled; "
                + (
                    "nothing is held from it"
                    if filled == 0
                    else "that part is a position, with its exits attached at the venue"
                )
            ),
        }
