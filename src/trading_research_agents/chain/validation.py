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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.chain.preregistration import KillCriterion, Preregistration
from trading_research_agents.chain.seal import research_only
from trading_research_agents.chain.trade_record import (
    CostOf,
    TradeRecord,
    ValidationError,
)
from trading_research_agents.chain.validation_stats import (
    Interval,
    NullMode,
    NullReport,
    block_bootstrap_ci,
    collapse,
    net_returns,
    permutation_null,
    subperiod_means,
)
from trading_research_agents.data.schema import Direction
from trading_research_agents.ledger.entry import LedgerEntry
from trading_research_agents.ledger.vocabulary import Axis, Transferability, Verdict

LAYER = 5


# Trades and their net returns


# Inference


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


def read_trades(path: Path) -> tuple[TradeRecord, ...]:
    """`trades.jsonl`: one trade per line, every number as a decimal string. A bad line stops it."""
    out: list[TradeRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                out.append(
                    TradeRecord(
                        instrument=str(d["instrument"]),
                        entry_ts_ns=int(d["entry_ts_ns"]),
                        exit_ts_ns=int(d["exit_ts_ns"]),
                        direction=Direction(str(d["direction"])),
                        weight=Decimal(str(d["weight"])),
                        gross_return_bps=Decimal(str(d["gross_return_bps"])),
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError, ArithmeticError) as exc:
                raise ValidationError(f"{path}:{number}: unreadable trade: {exc}") from exc
    return tuple(out)
