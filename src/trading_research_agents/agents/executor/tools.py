"""What the executor works with in the sandbox, and what it hands to the operator's own execution.

The runtime already writes every admitted intent. `export_signals` turns a run's admitted
intents into a file with a fixed, documented shape (`signals.jsonl` or CSV) so that whoever
runs execution elsewhere, a broker bridge, a spreadsheet, a different venue adapter, gets the
decisions without the package sending anything itself. `size_for` and `rate_budget` are the
arithmetic of an order that never exceeds a limit.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

SIGNAL_FIELDS = ("ts_ns", "instrument", "target", "size", "reason", "risk_bps", "run")


def export_signals(run_dir: Path, *, instrument: str, out: Path, fmt: str = "jsonl") -> int:
    """Admitted intents as signals. Returns how many were written."""
    intents = run_dir / "intents.jsonl"
    if not intents.is_file():
        raise FileNotFoundError(f"{intents} does not exist")
    rows = []
    for line in intents.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        i = json.loads(line)
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


@dataclass(frozen=True)
class Sizing:
    size: Decimal
    capped_by: str


def size_for(
    *,
    wanted: Decimal,
    price: Decimal,
    max_position: Decimal,
    max_notional: Decimal | None,
    step: Decimal,
) -> Sizing:
    """The largest size at or below every cap, rounded down to the venue's step. Never up."""
    if wanted <= 0 or price <= 0 or step <= 0:
        raise ValueError("wanted, price and step are positive")
    size, why = wanted, "declared size"
    if size > max_position:
        size, why = max_position, "max_position"
    if max_notional is not None and size * price > max_notional:
        size, why = max_notional / price, "max_notional"
    size = (size / step).to_integral_value(rounding=ROUND_DOWN) * step
    if size <= 0:
        return Sizing(Decimal(0), f"{why}: below one step of {step}")
    return Sizing(size, why)


def rate_budget(recent_order_ts_ns: list[int], *, now_ns: int, per_minute: int) -> dict[str, Any]:
    """How many orders the minute still allows, and when the oldest one falls out of it."""
    window = 60 * 1_000_000_000
    inside = sorted(t for t in recent_order_ts_ns if now_ns - t < window)
    left = max(per_minute - len(inside), 0)
    frees_in_s = None if not inside or left > 0 else (inside[0] + window - now_ns) / 1e9
    return {"used": len(inside), "left": left, "per_minute": per_minute, "frees_in_s": frees_in_s}
