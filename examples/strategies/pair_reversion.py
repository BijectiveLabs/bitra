"""Example: pairwise mean reversion on the walk-forward harness.

This is an example, not a recommendation, and not part of the package. It comes from the
Bijective research record, where the strategy it describes was measured and closed. It is
here because it is the case the harness was built around: the hedge ratio, mean and standard
deviation are fitted from the trailing window at every bar and frozen at entry, and a
precomputed hedge ratio is refused.

Run it with your own two log-price series::

    from examples.strategies.pair_reversion import PairReversion, PairCost
    result = walk_forward(np.column_stack([y, x]), PairReversion(...), lookback=60)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray

from trading_research_agent.envelope.model import Channel
from trading_research_agent.execution.cost import pair_leg_fee_bps
from trading_research_agent.strategy.harness import ols


@dataclass(frozen=True)
class PairCost:
    """Round trip for a hedged pair: fee per leg times (1 + beta) per touch, two touches."""

    channel: Channel
    spread_per_leg_bps: Decimal

    def round_trip_bps(self, beta: float) -> Decimal:
        b = Decimal(repr(abs(beta)))
        touch_in = pair_leg_fee_bps(self.channel.entry_fee_bps, b)
        touch_out = pair_leg_fee_bps(self.channel.exit_fee_bps, b)
        return touch_in + touch_out + 2 * self.spread_per_leg_bps


@dataclass(frozen=True)
class PairParams:
    beta: float
    alpha: float
    mu: float
    sigma: float


class PairReversion:
    """Rows are (y, x) log prices. Enter against the z-score of the spread; exit inside it."""

    def __init__(
        self,
        *,
        entry_z: float,
        exit_z: float,
        stop_z: float,
        max_hold: int,
        cost: PairCost,
        beta_bounds: tuple[float, float] = (0.3, 3.0),
    ) -> None:
        if not 0 < exit_z < entry_z < stop_z:
            raise ValueError("thresholds must satisfy 0 < exit_z < entry_z < stop_z")
        if max_hold <= 0:
            raise ValueError("max_hold must be positive")
        self.entry_z, self.exit_z, self.stop_z = entry_z, exit_z, stop_z
        self.max_hold = max_hold
        self.cost = cost
        self.beta_bounds = beta_bounds

    def fit(self, window: NDArray[np.float64]) -> PairParams:
        y, x = window[:, 0], window[:, 1]
        beta, alpha = ols(y, x)
        spread = y - beta * x - alpha
        return PairParams(beta, alpha, float(spread.mean()), float(spread.std()))

    def tradeable(self, params: PairParams) -> bool:
        lo, hi = self.beta_bounds
        return lo <= params.beta <= hi and params.sigma >= 1e-9

    def _z(self, row: NDArray[np.float64], params: PairParams) -> float:
        spread = float(row[0] - params.beta * row[1] - params.alpha)
        return (spread - params.mu) / params.sigma

    def entry(self, row: NDArray[np.float64], params: PairParams) -> int:
        z = self._z(row, params)
        if abs(z) < self.entry_z:
            return 0
        return -1 if z > 0 else 1

    def exit(self, row: NDArray[np.float64], params: PairParams, bars_held: int) -> bool:
        z = abs(self._z(row, params))
        return z <= self.exit_z or z >= self.stop_z or bars_held >= self.max_hold

    def mark(self, row: NDArray[np.float64], params: PairParams) -> float:
        return float(row[0] - params.beta * row[1] - params.alpha)

    def cost_bps(self, params: PairParams) -> Decimal:
        return self.cost.round_trip_bps(params.beta)
