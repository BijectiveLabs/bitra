"""What one cycle did, in the order a reader wants it, and its record."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trading_research_agents.agents.parallel import FanOut
from trading_research_agents.data.sources.base import Fetched
from trading_research_agents.execution.confluence import Confluence
from trading_research_agents.execution.conviction import (
    Stake,
)
from trading_research_agents.execution.desk import DeskVerdict
from trading_research_agents.execution.pretrade import PreTradeVerdict
from trading_research_agents.execution.risk import RiskVerdict
from trading_research_agents.execution.targets import (
    TargetVerdict,
)
from trading_research_agents.execution.trigger import (
    Wake,
)
from trading_research_agents.store.snapshot import (
    Snapshot,
)


@dataclass
class CycleResult:
    """What the cycle did, in the order a reader wants it."""

    cycle: str
    instrument: str
    snapshot: Snapshot
    briefs: dict[str, Any] = field(default_factory=dict)
    fan: FanOut = field(default_factory=FanOut)
    # The reviewers are a second round: they argue about a proposal that has to exist first. Kept
    # apart from the first round so each round's wall clock still means what it says.
    debate: FanOut | None = None
    context: dict[str, Any] = field(default_factory=dict)
    analyst: Any = None
    cited: list[dict[str, str]] = field(default_factory=list)
    confluence: Confluence | None = None
    stake: Stake | None = None
    risk: RiskVerdict | None = None
    book: DeskVerdict | None = None
    verdict: PreTradeVerdict | None = None
    targets: TargetVerdict | None = None
    intent: dict[str, Any] | None = None
    placed: dict[str, Any] | None = None
    refusals: list[str] = field(default_factory=list)
    stopped_by: str | None = None
    path: Path | None = None
    # What this cycle saw for free, and what the trigger made of it. The mid is written down even on
    # a cycle that asked nobody, because the next cycle measures its move against it.
    seen: dict[str, Any] = field(default_factory=dict)
    # Where the entry, the stop and the take profit sit, as prices and as distances from the entry.
    exits: dict[str, Any] = field(default_factory=dict)
    # The higher timeframes' candles, kept with the snapshot as this cycle's raw inputs.
    frames: dict[int, Fetched] = field(default_factory=dict)
    wake: Wake | None = None
    held: bool = False

    @property
    def acted(self) -> bool:
        return self.intent is not None

    def record(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle,
            "instrument": self.instrument,
            "data": self.briefs.get("data"),
            # Present only when the size was declared in quote currency: what it was converted at.
            "size": self.briefs.get("size"),
            # Venue mode only: the account as read before the cycle, and the equity sizing used.
            "equity": self.briefs.get("equity"),
            "agents": self.fan.record(),
            "reviewers": None if self.debate is None else self.debate.record(),
            "analyst": None if self.analyst is None else _said(self.analyst),
            "cited": self.cited,
            "context": self.context,
            "confluence": None if self.confluence is None else self.confluence.record(),
            "conviction": None if self.stake is None else self.stake.record(),
            # [sizing] only: what the desk's own round trips allowed, and the arithmetic behind it.
            "sizing": self.briefs.get("sizing"),
            "risk": None if self.risk is None else self.risk.record(),
            "desk": None if self.book is None else self.book.record(),
            "cost": None if self.verdict is None else self.verdict.record(),
            "targets": None if self.targets is None else self.targets.record(),
            "intent": self.intent,
            "placed": self.placed,
            "refusals": self.refusals,
            "stopped_by": self.stopped_by,
            "seen": self.seen,
            # Every series a reading read: where from, the value, and how old.
            "series": self.briefs.get("series"),
            # Readings this cycle did not read, and why.
            "not_read": self.briefs.get("not_read"),
            "exits": self.exits or None,
            # What the models cost this cycle, and which calls had no declared price.
            "spend": self.briefs.get("spend"),
            "targets_rule": self.briefs.get("targets_rule"),
            # Every check the risk gate ran on the intent, and why it refused if it did.
            "risk_gate": self.briefs.get("risk_gate"),
            # How often the instrument's own paths reach the take before the stop at these levels.
            "paths": self.briefs.get("paths"),
            "trigger": None if self.wake is None else self.wake.record(),
            "held": self.held,
            "mode": self.briefs.get("mode"),
        }


def _said(analyst: Any) -> dict[str, Any]:
    """The deciding analyst's answer as JSON, with its citations flattened for the record."""
    out = dict(vars(analyst))
    cited = out.get("figures_used")
    if isinstance(cited, tuple):
        out["figures_used"] = [figure.record() for figure in cited]
    return out
