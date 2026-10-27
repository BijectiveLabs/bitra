"""What the gate checks against, and what it reads about the desk at the moment it checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RiskLimits:
    """The operator's hard limits. None is a limit the operator did not declare.

    Notional and position caps are in the quote currency or in base units, as named. Basis points
    are of the equity the desk sizes from. `valid_for_s` is how long an approval may wait before
    the order hand must refuse it.
    """

    max_notional: Decimal | None = None
    max_position: Decimal | None = None
    max_position_quote: Decimal | None = None
    max_risk_per_trade_bps: Decimal | None = None
    max_trades_per_day: int | None = None
    daily_loss_limit_bps: Decimal | None = None
    max_drawdown_bps: Decimal | None = None
    valid_for_s: float = 120.0

    def record(self) -> dict[str, Any]:
        return {
            name: None if value is None else str(value) for name, value in self.__dict__.items()
        }


@dataclass(frozen=True)
class RiskState:
    """The desk as the caller read it just before the gate: every field is the caller's reading.

    `held_by` names a switch that holds the desk (the kill switch, a pause), `unknown_orders` an
    order on this instrument whose send was never answered. A loss figure left None was not read;
    a limit that needs it then refuses rather than passes.
    """

    equity: Decimal | None
    trades_today: int
    day_loss_bps: Decimal | None = None
    drawdown_bps: Decimal | None = None
    held_by: str = ""
    unknown_orders: str = ""
    # Whether the intent can reach a venue. Equity is then required wherever a limit reads it.
    places_orders: bool = False
