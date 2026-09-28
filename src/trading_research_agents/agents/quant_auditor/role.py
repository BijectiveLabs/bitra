"""The quant auditor: what the desk decided, what it cost, and what to change about it."""

from __future__ import annotations

from trading_research_agents.agents.auditor_report import SCHEMA, Report
from trading_research_agents.agents.auditor_report import parse as _parse

ROLE = "quant_auditor"

__all__ = ["ROLE", "SCHEMA", "Report", "parse"]


def parse(text: str) -> Report:
    return _parse(text, role=ROLE)
