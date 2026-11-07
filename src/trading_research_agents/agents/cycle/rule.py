"""The cycle's one decision when the operator declared a rule: closed candles in, an idea or none.

No model is asked. The rule reads the decision interval's closed candles as `strategy.core.Bar`,
cut back to the last unbroken run, which is the call the arena makes on the archive, so a rule
replayed and a rule run live decide from the same bars the same way. Its stop and take, distances
from the entry, are the trade's exits; everything after them is the cycle every desk runs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_research_agents.agents.cycle.pricing import _candles
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.state import Pass
from trading_research_agents.agents.progress import DONE, REFUSED, announce
from trading_research_agents.agents.structure_lead.role import StructureLead
from trading_research_agents.strategy.core import Bar, Idea, closed_run

NS_PER_S = 1_000_000_000
BPS = Decimal(10_000)
STEP = "rule"


def decided_by(p: Pass) -> str:
    rule = p.cfg.rule
    return "structure_lead" if rule is None else f"rule:{rule.name}"


def ask_the_rule(p: Pass) -> bool:
    """The rule's idea on this cycle's closed candles; a cycle with none ends here, flat."""
    cfg, result = p.cfg, p.result
    rule = cfg.rule
    if rule is None:
        raise ValueError("ask_the_rule runs only on a desk that declared a rule")
    bars = as_bars(_candles(p.snapshot), cfg.interval_s)
    run = closed_run(bars, bars[-1].end_ns) if bars else []
    idea = rule.decide_on_bars(run)
    result.briefs["rule"] = {"rule": rule.name, "bars": len(run), "idea": _said(idea)}
    if idea is None:
        return _refuse(p, f"rule: {rule.name} has no idea on these {len(run)} bars", "no idea")
    target = idea.side.value
    if cfg.spot and target == "short":
        why = f"market: {cfg.instrument} is a spot market; the rule's short is not taken"
        return _refuse(p, why, "short on a spot market")
    p.rule_idea = idea
    # The cycle's later steps read the deciding answer in this shape. A rule has no confidence and
    # cites nothing; `decided_by` on the intent says which kind of answer it was.
    p.analyst = StructureLead(
        target=target,
        setup=f"rule:{rule.name}",
        reason=idea.reason,
        figures_used=(),
        confidence=1.0,
        horizon_bars=idea.hold_bars,
    )
    result.analyst = p.analyst
    announce(cfg.watch, STEP, DONE, f"{target}: {idea.reason}")
    return True


def as_bars(candles: list[Any], interval_s: int) -> list[Bar]:
    """The snapshot's closed candles as the bars a rule reads.

    A candle is a traded interval by definition, but most venues' candles do not report how many
    trades made one, and ccxt leaves the count at zero. Such a candle is still a traded bar, so it
    is counted as one trade rather than read as an empty interval.
    """
    span = interval_s * NS_PER_S
    bars = []
    for c in candles:
        end = c.start_ns + span
        if c.volume <= 0:
            # Prices with no volume behind them: an interval nothing traded in, so an empty bar,
            # which ends the run the rule is handed rather than feeding it a price nobody paid.
            bars.append(Bar(c.start_ns, end, None, None, None, None, Decimal(0), 0))
            continue
        bars.append(
            Bar(c.start_ns, end, c.open, c.high, c.low, c.close, c.volume, max(c.trades, 1))
        )
    return bars


def rule_exits(mid: Decimal, idea: Idea) -> dict[str, str]:
    """The rule's stop and take as prices from the mid this cycle priced at."""
    away = Decimal(1) if idea.side.value == "long" else Decimal(-1)
    out: dict[str, str] = {}
    if idea.stop_bps is not None:
        out["stop_price"] = str((mid * (1 - away * idea.stop_bps / BPS)).quantize(mid))
    if idea.take_bps is not None:
        out["take_price"] = str((mid * (1 + away * idea.take_bps / BPS)).quantize(mid))
    return out


def _said(idea: Idea | None) -> dict[str, str | int | None] | None:
    if idea is None:
        return None
    return {
        "side": idea.side.value,
        "stop_bps": None if idea.stop_bps is None else str(idea.stop_bps),
        "take_bps": None if idea.take_bps is None else str(idea.take_bps),
        "hold_bars": idea.hold_bars,
        "reason": idea.reason,
    }


def _refuse(p: Pass, why: str, detail: str) -> bool:
    p.result.refusals.append(why)
    announce(p.cfg.watch, STEP, REFUSED, detail)
    _finish(p.cfg, p.where, p.result)
    return False
