"""What the risk manager is handed: the figures a control decision rests on, from a run directory.

Latency as measured from intent to fill; the rolling net return against the magnitude the
registration claimed; drawdown of the closed round trips; which control refused, how often.
All of it comes from the files the runtime wrote, so the model is judging what happened, not
what the run was supposed to do.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.execution.excursion import summarise


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


@dataclass(frozen=True)
class Latency:
    n: int
    median_ms: float | None
    max_ms: float | None
    declared_ms: float
    ratio_to_declared: float | None

    def record(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "median_ms": None if self.median_ms is None else round(self.median_ms, 1),
            "max_ms": None if self.max_ms is None else round(self.max_ms, 1),
            "declared_ms": self.declared_ms,
            "ratio_to_declared": (
                None if self.ratio_to_declared is None else round(self.ratio_to_declared, 1)
            ),
        }


def measured_latency(run_dir: Path, *, declared_ms: Decimal) -> Latency:
    """Admitted intents paired with the fills that followed them, in order."""
    intents = [i for i in _lines(run_dir / "intents.jsonl") if i.get("admitted")]
    fills = _lines(run_dir / "fills.jsonl")
    # The venue fills in order; each fill answers the earliest admitted intent not yet filled.
    gaps = []
    for intent, fill in zip(intents, fills, strict=False):
        gap_ns = int(fill["ts_ns"]) - int(intent["ts_ns"])
        if gap_ns >= 0:
            gaps.append(gap_ns / 1e6)
    if not gaps:
        return Latency(0, None, None, float(declared_ms), None)
    ordered = sorted(gaps)
    median = ordered[len(ordered) // 2]
    declared = float(declared_ms)
    return Latency(
        len(gaps), median, ordered[-1], declared, None if declared <= 0 else median / declared
    )


@dataclass(frozen=True)
class Decay:
    round_trips: int
    net_bps_total: Decimal
    mean_net_bps: Decimal | None
    rolling_mean_bps: Decimal | None
    rolling_window: int
    claimed_bps: Decimal
    below_claim: bool | None

    def record(self) -> dict[str, Any]:
        return {
            "round_trips": self.round_trips,
            "net_bps_total": str(self.net_bps_total),
            "mean_net_bps": None if self.mean_net_bps is None else str(self.mean_net_bps),
            "rolling_mean_bps": None
            if self.rolling_mean_bps is None
            else str(self.rolling_mean_bps),
            "rolling_window": self.rolling_window,
            "claimed_bps": str(self.claimed_bps),
            "below_claim": self.below_claim,
        }


def alpha_decay(run_dir: Path, *, claimed_bps: Decimal, window: int = 10) -> Decay:
    """Net per round trip, overall and over the last `window`, against the registered magnitude."""
    nets = [Decimal(str(t["net_bps"])) for t in _lines(run_dir / "trades.jsonl")]
    if not nets:
        return Decay(0, Decimal(0), None, None, window, claimed_bps, None)
    total = sum(nets, Decimal(0))
    mean = total / len(nets)
    tail = nets[-window:]
    rolling = sum(tail, Decimal(0)) / len(tail)
    return Decay(len(nets), total, mean, rolling, window, claimed_bps, rolling < claimed_bps)


def drawdown(run_dir: Path) -> dict[str, Any]:
    """Peak-to-trough of the cumulative net, in basis points, over the closed round trips."""
    nets = [Decimal(str(t["net_bps"])) for t in _lines(run_dir / "trades.jsonl")]
    peak = running = Decimal(0)
    worst = Decimal(0)
    for n in nets:
        running += n
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return {"round_trips": len(nets), "max_drawdown_bps": str(worst), "final_bps": str(running)}


def refusal_pattern(run_dir: Path) -> list[dict[str, Any]]:
    """Which control said no, and how many times; the controls that repeat are the finding."""
    counts: dict[str, int] = {}
    for intent in _lines(run_dir / "intents.jsonl"):
        if intent.get("admitted"):
            continue
        reason = str(intent.get("decision", "unknown"))
        key = reason.split(":")[0].strip()
        counts[key] = counts.get(key, 0) + 1
    return [{"control": k, "refusals": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def excursions(run_dir: Path) -> dict[str, Any]:
    """How far the closed round trips went each way before they ended.

    A position that closed at plus two after standing twenty in front says something about the
    exit; one that closed at plus two after standing fourteen under water says something about how
    near the stop came. Neither shows up in the net, so both are reported here.
    """
    return summarise(_lines(run_dir / "trades.jsonl"))


def risk_brief(
    run_dirs: Sequence[tuple[str, Path]], *, declared_ms: Decimal, claimed_bps: Decimal
) -> dict[str, Any]:
    return {
        name: {
            "latency": measured_latency(path, declared_ms=declared_ms).record(),
            "alpha_decay": alpha_decay(path, claimed_bps=claimed_bps).record(),
            "drawdown": drawdown(path),
            "excursions": excursions(path),
            "refusals": refusal_pattern(path),
        }
        for name, path in run_dirs
    }
