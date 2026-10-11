"""The roles: what each agent is asked, what it may answer, and what it may never do.

A role is a schema, a parser and a prompt. The model client, the call budget and the usage
ledger are one layer below, in `llm`; the gates are one layer below that. Nothing here places
an order: a role answers, and code decides what to do with the answer.
"""

from trading_research_agents.agents.pipeline import RunConfig, propose
from trading_research_agents.agents.roles import (
    Bound,
    BoundBasis,
    Critique,
    Memo,
    RoleError,
)
from trading_research_agents.agents.state import Exchange, Outcome, Refusal, RunResult

__all__ = [
    "Bound",
    "BoundBasis",
    "Critique",
    "Exchange",
    "Memo",
    "Outcome",
    "Refusal",
    "RoleError",
    "RunConfig",
    "RunResult",
    "propose",
]
