"""Risk manager: the controls, the canary caps, the ladder, measured latency against the
declared, alpha decay against the registration.

Writes: tighten_limits (smaller only), demote (down only, with a reason), arm_kill_switch.
One call, one schema, one typed object out; the pipeline records it. See `role.py`.
"""

from trading_research_agent.agent.risk_manager.role import ROLE, SCHEMA, RiskManager, parse

__all__ = ["ROLE", "SCHEMA", "RiskManager", "parse"]
