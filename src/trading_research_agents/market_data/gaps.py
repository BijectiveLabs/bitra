"""Time a stream or an archive should have covered and did not, with the reason.

A gap is written when it is found, before anything else is done about it, so the record says
what was not seen even if the backfill that follows also fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.records import append_line, read_lines

GAPS = "gaps.jsonl"


@dataclass(frozen=True, slots=True)
class GapMark:
    venue: str
    instrument: str
    stream: str
    start_ns: int
    end_ns: int
    reason: str

    def __post_init__(self) -> None:
        if self.end_ns <= self.start_ns:
            raise MarketDataError("a gap ends after it starts")
        if not self.reason.strip():
            raise MarketDataError("a gap states its reason")

    def record(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "instrument": self.instrument,
            "stream": self.stream,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "reason": self.reason,
        }


def gaps_file(root: Path, venue: str, instrument: str) -> Path:
    return root / "market" / venue / instrument / GAPS


def record_gap(root: Path, mark: GapMark) -> None:
    append_line(gaps_file(root, mark.venue, mark.instrument), mark.record(), durable=True)


def read_gaps(root: Path, venue: str, instrument: str) -> list[GapMark]:
    out = []
    for row in read_lines(gaps_file(root, venue, instrument)):
        try:
            out.append(
                GapMark(
                    str(row["venue"]),
                    str(row["instrument"]),
                    str(row["stream"]),
                    int(row["start_ns"]),
                    int(row["end_ns"]),
                    str(row["reason"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out
