"""What the auditor is handed: totals from the usage ledger, a run against its replay, the
ladder's receipts, and the ledger's own consistency. Numbers, with what they were computed from.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agent.agent.usage import UsageLedger


def usage_summary(usage_path: Path, *, round_trips: int) -> dict[str, Any]:
    rows = UsageLedger.read(usage_path)
    by_role: dict[str, dict[str, Any]] = {}
    tokens = 0
    cost = Decimal(0)
    errors = 0
    for r in rows:
        role = by_role.setdefault(
            str(r["role"]), {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
        )
        role["calls"] += 1
        role["input_tokens"] += int(r["input_tokens"])
        role["output_tokens"] += int(r["output_tokens"])
        role["latency_ms"] += int(r["latency_ms"])
        tokens += int(r["input_tokens"]) + int(r["output_tokens"])
        if r.get("cost") is not None:
            cost += Decimal(str(r["cost"]))
        if str(r.get("outcome", "")).startswith("error"):
            errors += 1
    for role in by_role.values():
        role["mean_latency_ms"] = (
            round(role["latency_ms"] / role["calls"], 1) if role["calls"] else None
        )
    return {
        "calls": len(rows),
        "errors": errors,
        "tokens": tokens,
        "cost": str(cost),
        "tokens_per_round_trip": None if round_trips == 0 else round(tokens / round_trips),
        "by_role": by_role,
    }


def _trades(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def run_vs_replay(run_dir: Path, replay_dir: Path) -> dict[str, Any]:
    """Trade by trade: the same entries, and how far the net differs once costs are aligned."""
    live = _trades(run_dir / "trades.jsonl")
    replay = _trades(replay_dir / "trades.jsonl")
    by_entry = {int(t["entry_ts_ns"]): t for t in replay}
    matched = 0
    net_gap = Decimal(0)
    unmatched: list[int] = []
    for t in live:
        key = int(t["entry_ts_ns"])
        twin = by_entry.get(key)
        if twin is None:
            unmatched.append(key)
            continue
        matched += 1
        net_gap += Decimal(str(t["net_bps"])) - Decimal(str(twin["net_bps"]))
    return {
        "live_round_trips": len(live),
        "replay_round_trips": len(replay),
        "matched": matched,
        "unmatched_live_entries": unmatched[:10],
        "net_gap_bps_total": str(net_gap),
        "net_gap_bps_per_matched": None if matched == 0 else str(net_gap / matched),
    }


def evidence_check(ladder_path: Path) -> list[dict[str, Any]]:
    """Each promotion, and whether its evidence names something a gate produced."""
    if not ladder_path.is_file():
        return []
    state = json.loads(ladder_path.read_text("utf-8"))
    words = ("validation", "layer 5", "open", "replay", "paper", "shadow", "canary", "seal")
    out = []
    for h in state.get("history", []):
        if h.get("kind") != "promotion":
            continue
        evidence = str(h.get("evidence", ""))
        out.append(
            {
                "to": h.get("to"),
                "signed_by": h.get("signed_by"),
                "evidence": evidence,
                "names_a_gate": any(w in evidence.lower() for w in words),
            }
        )
    return out


def audit_brief(
    *,
    usage_path: Path,
    round_trips: int,
    run_dir: Path | None,
    replay_dir: Path | None,
    ladder_path: Path,
) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "usage": usage_summary(usage_path, round_trips=round_trips),
        "promotions": evidence_check(ladder_path),
    }
    if run_dir is not None and replay_dir is not None:
        brief["run_vs_replay"] = run_vs_replay(run_dir, replay_dir)
    return brief
