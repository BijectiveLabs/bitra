"""On-chain analyst: on-chain series through data plugins and column maps, when the operator
has any.

Writes: nothing.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agents.agents.onchain_analyst.role import ROLE, SCHEMA, OnchainAnalyst, parse

__all__ = ["ROLE", "SCHEMA", "OnchainAnalyst", "parse"]
