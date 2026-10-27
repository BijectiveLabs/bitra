"""What one pass through the cycle carries from one step to the next.

Each step reads what the steps before it settled and adds its own. A step that stops the cycle
has already written it down, and says so by returning False; the next step never runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.execution.stops import Placement
from trading_research_agents.store.cycles import CycleDir
from trading_research_agents.store.snapshot import Snapshot


@dataclass
class Pass:
    cfg: CycleConfig
    where: CycleDir
    result: CycleResult
    snapshot: Snapshot
    cycle: str
    # A spot pair or a prediction outcome: no funding, no open interest, nothing to borrow.
    no_contract: bool
    # An equity underlying traded only while its exchange is open.
    equity_hours: bool
    spent_before: Decimal
    calls_before: int
    base: Decimal
    equity: Decimal | None = None
    mid_now: Decimal | None = None
    spread_now: Decimal | None = None
    technical: Any = None
    asked: dict[str, Any] = field(default_factory=dict)
    # The flow analyst's brief, and every member's raw answer by task name.
    flow_brief: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    # The side each member read, for the intent; empty when the analyst decides alone.
    team_reads: dict[str, str] = field(default_factory=dict)
    reading_briefs: dict[str, dict[str, Any]] = field(default_factory=dict)
    analyst: Any = None
    costs: Any = None
    exits: dict[str, Any] = field(default_factory=dict)
    placement: Placement | None = None
    edge: Decimal = Decimal(0)
    edge_source: str = ""
    size: Decimal = Decimal(0)
    allowed_bps: Decimal | None = None
    capped: Any = None
    verdict: Any = None
    # The intent as the gates drafted it, then as the risk gate approved it.
    intent: dict[str, Any] = field(default_factory=dict)


Step = Callable[[Pass], bool]


def through(p: Pass, steps: tuple[Step, ...]) -> bool:
    """Every step in order, until one stops the cycle."""
    return all(step(p) for step in steps)


__all__ = ["Pass", "Step", "through"]
