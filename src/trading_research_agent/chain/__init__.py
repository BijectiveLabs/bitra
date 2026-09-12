"""The falsification chain: the ways an idea can die, ordered by what each test costs to run.

Layer 0 is here. The later layers arrive release by release; see docs/architecture.md.
"""

from trading_research_agent.chain.floor import FloorCheck, FloorOutcome, cost_floor

__all__ = ["FloorCheck", "FloorOutcome", "cost_floor"]
