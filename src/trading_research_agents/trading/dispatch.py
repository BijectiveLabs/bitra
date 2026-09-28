"""Sending one admitted intent to a venue, under a mandate, with the key never leaving this layer.

A cycle decides; this sends. They are kept apart on purpose: nothing in `agents/` can reach a venue,
nothing in `execution/` holds a credential, and the only way an order is ever placed is that an
operator wrote a mandate, the ladder stands at its canary tier, and every gate in the cycle passed
first.

One order, one call, one record. There is no retry loop here: a market order that came back with an
id is the venue's business from then on, and a failure is written down rather than tried again with
different words. The session (`tra live`) is the place for a venue connection that lives across many
bars; this is for a cycle that decided once.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

SIDES = {"long": "buy", "short": "sell"}


class DispatchError(ValueError):
    """The order cannot be sent as configured, or the venue refused it."""


@dataclass
class VenueHand:
    """What a cycle in venue mode sends with: one venue, one instrument, one cap.

    The client is built here from the environment, so the key is read in one place and never
    travels through a config file, a brief, a prompt or a record. `paper` sends to the venue's own
    test environment when it has one, which is the only way this should ever be tried first.
    """

    venue: str
    symbol: str
    max_notional: Decimal
    # The two environment variables this venue's credentials live in, in that order. They travel
    # as a pair because they are only ever used as one.
    keys: tuple[str, str]
    environment: str = "demo"
    client: Any = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_notional <= 0:
            raise DispatchError("a venue hand needs a positive notional cap")
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
            # The venue's own test environment. An operator who means production says so.
            client.set_sandbox_mode(True)
        return client

    def __call__(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        """Send it, or say why it was not sent. Either way the caller gets a record."""
        target = str(intent.get("target", ""))
        if target not in SIDES:
            return {"sent": False, "refused": f"target {target!r} is not an order"}
        size = Decimal(str(intent.get("size", "0")))
        price = Decimal(str(intent.get("mid", "0")))
        if size <= 0:
            return {"sent": False, "refused": "size is not positive"}
        notional = size * price
        if notional > self.max_notional:
            return {
                "sent": False,
                "refused": (
                    f"notional {notional} is above [execution].max_notional {self.max_notional}"
                ),
            }
        try:
            order = self.client.create_order(self.symbol, "market", SIDES[target], float(size))
        except Exception as exc:
            raise DispatchError(f"{exc.__class__.__name__}: {exc}") from exc
        return {
            "sent": True,
            "venue": self.venue,
            "environment": self.environment,
            "symbol": self.symbol,
            "side": SIDES[target],
            "size": str(size),
            "notional": str(notional),
            "order_id": str(order.get("id", "")),
            "status": str(order.get("status", "")),
            "filled": None if order.get("filled") is None else str(order["filled"]),
            "average": None if order.get("average") is None else str(order["average"]),
        }


def hand_for(
    *,
    venue: str,
    symbol: str,
    max_notional: Decimal,
    environment: str = "demo",
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
        environment=environment,
        env=env if env is not None else os.environ,
        client=client,
    )
