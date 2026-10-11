"""The gates as MCP tools, for any agent host. A surface over the gates, never a gate.

Every tool here reads: a cost floor, a registration check, a dataset audit, a validation, the
ledger, the ladder, a run report. None places an order, promotes a tier, or writes a ledger
row. What a model may do through this server is ask the gates what they measured; what it may
not do is anything that governs capital. The `mcp` extra installs the SDK.
"""

from trading_research_agents.mcp.server import build_server, serve

__all__ = ["build_server", "serve"]
