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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_research_agents.records.files import append_line, read_lines, sha256_of

INDEX_DIR = "index"
SUMMARY = "cycle.json"
SNAPSHOT = "snapshot.json"
INTENTS = "intents.jsonl"
PLACED = "placed.json"


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
    root: Path, *, instrument: str | None = None, day: str | None = None, sent: bool = False
) -> Iterator[dict[str, Any]]:
    """Index rows newest first, one per cycle (the last line written for it wins).

    A cycle that stopped before its index line was written (a crash, an interrupt at the venue)
    still has its directory; the two newest days are checked for those, so an order such a cycle
    sent is never missing from what the desk reads. `day` (YYYY-MM-DD, UTC) reads one day only;
    `sent` reads only cycles that reached the venue.
    """
    folder = root / INDEX_DIR
    if not folder.is_dir():
        return
    if day:
        days = [day]
    else:
        now = datetime.now(UTC)
        present = {p.stem for p in folder.glob("*.jsonl")}
        present |= {f"{now:%Y-%m-%d}", f"{now - timedelta(days=1):%Y-%m-%d}"}
        days = sorted(present, reverse=True)
    tail = set(days[:2])
    having = b'"placed": {' if sent else b""
    known = {d: read_lines(folder / f"{d}.jsonl", having=having) for d in tail}
    named = {str(r.get("cycle_id")) for rows in known.values() for r in rows}
    late: dict[str, list[dict[str, Any]]] = {d: [] for d in tail}
    for cycle in _unindexed(root, tail, named):
        row = row_from_dir(cycle)
        if row is not None:
            late[cycle.name[:10]].append(row)
    for stamp in days:
        rows = (
            known[stamp] + late[stamp]
            if stamp in tail
            else read_lines(folder / f"{stamp}.jsonl", having=having)
        )
        seen: set[str] = set()
        for row in sorted(reversed(rows), key=lambda r: str(r.get("cycle_id")), reverse=True):
            name = str(row.get("cycle_id"))
            if name in seen:
                continue
            seen.add(name)
            if instrument is not None and str(row.get("instrument", "")) != instrument:
                continue
            yield row


def row_from_dir(cycle: Path) -> dict[str, Any] | None:
    """An index row read back from a cycle's own directory, finished or not."""
    try:
        summary = json.loads((cycle / SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict) and summary.get("cycle_id"):
        return index_row(
            summary,
            open_interest=_open_interest(cycle / SNAPSHOT),
            summary_sha256=sha256_of(cycle / SUMMARY),
        )
    intents = read_lines(cycle / INTENTS)
    try:
        placed = json.loads((cycle / PLACED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        placed = None
    if not intents and placed is None:
        return None
    intent = intents[-1] if intents else None
    return {
        "cycle_id": cycle.name,
        "instrument": (intent or {}).get("instrument") or cycle.name[21:] or None,
        "seen": None,
        "held": False,
        "intent": intent,
        "placed": placed if isinstance(placed, dict) else None,
        "open_interest": _open_interest(cycle / SNAPSHOT),
        "summary_sha256": None,
        "unfinished": True,
    }


def _unindexed(root: Path, days: set[str], known: set[str]) -> list[Path]:
    # Names first: a stat per directory is what makes a long history slow to walk.
    with os.scandir(root) as entries:
        return [
            Path(entry.path)
            for entry in entries
            if entry.name[:10] in days and entry.name not in known and entry.is_dir()
        ]


def build_index(root: Path) -> int:
    """The index for a desk that ran before it existed, from the cycle directories already there.

    Built aside and moved into place in one rename, so a desk that stops halfway leaves no index
    rather than half of one.
    """
    if has_index(root) or not root.is_dir():
        return 0
    aside = root / f".{INDEX_DIR}.part"
    shutil.rmtree(aside, ignore_errors=True)
    by_day: dict[str, list[bytes]] = {}
    count = 0
    for cycle in sorted(p for p in root.iterdir() if p.is_dir() and (p / SUMMARY).is_file()):
        row = row_from_dir(cycle)
        if row is None or not row.get("cycle_id"):
            continue
        line = json.dumps(row, sort_keys=True, default=str).encode("utf-8") + b"\n"
        by_day.setdefault(str(row["cycle_id"])[:10], []).append(line)
        count += 1
    aside.mkdir(parents=True, exist_ok=True)
    for day, lines in by_day.items():
        (aside / f"{day}.jsonl").write_bytes(b"".join(lines))
    os.replace(aside, root / INDEX_DIR)
    return count


def _open_interest(snapshot: Path) -> dict[str, Any] | None:
    try:
        values = json.loads(snapshot.read_text(encoding="utf-8")).get("values", {})
    except (OSError, ValueError, AttributeError):
        return None
    found = values.get("open_interest") if isinstance(values, dict) else None
    return found if isinstance(found, dict) else None
