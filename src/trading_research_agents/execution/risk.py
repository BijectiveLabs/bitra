"""How much of the account one trade may lose, and the size that follows from it.

Conviction says how much of the base size the desk wants behind an idea. This says how much the
account can afford to be wrong about, which is a different question and the one a risk manager is
paid to ask: with the stop where it is, what does this position lose if it is hit, and is that
inside the fraction of equity the operator declared?

The answer is arithmetic, not judgement. Risk per trade is the distance to the stop times the size,
against the equity, in basis points. If that exceeds `[controls].max_risk_per_trade_bps`, the size
is cut until it fits - never the stop moved, because moving a stop to make a trade fit is how a desk
learns the wrong lesson from a loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

BPS = Decimal(10_000)


class RiskError(ValueError):
    """The risk rule cannot be applied to these numbers."""


@dataclass(frozen=True)
class RiskVerdict:
    """What one trade puts at risk, and the size that survived the rule."""

    admitted: bool
    size: Decimal
    risk_bps: Decimal | None
    allowed_bps: Decimal | None
    reasons: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "size": str(self.size),
            "risk_bps": None if self.risk_bps is None else str(self.risk_bps),
            "allowed_bps": None if self.allowed_bps is None else str(self.allowed_bps),
            "reasons": list(self.reasons),
        }


def risk_bps_of(*, size: Decimal, entry: Decimal, stop: Decimal, equity: Decimal) -> Decimal:
    """What this position loses if the stop is hit, against the equity, in basis points."""
    if equity <= 0:
        raise RiskError("equity is positive when risk per trade is declared")
    if size <= 0 or entry <= 0:
        raise RiskError("size and entry are positive")
    distance = abs(entry - stop)
    return ((distance * size) / equity * BPS).quantize(Decimal("0.01"))


def size_for_risk(
    *, allowed_bps: Decimal, entry: Decimal, stop: Decimal, equity: Decimal, step: Decimal
) -> Decimal:
    """The largest size whose loss at the stop is inside the allowance, rounded down to the step."""
    distance = abs(entry - stop)
    if distance <= 0:
        raise RiskError("the stop and the entry are the same price; there is no risk to size from")
    capital = equity * allowed_bps / BPS
    raw = capital / distance
    if step <= 0:
        raise RiskError("the venue's size step is positive")
    return (raw / step).to_integral_value(rounding="ROUND_DOWN") * step


def within_risk(
    *,
    size: Decimal,
    entry: Decimal,
    stop: Decimal | None,
    equity: Decimal | None,
    allowed_bps: Decimal | None,
    step: Decimal = Decimal("0.000001"),
) -> RiskVerdict:
    """Cut the size until the loss at the stop fits the allowance, or say why it cannot.

    Nothing to check against - no stop, no equity, no declared allowance - passes the size through
    and says so, because a rule the operator did not declare is not a rule this invents for them.
    """
    if allowed_bps is None or equity is None or stop is None:
        return RiskVerdict(admitted=True, size=size, risk_bps=None, allowed_bps=allowed_bps)
    if allowed_bps <= 0:
        raise RiskError("[controls].max_risk_per_trade_bps is positive")

    at_risk = risk_bps_of(size=size, entry=entry, stop=stop, equity=equity)
    if at_risk <= allowed_bps:
        return RiskVerdict(admitted=True, size=size, risk_bps=at_risk, allowed_bps=allowed_bps)

    cut = size_for_risk(allowed_bps=allowed_bps, entry=entry, stop=stop, equity=equity, step=step)
    if cut <= 0:
        return RiskVerdict(
            admitted=False,
            size=Decimal(0),
            risk_bps=at_risk,
            allowed_bps=allowed_bps,
            reasons=(
                f"{at_risk} bps of equity at the stop is above the declared {allowed_bps} bps, and "
                "the size that would fit is below one step of the venue",
            ),
        )
    return RiskVerdict(
        admitted=True,
        size=cut,
        risk_bps=risk_bps_of(size=cut, entry=entry, stop=stop, equity=equity),
        allowed_bps=allowed_bps,
        reasons=(
            f"{size} would risk {at_risk} bps of equity at the stop, above the declared "
            f"{allowed_bps} bps; cut to {cut}",
        ),
    )
