"""Reviewer: one stance on a proposal, and only through claims that cite a figure it was given.

Three stances of the same role - pro, contra, neutral - asked after the deciding analyst answers.
Writes: nothing. Sizes: nothing. Its vote reaches the confluence gate as a count, and only after
code has checked every claim against the brief. See `role.py` and `tools.py`.
"""

from trading_research_agents.agents.reviewer.role import (
    ROLE,
    SCHEMA,
    Claim,
    Reviewer,
    parse,
)
from trading_research_agents.agents.reviewer.tools import (
    Checked,
    checked,
    figures_of,
    reviewer_brief,
)
from trading_research_agents.execution.confluence import STANCES

__all__ = [
    "ROLE",
    "SCHEMA",
    "STANCES",
    "Checked",
    "Claim",
    "Reviewer",
    "checked",
    "figures_of",
    "parse",
    "reviewer_brief",
]
