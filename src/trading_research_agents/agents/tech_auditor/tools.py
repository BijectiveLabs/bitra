"""The brief the technology auditor reads: how the machinery behaved, counted from the record."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from trading_research_agents import __version__
from trading_research_agents.agents.audits import (
    Cycles,
    agent_health,
    data_health,
    read_usage,
    refusal_kinds,
    token_spend,
)

WATCHED = ("ccxt", "rich", "pyarrow", "anthropic", "openai")


def installed() -> dict[str, str]:
    """What this deployment is actually running, so a finding can name a version."""
    out = {"trading_research_agents": __version__, "python": sys.version.split()[0]}
    for name in WATCHED:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            continue
    return out


def tech_brief(cycles: Cycles, *, usage: Path | None = None) -> dict[str, Any]:
    """Everything the technology auditor is given, and nothing that is not in the record."""
    rows = read_usage(usage) if usage is not None else ()
    return {
        "basis": (
            "counts and medians computed by code over the cycle records this project wrote; "
            "seconds are wall clock per role, tokens are what the models were billed for, and a "
            "refusal is one gate saying no in one cycle"
        ),
        "cycles": cycles.count,
        "unreadable_records": len(cycles.unreadable),
        "agents": agent_health(cycles),
        "data": data_health(cycles),
        "spend": token_spend(cycles, rows),
        "refusals": refusal_kinds(cycles),
        "installed": installed(),
    }
