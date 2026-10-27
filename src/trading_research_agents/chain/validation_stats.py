"""The statistics of layer 5: net returns per trade, the block bootstrap, the permutation null,
subperiods, false discovery and anchor robustness.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from trading_research_agents.chain.trade_record import (
    SIGNIFICANCE_T,
    CostOf,
    NetTrade,
    TradeRecord,
    ValidationError,
)

MIN_RESAMPLES = 1000


MIN_PERMUTATIONS = 1000


def net_returns(trades: Sequence[TradeRecord], cost_of: CostOf) -> tuple[NetTrade, ...]:
    """Every trade net of its own round trip. Gross stays beside it and never carries a verdict."""
    out: list[NetTrade] = []
    for t in trades:
        cost = cost_of(t)
        if not isinstance(cost, Decimal) or not cost.is_finite():
            raise ValidationError(
                f"cost for {t.instrument} at {t.entry_ts_ns} is not a finite Decimal"
            )
        out.append(NetTrade(t, cost))
    return tuple(out)


def collapse(
    nets: Sequence[NetTrade], *, gross: bool = False
) -> tuple[NDArray[np.int64], NDArray[np.float64]]:
    """One portfolio return per entry timestamp: R_t = sum_i w_i * r_i.

    This is where the correlation problem is solved. Fifteen correlated legs at one time become
    one observation at that time, which is what they are worth. Weights are the strategy's own
    sizing, applied as declared; the caller who wants equal weights declares equal weights.
    """
    if not nets:
        raise ValidationError("no trades to collapse")
    by_ts: dict[int, Decimal] = defaultdict(Decimal)
    for n in nets:
        value = n.record.gross_return_bps if gross else n.net_bps
        by_ts[n.record.entry_ts_ns] += n.record.weight * value
    stamps = sorted(by_ts)
    return (
        np.asarray(stamps, dtype=np.int64),
        np.asarray([float(by_ts[s]) for s in stamps], dtype=np.float64),
    )


@dataclass(frozen=True)
class Interval:
    lower: float
    point: float
    upper: float
    n: int
    block_length: int
    n_resamples: int


def block_bootstrap_ci(
    series: NDArray[np.float64],
    *,
    block_length: int,
    n_resamples: int = 5000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Interval:
    """Percentile interval on the mean, resampled in overlapping blocks of `block_length`.

    Blocks preserve whatever serial structure exists inside a window of that length; point
    resampling manufactures independence the data does not have. The block length exceeds the
    holding horizon and is declared at pre-registration, because it is exactly the kind of free
    parameter that a rebalance-phase sweep exists to catch.

    Following the published specification rather than the reference implementation: a block
    that does not fit the sample is refused rather than clamped, and a missing value is refused
    rather than dropped, because a timestamp that failed to compute is not a random one.
    """
    x = np.asarray(series, dtype=np.float64)
    t = int(x.size)
    if block_length < 1:
        raise ValidationError("block_length must be at least 1")
    if t < 2 * block_length:
        raise ValidationError(
            f"{t} observations cannot form a block structure with block_length {block_length}; "
            "the sample is too short for the declared horizon"
        )
    if n_resamples < MIN_RESAMPLES:
        raise ValidationError(f"n_resamples must be at least {MIN_RESAMPLES}")
    if not np.all(np.isfinite(x)):
        raise ValidationError("series contains a missing or non-finite value; refusing to drop it")
    if not 0 < alpha < 1:
        raise ValidationError("alpha must be between 0 and 1")

    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(t / block_length)
    starts = rng.integers(0, t - block_length + 1, size=(n_resamples, n_blocks))
    offsets = np.arange(block_length)
    index = (starts[:, :, None] + offsets[None, None, :]).reshape(n_resamples, -1)[:, :t]
    means = x[index].mean(axis=1)
    lower, upper = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return Interval(
        lower=float(lower),
        point=float(x.mean()),
        upper=float(upper),
        n=t,
        block_length=block_length,
        n_resamples=n_resamples,
    )


class NullMode(StrEnum):
    SIGN = "sign"
    CROSS_SECTIONAL = "cross_sectional"


@dataclass(frozen=True)
class NullReport:
    """Observed against a control that differs from the strategy in exactly one respect."""

    mode: NullMode
    observed: float
    null_mean: float
    null_sd: float
    p_perm: float
    sigma_above: float
    n_perm: int


def permutation_null(
    nets: Sequence[NetTrade],
    *,
    mode: NullMode,
    n_perm: int = 1000,
    seed: int = 0,
    turnover_tolerance: float = 0.05,
) -> NullReport:
    """Beat the same trades with the claimed association removed and everything else intact.

    Sign permutation flips the direction of every trade at a timestamp: the gross return flips
    sign, the cost does not. The null therefore centres near minus the round trip, and a null
    centred at zero is the sign that costs were not applied to the control. The cross-sectional
    shuffle permutes the declared weights across the instruments traded at each timestamp and
    refuses if that changes turnover by more than the tolerance, because a permutation that
    changed turnover changed something other than the signal.

    The p-value floor `(1 + hits) / (1 + n_perm)` keeps it off exactly zero, which no finite
    number of permutations can support.
    """
    if n_perm < MIN_PERMUTATIONS:
        raise ValidationError(f"n_perm must be at least {MIN_PERMUTATIONS}")
    if not nets:
        raise ValidationError("no trades")
    stamps = sorted({n.record.entry_ts_ns for n in nets})
    position = {s: i for i, s in enumerate(stamps)}
    t = len(stamps)
    ts_index = np.asarray([position[n.record.entry_ts_ns] for n in nets], dtype=np.int64)
    weight = np.asarray([float(n.record.weight) for n in nets], dtype=np.float64)
    gross = np.asarray([float(n.record.gross_return_bps) for n in nets], dtype=np.float64)
    cost = np.asarray([float(n.cost_bps) for n in nets], dtype=np.float64)

    def series_mean(contrib: NDArray[np.float64]) -> float:
        totals = np.zeros(t, dtype=np.float64)
        np.add.at(totals, ts_index, contrib)
        return float(totals.mean())

    observed = series_mean(weight * (gross - cost))
    rng = np.random.default_rng(seed)
    draws = np.empty(n_perm, dtype=np.float64)

    if mode is NullMode.SIGN:
        for i in range(n_perm):
            eps = rng.choice(np.array([-1.0, 1.0]), size=t)
            draws[i] = series_mean(weight * (eps[ts_index] * gross - cost))
    else:
        groups = [np.flatnonzero(ts_index == k) for k in range(t)]
        base_turnover = _turnover(nets, weight)
        for i in range(n_perm):
            permuted = weight.copy()
            for members in groups:
                if members.size > 1:
                    permuted[members] = weight[rng.permutation(members)]
            turnover = _turnover(nets, permuted)
            if (
                base_turnover > 0
                and abs(turnover - base_turnover) / base_turnover > turnover_tolerance
            ):
                raise ValidationError(
                    "the cross-sectional shuffle changed turnover beyond the tolerance; the null "
                    "is no longer matched to the strategy"
                )
            draws[i] = series_mean(permuted * (gross - cost))

    null_mean = float(draws.mean())
    null_sd = float(draws.std(ddof=1)) if n_perm > 1 else 0.0
    if null_sd == 0:
        raise ValidationError("degenerate null: every permutation produced the same result")
    hits = int(np.count_nonzero(draws >= observed))
    return NullReport(
        mode=mode,
        observed=observed,
        null_mean=null_mean,
        null_sd=null_sd,
        p_perm=(1 + hits) / (1 + n_perm),
        sigma_above=(observed - null_mean) / null_sd,
        n_perm=n_perm,
    )


def _turnover(nets: Sequence[NetTrade], weight: NDArray[np.float64]) -> float:
    """Sum over consecutive timestamps of the absolute change in each instrument's weight."""
    by_ts: dict[int, dict[str, float]] = defaultdict(dict)
    for n, w in zip(nets, weight, strict=True):
        by_ts[n.record.entry_ts_ns][n.record.instrument] = float(w)
    stamps = sorted(by_ts)
    total = 0.0
    previous: dict[str, float] = {}
    for s in stamps:
        current = by_ts[s]
        for name in set(previous) | set(current):
            total += abs(current.get(name, 0.0) - previous.get(name, 0.0))
        previous = current
    return total


def subperiod_means(series: NDArray[np.float64], k: int = 3) -> tuple[float, ...]:
    """Means over `k` sequential blocks. An edge that lives in one block is a `Condition` death."""
    x = np.asarray(series, dtype=np.float64)
    if k < 2:
        raise ValidationError("at least two subperiods")
    if x.size < k:
        raise ValidationError(f"{x.size} observations cannot be cut into {k} subperiods")
    return tuple(float(part.mean()) for part in np.array_split(x, k))


def benjamini_hochberg(p_values: Sequence[float], q: float = 0.05) -> tuple[bool, ...]:
    """Which members of a family of tests survive at false-discovery rate `q`."""
    p = np.asarray(p_values, dtype=np.float64)
    if p.size == 0:
        return ()
    if not np.all(np.isfinite(p)) or np.any(p < 0) or np.any(p > 1):
        raise ValidationError("p-values must lie in [0, 1]")
    if not 0 < q < 1:
        raise ValidationError("q must be between 0 and 1")
    order = np.argsort(p)
    ranked = p[order]
    m = p.size
    thresholds = q * (np.arange(1, m + 1) / m)
    below = np.flatnonzero(ranked <= thresholds)
    survivors = np.zeros(m, dtype=bool)
    if below.size:
        survivors[order[: below.max() + 1]] = True
    return tuple(bool(v) for v in survivors)


@dataclass(frozen=True)
class AnchorReport:
    n_phases: int
    n_positive: int
    n_significant_positive: int
    n_significant_negative: int
    net_min: float
    net_median: float
    net_max: float
    sign_consistent: bool
    reported_rank: int
    reported_label: str


def anchor_robustness(
    nets: Sequence[float], t_stats: Sequence[float], reported_index: int
) -> AnchorReport:
    """Sensitivity of a result to the phase of a periodic rebalance, checked by code.

    Nineteen pre-registrations once fixed a rebalance hour without recording it as a choice; the
    published headline turned out to be the best of twenty-four undisclosed phases. A single
    phase is the defect itself and is refused. Significance is two-sided: a phase at -6.0 with
    t = -3.1 is exactly as significant as one at +6.0 with t = +3.1. Labels follow terciles.
    """
    a = np.asarray(nets, dtype=np.float64)
    tt = np.asarray(t_stats, dtype=np.float64)
    if a.size < 2:
        raise ValidationError(
            "anchor robustness needs at least two phases; one phase is the defect"
        )
    if tt.size != a.size:
        raise ValidationError("t_stats and nets differ in length")
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(tt))):
        raise ValidationError("a phase that failed to compute must be explicit, not passed through")
    if not 0 <= reported_index < a.size:
        raise ValidationError("reported_index out of range")
    median = float(np.median(a))
    rank = int(np.count_nonzero(a > a[reported_index]) + 1)
    tercile = a.size / 3
    if rank <= tercile:
        label = "OPTIMISTIC"
    elif rank > a.size - tercile:
        label = "PESSIMISTIC"
    else:
        label = "TYPICAL"
    return AnchorReport(
        n_phases=int(a.size),
        n_positive=int(np.count_nonzero(a > 0)),
        n_significant_positive=int(np.count_nonzero(tt > SIGNIFICANCE_T)),
        n_significant_negative=int(np.count_nonzero(tt < -SIGNIFICANCE_T)),
        net_min=float(a.min()),
        net_median=median,
        net_max=float(a.max()),
        sign_consistent=bool(np.all(np.sign(a) == np.sign(median)) and median != 0),
        reported_rank=rank,
        reported_label=label,
    )
