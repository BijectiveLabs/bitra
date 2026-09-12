"""The agent: an idea in, one of four recorded outcomes out.

Language models are used at exactly three points, to bound a magnitude, to write a
mechanism memo, and to draft a pre-registration. Every decision is made by code, against
the gates that already exist in `chain`, and every outcome is written down.
"""

from trading_research_agent.agent.completer import Completer, Completion, FakeCompleter
from trading_research_agent.agent.pipeline import RunConfig, propose
from trading_research_agent.agent.roles import Bound, BoundBasis, Critique, Memo, RoleError
from trading_research_agent.agent.state import Exchange, Outcome, Refusal, RunResult

__all__ = [
    "Bound",
    "BoundBasis",
    "Completer",
    "Completion",
    "Critique",
    "Exchange",
    "FakeCompleter",
    "Memo",
    "Outcome",
    "Refusal",
    "RoleError",
    "RunConfig",
    "RunResult",
    "propose",
]
