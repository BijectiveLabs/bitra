from __future__ import annotations

from decimal import Decimal

import pytest

from trading_research_agents.strategy.core import Idea, Target


def test_an_idea_has_a_side_a_hold_and_positive_levels() -> None:
    idea = Idea(Target.LONG, Decimal(150), Decimal(300), 6, "mom_z_24 +1.2")
    assert idea.hold_bars == 6
    with pytest.raises(ValueError, match="long or short"):
        Idea(Target.FLAT, None, None, 6, "r")
    with pytest.raises(ValueError, match="at least one bar"):
        Idea(Target.LONG, None, None, 0, "r")
    with pytest.raises(ValueError, match="positive"):
        Idea(Target.LONG, Decimal(-1), None, 6, "r")
    with pytest.raises(ValueError, match="reason"):
        Idea(Target.LONG, None, None, 6, " ")
