"""The written rules a research session decides by, where a model used to be asked."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.session_rules import (
    allocate_evenly,
    risk_by_rule,
    screen_by_rule,
)


@dataclass
class Screen:
    proposed_regime: str = "ranging"
    regime_reason: str = "variance ratio below one"


@dataclass
class Figures:
    instrument: str
    rows: int = 100
    prints_per_minute: float = 2.0
    realised_vol_bps: float = 30.0
    floor_outcome: str = "cleared"
    floor_margin_bps: str = "4"
    screen: Screen | None = None
    observation: dict[str, Any] | None = None


def test_the_screen_keeps_what_clears_and_names_why_the_rest_is_out() -> None:
    kept, excluded = screen_by_rule(
        [
            Figures("AAA", screen=Screen()),
            Figures("BBB", prints_per_minute=0),
            Figures("CCC", realised_vol_bps=0),
            Figures("DDD", floor_outcome="stop", floor_margin_bps="-3"),
            Figures("EEE", observation={"recurrence": "does not recur: too few events"}),
        ]
    )
    assert kept == [
        {"instrument": "AAA", "regime": "ranging", "reason": "variance ratio below one"}
    ]
    reasons = {row["instrument"]: row["reason"] for row in excluded}
    assert reasons["BBB"] == "no prints"
    assert reasons["CCC"].startswith("no volatility")
    assert reasons["DDD"] == "cost floor: stop (margin -3 bps)"
    assert reasons["EEE"] == "the declared event does not recur: too few events"


def brief(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "latency": {"ratio_to_declared": 1.0},
        "alpha_decay": {"below_claim": False},
        "refusals": [],
    }
    return {**base, **over}


def test_the_risk_rule_continues_demotes_or_halts_on_the_measured_figures() -> None:
    assert risk_by_rule({"A": brief()}, {"A": None}).decision == "continue"
    slow = risk_by_rule({"A": brief(latency={"ratio_to_declared": 3.5})}, {"A": None})
    assert slow.decision == "demote" and "latency 3.5x" in slow.reasons[0]
    decayed = risk_by_rule(
        {
            "A": brief(
                alpha_decay={"below_claim": True, "rolling_mean_bps": "2", "claimed_bps": "20"}
            )
        },
        {"A": None},
    )
    assert decayed.decision == "demote" and "below the claimed 20 bps" in decayed.reasons[0]
    refused = risk_by_rule(
        {"A": brief(refusals=[{"control": "max_position", "refusals": 3}])}, {"A": None}
    )
    assert refused.decision == "demote" and "max_position refused 3 times" in refused.reasons[0]
    halted = risk_by_rule({"A": brief()}, {"A": "kill switch"})
    assert halted.decision == "halt" and halted.reasons == ("A: stopped by kill switch",)
    assert halted.record()["decided_by"] == "rule"


def test_the_allocation_is_an_equal_share_that_sums_to_one() -> None:
    shares = allocate_evenly([{"instrument": "A"}, {"instrument": "B"}, {"instrument": "C"}])
    assert [row["instrument"] for row in shares] == ["A", "B", "C"]
    total = sum((Decimal(str(row["fraction"])) for row in shares), Decimal(0))
    assert abs(total - 1) < Decimal("0.00001")
    assert allocate_evenly([]) == []
