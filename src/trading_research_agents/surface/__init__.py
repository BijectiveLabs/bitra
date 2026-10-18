"""The gates as callable tools. One surface, two callers: the MCP server and the agent roles.

Reads answer what a gate measured; actions go through the same controls the CLI uses, and
nothing here can loosen one. A role or a host that wants more than this surface offers is
asking for something the package refuses on purpose.
"""

from trading_research_agents.surface.actions import (
    FORBIDDEN,
    ActionRefusedError,
    Tightened,
    arm_kill_switch,
    demote,
    refuse,
    tighten_limits,
)
from trading_research_agents.surface.tools import (
    PARAMETERS,
    READ_TOOLS,
    audit_dataset,
    check_cost_floor,
    check_registration,
    list_runs,
    observe_events,
    read_run_report,
    search_ledger,
    show_ladder,
    show_ledger,
    validate_trades,
)

__all__ = [
    "FORBIDDEN",
    "PARAMETERS",
    "READ_TOOLS",
    "ActionRefusedError",
    "Tightened",
    "arm_kill_switch",
    "audit_dataset",
    "check_cost_floor",
    "check_registration",
    "demote",
    "list_runs",
    "observe_events",
    "read_run_report",
    "refuse",
    "search_ledger",
    "show_ladder",
    "show_ledger",
    "tighten_limits",
    "validate_trades",
]
