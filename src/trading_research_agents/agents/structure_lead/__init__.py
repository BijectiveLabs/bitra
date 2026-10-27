"""Structure lead: the recorded trades and bars, the walk-forward harness, the registered
mechanism.

Writes: a Signal, through the Strategy protocol; the runtime and the controls decide what
becomes an order.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agents.agents.structure_lead.role import (
    ROLE,
    SCHEMA,
    StructureLead,
    parse,
)

__all__ = ["ROLE", "SCHEMA", "StructureLead", "parse"]
