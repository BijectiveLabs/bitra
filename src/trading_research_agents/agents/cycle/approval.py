"""The cycle's last step before its intent is written: the risk gate, with the desk read fresh."""

from __future__ import annotations

from decimal import Decimal

from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.state import Pass
from trading_research_agents.agents.progress import DONE, REFUSED, announce
from trading_research_agents.execution.targets import intents_today
from trading_research_agents.execution.unknown import blocking
from trading_research_agents.records import write_json
from trading_research_agents.risk import RiskLimits, RiskState, evaluate


def risk_limits(cfg: CycleConfig) -> RiskLimits:
    """The hard limits, from the same declarations the cycle's own gates read."""
    return RiskLimits(
        max_notional=cfg.max_notional,
        max_position=cfg.max_position,
        max_position_quote=cfg.max_position_quote,
        max_risk_per_trade_bps=cfg.max_risk_per_trade_bps,
        max_trades_per_day=cfg.max_trades_per_day,
        daily_loss_limit_bps=cfg.daily_loss_limit_bps,
        max_drawdown_bps=cfg.max_drawdown_bps,
    )


def _held_by(cfg: CycleConfig) -> str:
    armed = [path for path in cfg.switches if path.exists()]
    return f"the switch at {armed[0]}" if armed else ""


def _read(equity: dict[str, str], key: str) -> Decimal | None:
    value = equity.get(key)
    return None if value in (None, "") else Decimal(str(value))


def risk_state(p: Pass) -> RiskState:
    """The desk as it stands now: switches, unanswered sends, today's trades and losses."""
    cfg = p.cfg
    equity = p.result.briefs.get("equity") or {}
    return RiskState(
        equity=p.equity,
        trades_today=intents_today(cfg.root, at=cfg.clock(), instrument=None),
        day_loss_bps=_read(equity, "day_loss_bps"),
        drawdown_bps=_read(equity, "drawdown_bps"),
        held_by=_held_by(cfg),
        unknown_orders=blocking(cfg.root, cfg.instrument),
        places_orders=cfg.mode == "venue",
    )


def risk_gate(p: Pass) -> bool:
    """Approve the drafted intent or end the cycle. A gate that cannot read the desk refuses."""
    cfg, result = p.cfg, p.result
    try:
        state = risk_state(p)
    except Exception as exc:
        why = f"risk gate: the desk could not be read ({exc.__class__.__name__}: {exc})"
        result.refusals.append(why)
        result.briefs["risk_gate"] = {"approved": False, "checks": [], "reasons": [why]}
        announce(cfg.watch, "risk gate", REFUSED, why)
        _finish(cfg, p.where, result)
        return False
    decision = evaluate(p.intent, limits=risk_limits(cfg), state=state, now=cfg.clock().timestamp())
    result.briefs["risk_gate"] = decision.record()
    write_json(p.where.brief("risk_gate"), decision.record())
    if not decision.approved:
        result.refusals.extend(f"risk gate: {why}" for why in decision.reasons)
        announce(cfg.watch, "risk gate", REFUSED, decision.reasons[0])
        _finish(cfg, p.where, result)
        return False
    p.intent = decision.approved_intent(p.intent)
    announce(cfg.watch, "risk gate", DONE, f"{len(decision.checks)} checks passed")
    return True
