"""Technical peer: a second, independent read of the technical analyst's own brief.

It is handed the same figures and answers in the same schema, without seeing the lead's answer.
Two reads count only when they agree, and they say most when they come from different model
families: answers from one model err together. Writes nothing; code compares the two.
"""

from trading_research_agents.agents.technical_analyst.role import SCHEMA, parse

ROLE = "technical_peer"

__all__ = ["ROLE", "SCHEMA", "parse"]
