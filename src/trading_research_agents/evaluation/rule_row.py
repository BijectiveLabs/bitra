"""A rule from a file, asked as an arena row: the same call `tra run` makes live.

The row hands the rule the closed bars of the decision interval up to the decision, as
`strategy.core.Bar`, cut back to the last run that was traded, seen and contiguous, so a rule never
reads across a gap. The rule's idea is settled by the harness like any other row's.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.policy import DecisionView, TradeIdea
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.strategy.core import Bar, Rule, Target, closed_run
from trading_research_agents.strategy.load import load_class

SIDES = {Target.LONG: Direction.LONG, Target.SHORT: Direction.SHORT}


def as_bar(flow: FlowBar) -> Bar:
    return Bar(
        start_ns=flow.start_ns,
        end_ns=flow.end_ns,
        open=flow.open,
        high=flow.high,
        low=flow.low,
        close=flow.close,
        volume=flow.volume_base,
        trades=flow.trade_count,
    )


class RuleRow:
    """An arena row that asks a rule, under the row name the arena was given."""

    knowledge_cutoff: date | None = None

    def __init__(self, name: str, rule: Rule) -> None:
        self.name = name
        self.rule = rule

    def decide(self, view: DecisionView) -> TradeIdea | None:
        seen = [as_bar(b) for b in view.bars if b.gap is not GapState.MISSING]
        idea = self.rule.decide_on_bars(closed_run(seen, view.at_ns))
        if idea is None:
            return None
        return TradeIdea(
            side=SIDES[idea.side],
            playbook=self.rule.name,
            stop_bps=idea.stop_bps,
            take_bps=idea.take_bps,
            max_hold_bars=idea.hold_bars,
            reason=idea.reason,
        )


def rule_from(spec: str) -> Rule:
    """The rule a row spec `path/to/file.py:Class` names, built with its own defaults."""
    file, _, class_name = spec.rpartition(":")
    rule: Rule = load_class(Path(file), class_name)()
    if not callable(getattr(rule, "decide_on_bars", None)):
        raise ValueError(f"{spec!r} is not a rule: it needs `name` and `decide_on_bars`")
    return rule
