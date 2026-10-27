"""Blind confirmer: a second, independent read of the structure lead's own brief.

It is handed the same figures and answers in the same schema, without seeing the lead's answer.
Two reads count only when they agree, and they say most when they come from different model
families: answers from one model err together. Writes nothing; code compares the two.
"""

from trading_research_agents.agents.structure_lead.role import SCHEMA, parse

ROLE = "blind_confirmer"

__all__ = ["ROLE", "SCHEMA", "parse"]
