"""Flow analyst: who is pressing on this market, read from the book, funding and open interest.

Reads only the flow figures, which leave the technical analyst's brief when this role is on the
team, so each figure has one reader. Answers a side and a confidence with the figures it rests on;
never a level and never a size. Its answer is a voice in conviction, see `role.py`.
"""

from trading_research_agents.agents.flow_analyst.role import ROLE, SCHEMA, FlowAnalyst, parse

__all__ = ["ROLE", "SCHEMA", "FlowAnalyst", "parse"]
