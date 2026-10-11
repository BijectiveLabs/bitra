"""How large an order may be. One implementation, because this is the arithmetic of capital.

Every cap here shrinks and none of them grows. A size that cannot be met is zero with the cap that
refused it named, never a size rounded up to something the venue would accept.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any


class SizingError(ValueError):
    """The inputs are not a size this can reason about."""


@dataclass(frozen=True)
class Sizing:
    size: Decimal
    capped_by: str

    def record(self) -> dict[str, Any]:
        return {"size": str(self.size), "capped_by": self.capped_by}


def size_for(
    *,
    wanted: Decimal,
    price: Decimal,
    max_position: Decimal,
    max_notional: Decimal | None,
    step: Decimal,
) -> Sizing:
    """The largest size at or below every cap, rounded down to the venue's step. Never up."""
    if wanted <= 0 or price <= 0 or step <= 0:
        raise SizingError("wanted, price and step are positive")
    size, why = wanted, "declared size"
    if size > max_position:
        size, why = max_position, "max_position"
    if max_notional is not None and size * price > max_notional:
        size, why = max_notional / price, "max_notional"
    size = (size / step).to_integral_value(rounding=ROUND_DOWN) * step
    if size <= 0:
        return Sizing(Decimal(0), f"{why}: below one step of {step}")
    return Sizing(size, why)


def notional_cap(*, equity: Decimal | None, max_leverage: Decimal | None) -> Decimal | None:
    """The most notional an operator's own declarations allow, or nothing when they declared none.

    Leverage is not a lever any role gets to pull. It is what the notional works out to against the
    equity, and this is where the operator's ceiling on that is applied.
    """
    if equity is None or max_leverage is None:
        return None
    if equity <= 0 or max_leverage <= 0:
        raise SizingError("equity and max_leverage are positive when they are declared")
    return equity * max_leverage


def leverage_of(*, size: Decimal, price: Decimal, equity: Decimal | None) -> Decimal | None:
    """What this order works out to against the equity, for the record."""
    if equity is None or equity <= 0:
        return None
    return ((size * price) / equity).quantize(Decimal("0.001"))
