"""Technology auditor: the package's own behaviour, read from the record it wrote.

Reads: cycle records and the usage ledger. Trades: nothing. Writes: a report with proposals that a
person applies, or does not. It has no venue, no key and no place in a cycle.
"""

from trading_research_agents.agents.tech_auditor.role import ROLE, SCHEMA, Report, parse
from trading_research_agents.agents.tech_auditor.tools import installed, tech_brief

__all__ = ["ROLE", "SCHEMA", "Report", "installed", "parse", "tech_brief"]
