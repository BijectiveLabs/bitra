"""Validator: one stance on a proposal, and only through claims that cite a figure it was given.

Three stances of the same role - pro, contra, neutral - asked after the deciding analyst answers.
Writes: nothing. Sizes: nothing. Its vote reaches the confluence gate as a count, and only after
code has checked every claim against the brief. See `role.py` and `tools.py`.
"""

from trading_research_agents.agents.validator.role import (
    ROLE,
    SCHEMA,
    Claim,
    Validator,
    parse,
)
from trading_research_agents.agents.validator.tools import (
    Checked,
    checked,
    figures_of,
    validator_brief,
)
from trading_research_agents.execution.confluence import STANCES

__all__ = [
    "ROLE",
    "SCHEMA",
    "STANCES",
    "Checked",
    "Claim",
    "Validator",
    "checked",
    "figures_of",
    "parse",
    "validator_brief",
]
