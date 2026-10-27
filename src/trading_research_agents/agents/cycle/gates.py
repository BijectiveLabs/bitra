"""The cycle from a priced proposal to an intent: every gate the operator declared, in order.

Confluence asks whether the declared states agree with the direction; conviction sets how much
goes behind it; the desk's own record may lower the risk per trade; the loss at the stop is held
to the risk per trade; the whole book is held to the desk's limits; the trade is priced again at
the size that goes out; the cost gate and the targets have the last word. Each is arithmetic over
the operator's declarations, and each says which figure refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.briefs import BriefError
from trading_research_agents.agents.cycle.approval import risk_gate
from trading_research_agents.agents.cycle.briefing import _conviction_states
from trading_research_agents.agents.cycle.decide import _stop, price_at
from trading_research_agents.agents.cycle.pricing import (
    _break_even,
    _channel,
    _drawdown_room,
    _held,
    _loss_at_stop,
    _position_cap,
    _touch,
)
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.reviewers import _informative_votes, _votes
from trading_research_agents.agents.cycle.state import Pass, Step
from trading_research_agents.agents.progress import DONE, REFUSED, announce
from trading_research_agents.execution.confluence import confluent
from trading_research_agents.execution.conviction import score_of, stake_for
from trading_research_agents.execution.desk import within_desk
from trading_research_agents.execution.kelly import by_record
from trading_research_agents.execution.pretrade import admit
from trading_research_agents.execution.risk import within_risk
from trading_research_agents.execution.targets import (
    edge_multiple,
    intents_today,
    research_bps,
    within_targets,
)
from trading_research_agents.records import append_line, read_lines, write_json
from trading_research_agents.trading.positions import ROUND_TRIPS


def _admitted(p: Pass, step: str, admitted: bool, detail: str) -> bool:
    """Announce a gate's verdict; a refusal writes the cycle down and ends the pass."""
    announce(p.cfg.watch, step, DONE if admitted else REFUSED, detail)
    if not admitted:
        _finish(p.cfg, p.where, p.result)
    return admitted


def _confluence(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    if cfg.matrix.idle:
        return True
    agreement = confluent(
        cfg.matrix,
        target=p.analyst.target,
        states={
            role: said["declared_state"]
            for role, said in result.context.items()
            if isinstance(said.get("declared_state"), Mapping)
        },
        votes=_votes(result),
    )
    result.confluence = agreement
    for reason in agreement.reasons:
        result.refusals.append(f"confluence: {reason}")
    detail = agreement.reasons[0] if agreement.reasons else ""
    return _admitted(p, "confluence", agreement.admitted, detail)


def _conviction(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    p.size = p.base
    if cfg.conviction.idle:
        return True
    mid = p.costs.cost.mid
    stake = stake_for(
        cfg.conviction,
        score_of(
            cfg.conviction,
            target=p.analyst.target,
            states=_conviction_states(result),
            confidence=p.analyst.confidence,
            votes=_informative_votes(result),
        ),
        base_size=p.base,
        price=mid,
        max_position=_position_cap(cfg, mid, p.base),
        equity=p.equity,
        step=cfg.size_step,
    )
    result.stake = stake
    for reason in stake.reasons:
        result.refusals.append(f"conviction: {reason}")
    if not _admitted(p, "conviction", stake.admitted, f"score {stake.score.score}"):
        return False
    p.size = stake.size
    return True


def _sizing(p: Pass) -> bool:
    """The desk's own round trips may lower the declared risk per trade, never raise it."""
    cfg, result = p.cfg, p.result
    p.allowed_bps = cfg.max_risk_per_trade_bps
    if cfg.sizing.idle:
        return True
    sized = by_record(
        cfg.sizing,
        read_lines(cfg.root / ROUND_TRIPS),
        room_bps=_drawdown_room(cfg, result),
    )
    result.briefs["sizing"] = sized.record()
    if sized.refuses:
        return _stop(p, f"sizing: {sized.reason}", "sizing", "the record supports no new entry")
    if sized.cap_bps is not None:
        allowed = p.allowed_bps
        p.allowed_bps = sized.cap_bps if allowed is None else min(allowed, sized.cap_bps)
    announce(
        cfg.watch,
        "sizing",
        DONE,
        "declared risk stands"
        if sized.cap_bps is None
        else f"at most {p.allowed_bps} bps at the stop",
    )
    return True


def _risk(p: Pass) -> bool:
    """The loss at the stop, round trip included, against the equity and the risk per trade."""
    cfg, result = p.cfg, p.result
    stop_at = p.exits.get("stop_price")
    if p.allowed_bps is not None and stop_at is None:
        # A declared risk limit that cannot be computed is not a limit. Fail closed and say which
        # declaration is missing rather than sending a size nothing checked.
        why = (
            "risk: [controls].max_risk_per_trade_bps is declared and no stop is, so the loss at "
            "the stop cannot be computed; declare [exit].stop_frac or drop the limit"
        )
        return _stop(p, why, "risk", "no stop to size from")
    # Priced at the size about to be checked, or the base when that is larger: impact only falls
    # as a size is cut, so the loss at the stop is never understated by the cut it leads to. A
    # conviction rung above 1x is priced at its own size, since its impact is larger.
    risk_costs = p.costs
    if p.size > p.base:
        try:
            risk_costs = price_at(p, p.size)
        except BriefError as exc:
            return _cost_failed(p, exc)
    capped = within_risk(
        size=p.size,
        entry=p.costs.cost.mid,
        stop=None if stop_at is None else Decimal(stop_at),
        equity=p.equity,
        allowed_bps=p.allowed_bps,
        step=cfg.size_step,
        cost_bps=max(risk_costs.cost.round_trip_bps, Decimal(0)),
    )
    result.risk = capped
    p.capped = capped
    for reason in capped.reasons:
        result.refusals.append(f"risk: {reason}")
    detail = "" if capped.risk_bps is None else f"{capped.risk_bps} bps of equity at the stop"
    if not _admitted(p, "risk", capped.admitted, detail):
        return False
    p.size = capped.size
    return True


def _cost_failed(p: Pass, exc: BriefError) -> bool:
    why = f"cost at {p.size}: {exc}"
    p.result.refusals.append(why)
    p.result.stopped_by = why
    _finish(p.cfg, p.where, p.result)
    return False


def _desk(p: Pass) -> bool:
    """What the whole book is already carrying, with this trade added."""
    cfg, result = p.cfg, p.result
    if not cfg.desk.bounds_a_trade:
        return True
    held, source = _held(cfg, result)
    book = within_desk(
        cfg.desk,
        instrument=cfg.instrument,
        adding_bps=p.capped.risk_bps,
        held=held,
        source=source,
    )
    result.book = book
    for reason in book.reasons:
        result.refusals.append(f"desk: {reason}")
    detail = "" if book.open_risk_bps is None else f"{book.open_risk_bps} bps open across the book"
    return _admitted(p, "desk", book.admitted, detail)


def _reprice(p: Pass) -> bool:
    """Impact is a function of the size that goes out, so the gate prices the trade it admits."""
    if p.size == p.base:
        return True
    try:
        p.costs = price_at(p, p.size)
    except BriefError as exc:
        return _cost_failed(p, exc)
    p.result.briefs["cost"] = p.costs.record()
    write_json(p.where.brief("cost"), p.costs.record())
    return True


def _cost_gate(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    verdict = admit(
        edge_bps=p.edge,
        cost=p.costs.cost,
        margin_bps=cfg.margin_bps,
        edge_source=p.edge_source,
    )
    result.verdict = verdict
    p.verdict = verdict
    announce(
        cfg.watch,
        "cost gate",
        DONE if verdict.admitted else REFUSED,
        f"net {verdict.net_bps:.2f} bps",
    )
    if not verdict.admitted:
        result.refusals.append(f"cost gate: {verdict.reason}")
        _finish(cfg, p.where, result)
        return False
    return True


def _targets(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    spent = cfg.carried_spend + (cfg.usage.cost - p.spent_before)
    calls = cfg.usage.records[p.calls_before :]
    result.briefs["spend"] = {
        "calls": len(calls),
        "unpriced": sorted({f"{c.provider}:{c.model}" for c in calls if c.cost is None}),
    }
    if cfg.targets.idle:
        return True
    result.briefs["targets_rule"] = cfg.targets.record()
    cost = p.costs.cost
    met = within_targets(
        cfg.targets,
        net_bps=p.verdict.net_bps,
        reward_to_risk=cfg.reward_to_risk,
        round_trips_today=intents_today(cfg.root, at=cfg.clock(), instrument=cfg.instrument),
        over_cost=edge_multiple(edge_bps=p.edge, cost_bps=cost.round_trip_bps),
        research=research_bps(spend=spent, notional=p.size * cost.mid),
    )
    result.targets = met
    for reason in met.reasons:
        result.refusals.append(f"targets: {reason}")
    detail = met.reasons[0] if met.reasons else "every declared target met"
    return _admitted(p, "targets", met.admitted, detail)


def _draft(p: Pass) -> bool:
    p.intent = _intent_record(p)
    return True


def _intent(p: Pass) -> bool:
    """The approved intent, written before anything is sent, so the decision survives whatever
    comes next."""
    cfg, result, analyst = p.cfg, p.result, p.analyst
    result.intent = p.intent
    announce(cfg.watch, "intent", DONE, f"{analyst.target} {p.size} on {analyst.setup}")
    append_line(p.where.path / "intents.jsonl", result.intent, durable=True)
    return True


def _intent_record(p: Pass) -> dict[str, Any]:
    cfg, result, analyst, costs = p.cfg, p.result, p.analyst, p.costs
    cost = costs.cost
    targets = result.targets
    fees = sum((c.bps for c in cost.components if c.name == "fee_round_trip"), Decimal(0))
    return {
        "instrument": cfg.instrument,
        "target": analyst.target,
        "setup": analyst.setup,
        "size": str(p.size),
        "base_size": str(p.base),
        "conviction": None if result.stake is None else str(result.stake.score.score),
        "risk_bps": None
        if result.risk is None or result.risk.risk_bps is None
        else str(result.risk.risk_bps),
        "loss_at_stop_bps": _loss_at_stop(cost.mid, p.exits, cost.round_trip_bps),
        # What the trade was priced at, so the finished trade can be set against it.
        "priced_round_trip_bps": str(cost.round_trip_bps.quantize(Decimal("0.0001"))),
        "priced_fee_bps": str(fees.quantize(Decimal("0.0001"))),
        "horizon_bars": analyst.horizon_bars,
        "confidence": analyst.confidence,
        "reason": analyst.reason,
        # What the direction was read from, after code checked each figure against the brief. An
        # intent that cannot say this is an intent nobody can review a month later.
        "cited": result.cited,
        "edge_bps": str(p.edge),
        "edge_source": p.verdict.edge_source,
        "break_even_win_rate": _break_even(result.exits, cost.round_trip_bps),
        "net_bps": str(p.verdict.net_bps.quantize(Decimal("0.0001"))),
        "edge_multiple": (
            None if targets is None or targets.edge_multiple is None else str(targets.edge_multiple)
        ),
        "research_bps": (
            None if targets is None or targets.research_bps is None else str(targets.research_bps)
        ),
        "mode": cfg.mode,
        "mid": str(cost.mid),
        "at": p.cycle,
        # The touch this cycle saw, so an order that rests on the book rests where the cost was
        # priced rather than wherever the book has moved to by the time it is sent.
        **_touch(p.snapshot),
        **p.exits,
        "channel": cfg.venue_key,
        "entry_role": _channel(cfg).entry_role,
        # Each member's side, when the analyst did not decide alone.
        **({"team": dict(p.team_reads)} if p.team_reads else {}),
    }


GATES: tuple[Step, ...] = (
    _confluence,
    _conviction,
    _sizing,
    _risk,
    _desk,
    _reprice,
    _cost_gate,
    _targets,
    _draft,
    risk_gate,
    _intent,
)

__all__ = ["GATES"]
