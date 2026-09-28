"""Sending one admitted intent to a venue, under a mandate, with the key never leaving this layer.

A cycle decides; this sends. They are kept apart on purpose: nothing in `agents/` can reach a venue,
nothing in `execution/` holds a credential, and the only way an order is ever placed is that an
operator wrote a mandate, the ladder stands at its canary tier, and every gate in the cycle passed
first.

The order obeys the channel. A channel named `taker-taker` was priced at the venue's taker fee, so
its entry crosses the book with a market order; a channel named `maker-taker` was priced at the
maker fee, so its entry rests on the book as a post-only limit and is cancelled if it has not filled
by the operator's timeout. A channel whose name does not say is refused rather than guessed, because
guessing here means paying a fee the gate did not price.

Three habits make this auditable after a failure, which is the only time it matters: every order
carries a client id derived from the cycle, so the same cycle cannot open two positions; the account
is read before anything is sent, so an order is never added to a position this desk did not expect;
and nothing is retried. A retry loop around real capital is how one defect becomes ten orders.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

SIDES = {"long": "buy", "short": "sell"}
ROLES = ("maker", "taker")
BPS = Decimal(10_000)


class DispatchError(ValueError):
    """The order cannot be sent as configured, or the venue refused it."""


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
    keys: tuple[str, str]
    role: str = "taker"
    environment: str = "demo"
    limit_timeout_s: float = 30.0
    price_offset_bps: Decimal = Decimal(0)
    adopt_open_position: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)
    client: Any = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    options: Mapping[str, Any] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep

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
        if self.client is None:
            self.client = self._build()

    def _build(self) -> Any:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise DispatchError(
                "ccxt is not installed; pip install 'trading-research-agents[ccxt]'"
            ) from exc
        factory = getattr(ccxt, self.venue, None)
        if factory is None:
            raise DispatchError(f"ccxt has no exchange named {self.venue!r}")
        named, held = self.keys
        key, secret = self.env.get(named, ""), self.env.get(held, "")
        if not key or not secret:
            raise DispatchError(
                f"{named} and {held} are not in the environment; keys never come from a file"
            )
        client = factory({"apiKey": key, "secret": secret, "enableRateLimit": True, **self.options})
        if self.environment in ("demo", "testnet") and hasattr(client, "set_sandbox_mode"):
            client.set_sandbox_mode(True)
        return client

    def __call__(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        """Send it, or say why it was not sent. Either way the caller gets a record."""
        target = str(intent.get("target", ""))
        if target not in SIDES:
            return {"sent": False, "refused": f"target {target!r} is not an order"}
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
        standing = self._position()
        if standing is not None and not self.adopt_open_position and standing != 0:
            wanted = Decimal(1) if target == "long" else Decimal(-1)
            if (standing > 0) != (wanted > 0):
                return {
                    "sent": False,
                    "refused": (
                        f"the account already holds {standing} of {self.symbol} the other way; "
                        "flatten it or set [execution].adopt_open_position"
                    ),
                    "position": str(standing),
                }

        order_id = _client_id(intent)
        params = {"clientOrderId": order_id, **dict(self.params)}
        price = self._resting_price(intent, side) if self.role == "maker" else None
        if self.role == "maker":
            if price is None:
                return {"sent": False, "refused": "no book side to rest on; the snapshot had none"}
            params["postOnly"] = True
            params.setdefault("timeInForce", "GTC")
        try:
            order = self.client.create_order(
                self.symbol,
                "limit" if self.role == "maker" else "market",
                side,
                float(size),
                None if price is None else float(price),
                params,
            )
        except Exception as exc:
            raise DispatchError(f"{exc.__class__.__name__}: {exc}") from exc

        placed: dict[str, Any] = {
            "sent": True,
            "venue": self.venue,
            "environment": self.environment,
            "symbol": self.symbol,
            "role": self.role,
            "side": side,
            "size": str(size),
            "price": None if price is None else str(price),
            "notional": str(notional),
            "client_order_id": order_id,
            "order_id": str(order.get("id", "")),
            "status": str(order.get("status", "")),
            "filled": None if order.get("filled") is None else str(order["filled"]),
            "average": None if order.get("average") is None else str(order["average"]),
        }
        if self.role == "maker":
            placed.update(self._settle(str(placed["order_id"])))
        return placed

    def _position(self) -> Decimal | None:
        """What the account already holds, or nothing when the venue will not say.

        A hand that cannot read the account does not send: an order into a position nobody can see
        is the one mistake that cannot be undone by reading the record afterwards.
        """
        fetch = getattr(self.client, "fetch_position", None)
        if fetch is None:
            return None
        try:
            held = fetch(self.symbol)
        except Exception as exc:
            raise DispatchError(
                f"cannot read the account: {exc.__class__.__name__}: {exc}"
            ) from exc
        if not held:
            return Decimal(0)
        contracts = held.get("contracts")
        if contracts is None:
            return Decimal(0)
        signed = Decimal(str(contracts))
        return -signed if str(held.get("side", "")).lower() == "short" else signed

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
        while self.clock() - started < self.limit_timeout_s:
            status = self._status(order_id)
            if status is not None and str(status.get("status", "")) in ("closed", "filled"):
                return {
                    "filled": str(status.get("filled", "")),
                    "average": None if status.get("average") is None else str(status["average"]),
                    "cancelled": False,
                    "waited_s": round(self.clock() - started, 3),
                }
            self.sleep(min(1.0, self.limit_timeout_s))
        cancelled = self._cancel(order_id)
        return {
            "cancelled": cancelled,
            "waited_s": round(self.clock() - started, 3),
            "note": "it did not fill inside [execution].limit_timeout_s and was cancelled",
        }

    def _status(self, order_id: str) -> Mapping[str, Any] | None:
        fetch = getattr(self.client, "fetch_order", None)
        if fetch is None:
            return None
        try:
            return dict(fetch(order_id, self.symbol))
        except Exception:
            # A status this hand cannot read is not a reason to send anything else.
            return None

    def _cancel(self, order_id: str) -> bool:
        cancel = getattr(self.client, "cancel_order", None)
        if cancel is None:
            return False
        try:
            cancel(order_id, self.symbol)
        except Exception:
            return False
        return True


def _client_id(intent: Mapping[str, Any]) -> str:
    """One id per cycle and instrument, so a cycle run twice cannot open two positions."""
    cycle = str(intent.get("at", "")) or "unknown"
    instrument = str(intent.get("instrument", "")).replace("/", "")
    return f"tra-{cycle}-{instrument}"[:36]


def hand_for(
    *,
    venue: str,
    symbol: str,
    max_notional: Decimal,
    role: str = "taker",
    environment: str = "demo",
    limit_timeout_s: float = 30.0,
    price_offset_bps: Decimal = Decimal(0),
    adopt_open_position: bool = False,
    params: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    client: Any = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """A hand for `venue`, with the key names that venue's plugin already documents."""
    prefix = venue.strip().upper()
    return VenueHand(
        venue=venue.strip().lower(),
        symbol=symbol,
        max_notional=max_notional,
        keys=(f"{prefix}_API_KEY", f"{prefix}_API_SECRET"),
        role=role,
        environment=environment,
        limit_timeout_s=limit_timeout_s,
        price_offset_bps=price_offset_bps,
        adopt_open_position=adopt_open_position,
        params=dict(params or {}),
        env=env if env is not None else os.environ,
        client=client,
    )
