"""What finished trades cost against what they were priced at.

The cost gate prices a trade before it exists. The venue's fills say what it actually cost. Kept
side by side over the desk's round trips, the two show whether the gate's prices hold: an entry
that keeps filling worse than the touch it was priced at, or fees above the schedule the gate
counted, mean the gate is admitting trades on a cost that is not the real one.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

QUANTUM = Decimal("0.01")


def _figures(rows: Sequence[Mapping[str, Any]], key: str) -> list[Decimal]:
    found: list[Decimal] = []
    for row in rows:
        if "net" not in row or row.get(key) in (None, ""):
            continue
        try:
            value = Decimal(str(row[key]))
        except InvalidOperation:
            continue
        if value.is_finite():
            found.append(value)
    return found


def _median(values: Sequence[Decimal]) -> str | None:
    return None if not values else str(Decimal(statistics.median(values)).quantize(QUANTUM))


def reconcile(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Medians of the realised entry slippage and fees, beside the fees the gate priced."""
    slippage = _figures(rows, "entry_slippage_bps")
    fees = _figures(rows, "fees_bps")
    priced = _figures(rows, "priced_fee_bps")
    return {
        "round_trips": sum(1 for row in rows if "net" in row),
        "entry_slippage": {
            "measured": len(slippage),
            "median_bps": _median(slippage),
            "worse_than_priced": sum(1 for x in slippage if x > 0),
        },
        "fees": {
            "measured": len(fees),
            "median_bps": _median(fees),
            "priced_median_bps": _median(priced),
        },
    }
