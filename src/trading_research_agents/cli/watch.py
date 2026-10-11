"""What a desk is doing, read from the files it writes, by something that can only read them.

The console runs commands. This does not: it opens a project's run directory, reads the records a
desk has already written, and redraws them. It holds no key, builds no client, asks no model and
takes no lock. Nothing it does can reach a cycle, a venue or a decision, which is the whole point -
a desk on a server should be watchable from another terminal without anybody wondering whether
watching it changed it.

Opening it, closing it, and opening it again does nothing at all to the desk. The worst it can do to
a running cycle is read a file halfway through being written, and a half-written record is skipped
rather than shown wrong.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCREENS = "screens"
AUDITS = "audits"
SUMMARY = "cycle.json"


def _read(path: Path) -> dict[str, Any] | None:
    """One record, or nothing when it is missing, unreadable or still being written."""
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return row if isinstance(row, dict) else None


@dataclass(frozen=True)
class Decision:
    """One cycle as a watcher needs it: when, what, and how it ended."""

    at: str
    instrument: str
    setup: str = ""
    target: str = ""
    conviction: str = ""
    risk_bps: str = ""
    net_bps: str = ""
    outcome: str = ""
    detail: str = ""

    def record(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "instrument": self.instrument,
            "setup": self.setup,
            "target": self.target,
            "conviction": self.conviction,
            "risk_bps": self.risk_bps,
            "net_bps": self.net_bps,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def decision_of(row: dict[str, Any]) -> Decision:
    """A cycle summary as one line. Nothing is computed here that the record does not carry."""
    analyst = row.get("analyst") or {}
    targets = row.get("targets") or {}
    intent = row.get("intent")
    placed = row.get("placed") or {}
    risk = row.get("risk") or {}
    stake = row.get("conviction") or {}
    score = (stake.get("score") or {}).get("score")

    if row.get("held"):
        outcome, detail = "held", _first((row.get("trigger") or {}).get("reasons"))
    elif placed.get("sent"):
        outcome = "placed"
        detail = f"{placed.get('side')} {placed.get('size')} on {placed.get('venue')}"
        filled = placed.get("filled")
        if placed.get("cancelled"):
            detail += ", cancelled unfilled"
        elif placed.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            detail += f", {placed['status']} by the venue, {filled or 0} filled"
        elif filled not in (None, "", "0", "0.0"):
            detail += f", filled {filled}"
        elif placed.get("cancel_refused"):
            detail += ", state unknown; check the venue"
    elif placed.get("refused") or placed.get("error"):
        outcome, detail = "not placed", str(placed.get("refused") or placed.get("error"))
    elif intent is not None:
        outcome, detail = "intent", f"{intent.get('target')} {intent.get('size')}"
    elif row.get("stopped_by"):
        outcome, detail = "stopped", str(row["stopped_by"])
    else:
        outcome, detail = "refused", _first(row.get("refusals"))
    return Decision(
        at=str(row.get("cycle_id", "")),
        instrument=str(row.get("instrument", "")),
        setup=str(analyst.get("setup") or ""),
        target=str(analyst.get("target") or ""),
        conviction="" if score is None else str(score),
        risk_bps=str(risk.get("risk_bps") or ""),
        net_bps=str(targets.get("net_bps") or "")[:6],
        outcome=outcome,
        detail=detail,
    )


def _first(rows: Any) -> str:
    return str(rows[0]) if isinstance(rows, list) and rows else ""


@dataclass
class Desk:
    """Everything a watcher shows, gathered from one project's run directory."""

    root: Path
    at: str = ""
    decisions: list[Decision] = field(default_factory=list)
    screen: dict[str, Any] = field(default_factory=dict)
    book: list[dict[str, Any]] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)
    spend: dict[str, Any] = field(default_factory=dict)
    audits: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "at": self.at,
            "decisions": [d.record() for d in self.decisions],
            "screen": self.screen,
            "book": self.book,
            "alarms": self.alarms,
            "spend": self.spend,
            "audits": self.audits,
        }


def look(root: Path, *, limit: int = 12, now: datetime | None = None) -> Desk:
    """Read what the desk has written. Nothing here opens a socket or takes a lock."""
    moment = now or datetime.now(UTC)
    desk = Desk(root=root, at=moment.isoformat(timespec="seconds"))
    # `root` is the cycle directory itself - what `tra run --out` names - and the screens and the
    # audits are its siblings, which is how every command already writes them.
    found = sorted(root.glob(f"*/{SUMMARY}"), reverse=True)[:limit] if root.is_dir() else []
    rows = [row for row in (_read(path) for path in found) if row is not None]
    desk.decisions = [decision_of(row) for row in rows]

    for row in rows:
        for line in row.get("refusals") or []:
            text = str(line)
            if text.startswith(("data ", "venue:")) or "ProviderError" in text:
                desk.alarms.append(f"{row.get('cycle_id', '')}: {text}")
        if row.get("stopped_by"):
            desk.alarms.append(f"{row.get('cycle_id', '')}: {row['stopped_by']}")
    desk.alarms = desk.alarms[:6]

    newest = next(iter(rows), {})
    held = (newest.get("desk") or {}).get("held") or []
    desk.book = [row for row in held if isinstance(row, dict)]
    desk.screen = _latest_screen(root)
    desk.spend = _spend(root, rows)
    desk.audits = _audits(root)
    return desk


def _latest_screen(root: Path) -> dict[str, Any]:
    where = root.parent / SCREENS
    if not where.is_dir():
        return {}
    for path in sorted(where.glob("*.json"), reverse=True):
        row = _read(path)
        if row is not None:
            return row
    return {}


def _spend(root: Path, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """What the models have cost, from the cycles themselves rather than from a running total."""
    tokens = sum(int(row.get("tokens") or 0) for row in rows)
    declared = sum((Decimal(str(row.get("cost_declared") or "0")) for row in rows), Decimal(0))
    return {
        "cycles_read": len(rows),
        "tokens_in_view": tokens,
        "cost_in_view": str(declared),
        "usage": str(root / "usage.jsonl"),
    }


def _audits(root: Path) -> list[str]:
    where = root.parent / AUDITS
    if not where.is_dir():
        return []
    return [p.name for p in sorted(where.iterdir(), reverse=True) if p.is_dir()][:3]


def short_time(at: str) -> str:
    """A cycle id's moment as a person reads it: 10-04 15:30:17, the date kept because a desk's
    view easily spans more than one day."""
    text = str(at or "")
    if len(text) < 19:
        return text
    return f"{text[5:10]} {text[11:19].replace('-', ':')}"
