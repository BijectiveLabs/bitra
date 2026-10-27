"""What layer 5 is handed: a trade as recorded, net of its cost, and the error it raises when one
cannot be read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from trading_research_agents.data.schema import Direction

SIGNIFICANCE_T = 1.96


class ValidationError(ValueError):
    """The measurement cannot be turned into a verdict as it stands."""


@dataclass(frozen=True)
class TradeRecord:
    instrument: str
    entry_ts_ns: int
    exit_ts_ns: int
    direction: Direction
    weight: Decimal
    gross_return_bps: Decimal

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise ValidationError("a trade names its instrument")
        if self.exit_ts_ns <= self.entry_ts_ns:
            raise ValidationError("a trade exits after it enters")
        for label, value in (("weight", self.weight), ("gross_return_bps", self.gross_return_bps)):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValidationError(f"{label} must be a finite Decimal")
        if self.weight <= 0:
            raise ValidationError("weight is the strategy's own sizing and is positive")


CostOf = Callable[[TradeRecord], Decimal]


@dataclass(frozen=True)
class NetTrade:
    record: TradeRecord
    cost_bps: Decimal

    @property
    def net_bps(self) -> Decimal:
        return self.record.gross_return_bps - self.cost_bps
