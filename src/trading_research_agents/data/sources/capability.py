"""What one venue can actually do, asked of the library rather than promised in a README.

"ccxt supports a hundred venues" is true and not useful: a spot exchange has no funding rate, some
venues cannot attach a stop to an entry, and a few will not say what position you are holding. Each
of those is the difference between a cycle that runs and a cycle that refuses, so the answer belongs
in a command an operator can run before they commit a key, not in a paragraph.

Nothing here touches the network or reads a credential. It reads the capability map the library
already carries and the methods the adapter would call.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# What the data adapter calls for each kind a chain can declare.
DATA_METHODS = {
    "ohlcv": "fetchOHLCV",
    "orderbook": "fetchOrderBook",
    "funding": "fetchFundingRate",
    "open_interest": "fetchOpenInterest",
}
# What the venue hand needs, and what each one buys.
EXECUTION_METHODS = {
    "create_order": ("createOrder", "sending an order at all"),
    "attached_exits": (
        "createOrderWithTakeProfitAndStopLoss",
        "the stop and take profit going out with the entry",
    ),
    "position": ("fetchPosition", "refusing to add to a position you already hold"),
    "open_orders": ("fetchOpenOrders", "refusing a second decision while one is still working"),
    "cancel": ("cancelOrder", "cancelling a resting maker order that did not fill"),
}


class CapabilityError(ValueError):
    """The venue cannot be inspected: ccxt does not know it, or is not installed."""


@dataclass(frozen=True)
class Capability:
    """One venue, read from the library: what it serves, what it can execute, what it cannot."""

    venue: str
    data: dict[str, bool] = field(default_factory=dict)
    execution: dict[str, bool] = field(default_factory=dict)
    sandbox: bool = False
    kinds_missing: tuple[str, ...] = ()

    @property
    def serves_data(self) -> tuple[str, ...]:
        return tuple(kind for kind, ok in sorted(self.data.items()) if ok)

    @property
    def can_send(self) -> bool:
        return bool(self.execution.get("create_order"))

    @property
    def can_attach_exits(self) -> bool:
        return bool(self.execution.get("attached_exits"))

    @property
    def guards(self) -> bool:
        """Whether the two reads that stop a second position are both available."""
        return bool(self.execution.get("position")) and bool(self.execution.get("open_orders"))

    def verdict(self) -> str:
        if not self.can_send:
            return "data only; this venue does not take orders through ccxt"
        if not self.can_attach_exits:
            return (
                'data, and orders only with [execution].exits = "none"; it cannot take a stop '
                "and a take profit with the entry"
            )
        if not self.guards:
            return (
                "data and orders, but the reads a cycle uses to refuse a second position "
                "are missing"
            )
        return "data and venue execution, with the exits attached and the account readable"

    def record(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "data": dict(sorted(self.data.items())),
            "execution": dict(sorted(self.execution.items())),
            "sandbox": self.sandbox,
            "verdict": self.verdict(),
        }


def capability_of(
    venue: str, *, client: Any = None, kinds: Iterable[str] | None = None
) -> Capability:
    """Ask ccxt what this venue can do. No network, no key, no order."""
    name = venue.strip().lower()
    if client is None:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise CapabilityError(
                "ccxt is not installed; pip install 'trading-research-agents[ccxt]'"
            ) from exc
        factory = getattr(ccxt, name, None)
        if factory is None:
            raise CapabilityError(f"ccxt has no exchange named {name!r}")
        client = factory()
    has = dict(getattr(client, "has", {}) or {})
    data = {kind: bool(has.get(method)) for kind, method in DATA_METHODS.items()}
    execution = {key: bool(has.get(method)) for key, (method, _) in EXECUTION_METHODS.items()}
    wanted = tuple(kinds or ())
    missing = tuple(kind for kind in wanted if kind in data and not data[kind])
    return Capability(
        venue=name,
        data=data,
        execution=execution,
        sandbox=hasattr(client, "set_sandbox_mode"),
        kinds_missing=missing,
    )
