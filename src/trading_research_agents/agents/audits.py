"""What the two auditors read: the cycles this desk already ran, counted by code.

An auditor reads the record, never the market and never a key. Everything here comes from files the
project wrote itself - `runs/cycles/*/cycle.json` and `usage.jsonl` - so an audit can be re-run
tomorrow on the same evidence and reach the same figures. That is the whole point of auditing from a
record rather than from a memory of how the week felt.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any


class AuditError(ValueError):
    """There is nothing recorded to audit, or what is recorded cannot be read."""


@dataclass(frozen=True)
class Cycles:
    """Every cycle record that could be read, newest last, with what could not."""

    records: tuple[dict[str, Any], ...] = ()
    unreadable: tuple[str, ...] = ()
    root: Path | None = None

    @property
    def count(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.records)


def read_cycles(root: Path, *, limit: int = 500) -> Cycles:
    """The cycle records under `root`, oldest first. A file that cannot be read is named."""
    if not root.is_dir():
        raise AuditError(f"{root} is not a directory; there is nothing recorded to audit")
    found: list[dict[str, Any]] = []
    bad: list[str] = []
    for path in sorted(root.glob("*/cycle.json"))[-limit:]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            bad.append(f"{path.parent.name}: {exc}")
            continue
        if isinstance(record, dict):
            found.append(record)
    if not found:
        raise AuditError(f"no cycle record under {root} could be read")
    return Cycles(records=tuple(found), unreadable=tuple(bad), root=root)


def read_usage(path: Path) -> tuple[dict[str, Any], ...]:
    """The model-call ledger, when the project kept one."""
    if not path.is_file():
        return ()
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return tuple(rows)


@dataclass
class Counts:
    """A tally that keeps its order, so two audits of the same runs read the same way."""

    tallied: dict[str, int] = field(default_factory=dict)

    def add(self, key: str) -> None:
        self.tallied[key] = self.tallied.get(key, 0) + 1

    def record(self) -> dict[str, int]:
        return dict(sorted(self.tallied.items(), key=lambda kv: (-kv[1], kv[0])))


def refusal_kinds(cycles: Cycles) -> dict[str, int]:
    """What stopped things, by the first word of each refusal: the gate that said no."""
    counts = Counts()
    for record in cycles:
        for refusal in record.get("refusals") or ():
            head = str(refusal).split(":", 1)[0].strip()
            counts.add(head or "unnamed")
    return counts.record()


def agent_health(cycles: Cycles) -> dict[str, Any]:
    """How the roles behaved: how often they answered, how long they took, what failed."""
    ok = failed = reused = 0
    seconds: dict[str, list[float]] = {}
    errors = Counts()
    for record in cycles:
        rounds = [
            *(record.get("agents") or {}).get("agents", []),
            *((record.get("validators") or {}) or {}).get("agents", []),
        ]
        for row in rounds:
            name = str(row.get("agent", "unknown"))
            if row.get("reused"):
                reused += 1
            if row.get("ok"):
                ok += 1
            else:
                failed += 1
                errors.add(f"{name}: {str(row.get('error', '')).split(':', 1)[0]}")
            seconds.setdefault(name, []).append(float(row.get("seconds") or 0.0))
    calls = ok + failed
    return {
        "calls": calls,
        "answered": ok,
        "failed": failed,
        "reused": reused,
        "failure_rate": round(failed / calls, 4) if calls else 0.0,
        "median_seconds": {
            name: round(median(values), 3) for name, values in sorted(seconds.items()) if values
        },
        "failures": errors.record(),
    }


def data_health(cycles: Cycles) -> dict[str, Any]:
    """Where the values came from, how old they were, and how often a kind was refused."""
    ages: dict[str, list[float]] = {}
    bars: list[float] = []
    sources = Counts()
    refused = Counts()
    stale = Counts()
    for record in cycles:
        data = record.get("data") or {}
        for kind, value in (data.get("values") or {}).items():
            ages.setdefault(kind, []).append(float(value.get("age_s") or 0.0))
            if value.get("interval_s"):
                bars.append(float(value["interval_s"]))
            sources.add(f"{kind} <- {value.get('source')}")
        for kind in data.get("refused") or {}:
            refused.add(kind)
        for kind in data.get("stale") or ():
            stale.add(kind)
    out: dict[str, Any] = {
        "median_age_s": {kind: round(median(v), 3) for kind, v in sorted(ages.items()) if v},
    }
    if bars:
        # Without this an auditor reads a fifteen-minute bar ten minutes after its close as stale.
        out["ohlcv_bar_s"] = median(bars)
        out["ohlcv_age_note"] = (
            "ohlcv is aged from the close of its last finished bar, so any age below ohlcv_bar_s "
            "plus the chain's ttl is the newest bar there is, not a stale one"
        )
    return {
        **out,
        "sources": sources.record(),
        "refused": refused.record(),
        "used_stale": stale.record(),
    }


def token_spend(cycles: Cycles, usage: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """What the thinking cost, and what it bought."""
    tokens = [int(r.get("tokens") or 0) for r in cycles]
    intents = sum(1 for r in cycles if r.get("intent"))
    spent = sum(tokens)
    by_role = Counts()
    latencies: dict[str, list[float]] = {}
    for row in usage:
        role = str(row.get("role", "unknown"))
        by_role.tallied[role] = by_role.tallied.get(role, 0) + int(row.get("output_tokens") or 0)
        latencies.setdefault(role, []).append(float(row.get("latency_ms") or 0.0))
    return {
        "cycles": len(cycles.records),
        "tokens_total": spent,
        "tokens_per_cycle": round(spent / len(cycles.records), 1) if cycles.records else 0.0,
        "intents": intents,
        "tokens_per_intent": round(spent / intents, 1) if intents else None,
        "output_tokens_by_role": by_role.record(),
        "median_latency_ms": {
            role: round(median(v), 1) for role, v in sorted(latencies.items()) if v
        },
    }


def decisions(cycles: Cycles) -> dict[str, Any]:
    """What the desk decided: targets, sizes, conviction, and what the trade was worth on paper."""
    targets = Counts()
    nets: list[Decimal] = []
    edges: list[Decimal] = []
    costs: list[Decimal] = []
    scores: list[Decimal] = []
    sizes: list[Decimal] = []
    admitted_scores: list[Decimal] = []
    refused_scores: list[Decimal] = []
    placed = 0
    for record in cycles:
        analyst = record.get("analyst") or {}
        if analyst.get("target"):
            targets.add(str(analyst["target"]))
        cost = (record.get("cost") or {}).get("cost") or {}
        if cost.get("round_trip_bps"):
            costs.append(_decimal(cost["round_trip_bps"]))
        conviction = record.get("conviction") or {}
        score = (conviction.get("score") or {}).get("score")
        if score is not None:
            scores.append(_decimal(score))
            (admitted_scores if conviction.get("admitted") else refused_scores).append(
                _decimal(score)
            )
        intent = record.get("intent")
        if intent:
            nets.append(_decimal(intent.get("net_bps", "0")))
            edges.append(_decimal(intent.get("edge_bps", "0")))
            sizes.append(_decimal(intent.get("size", "0")))
        if (record.get("placed") or {}).get("sent"):
            placed += 1
    return {
        "targets": targets.record(),
        "intents": len(nets),
        "placed": placed,
        "median_claimed_edge_bps": _median(edges),
        "median_net_after_cost_bps": _median(nets),
        "median_round_trip_cost_bps": _median(costs),
        "median_conviction": _median(scores),
        "median_conviction_admitted": _median(admitted_scores),
        "median_conviction_refused": _median(refused_scores),
        "median_size": _median(sizes),
    }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)


def _median(values: Sequence[Decimal]) -> str | None:
    """The middle of what was recorded, to four places. A figure nobody can read is not a figure."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    found = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return str(found.quantize(Decimal("0.0001")))
