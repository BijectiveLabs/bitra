"""Executor: the venue (simulated or live), the rate limits, the allocation across runtimes.

Writes: submit_intent (through the controls and the canary caps); allocate (within
max_position and max_gross_exposure).
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agent.agent.executor.role import ROLE, SCHEMA, Executor, parse

__all__ = ["ROLE", "SCHEMA", "Executor", "parse"]
