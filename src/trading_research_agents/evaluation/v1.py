"""The desk's own v1 record as a row: each decision it sent, settled by the same engine.

These are live decisions, not a replay, and the row is labelled so. It is the only row whose times
are not the arena's clock: each enters at the first minute boundary after its recorded decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.policy import TradeIdea
from trading_research_agents.execution.hindsight import ADMITTED, read_decisions
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class RecordedIdea:
    instrument: str
    decided_ns: int
    idea: TradeIdea


def _ns(at: datetime) -> int:
    delta = at - EPOCH
    return (delta.days * 86_400 + delta.seconds) * NS_PER_S + delta.microseconds * 1_000


def recorded_ideas(
    cycles: Path, instruments: Sequence[str], start_ns: int, end_ns: int, hold_ns: int
) -> list[RecordedIdea]:
    """Every decision the desk sent on `instruments` inside the window, held for `hold_ns`."""
    out = []
    for decision in read_decisions(cycles):
        if decision.gate != ADMITTED or decision.instrument not in instruments:
            continue
        decided = -(-_ns(decision.at) // NS_PER_MINUTE) * NS_PER_MINUTE
        if not start_ns <= decided < end_ns:
            continue
        idea = TradeIdea(
            side=Direction.LONG if decision.side > 0 else Direction.SHORT,
            playbook="v1",
            stop_bps=decision.stop_bps,
            take_bps=decision.take_bps,
            max_hold_bars=hold_ns // NS_PER_MINUTE,
            reason=f"cycle {decision.cycle}",
        )
        out.append(RecordedIdea(decision.instrument, decided, idea))
    return out
