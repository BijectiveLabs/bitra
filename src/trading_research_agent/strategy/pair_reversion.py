"""Walk-forward harness for pairwise mean reversion: every parameter fitted on trailing data.

A cointegration backtest once reported +180 bps at an 80 percent win rate. The hedge ratio had
been fitted on the whole panel and traded on the same panel. Re-run with every parameter fitted
at each bar from data strictly before it, the same strategy returned -55 to -68 bps, and live it
returned -25.5. The defect is one missing subscript: a beta instead of a beta_t.

    at each bar t:
        window  = [t - L, t - 1]
        beta_t  = fit(window)
        mu_t    = mean(spread over window)
        sigma_t = sd(spread over window)
        z_t     = (spread_t - mu_t) / sigma_t

Once a position opens, beta, mu and sigma freeze at their entry values for the life of that
position. Refitting mid-position would let information from after the entry reach back into a
trade already taken. The current bar's trailing fit is still recorded, so the claim that beta_t
is trailing can be checked against numbers rather than trusted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray

from trading_research_agent.envelope.model import Channel
from trading_research_agent.execution.cost import pair_leg_fee_bps

FitRule = Callable[[NDArray[np.float64], NDArray[np.float64]], tuple[float, float]]


class PrecomputedParameterError(ValueError):
    """A fitted value was passed where the harness must fit the rule itself."""


def ols(y: NDArray[np.float64], x: NDArray[np.float64]) -> tuple[float, float]:
    """y = beta * x + alpha. A window with no variance in x reads as no fit, not as a crash."""
    vx = float(np.var(x))
    if vx < 1e-18:
        return 0.0, 0.0
    beta = float(np.cov(x, y, bias=True)[0, 1] / vx)
    return beta, float(np.mean(y) - beta * np.mean(x))


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
class PairTrade:
    entry_t: int
    exit_t: int
    direction: int
    beta: float
    alpha: float
    mu: float
    sigma: float
    gross_bps: float
    cost_bps: float

    @property
    def net_bps(self) -> float:
        return self.gross_bps - self.cost_bps


@dataclass(frozen=True)
class WalkForwardResult:
    trades: tuple[PairTrade, ...]
    fits: dict[int, tuple[float, float]]

    @property
    def net_bps(self) -> list[float]:
        return [t.net_bps for t in self.trades]

    @property
    def mean_net_bps(self) -> float:
        return float(np.mean(self.net_bps)) if self.trades else 0.0


def walk_forward(
    y: NDArray[np.float64],
    x: NDArray[np.float64],
    *,
    entry_z: float,
    exit_z: float,
    stop_z: float,
    max_hold: int,
    lookback: int,
    cost: PairCost,
    fit_rule: FitRule = ols,
    train_bound: int | None = None,
    beta_bounds: tuple[float, float] = (0.3, 3.0),
    beta: float | None = None,
    alpha: float | None = None,
) -> WalkForwardResult:
    """Simulate the pair with no look-ahead. `beta` and `alpha` exist only to be refused."""
    if beta is not None or alpha is not None:
        raise PrecomputedParameterError(
            "walk_forward fits beta and alpha itself at every bar; a precomputed value is either "
            "fitted on data the harness cannot audit or on the whole series"
        )
    y = np.asarray(y, dtype=np.float64)
    x = np.asarray(x, dtype=np.float64)
    if y.ndim != 1 or x.shape != y.shape:
        raise ValueError("y and x must be one-dimensional arrays of equal length")
    n = int(y.shape[0])
    if lookback >= n:
        raise ValueError(f"lookback ({lookback}) must be smaller than the series length ({n})")
    if train_bound is not None and train_bound < lookback:
        raise ValueError(
            f"train_bound ({train_bound}) falls inside the first lookback window ({lookback}); "
            "the walk would silently start later than asked"
        )
    if not 0 < exit_z < entry_z < stop_z:
        raise ValueError("thresholds must satisfy 0 < exit_z < entry_z < stop_z")
    if max_hold <= 0:
        raise ValueError("max_hold must be positive")

    start = lookback if train_bound is None else max(lookback, train_bound)
    beta_lo, beta_hi = beta_bounds
    trades: list[PairTrade] = []
    fits: dict[int, tuple[float, float]] = {}

    open_position = False
    e_beta = e_alpha = e_mu = e_sigma = e_spread = 0.0
    e_dir = 0
    e_t = 0

    for t in range(start, n):
        y_tr, x_tr = y[t - lookback : t], x[t - lookback : t]
        beta_t, alpha_t = fit_rule(y_tr, x_tr)
        fits[t] = (beta_t, alpha_t)

        if not open_position:
            # The sanity checks belong to the flat branch only. An open position is evaluated
            # against its frozen parameters on every bar, whatever the unused current fit says;
            # a stop that can silently fail to run is a risk-control defect, not a cosmetic one.
            if not beta_lo <= beta_t <= beta_hi:
                continue
            spread_tr = y_tr - beta_t * x_tr - alpha_t
            mu_t, sigma_t = float(spread_tr.mean()), float(spread_tr.std())
            if sigma_t < 1e-9:
                continue
            spread_t = float(y[t] - beta_t * x[t] - alpha_t)
            z_t = (spread_t - mu_t) / sigma_t
            if abs(z_t) >= entry_z:
                open_position = True
                e_beta, e_alpha, e_mu, e_sigma = beta_t, alpha_t, mu_t, sigma_t
                e_spread, e_t = spread_t, t
                e_dir = -1 if z_t > 0 else 1
            continue

        spread_now = float(y[t] - e_beta * x[t] - e_alpha)
        z_now = (spread_now - e_mu) / e_sigma
        if abs(z_now) <= exit_z or abs(z_now) >= stop_z or (t - e_t) >= max_hold:
            gross_bps = e_dir * (spread_now - e_spread) * 1e4
            trades.append(
                PairTrade(
                    entry_t=e_t,
                    exit_t=t,
                    direction=e_dir,
                    beta=e_beta,
                    alpha=e_alpha,
                    mu=e_mu,
                    sigma=e_sigma,
                    gross_bps=float(gross_bps),
                    cost_bps=float(cost.round_trip_bps(e_beta)),
                )
            )
            open_position = False

    return WalkForwardResult(trades=tuple(trades), fits=fits)
