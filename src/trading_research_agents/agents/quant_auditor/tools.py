"""The brief the quant auditor reads: what the desk decided and what those decisions were worth.

Closed round trips are read from the runs a session wrote, not from the cycles: a cycle decides, a
session lives long enough to find out. When there are none, the brief says so rather than filling
the gap, because an audit of results with no results is a thing an operator must be able to see.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trading_research_agents.agents.audits import Cycles, decisions, refusal_kinds
from trading_research_agents.execution.excursion import summarise


def read_trades(runs: Path, *, limit: int = 2000) -> tuple[dict[str, Any], ...]:
    """Closed round trips from `runs/*/trades.jsonl`, oldest first."""
    if not runs.is_dir():
        return ()
    rows: list[dict[str, Any]] = []
    for path in sorted(runs.glob("*/trades.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return tuple(rows[-limit:])


def excursions_of(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The favourable and adverse excursions of the closed round trips, when they carry them."""
    rows = [t["excursion"] for t in trades if isinstance(t.get("excursion"), dict)]
    if not rows:
        return {"round_trips_with_excursions": 0}
    return {"round_trips_with_excursions": len(rows), **summarise(rows)}


def quant_brief(cycles: Cycles, *, runs: Path | None = None) -> dict[str, Any]:
    """Everything the quant auditor is given: decisions, their cost, and what came of them."""
    trades = read_trades(runs) if runs is not None else ()
    closed = [t for t in trades if t.get("net_bps") is not None]
    nets = sorted(float(t["net_bps"]) for t in closed)
    return {
        "basis": (
            "the three figures that go together are decisions.median_claimed_edge_bps, "
            "decisions.median_round_trip_cost_bps and decisions.median_net_after_cost_bps: the "
            "third is the first minus the second, so a positive net is an edge that covered its "
            "cost; "
            "decisions.median_net_after_cost_bps is what was left of the claimed edge once every "
            "cost the gate priced was taken off it, so it is already net and it is not to be "
            "compared with decisions.median_round_trip_cost_bps, which is what that round trip "
            "was priced at; a refusal is one gate saying no in one cycle, and several can fall in "
            "one cycle; round_trips are positions a session actually closed, and there may be none"
        ),
        "cycles": cycles.count,
        "decisions": decisions(cycles),
        "refusals": refusal_kinds(cycles),
        "round_trips": {
            "closed": len(closed),
            "median_realised_net_bps": None if not nets else round(nets[len(nets) // 2], 4),
            "worst_realised_net_bps": None if not nets else round(nets[0], 4),
            "best_realised_net_bps": None if not nets else round(nets[-1], 4),
            **excursions_of(trades),
        },
    }
