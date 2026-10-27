"""Orders whose create call ended without an answer: on the book or not, nobody knows yet.

A create that timed out may have reached the venue. The order hand looks for it by its client id
before it concludes anything, and an order it cannot find is written here as UNKNOWN. It stays
unresolved until the venue lists it by that id, or until the time a venue takes to list an order
it accepted has passed with every list readable. While one is unresolved on an instrument, the
risk gate admits no new entry there. Every line is appended, so the record of what was not known,
and when it became known, survives.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_research_agents.records import append_line, read_lines

UNKNOWN_ORDERS = "unknown_orders.jsonl"
# A venue lists an accepted order within seconds; ten minutes without it, every list readable,
# means the request never reached the book.
SETTLE_S = 600
OUTCOMES = ("found", "not_found")


def record_unknown(
    root: Path, *, cycle: str, instrument: str, client_order_id: str, said: str, at: datetime
) -> None:
    append_line(
        Path(root) / UNKNOWN_ORDERS,
        {
            "event": "unknown",
            "at": at.isoformat(),
            "cycle": cycle,
            "instrument": instrument,
            "client_order_id": client_order_id,
            "said": said,
        },
        durable=True,
    )


def resolve(
    root: Path,
    *,
    client_order_id: str,
    outcome: str,
    order: Mapping[str, Any] | None,
    at: datetime,
) -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome {outcome!r} is not one of {OUTCOMES}")
    append_line(
        Path(root) / UNKNOWN_ORDERS,
        {
            "event": "resolved",
            "at": at.isoformat(),
            "client_order_id": client_order_id,
            "outcome": outcome,
            "order": None if order is None else dict(order),
        },
        durable=True,
    )


def _rows(root: Path) -> list[dict[str, Any]]:
    path = Path(root) / UNKNOWN_ORDERS
    return read_lines(path) if path.is_file() else []


def resolutions(root: Path) -> dict[str, dict[str, Any]]:
    """Per client id, how its UNKNOWN ended."""
    return {
        str(row.get("client_order_id")): row
        for row in _rows(root)
        if row.get("event") == "resolved"
    }


def unresolved(root: Path) -> dict[str, dict[str, Any]]:
    """Per client id, the orders still UNKNOWN."""
    ended = resolutions(root)
    return {
        str(row.get("client_order_id")): row
        for row in _rows(root)
        if row.get("event") == "unknown" and str(row.get("client_order_id")) not in ended
    }


def blocking(root: Path, instrument: str) -> str:
    """Why no new entry may go out on this instrument, or empty."""
    open_ids = [cid for cid, row in unresolved(root).items() if row.get("instrument") == instrument]
    if not open_ids:
        return ""
    return (
        f"order {', '.join(open_ids)} on {instrument} is UNKNOWN: its send was not answered and "
        "the venue has not listed it yet; it is looked for on every pass"
    )
