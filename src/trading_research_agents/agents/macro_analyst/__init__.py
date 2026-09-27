"""Macro analyst: external series through data plugins and column maps (scheduled events,
cross-venue funding, sentiment).

Writes: nothing.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agents.agents.macro_analyst.role import ROLE, SCHEMA, MacroAnalyst, parse

__all__ = ["ROLE", "SCHEMA", "MacroAnalyst", "parse"]
