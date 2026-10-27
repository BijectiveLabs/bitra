"""The risk gate: one entry point every new order passes, and that fails closed.

A cycle sizes a trade through its own gates. This package checks the intent that comes out of them
against the operator's hard limits, with the desk's state read just before it goes out, and signs
what it approved. The order hand sends nothing without a signature that matches the intent, so no
path to the venue goes around it. Closing or flattening a position never needs approval: reducing
risk is always allowed.
"""

from trading_research_agents.risk.approval import approval_problem
from trading_research_agents.risk.gate import RiskDecision, evaluate
from trading_research_agents.risk.limits import RiskLimits, RiskState

__all__ = [
    "RiskDecision",
    "RiskLimits",
    "RiskState",
    "approval_problem",
    "evaluate",
]
