"""Layer 5: what has to be true before a measurement becomes a verdict.

Eight requirements, each replacing a specific way the research programme this descends from
had already fooled itself: net from day one (a whole programme measured mid to mid), inference
that knows fifteen correlated instruments carry the information of about one and a third, sizing
that matches the live path, stability across subperiods, declared power, a matched null, a
correction for the family of tests, and no selection after the fact.

This module assesses results. It does not simulate strategies: the trades come in as records,
and who produced them is not its concern. Money stays in `Decimal` up to the collapsed series;
the statistics run in floating point from there, and that boundary is the only one.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import NDArray

from trading_research_agent.chain.preregistration import KillCriterion, Preregistration
from trading_research_agent.chain.seal import research_only
from trading_research_agent.data.schema import Direction
from trading_research_agent.ledger.entry import LedgerEntry
from trading_research_agent.ledger.vocabulary import Axis, Transferability, Verdict

LAYER = 5
MIN_RESAMPLES = 1000
MIN_PERMUTATIONS = 1000
SIGNIFICANCE_T = 1.96


class ValidationError(ValueError):
    """The measurement cannot be turned into a verdict as it stands."""


# Trades and their net returns


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


# Inference


@dataclass(frozen=True)
class Interval:
    lower: float
    point: float
    upper: float
    n: int
    block_length: int
    n_resamples: int

    @property
    def spans_zero(self) -> bool:
        return self.lower <= 0 <= self.upper


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


# The verdict


@dataclass(frozen=True)
class Fired:
    criterion: KillCriterion
    value: Decimal
    effective: Verdict


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    axis: Axis
    fired: tuple[Fired, ...]
    power_achieved: bool
    note: str


@dataclass(frozen=True)
class Validation:
    registration_seal: str
    trades_sha256: str
    n_trades: int
    n_timestamps: int
    power_required: int
    interval: Interval
    null: NullReport
    subperiods: tuple[float, ...]
    gross_mean_bps: float
    metrics: dict[str, Decimal]
    decision: Decision

    @property
    def power_achieved(self) -> bool:
        return self.decision.power_achieved


def validate(
    registration: Preregistration,
    trades: Sequence[TradeRecord],
    cost_of: CostOf,
    *,
    block_length: int,
    n_resamples: int = 5000,
    n_perm: int = 1000,
    seed: int = 0,
    k_subperiods: int = 3,
    null_mode: NullMode = NullMode.SIGN,
) -> Validation:
    """Run the eight requirements on a set of trades and apply the registration's kill criteria."""
    if not trades:
        raise ValidationError("no trades")
    universe = set(registration.universe.instruments)
    foreign = sorted({t.instrument for t in trades} - universe)
    if foreign:
        raise ValidationError(f"trades on instruments outside the declared universe: {foreign}")
    research_only(
        [ts for t in trades for ts in (t.entry_ts_ns, t.exit_ts_ns)],
        registration.era,
        who="validate",
    )

    nets = net_returns(trades, cost_of)
    _, series = collapse(nets)
    _, gross_series = collapse(nets, gross=True)
    interval = block_bootstrap_ci(
        series, block_length=block_length, n_resamples=n_resamples, seed=seed
    )
    null = permutation_null(nets, mode=null_mode, n_perm=n_perm, seed=seed)
    parts = subperiod_means(series, k_subperiods)
    n_timestamps = int(series.size)
    power_achieved = n_timestamps >= registration.power.required_sample

    metrics: dict[str, Decimal] = {
        "net_return_bps": _dec(interval.point),
        "net_return_ci95_lower_bps": _dec(interval.lower),
        "net_return_ci95_upper_bps": _dec(interval.upper),
        "gross_return_bps": _dec(float(gross_series.mean())),
        "permutation_p_value": _dec(null.p_perm),
        "null_mean_bps": _dec(null.null_mean),
        "sigma_above_null": _dec(null.sigma_above),
        "subperiod_min_net_bps": _dec(min(parts)),
        "n_timestamps": Decimal(n_timestamps),
        "n_trades": Decimal(len(trades)),
    }
    decision = decide(registration, metrics, power_achieved=power_achieved)
    return Validation(
        registration_seal=registration.seal(),
        trades_sha256=_trades_hash(trades),
        n_trades=len(trades),
        n_timestamps=n_timestamps,
        power_required=registration.power.required_sample,
        interval=interval,
        null=null,
        subperiods=parts,
        gross_mean_bps=float(gross_series.mean()),
        metrics=metrics,
        decision=decision,
    )


def decide(
    registration: Preregistration, metrics: dict[str, Decimal], *, power_achieved: bool
) -> Decision:
    """Apply the pre-registered kill criteria. A kill without power is NOT PROVEN, not a kill."""
    fired: list[Fired] = []
    for k in registration.kill_criteria:
        if k.metric not in metrics:
            raise ValidationError(
                f"kill criterion names {k.metric!r}, which this validation does not compute"
            )
        value = metrics[k.metric]
        if _crossed(value, k.comparison, k.threshold):
            effective = k.verdict
            if effective is Verdict.FALSIFIED and not power_achieved:
                effective = Verdict.NOT_PROVEN
            fired.append(Fired(k, value, effective))

    kills = [f for f in fired if f.effective is Verdict.FALSIFIED]
    if kills:
        only_condition = all(f.criterion.metric.startswith("subperiod") for f in kills)
        axis = Axis.CONDITION if only_condition else Axis.IDEA
        summary = _summary(kills)
        return Decision(
            Verdict.FALSIFIED, axis, tuple(fired), power_achieved, f"killed on {summary}"
        )
    if fired:
        summary = _summary(fired)
        why = (
            "declared power not achieved" if not power_achieved else "interval spans the threshold"
        )
        return Decision(
            Verdict.NOT_PROVEN, Axis.POWER, tuple(fired), power_achieved, f"{why}: {summary}"
        )
    if not power_achieved:
        return Decision(
            Verdict.NOT_PROVEN,
            Axis.POWER,
            (),
            power_achieved,
            f"no criterion fired, but {metrics['n_timestamps']} of "
            f"{registration.power.required_sample} declared observations",
        )
    return Decision(
        Verdict.OPEN, Axis.DATA, (), power_achieved, "survived layer 5; awaiting shadow at layer 7"
    )


def to_entry(
    validation: Validation, registration: Preregistration, *, entry_id: str, recorded_at: datetime
) -> LedgerEntry:
    d = validation.decision
    evidence = (
        f"preregistration:{validation.registration_seal}",
        f"trades:{validation.trades_sha256}",
    )
    if d.verdict is Verdict.FALSIFIED:
        return LedgerEntry(
            entry_id=entry_id,
            subject=registration.name,
            verdict=Verdict.FALSIFIED,
            recorded_at=recorded_at,
            layer=LAYER,
            axis_of_death=(d.axis,),
            transferability=Transferability.UNIVERSAL,
            evidence=evidence,
            note=d.note,
        )
    return LedgerEntry(
        entry_id=entry_id,
        subject=registration.name,
        verdict=d.verdict,
        recorded_at=recorded_at,
        layer=LAYER,
        blocking_axis=d.axis,
        evidence=evidence,
        note=d.note,
    )


def to_record(v: Validation) -> dict[str, Any]:
    return {
        "schema": 1,
        "registration_seal": v.registration_seal,
        "trades_sha256": v.trades_sha256,
        "n_trades": v.n_trades,
        "n_timestamps": v.n_timestamps,
        "power_required": v.power_required,
        "power_achieved": v.power_achieved,
        "interval": {
            "lower": v.interval.lower,
            "point": v.interval.point,
            "upper": v.interval.upper,
            "n": v.interval.n,
            "block_length": v.interval.block_length,
            "n_resamples": v.interval.n_resamples,
        },
        "null": {
            "mode": v.null.mode.value,
            "observed": v.null.observed,
            "null_mean": v.null.null_mean,
            "null_sd": v.null.null_sd,
            "p_perm": v.null.p_perm,
            "sigma_above": v.null.sigma_above,
            "n_perm": v.null.n_perm,
        },
        "subperiods": list(v.subperiods),
        "gross_mean_bps": v.gross_mean_bps,
        "metrics": {k: str(val) for k, val in v.metrics.items()},
        "decision": {
            "verdict": v.decision.verdict.value,
            "axis": v.decision.axis.value,
            "note": v.decision.note,
            "fired": [
                {
                    "metric": f.criterion.metric,
                    "comparison": f.criterion.comparison,
                    "threshold": str(f.criterion.threshold),
                    "value": str(f.value),
                    "effective": f.effective.value,
                }
                for f in v.decision.fired
            ],
        },
    }


def _summary(fired: Sequence[Fired]) -> str:
    return "; ".join(
        f"{f.criterion.metric} {f.criterion.comparison} {f.criterion.threshold} "
        f"(observed {f.value})"
        for f in fired
    )


def _crossed(value: Decimal, comparison: str, threshold: Decimal) -> bool:
    if comparison == "<":
        return value < threshold
    if comparison == "<=":
        return value <= threshold
    if comparison == ">":
        return value > threshold
    return value >= threshold


def _dec(value: float) -> Decimal:
    if not math.isfinite(value):
        raise ValidationError("a metric is not finite")
    return Decimal(repr(value))


def _trades_hash(trades: Sequence[TradeRecord]) -> str:
    rows = [
        [
            t.instrument,
            t.entry_ts_ns,
            t.exit_ts_ns,
            t.direction.value,
            str(t.weight),
            str(t.gross_return_bps),
        ]
        for t in trades
    ]
    payload = json.dumps(rows, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()
