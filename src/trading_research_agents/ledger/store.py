"""Append-only JSON Lines store for ledger entries.

Nothing in the file is ever rewritten. A retraction is a new WITHDRAWN row that names the row
it retracts. A line that cannot be read stops the reader with its line number rather than being
skipped, because a ledger with a silently missing row is worse than one that refuses to load.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agent.ledger.entry import LedgerEntry, LedgerError, ReversalPoint
from trading_research_agent.ledger.vocabulary import Axis, Transferability, Verdict

SCHEMA = 1


def to_record(entry: LedgerEntry) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "entry_id": entry.entry_id,
        "subject": entry.subject,
        "verdict": entry.verdict.value,
        "recorded_at": entry.recorded_at.isoformat(),
        "layer": entry.layer,
        "axis_of_death": [a.value for a in entry.axis_of_death],
        "blocking_axis": entry.blocking_axis.value if entry.blocking_axis else None,
        "transferability": entry.transferability.value if entry.transferability else None,
        "reversal_point": None,
        "evidence": list(entry.evidence),
        "envelope": dict(entry.envelope),
        "retracts": entry.retracts,
        "note": entry.note,
    }
    if entry.reversal_point is not None:
        rp = entry.reversal_point
        record["reversal_point"] = {
            "parameter": rp.parameter,
            "value": str(rp.value),
            "unit": rp.unit,
            "comparison": rp.comparison,
        }
    return record


def from_record(record: dict[str, Any]) -> LedgerEntry:
    if record.get("schema") != SCHEMA:
        raise LedgerError(f"unsupported ledger schema {record.get('schema')!r}")
    reversal = None
    if record.get("reversal_point") is not None:
        rp = record["reversal_point"]
        reversal = ReversalPoint(
            parameter=rp["parameter"],
            value=Decimal(rp["value"]),
            unit=rp["unit"],
            comparison=rp.get("comparison", "<"),
        )
    return LedgerEntry(
        entry_id=record["entry_id"],
        subject=record["subject"],
        verdict=Verdict(record["verdict"]),
        recorded_at=datetime.fromisoformat(record["recorded_at"]),
        layer=record.get("layer"),
        axis_of_death=tuple(Axis(a) for a in record.get("axis_of_death", [])),
        blocking_axis=Axis(record["blocking_axis"]) if record.get("blocking_axis") else None,
        transferability=(
            Transferability(record["transferability"]) if record.get("transferability") else None
        ),
        reversal_point=reversal,
        evidence=tuple(record.get("evidence", [])),
        envelope=tuple((str(k), str(v)) for k, v in record.get("envelope", {}).items()),
        retracts=record.get("retracts"),
        note=record.get("note", ""),
    )


class Ledger:
    """A ledger file on disk. Open it, append to it, read it back. Nothing else."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def entries(self) -> Iterator[LedgerEntry]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                if not line.strip():
                    # A buffered writer ends with a newline; that is not a corrupt row.
                    continue
                try:
                    yield from_record(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    raise LedgerError(
                        f"{self.path}:{number}: unreadable ledger row: {exc}"
                    ) from exc

    def ids(self) -> set[str]:
        return {e.entry_id for e in self.entries()}

    def append(self, entry: LedgerEntry) -> None:
        existing = list(self.entries())
        known = {e.entry_id for e in existing}
        if entry.entry_id in known:
            raise LedgerError(f"entry_id {entry.entry_id!r} already exists; ids are never reused")
        if entry.verdict is Verdict.WITHDRAWN:
            self._check_retraction(entry, existing)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_record(entry), ensure_ascii=True, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def withdraw(
        self, entry_id: str, *, new_entry_id: str, reason: str, recorded_at: datetime
    ) -> LedgerEntry:
        target = next((e for e in self.entries() if e.entry_id == entry_id), None)
        if target is None:
            raise LedgerError(f"cannot withdraw {entry_id!r}: no such entry")
        retraction = LedgerEntry(
            entry_id=new_entry_id,
            subject=target.subject,
            verdict=Verdict.WITHDRAWN,
            recorded_at=recorded_at,
            layer=target.layer,
            retracts=entry_id,
            note=reason,
        )
        self.append(retraction)
        return retraction

    def effective(self) -> list[LedgerEntry]:
        """Rows that still stand: everything except retractions and what they retracted."""
        rows = list(self.entries())
        retracted = {e.retracts for e in rows if e.verdict is Verdict.WITHDRAWN}
        return [
            e for e in rows if e.verdict is not Verdict.WITHDRAWN and e.entry_id not in retracted
        ]

    def counts(self) -> dict[Verdict, int]:
        """The kill count, by verdict, over rows that still stand."""
        tally: dict[Verdict, int] = {v: 0 for v in Verdict if v is not Verdict.WITHDRAWN}
        for e in self.effective():
            tally[e.verdict] += 1
        return tally

    @staticmethod
    def _check_retraction(entry: LedgerEntry, existing: list[LedgerEntry]) -> None:
        by_id = {e.entry_id: e for e in existing}
        target = by_id.get(entry.retracts or "")
        if target is None:
            raise LedgerError(f"cannot retract {entry.retracts!r}: no such entry")
        if target.verdict is Verdict.WITHDRAWN:
            raise LedgerError("a retraction cannot itself be retracted")
        already = [e for e in existing if e.retracts == entry.retracts]
        if already:
            raise LedgerError(f"{entry.retracts!r} is already retracted by {already[0].entry_id!r}")
