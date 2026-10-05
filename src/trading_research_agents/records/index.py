"""One line per finished cycle, per day, so the desk reads its recent past without walking it all.

The trigger, the targets, the desk and the open interest change only ever need the last few cycles
of an instrument, or today's intents. Reading them from the index keeps that cost flat however long
a desk has run; the cycle directories stay the full record behind each line.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from trading_research_agents.records.files import append_line, read_lines, sha256_of

INDEX_DIR = "index"
SUMMARY = "cycle.json"
SNAPSHOT = "snapshot.json"


def index_row(
    summary: Mapping[str, Any],
    *,
    open_interest: Mapping[str, Any] | None,
    summary_sha256: str | None,
) -> dict[str, Any]:
    """What a later cycle needs to know about this one, and the hash of its full summary."""
    return {
        "cycle_id": summary.get("cycle_id"),
        "instrument": summary.get("instrument"),
        "seen": summary.get("seen"),
        "held": bool(summary.get("held")),
        "intent": summary.get("intent"),
        "placed": summary.get("placed"),
        "open_interest": dict(open_interest) if open_interest else None,
        "summary_sha256": summary_sha256,
    }


def append_index(root: Path, row: Mapping[str, Any]) -> None:
    cycle = str(row.get("cycle_id") or "")
    if len(cycle) < 10:
        raise ValueError("an index row names its cycle")
    append_line(root / INDEX_DIR / f"{cycle[:10]}.jsonl", row, durable=True)


def has_index(root: Path) -> bool:
    return (root / INDEX_DIR).is_dir()


def recent(
    root: Path, *, instrument: str | None = None, day: str | None = None
) -> Iterator[dict[str, Any]]:
    """Index rows newest first, one per cycle (the last line written for it wins).

    `day` (YYYY-MM-DD, UTC) reads that one day only.
    """
    folder = root / INDEX_DIR
    if not folder.is_dir():
        return
    days = [folder / f"{day}.jsonl"] if day else sorted(folder.glob("*.jsonl"), reverse=True)
    for path in days:
        seen: set[str] = set()
        rows = read_lines(path)
        for row in sorted(reversed(rows), key=lambda r: str(r.get("cycle_id")), reverse=True):
            cycle = str(row.get("cycle_id"))
            if cycle in seen:
                continue
            seen.add(cycle)
            if instrument is not None and str(row.get("instrument", "")) != instrument:
                continue
            yield row


def build_index(root: Path) -> int:
    """The index for a desk that ran before it existed, from the cycle directories already there.

    Built aside and moved into place in one rename, so a desk that stops halfway leaves no index
    rather than half of one.
    """
    if has_index(root) or not root.is_dir():
        return 0
    aside = root / f".{INDEX_DIR}.part"
    shutil.rmtree(aside, ignore_errors=True)
    count = 0
    for cycle in sorted(p for p in root.iterdir() if p.is_dir() and (p / SUMMARY).is_file()):
        try:
            summary = json.loads((cycle / SUMMARY).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(summary, dict) or not summary.get("cycle_id"):
            continue
        row = index_row(
            summary,
            open_interest=_open_interest(cycle / SNAPSHOT),
            summary_sha256=sha256_of(cycle / SUMMARY),
        )
        append_line(aside / f"{str(row['cycle_id'])[:10]}.jsonl", row)
        count += 1
    aside.mkdir(parents=True, exist_ok=True)
    os.replace(aside, root / INDEX_DIR)
    return count


def _open_interest(snapshot: Path) -> dict[str, Any] | None:
    try:
        values = json.loads(snapshot.read_text(encoding="utf-8")).get("values", {})
    except (OSError, ValueError, AttributeError):
        return None
    found = values.get("open_interest") if isinstance(values, dict) else None
    return found if isinstance(found, dict) else None
