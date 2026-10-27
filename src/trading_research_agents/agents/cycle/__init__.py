"""One cycle: what the engine saw, what the agents said, what the cost gate decided.

The order is fixed and short. Code takes a snapshot, which costs nothing, and asks the trigger
whether anything has happened worth paying a model to look at. Code builds the briefs. Agents read
the briefs, in parallel, and answer inside a schema. Code prices the round trip on the book it is
holding, sizes the position from the agreement and then from what the account can afford to lose
at the stop, and compares what is left with the edge the analyst claimed. Only then is there an
intent, and in the default mode that intent goes to a file rather than to a venue.

Two steps here belong to a risk manager and are deliberately not a model's to make. How much goes
behind an idea comes from the conviction ladder; how much the account may lose if the stop is hit
comes from `[controls].max_risk_per_trade_bps`. Both are arithmetic over the operator's own
declarations, so neither can be talked out of.

Everything the cycle touched stays in the cycle's directory, so an auditor can replay the
reasoning without asking anyone to remember it.
"""

from trading_research_agents.agents.cycle.config import (
    CONTRACT_KINDS,
    MODES,
    TARGETS,
    CycleConfig,
    CycleError,
)
from trading_research_agents.agents.cycle.record import budget_spent, manifest
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.cycle.run import run_cycle
from trading_research_agents.agents.cycle.validators import judge_mode

__all__ = [
    "CONTRACT_KINDS",
    "MODES",
    "TARGETS",
    "CycleConfig",
    "CycleError",
    "CycleResult",
    "budget_spent",
    "judge_mode",
    "manifest",
    "run_cycle",
]
