"""Macro reading: external series through data plugins and column maps (rates, scheduled events,
cross-venue funding), read by code against the operator's declared thresholds.

Writes: nothing. Asks no model: until 0.11 a model labelled the same brief, and no gate ever read
that label. The brief builder is in `tools.py`; the declared state in `agents.briefs`.
"""

ROLE = "macro_analyst"

__all__ = ["ROLE"]
