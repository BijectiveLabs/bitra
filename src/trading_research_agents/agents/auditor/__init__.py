"""Auditor: the ledger, every run directory, usage.jsonl, the ladder, and a replay of the same bars.

Writes: audit.json; a WITHDRAWN or NOT PROVEN row in the ledger when a claim is not supported.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agents.agents.auditor.role import ROLE, SCHEMA, Auditor, parse

__all__ = ["ROLE", "SCHEMA", "Auditor", "parse"]
