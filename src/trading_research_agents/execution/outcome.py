"""What an outcome share is worth to the desk at its ask: arithmetic, no model.

A share of a binary outcome pays 1 if it wins and 0 if it loses. Bought at the ask `a` with a fee
`f` per share, held to resolution, its expected value per share at a probability `p` is

    p - a - f

and nothing can be lost beyond `a + f`. The fee is either a flat rate on what the share costs or,
as one venue charges, a coefficient on `a * (1 - a)`, which peaks at 0.5 and vanishes near the
ends. The desk buys only when the expected value, as a share of what it pays, clears the margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

BPS = Decimal(10_000)


class OutcomeError(ValueError):
    """The outcome cannot be priced as given."""


def fee_per_share(
    price: Decimal, *, coefficient: Decimal | None = None, fee_bps: Decimal | None = None
) -> Decimal:
    """The fee of one share at `price`: coefficient x p x (1 - p), or a flat rate on p."""
    if not Decimal(0) < price < Decimal(1):
        raise OutcomeError(f"an outcome price lies between 0 and 1, not {price}")
    if coefficient is not None:
        if coefficient < 0:
            raise OutcomeError("a fee coefficient is not negative")
        return coefficient * price * (Decimal(1) - price)
    if fee_bps is not None:
        if fee_bps < 0:
            raise OutcomeError("a fee is not negative")
        return price * fee_bps / BPS
    return Decimal(0)


@dataclass(frozen=True)
class OutcomeEdge:
    probability: Decimal
    ask: Decimal
    fee: Decimal
    value: Decimal
    edge_bps: Decimal
    admitted: bool
    reason: str

    def record(self) -> dict[str, str | bool]:
        return {
            "probability": str(self.probability),
            "ask": str(self.ask),
            "fee_per_share": str(self.fee.quantize(Decimal("0.000001"))),
            "value_per_share": str(self.value.quantize(Decimal("0.000001"))),
            "edge_bps": str(self.edge_bps.quantize(Decimal("0.01"))),
            "admitted": self.admitted,
            "reason": self.reason,
        }


def outcome_edge(
    probability: Decimal,
    ask: Decimal,
    *,
    margin_bps: Decimal = Decimal(0),
    coefficient: Decimal | None = None,
    fee_bps: Decimal | None = None,
) -> OutcomeEdge:
    """Whether buying at `ask` is worth it at `probability`, held to resolution."""
    if not Decimal(0) <= probability <= Decimal(1):
        raise OutcomeError(f"a probability lies in [0, 1], not {probability}")
    fee = fee_per_share(ask, coefficient=coefficient, fee_bps=fee_bps)
    value = probability - ask - fee
    edge = value / ask * BPS
    if value <= 0:
        reason = (
            f"at a probability of {probability} the share is worth {value:.4f} after the ask "
            f"{ask} and a fee of {fee:.4f}; there is nothing to buy"
        )
        return OutcomeEdge(probability, ask, fee, value, edge, False, reason)
    if edge < margin_bps:
        reason = f"the edge is {edge:.2f} bps of the ask and the margin is {margin_bps} bps"
        return OutcomeEdge(probability, ask, fee, value, edge, False, reason)
    reason = f"worth {value:.4f} a share after the fee, {edge:.2f} bps of the ask"
    return OutcomeEdge(probability, ask, fee, value, edge, True, reason)
