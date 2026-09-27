"""Technical analyst: the recorded trades and bars, the walk-forward harness, the registered
mechanism.

Writes: a Signal, through the Strategy protocol; the runtime and the controls decide what
becomes an order.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agent.agent.technical_analyst.role import (
    ROLE,
    SCHEMA,
    TechnicalAnalyst,
    parse,
)

__all__ = ["ROLE", "SCHEMA", "TechnicalAnalyst", "parse"]
