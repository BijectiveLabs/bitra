"""The evaluation harness: policies replayed over the market archive, every idea settled by the
desk's own exits and costs, and reported beside baselines that are there to be beaten."""

from __future__ import annotations

from trading_research_agents.evaluation.arena import ArenaConfig, ArenaResult, run_arena
from trading_research_agents.evaluation.policy import DecisionView, Policy, Scored, TradeIdea
from trading_research_agents.evaluation.records import write_arena
from trading_research_agents.evaluation.settle import Costs, Settlement, Unsettled
from trading_research_agents.evaluation.splits import walk_forward

__all__ = [
    "ArenaConfig",
    "ArenaResult",
    "Costs",
    "DecisionView",
    "Policy",
    "Scored",
    "Settlement",
    "TradeIdea",
    "Unsettled",
    "run_arena",
    "walk_forward",
    "write_arena",
]
