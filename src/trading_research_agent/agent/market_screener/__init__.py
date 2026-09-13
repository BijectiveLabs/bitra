"""Market screener: the data gate, the operating envelope and the cost floor.

Writes: nothing.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agent.agent.market_screener.role import ROLE, SCHEMA, MarketScreener, parse

__all__ = ["ROLE", "SCHEMA", "MarketScreener", "parse"]
