"""Event analyst: the deciding role on a prediction market outcome.

Reads the question, the outcome, when it resolves, its prices and the operator's notes, and says how
likely the outcome is to win. Code compares that with what the share costs; see `role.py` for the
form and `agents/cycle/` for what is done with it.
"""

from trading_research_agents.agents.event_analyst.role import ROLE, SCHEMA, EventAnalyst, parse

__all__ = ["ROLE", "SCHEMA", "EventAnalyst", "parse"]
