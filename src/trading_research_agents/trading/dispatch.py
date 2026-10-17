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
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_research_agents.execution.mandate import DEFAULT_MARKETS, MARKETS
from trading_research_agents.extras import install_hint
from trading_research_agents.trading.capital import (
    CapitalError,
    liquidation_estimate,
    maintenance_rate,
    margin_needed,
    read_account,
)
from trading_research_agents.trading.market import Market, MarketError
from trading_research_agents.trading.venues.accounts import account_refusal
from trading_research_agents.trading.venues.exits import exit_cancel_params, exit_reads

SIDES = {"long": "buy", "short": "sell"}
# Read from ccxt 4.5 on 2026-09-29: a venue reaches its demo environment through
# `enable_demo_trading` and its testnet through `set_sandbox_mode`, and the library refuses to have
# both at once. Mainnet is the client's own default and is never switched into.
ENVIRONMENTS = ("demo", "testnet", "mainnet")
ROLES = ("maker", "taker")
EXITS = ("attached", "none")
# A venue that does not describe its markets through ccxt is let through: the size, margin and
# exit checks that follow still read the account, and refuse on what they find there.
UNDESCRIBED = "unknown"
# Order states in which the venue has finished with an order without it being fully filled.
ENDED = ("canceled", "cancelled", "rejected", "expired")
BPS = Decimal(10_000)
# The same bound a live session holds itself to. ccxt signs with a wider receive window, so a clock
# past this still works until the day it drifts a little further, mid-order.
MAX_CLOCK_SKEW_MS = 1000


# ccxt's credential fields, and the suffix of the environment variable each is read from. Most
# venues want a key and a secret; some add a passphrase, and some sign with a wallet instead.
CREDENTIAL_ENV = {
    "apiKey": "API_KEY",
    "secret": "API_SECRET",
    "password": "API_PASSPHRASE",
    "uid": "API_UID",
    "walletAddress": "WALLET_ADDRESS",
    "privateKey": "PRIVATE_KEY",
}
PLAIN_PAIR = ("apiKey", "secret")


class DispatchError(ValueError):
    """The order cannot be sent as configured, or the venue refused it."""


@dataclass
class OrderRate:
    """The orders the desk sent in the last minute, against [controls].max_orders_per_minute."""

    per_minute: int
    sent: list[float] = field(default_factory=list)

    def admit(self, now: float) -> str:
        self.sent = [at for at in self.sent if now - at < 60]
        if len(self.sent) >= self.per_minute:
            return (
                f"{len(self.sent)} orders in the last minute and [controls].max_orders_per_minute "
                f"is {self.per_minute}"
            )
        self.sent.append(now)
        return ""


def credentials_for(venue: str, client: Any = None) -> dict[str, str]:
    """What this venue signs with, as ccxt field -> environment variable.

    Read from ccxt's own `requiredCredentials`, so a venue that needs a passphrase is asked for one
    by name before anything is sent rather than refused at the venue with a signature error.
    """
    prefix = venue.strip().upper()
    if not prefix:
        return {}
    required: dict[str, Any] = {}
    if client is None:
        try:
            import ccxt

            factory = getattr(ccxt, venue.strip().lower(), None)
            client = factory() if factory is not None else None
        except ImportError:  # pragma: no cover - exercised only without the extra
            client = None
    if client is not None:
        required = dict(getattr(client, "requiredCredentials", None) or {})
    fields = [name for name in CREDENTIAL_ENV if required.get(name)] or list(PLAIN_PAIR)
    return {name: f"{prefix}_{CREDENTIAL_ENV[name]}" for name in fields}


@dataclass
class VenueHand:
    """What a cycle in venue mode sends with: one venue, one instrument, one cap, one role.

    The client is built here from the environment, so the key is read in one place and never
    travels through a config file, a brief, a prompt or a record. `demo` and `testnet` put the
    client into the venue's own sandbox, which is the only way this should ever be tried first.
    """

    venue: str
    symbol: str
    max_notional: Decimal
    # ccxt field -> environment variable. A plain (key, secret) pair is accepted and read as such.
    keys: Mapping[str, str] | tuple[str, ...]
    # The instrument this hand's symbol stands for. A hand is built for one instrument, and an
    # intent about another one is refused rather than sent under this symbol.
    instrument: str = ""
    role: str = "taker"
    exits: str = "attached"
    environment: str = "demo"
    limit_timeout_s: float = 30.0
    price_offset_bps: Decimal = Decimal(0)
    adopt_open_position: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)
    max_drift_bps: Decimal | None = None
    max_decision_age_s: float = 120.0
    leverage: Decimal | None = None
    margin_mode: str = "isolated"
    max_slippage_bps: Decimal = Decimal(20)
    max_margin_share: Decimal = Decimal("0.5")
    liquidation_buffer_bps: Decimal = Decimal(50)
    markets: tuple[str, ...] = DEFAULT_MARKETS
    # Shared by every hand in a run: [controls].max_orders_per_minute is the desk's, not a hand's.
    rate: OrderRate | None = None
    client: Any = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    options: Mapping[str, Any] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    skew_checked: bool = False
    account_checked: bool = False
    leverage_set: bool = False
    sent_count: int = 0

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

    def _build(self) -> Any:
        wanted = self.credentials
        held = {field: self.env.get(name, "") for field, name in wanted.items()}
        missing = [wanted[field] for field, value in held.items() if not value]
        if missing:
            raise DispatchError(
                f"{' and '.join(missing)} not set: not in the shell and not in the .env beside "
                "strategy.toml, the only two places keys are read from"
            )
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise DispatchError(f"ccxt is not installed; {install_hint('ccxt')}") from exc
        factory = getattr(ccxt, self.venue, None)
        if factory is None:
            raise DispatchError(f"ccxt has no exchange named {self.venue!r}")
        client = factory({**held, "enableRateLimit": True, **self.options})
        self.to_environment(client)
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
            derivative = not market.held_as_balance
            hedged = self.hedged() if derivative else ""
            if hedged:
                return hedged
            setting = account_refusal(self.venue, self.client, derivative=derivative)
            if setting:
                return setting
            self._open_orders()
            self.position()
        except DispatchError as exc:
            return str(exc)
        except Exception as exc:
            return f"{exc.__class__.__name__}: {exc}"
        return ""

    def __call__(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        """Send it, or say why it was not sent. Either way the caller gets a record."""
        about = str(intent.get("instrument", ""))
        if self.instrument and about and about != self.instrument:
            # A desk that watches several instruments builds a hand per instrument. Sending this
            # one under another's symbol is the one mistake no record afterwards can undo.
            return {
                "sent": False,
                "refused": (
                    f"this hand sends {self.instrument} as {self.symbol}; the intent is about "
                    f"{about}"
                ),
            }
        if self.sent_count:
            # One decision, one order. A hand asked twice is a wiring fault, not a second trade.
            return {"sent": False, "refused": "this hand has already sent its one order"}
        market = self.market()
        kind = market.label
        if kind not in MARKETS and kind != UNDESCRIBED:
            # A type with no rules here would be priced and protected by another type's rules.
            return {
                "sent": False,
                "refused": (
                    f"{self.symbol} is a {kind} market; venue mode trades "
                    f"{' and '.join(MARKETS)} markets today, and other market types are not "
                    "built yet"
                ),
            }
        if kind not in self.markets and kind != UNDESCRIBED:
            return {
                "sent": False,
                "refused": (
                    f"{self.symbol} is a {kind} market and [execution].markets lists "
                    f"{list(self.markets)}"
                ),
            }
        target = str(intent.get("target", ""))
        if target not in SIDES:
            return {"sent": False, "refused": f"target {target!r} is not an order"}
        if target == "short" and not market.shortable:
            return {
                "sent": False,
                "refused": (
                    f"{self.symbol} is a {kind} market; a short there sells coins the account "
                    "does not hold, which needs a borrow this desk does not take"
                ),
            }
        derivative = not market.held_as_balance
        side = SIDES[target]
        size = Decimal(str(intent.get("size", "0")))
        mid = Decimal(str(intent.get("mid", "0")))
        if size <= 0:
            return {"sent": False, "refused": "size is not positive"}
        notional = size * mid
        if notional > self.max_notional:
            return {
                "sent": False,
                "refused": (
                    f"notional {notional} is above [execution].max_notional {self.max_notional}"
                ),
            }
        age = _decision_age_s(intent, self.clock())
        if age is not None and age > self.max_decision_age_s:
            return {
                "sent": False,
                "refused": (
                    f"the decision is {age:.0f}s old, past [execution].max_decision_age_s "
                    f"{self.max_decision_age_s:.0f}; its figures are no longer the market's"
                ),
                "decision_age_s": round(age, 1),
            }
        if not self.skew_checked:
            skew = self.clock_skew()
            if skew:
                return {"sent": False, "refused": skew}
            self.skew_checked = True
        hedged = self.hedged() if derivative else ""
        if hedged:
            return {"sent": False, "refused": hedged}
        if not self.account_checked:
            setting = account_refusal(self.venue, self.client, derivative=derivative)
            if setting:
                return {"sent": False, "refused": setting}
            self.account_checked = True
        standing = self.position()
        if standing and not self.adopt_open_position:
            # Any open position, not only one pointing the other way. A second entry in the same
            # direction is a larger position than the conviction rule sized, and nothing upstream
            # asked for it.
            way = "the other way" if (standing > 0) != (target == "long") else "the same way"
            return {
                "sent": False,
                "refused": (
                    f"the account already holds {standing} of {self.symbol} {way}; flatten it, "
                    "wait for it to close, or set [execution].adopt_open_position"
                ),
                "position": str(standing),
            }
        resting = self._open_orders()
        if resting:
            return {
                "sent": False,
                "refused": (
                    f"{resting} order(s) are still open on {self.symbol}; a second one would be a "
                    "second decision on the same position"
                ),
                "open_orders": resting,
            }

        try:
            unit = self.contract_size()
        except DispatchError as exc:
            return {"sent": False, "refused": str(exc)}
        contracts = size / unit
        off_step = self.off_step(contracts, size, unit)
        if off_step:
            return {"sent": False, "refused": off_step}

        # The snapshot this was priced on is as old as the analysts and validators were slow -
        # half a minute is ordinary. Seen on 2026-10-04: a post-only order rested at that stale
        # touch was on the wrong side of a market that had moved, and the venue rejected it at once.
        now_bid, now_ask = self._touch_now()
        drift = _drift_bps(mid, now_bid, now_ask, side)
        if drift is not None and self.max_drift_bps is not None and drift > self.max_drift_bps:
            return {
                "sent": False,
                "refused": (
                    f"the book moved {drift:.2f} bps against the trade since it was priced, more "
                    f"than [execution].max_drift_bps {self.max_drift_bps}; the decision is stale"
                ),
                "drift_bps": str(drift.quantize(Decimal("0.01"))),
            }
        fresh = {"bid": now_bid, "ask": now_ask} if now_bid and now_ask else None

        held_back = self._ensure_leverage() if derivative else ""
        if held_back:
            return {"sent": False, "refused": held_back}
        carried = (
            self._capital(notional, side, mid, intent)
            if derivative
            else self._cash(notional, market)
        )
        if isinstance(carried, str):
            return {"sent": False, "refused": carried}
        if self.rate is not None:
            busy = self.rate.admit(self.clock())
            if busy:
                return {"sent": False, "refused": busy}

        order_id = _client_id(intent)
        params: dict[str, Any] = {"clientOrderId": order_id, **dict(self.params)}
        protective = self._protective(intent, target)
        if isinstance(protective, str):
            return {"sent": False, "refused": protective}
        params.update(protective)
        price = self._resting_price(fresh or intent, side) if self.role == "maker" else None
        if self.role == "maker":
            if price is None:
                return {"sent": False, "refused": "no book side to rest on; the snapshot had none"}
            params["postOnly"] = True
            params.setdefault("timeInForce", "GTC")
        else:
            # Never a naked market order: a limit that takes what is there within the band and
            # cancels the rest, so a thin book cannot fill the entry at any price it likes.
            price = self._protected_price(fresh or intent, side)
            if price is None:
                return {"sent": False, "refused": "no book to protect a taker order against"}
            params.setdefault("timeInForce", "IOC")
        began = self.clock()
        try:
            order = self.client.create_order(
                self.symbol,
                "limit",
                side,
                float(contracts),
                float(price),
                params,
            )
        except Exception as exc:
            raise DispatchError(self.redact(f"{exc.__class__.__name__}: {exc}")) from exc
        send_ms = round((self.clock() - began) * 1000)
        order = self.in_base(order, unit)
        self.sent_count += 1

        placed: dict[str, Any] = {
            "sent": True,
            "venue": self.venue,
            "instrument": self.instrument,
            "environment": self.environment,
            "symbol": self.symbol,
            "role": self.role,
            "side": side,
            "size": str(size),
            **({"contracts": f"{contracts:f}", "contract_size": f"{unit:f}"} if unit != 1 else {}),
            "price": None if price is None else str(price),
            "notional": str(notional),
            "client_order_id": order_id,
            "exits": self.exits,
            "stop_price": str(intent.get("stop_price", "")) or None,
            "take_price": str(intent.get("take_price", "")) or None,
            "priced_mid": str(mid),
            "drift_bps": None if drift is None else str(drift.quantize(Decimal("0.01"))),
            "decision_age_s": None if age is None else round(age, 1),
            "send_ms": send_ms,
            "rested_at": "the book at send time" if fresh else "the snapshot's touch",
            "capital": carried,
            "order_id": str(order.get("id", "")),
            # A venue that reports no status reports none: "None" is not a status.
            "status": None if order.get("status") is None else str(order["status"]),
            "filled": None if order.get("filled") is None else str(order["filled"]),
            "average": None if order.get("average") is None else str(order["average"]),
        }
        if self.role == "maker":
            placed.update(self._settle(str(placed["order_id"])))
        elif placed["filled"] is None:
            placed.update(self._taken(str(placed["order_id"])))
        placed["slippage_bps"] = _slippage_bps(placed.get("average"), mid, side)
        return placed

    def _taken(self, order_id: str, tries: int = 4) -> dict[str, Any]:
        """What an IOC entry actually filled. Some venues answer a create with only an id."""
        for attempt in range(tries):
            status = self.order_status(order_id) if order_id else None
            state = "" if status is None else str(status.get("status", ""))
            if status is not None and state in ("closed", "filled", *ENDED):
                return {
                    "status": state,
                    "filled": str(status.get("filled") or 0),
                    "average": None if status.get("average") is None else str(status["average"]),
                }
            if attempt + 1 < tries:
                self.sleep(0.5)
        return {"note": "the venue had not reported a fill when this record was written"}

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
        if not self._can_attach():
            return (
                f"{self.venue} cannot take a stop and a take profit with the entry through ccxt; "
                'set [execution].exits = "none" only if you are carrying the exits yourself'
            )
        low, high = Decimal(str(stop)), Decimal(str(take))
        if (target == "long" and low >= high) or (target == "short" and low <= high):
            return f"the stop {stop} and the take profit {take} are the wrong way round"
        return {
            "stopLoss": {"triggerPrice": float(low)},
            "takeProfit": {"triggerPrice": float(high)},
        }

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
        has = getattr(self.client, "has", {}) or {}
        read = getattr(self.client, "fetch_position_mode", None)
        if read is None or not has.get("fetchPositionMode"):
            return ""
        try:
            mode = read(self.symbol) or {}
        except Exception:
            # Not readable is not the same as hedged; a venue that cannot say refuses at its end.
            return ""
        if mode.get("hedged") is True:
            return (
                f"the {self.venue} account is in long/short position mode; set it to one-way (net) "
                "mode, because this desk holds one position per instrument"
            )
        return ""

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

    def off_step(self, contracts: Decimal, size: Decimal, unit: Decimal) -> str:
        """Empty when the venue takes this size as it is; otherwise why it would not.

        ccxt rounds an amount to the market's precision before sending it. That is helpful
        everywhere except here: the cycle priced, risked and recorded one size, and a quietly
        rounded order is a different trade from the one on disk. So a size off the venue's own step,
        or under its minimum, is refused with both figures rather than adjusted. The step and the
        minimum are in contracts, so the size is judged as contracts.
        """
        to_step = getattr(self.client, "amount_to_precision", None)
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

    def _can_attach(self) -> bool:
        has = getattr(self.client, "has", {}) or {}
        return bool(has.get("createOrderWithTakeProfitAndStopLoss"))

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

    def position(self) -> Decimal | None:
        """What the account already holds, or nothing when the venue will not say.

        A hand that cannot read the account does not send: an order into a position nobody can see
        is the one mistake that cannot be undone by reading the record afterwards.
        """
        market = self.market()
        if market.held_as_balance:
            return self._holding(market)
        fetch = getattr(self.client, "fetch_position", None)
        if fetch is None:
            return None
        try:
            held = fetch(self.symbol)
        except Exception as exc:
            if type(exc).__name__ == "NullResponse":
                # The venue answered, with no position in it. Some adapters say that by raising
                # (seen through ccxt 4.5.84, 2026-10-08) rather than returning an empty one;
                # it is a read that succeeded and found the account flat, not a failed read.
                return Decimal(0)
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

    def _ensure_leverage(self) -> str:
        """Apply the declared leverage and margin mode at the venue, then confirm them.

        Setting a value a venue already holds is reported as an error by some venues, so success is
        judged by reading the setting back rather than by the reply to the request.
        """
        if self.leverage is None or self.leverage_set:
            return ""
        set_mode = getattr(self.client, "set_margin_mode", None)
        if set_mode is not None:
            with suppress(Exception):
                set_mode(self.margin_mode, self.symbol)
        set_leverage = getattr(self.client, "set_leverage", None)
        if set_leverage is None:
            return f"{self.venue} cannot set leverage through ccxt; set it at the venue and say so"
        with suppress(Exception):
            set_leverage(float(self.leverage), self.symbol)
        now = self._leverage_now()
        if now is None:
            return f"{self.venue} would not confirm the leverage on {self.symbol}; nothing was sent"
        if now != self.leverage:
            return (
                f"{self.symbol} is at {now}x at the venue and [execution].leverage is "
                f"{self.leverage}x; it could not be changed, so nothing was sent"
            )
        self.leverage_set = True
        return ""

    def _leverage_now(self) -> Decimal | None:
        fetch = getattr(self.client, "fetch_leverage", None)
        if fetch is None:
            return None
        try:
            found = fetch(self.symbol) or {}
            value = found.get("longLeverage") or found.get("leverage")
            if value is None:
                return None
            read = Decimal(str(value))
            # Venues answer 10.0 for 10; the record and the refusal should say 10.
            return read.to_integral_value() if read == read.to_integral_value() else read
        except Exception:
            return None

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

    def _cash(self, notional: Decimal, market: Market) -> dict[str, str] | str:
        """Is there quote currency for this order? A spot order is paid in full, fee included."""
        currency = market.quote
        if not currency:
            return f"the quote currency of {self.symbol} is unknown; nothing was sent"
        try:
            account = read_account(self.client, currency)
        except CapitalError as exc:
            return self.redact(str(exc))
        fee_bps = (market.taker or Decimal(0)) * BPS
        needed = notional + notional * fee_bps / BPS
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

    def market(self) -> Market:
        """This hand's market as the venue describes it through ccxt, in the package's one shape."""
        return Market.from_ccxt(self.venue, self.symbol, self._market())

    def _market(self) -> Mapping[str, Any]:
        with suppress(Exception):
            load = getattr(self.client, "load_markets", None)
            if load is not None:
                load()
            found = self.client.market(self.symbol)
            if found:
                return dict(found)
        return {}

    def _protected_price(self, touch: Mapping[str, Any], side: str) -> Decimal | None:
        """The far edge of the band a taker order may fill within, on the venue's tick."""
        raw = touch.get("ask") if side == "buy" else touch.get("bid")
        if raw is None:
            return None
        edge = Decimal(str(raw))
        step = edge * self.max_slippage_bps / BPS
        price = edge + step if side == "buy" else edge - step
        to_tick = getattr(self.client, "price_to_precision", None)
        if to_tick is not None:
            with suppress(Exception):
                price = Decimal(str(to_tick(self.symbol, float(price))))
        return price

    def close_position(self, reason: str) -> dict[str, Any]:
        """Close whatever this account holds on the symbol with a reduce-only market order.

        Exits are never price-limited: a close that fails is worse than one that slips. Reduce-only
        means the order can shrink the position and never open or flip one.
        """
        held = self.position()
        if not held:
            return {"closed": False, "note": "nothing is held"}
        side = "sell" if held > 0 else "buy"
        spot = self.market().held_as_balance
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
                {} if spot else {"reduceOnly": True},
            )
        except Exception as exc:
            return {
                "closed": False,
                "error": self.redact(f"{exc.__class__.__name__}: {exc}")[:300],
                "reason": reason,
            }
        return {
            "closed": True,
            "reason": reason,
            "side": side,
            "size": f"{amount * self.contract_size():f}",
            "order_id": str(order.get("id", "")),
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

    def _touch_now(self) -> tuple[Decimal | None, Decimal | None]:
        """The best bid and ask at this moment, or nothing when the venue will not say."""
        fetch = getattr(self.client, "fetch_order_book", None)
        if fetch is None:
            return None, None
        try:
            book = fetch(self.symbol)
            bid = Decimal(str(book["bids"][0][0])) if book.get("bids") else None
            ask = Decimal(str(book["asks"][0][0])) if book.get("asks") else None
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


def _drift_bps(
    priced: Decimal, bid: Decimal | None, ask: Decimal | None, side: str
) -> Decimal | None:
    """How far the mid moved against the trade since it was priced; negative is in its favour."""
    if not priced or bid is None or ask is None:
        return None
    now = (bid + ask) / 2
    moved = (now - priced) / priced * BPS
    return moved if side == "buy" else -moved


def _decision_age_s(intent: Mapping[str, Any], now: float) -> float | None:
    """Seconds since the cycle that made this decision began, read from its id."""
    from datetime import UTC, datetime

    stamp = str(intent.get("at", ""))[:20]
    try:
        began = datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return now - began.timestamp()


def _slippage_bps(average: Any, mid: Decimal, side: str) -> str | None:
    """What the fill paid against the mid the trade was priced at; positive is worse."""
    if average in (None, "") or mid <= 0:
        return None
    try:
        paid = Decimal(str(average))
    except ArithmeticError:
        return None
    moved = (paid - mid) / mid * BPS
    return str((moved if side == "buy" else -moved).quantize(Decimal("0.01")))


def _client_id(intent: Mapping[str, Any]) -> str:
    """One id per cycle and instrument, so a cycle run twice cannot open two positions.

    Letters and digits only, 32 at most: the narrowest rule among the venues holds for all.
    """
    cycle = re.sub(r"[^0-9A-Za-z]", "", str(intent.get("at", "")) or "unknown")
    instrument = re.sub(r"[^0-9A-Za-z]", "", str(intent.get("instrument", "")))
    body = cycle if instrument and cycle.endswith(instrument) else cycle + instrument
    return f"tra{body}"[:32]


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
    )


def market_kind(market: Mapping[str, Any]) -> str:
    """What ccxt says a market is, in the words the refusal uses."""
    return Market.from_ccxt("", "", market).label
