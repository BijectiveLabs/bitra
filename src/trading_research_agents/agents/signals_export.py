"""What a run hands to the operator's own execution: the admitted intents, in a fixed shape.

The runtime already writes every admitted intent. `export_signals` turns a run's admitted
intents into a file with a fixed, documented shape (`signals.jsonl` or CSV) so that whoever
runs execution elsewhere, a broker bridge, a spreadsheet, a different venue adapter, gets the
decisions without the package sending anything itself. `size_for` comes from the execution gates,
and `rate_budget` is what the minute still allows.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

# The arithmetic of how large an order may be belongs with the other capital gates, and this role
# calls it rather than keeping a second copy of it.
from trading_research_agents.execution.sizing import Sizing, size_for
from trading_research_agents.records import read_lines

__all__ = ["Sizing", "export_signals", "rate_budget", "size_for"]

SIGNAL_FIELDS = ("ts_ns", "instrument", "target", "size", "reason", "risk_bps", "run")


def export_signals(run_dir: Path, *, instrument: str, out: Path, fmt: str = "jsonl") -> int:
    """Admitted intents as signals. Returns how many were written."""
    intents = run_dir / "intents.jsonl"
    if not intents.is_file():
        raise FileNotFoundError(f"{intents} does not exist")
    rows = []
    for i in read_lines(intents):
        if not i.get("admitted"):
            continue
        rows.append(
            {
                "ts_ns": int(i["ts_ns"]),
                "instrument": instrument,
                "target": str(i["target"]),
                "size": str(i["size"]),
                "reason": str(i.get("reason", "")),
                "risk_bps": i.get("risk_bps"),
                "run": run_dir.name,
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        out.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
        )
    elif fmt == "csv":
        with out.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SIGNAL_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise ValueError("fmt is jsonl or csv")
    return len(rows)


def rate_budget(recent_order_ts_ns: list[int], *, now_ns: int, per_minute: int) -> dict[str, Any]:
    """How many orders the minute still allows, and when the oldest one falls out of it."""
    window = 60 * 1_000_000_000
    inside = sorted(t for t in recent_order_ts_ns if now_ns - t < window)
    left = max(per_minute - len(inside), 0)
    frees_in_s = None if not inside or left > 0 else (inside[0] + window - now_ns) / 1e9
    return {"used": len(inside), "left": left, "per_minute": per_minute, "frees_in_s": frees_in_s}
