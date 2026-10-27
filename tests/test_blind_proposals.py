"""Agents propose from the market alone; the operator's execution parameters stay with code.

A role that knows what a trade has to clear can shape its answer to clear it: a target stretched
past the cost gate, a confidence raised to reach a size. So no proposing role is handed the desk's
rules, and the prompts that brief them do not describe those rules either. What code then changes
in a proposal is recorded beside it, so a result can be traced to the data, the model or the
operator's own settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_research_agents.agents import structure_lead
from trading_research_agents.agents.scope import VALIDATOR, scope_of

PROMPTS = Path(__file__).resolve().parent.parent / "src" / "trading_research_agents" / "agents"
PROMPTS = PROMPTS / "prompts"

# The operator's execution parameters and what code derives from them.
POLICY = (
    "cost_bps",
    "round_trip_bps",
    "edge_bps",
    "min_edge_multiple",
    "min_reward_risk",
    "reward_to_risk",
    "risk_per_trade",
    "stop_frac",
    "take_profit_frac",
    "size",
    "size_quote",
    "equity",
    "leverage",
    "max_notional",
    "controls",
    "targets_rule",
)
# Wording that told a proposing role what code would accept.
RULE_WORDING = (
    "several times what it costs",
    "one and a half times",
    "risk per trade",
    "floor and ceiling",
    "share of winners",
    "is refused by code",
    "than the ask by more than",
)


@pytest.mark.parametrize("role", [structure_lead.ROLE, VALIDATOR])
def test_a_proposing_role_is_never_handed_the_desks_rules(role: str) -> None:
    scope = scope_of(role)
    assert [key for key in POLICY if scope.keeps(key)] == []


@pytest.mark.parametrize(
    "name",
    [
        "structure_lead_live",
        "structure_lead",
        "validator",
        "validator_judge",
        "outcome_assessor",
    ],
)
def test_the_prompts_do_not_describe_what_code_will_accept(name: str) -> None:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8").lower()
    assert [phrase for phrase in RULE_WORDING if phrase in text] == []
