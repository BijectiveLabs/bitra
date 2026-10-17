"""The brief the quant auditor reads: what the desk decided and what those decisions were worth.

Closed round trips are read from where they were closed: the runs a session wrote, and the desk's
own `round_trips.jsonl` beside its cycles, which is where a venue desk records them. When there are
none, the brief says so rather than filling the gap, because an audit of results with no results is
a thing an operator must be able to see. When `tra hindsight` has been run, what each gate's
refusals would have done is in the brief as well.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
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


def desk_round_trips(cycles: Path, *, limit: int = 2000) -> tuple[dict[str, Any], ...]:
    """The round trips a venue desk closed, each with its net in bps of the entry notional.

    Rows written before the net was recorded in bps carry the net in quote currency, the entry
    and the size, which is enough to state it; a row with no exit has no result and is left out.
    """
    path = cycles / "round_trips.jsonl"
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("net") is None:
            continue
        if row.get("net_bps") is None:
            try:
                notional = Decimal(str(row["entry"])) * Decimal(str(row["size"]))
                row["net_bps"] = str(Decimal(str(row["net"])) / notional * 10_000)
            except (KeyError, InvalidOperation, ArithmeticError):
                continue
        rows.append(row)
    return tuple(rows[-limit:])


def hindsight_of(cycles: Path) -> dict[str, Any] | None:
    """The last `tra hindsight` report, when one was written."""
    path = cycles / "hindsight.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    keep = ("hold_s", "replayed", "independent", "days", "admitted", "gates", "all_refused", "side")
    return {k: report[k] for k in keep if k in report}


def excursions_of(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The favourable and adverse excursions of the closed round trips, when they carry them."""
    rows = [t["excursion"] for t in trades if isinstance(t.get("excursion"), dict)]
    if not rows:
        return {"round_trips_with_excursions": 0}
    return {"round_trips_with_excursions": len(rows), **summarise(rows)}


def quant_brief(
    cycles: Cycles, *, runs: Path | None = None, desk: Path | None = None
) -> dict[str, Any]:
    """Everything the quant auditor is given: decisions, their cost, and what came of them."""
    trades = (read_trades(runs) if runs is not None else ()) + (
        desk_round_trips(desk) if desk is not None else ()
    )
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
            "one cycle; round_trips are positions a session or the venue actually closed, and "
            "there may be none; hindsight, when present, is each gate's refusals replayed on the "
            "candles that followed them, beside the decisions it admitted"
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
        **({"hindsight": seen} if desk is not None and (seen := hindsight_of(desk)) else {}),
    }
