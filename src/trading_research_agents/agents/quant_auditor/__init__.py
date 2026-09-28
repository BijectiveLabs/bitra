"""Quant auditor: what the desk's own decisions have been worth, read from the record.

Reads: cycle records, the intents they wrote and the trades a session closed. Trades: nothing.
Writes: a report with proposals that a person applies, or does not.
"""

from trading_research_agents.agents.quant_auditor.role import ROLE, SCHEMA, Report, parse
from trading_research_agents.agents.quant_auditor.tools import quant_brief

__all__ = ["ROLE", "SCHEMA", "Report", "parse", "quant_brief"]
