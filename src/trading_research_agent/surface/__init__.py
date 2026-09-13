"""The gates as callable tools. One surface, two callers: the MCP server and the agent roles.

Reads answer what a gate measured; actions go through the same controls the CLI uses, and
nothing here can loosen one. A role or a host that wants more than this surface offers is
asking for something the package refuses on purpose.
"""

from trading_research_agent.surface.tools import (
    READ_TOOLS,
    audit_dataset,
    check_registration,
    cost_floor_check,
    ladder_show,
    ledger_show,
    run_report,
    validate_trades,
)

__all__ = [
    "READ_TOOLS",
    "audit_dataset",
    "check_registration",
    "cost_floor_check",
    "ladder_show",
    "ledger_show",
    "run_report",
    "validate_trades",
]
