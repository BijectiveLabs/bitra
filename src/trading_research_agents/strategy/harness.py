"""The walk-forward mechanism, with no strategy inside it.

Every parameter a decision depends on is estimated only from data available at the time of the
decision, refitted at every bar from a trailing window, frozen at entry for the life of the
position, and never accepted precomputed. The mechanism is the point; what is fitted and what
signal is derived from it belong to the core the caller supplies.

A cointegration backtest once reported +180 bps at an 80 percent win rate with the hedge ratio
fitted on the whole panel and traded on the same panel. Refitted at each bar from data strictly
before it, the same strategy returned -55 to -68 bps, and live it returned -25.5. The defect
was one missing subscript, and this harness exists so that subscript cannot go missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, Protocol, TypeVar

import numpy as np
from numpy.typing import NDArray

P = TypeVar("P")


class PrecomputedParameterError(ValueError):
    """A fitted value was passed where the harness must fit the rule itself."""


class FittedCore(Protocol[P]):
    """What the caller supplies: how to fit a window, and what to do with the fit.

    `fit` sees only the trailing window and returns the parameters. `tradeable` says whether a
    fit is usable at all (a degenerate window reads as no fit, not as a crash). `entry` looks at
    the current row against the fit and returns a direction, +1 or -1, or 0 for nothing.
    `exit` looks at the current row against the parameters frozen at entry and says whether the
    position closes. `mark` prices the position for the round trip. `cost_bps` is the round trip
    for the parameters the position was actually opened under.
    """

    def fit(self, window: NDArray[np.float64]) -> P: ...

    def tradeable(self, params: P) -> bool: ...

    def entry(self, row: NDArray[np.float64], params: P) -> int: ...

    def exit(self, row: NDArray[np.float64], params: P, bars_held: int) -> bool: ...

    def mark(self, row: NDArray[np.float64], params: P) -> float: ...

    def cost_bps(self, params: P) -> Decimal: ...


@dataclass(frozen=True)
class HarnessTrade(Generic[P]):
    entry_t: int
    exit_t: int
    direction: int
    params: P
    gross_bps: float
    cost_bps: float

    @property
    def net_bps(self) -> float:
        return self.gross_bps - self.cost_bps


@dataclass(frozen=True)
class HarnessResult(Generic[P]):
    trades: tuple[HarnessTrade[P], ...]
    fits: dict[int, P]

    @property
    def net_bps(self) -> list[float]:
        return [t.net_bps for t in self.trades]

    @property
    def mean_net_bps(self) -> float:
        return float(np.mean(self.net_bps)) if self.trades else 0.0


def walk_forward(
    panel: NDArray[np.float64],
    core: FittedCore[P],
    *,
    lookback: int,
    train_bound: int | None = None,
    precomputed: object | None = None,
) -> HarnessResult[P]:
    """Walk `panel` (T rows) with `core`. `precomputed` exists only to be refused.

    Sanity checks on the current fit run only while flat. An open position is evaluated for
    exit on every bar against its frozen parameters, whatever the unused current fit says: a
    stop that can silently fail to run is a risk-control defect, not a cosmetic one.
    """
    if precomputed is not None:
        raise PrecomputedParameterError(
            "the harness fits every parameter itself from the trailing window; a precomputed "
            "value is either fitted on data it cannot audit or on the whole series"
        )
    data = np.asarray(panel, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    n = int(data.shape[0])
    if lookback >= n:
        raise ValueError(f"lookback ({lookback}) must be smaller than the series length ({n})")
    if train_bound is not None and train_bound < lookback:
        raise ValueError(
            f"train_bound ({train_bound}) falls inside the first lookback window ({lookback}); "
            "the walk would silently start later than asked"
        )
    start = lookback if train_bound is None else max(lookback, train_bound)

    trades: list[HarnessTrade[P]] = []
    fits: dict[int, P] = {}
    open_params: P | None = None
    entry_t = 0
    direction = 0
    entry_mark = 0.0

    for t in range(start, n):
        params = core.fit(data[t - lookback : t])
        fits[t] = params
        if open_params is None:
            if not core.tradeable(params):
                continue
            direction = core.entry(data[t], params)
            if direction not in (-1, 1):
                continue
            open_params = params
            entry_t = t
            entry_mark = core.mark(data[t], params)
            continue
        if core.exit(data[t], open_params, t - entry_t):
            gross = direction * (core.mark(data[t], open_params) - entry_mark) * 1e4
            trades.append(
                HarnessTrade(
                    entry_t=entry_t,
                    exit_t=t,
                    direction=direction,
                    params=open_params,
                    gross_bps=float(gross),
                    cost_bps=float(core.cost_bps(open_params)),
                )
            )
            open_params = None
    return HarnessResult(trades=tuple(trades), fits=fits)


FitRule = Callable[[NDArray[np.float64], NDArray[np.float64]], tuple[float, float]]


def ols(y: NDArray[np.float64], x: NDArray[np.float64]) -> tuple[float, float]:
    """y = beta * x + alpha. A window with no variance in x reads as no fit, not as a crash."""
    vx = float(np.var(x))
    if vx < 1e-18:
        return 0.0, 0.0
    beta = float(np.cov(x, y, bias=True)[0, 1] / vx)
    return beta, float(np.mean(y) - beta * np.mean(x))
