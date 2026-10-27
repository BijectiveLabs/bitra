"""A prediction outcome: the outcome assessor's probability against the ask, held to resolution.

The share pays 1 or nothing, so what it can lose is its cost: that is the stop, and the size is cut
until that cost fits the risk per trade. The take profit is the analyst's own probability, offered
on the book after the fill; otherwise the share is held to resolution or until time closes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_research_agents.agents import outcome_assessor
from trading_research_agents.agents.cited import survived
from trading_research_agents.agents.cycle.asking import _ask_readable
from trading_research_agents.agents.cycle.briefing import _calendar
from trading_research_agents.agents.cycle.pricing import (
    _candles,
    _channel,
    _held,
    _prices,
    _quantized,
    _touch,
)
from trading_research_agents.agents.cycle.record import _finish, _send, _stop_before_asking
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.cycle.state import Pass
from trading_research_agents.agents.progress import DONE, REFUSED, RUNNING, announce
from trading_research_agents.agents.validator.tools import figures_of
from trading_research_agents.execution.desk import within_desk
from trading_research_agents.execution.outcome import BPS, OutcomeError, outcome_edge
from trading_research_agents.records import append_line, write_json
from trading_research_agents.trading.market import Market


@dataclass(frozen=True)
class _Book:
    bid: Decimal | None
    ask: Decimal
    coefficient: Any
    fee_bps: Any


def decide_on_an_outcome(p: Pass, market: Market) -> CycleResult:
    book = _book(p)
    if book is None:
        return p.result
    analyst = _ask_event(p, market, book)
    if analyst is None:
        return p.result
    try:
        edge = outcome_edge(
            analyst.probability,
            book.ask,
            margin_bps=p.cfg.margin_bps,
            coefficient=book.coefficient,
            fee_bps=book.fee_bps,
        )
    except OutcomeError as exc:
        return _stop_before_asking(p.cfg, p.where, p.result, f"outcome: {exc}", step="cost")
    p.result.briefs["outcome"] = edge.record()
    write_json(p.where.brief("cost"), edge.record())
    announce(p.cfg.watch, "cost gate", DONE if edge.admitted else REFUSED, edge.reason)
    if not edge.admitted:
        p.result.refusals.append(f"cost gate: {edge.reason}")
        _finish(p.cfg, p.where, p.result)
        return p.result
    return _stake(p, market, book, analyst, edge)


def _book(p: Pass) -> _Book | None:
    cfg = p.cfg
    bid, ask = _prices(p.snapshot)
    if ask is None:
        why = "outcome: the book has no ask, so the share cannot be priced"
        _stop_before_asking(cfg, p.where, p.result, why)
        return None
    fees = _channel(cfg).outcome_fees()
    if fees is None:
        why = (
            f"outcome: channel {cfg.venue_key} declares no fees for a prediction outcome; add "
            "[channel.prediction] with fee_coefficient or entry_fee_bps and exit_fee_bps"
        )
        _stop_before_asking(cfg, p.where, p.result, why, step="cost")
        return None
    coefficient, fee_bps = fees
    return _Book(bid, ask, coefficient, fee_bps)


def _event_brief(p: Pass, market: Market, book: _Book) -> dict[str, Any]:
    cfg = p.cfg
    now = cfg.clock()
    expiry = market.expiry
    hours = (
        None
        if expiry is None
        else str(
            ((Decimal(expiry) / 1000 - Decimal(str(now.timestamp()))) / 3600).quantize(
                Decimal("0.1")
            )
        )
    )
    return {
        "instrument": cfg.instrument,
        "question": market.question,
        "outcome": market.outcome_label,
        "resolves_at": None
        if expiry is None
        else datetime.fromtimestamp(int(expiry) / 1000, tz=UTC).isoformat(),
        "hours_to_resolution": hours,
        "bid": None if book.bid is None else str(book.bid),
        "ask": str(book.ask),
        "mid": None if book.bid is None else str((book.bid + book.ask) / 2),
        "closes": [str(getattr(c, "close", "")) for c in _candles(p.snapshot)[-24:]],
        "calendar": [e.record() for e in _calendar(cfg, p.result)],
    }


def _ask_event(p: Pass, market: Market, book: _Book) -> Any:
    """The outcome assessor's probability, its citations checked; None when the cycle has ended."""
    cfg, result = p.cfg, p.result
    brief = _event_brief(p, market, book)
    write_json(p.where.brief(outcome_assessor.ROLE), brief)
    announce(cfg.watch, outcome_assessor.ROLE, RUNNING)
    try:
        answer = _ask_readable(
            cfg, outcome_assessor.ROLE, brief, outcome_assessor.SCHEMA, outcome_assessor.parse
        )
        analyst = outcome_assessor.parse(answer)
    except Exception as exc:
        why = f"{outcome_assessor.ROLE}: {exc.__class__.__name__}: {exc}"
        result.refusals.append(why)
        result.stopped_by = why
        _finish(cfg, p.where, result)
        return None
    write_json(p.where.answer(outcome_assessor.ROLE), json.loads(answer))
    kept, lost = survived(analyst.figures_used, figures_of(brief))
    result.cited = [figure.record() for figure in kept]
    for gone in lost:
        result.refusals.append(f"{outcome_assessor.ROLE}: discarded a citation, {gone}")
    result.briefs["event"] = {
        "probability": str(analyst.probability),
        "confidence": analyst.confidence,
        "reason": analyst.reason,
    }
    announce(cfg.watch, outcome_assessor.ROLE, DONE, f"probability {analyst.probability}")
    why = ""
    if not kept:
        why = (
            f"{outcome_assessor.ROLE}: not one figure from its brief survived the check, so the "
            "probability rests on nothing this cycle measured"
        )
    elif analyst.confidence < cfg.min_confidence:
        why = (
            f"{outcome_assessor.ROLE}: confidence {analyst.confidence} below the declared "
            f"{cfg.min_confidence}"
        )
    if why:
        result.refusals.append(why)
        _finish(cfg, p.where, result)
        return None
    return analyst


def _stake(p: Pass, market: Market, book: _Book, analyst: Any, edge: Any) -> CycleResult:
    """The size the caps leave, the desk's limits, then the intent."""
    cfg, result = p.cfg, p.result
    ask = book.ask
    each = ask + edge.fee
    size = cfg.size if cfg.size_quote is None else cfg.size_quote / ask
    caps = [size]
    if cfg.max_position_quote is not None:
        caps.append(cfg.max_position_quote / ask)
    if cfg.max_position is not None:
        caps.append(cfg.max_position)
    if p.equity is not None and cfg.max_risk_per_trade_bps is not None:
        # The whole stake is what is at risk, so the risk rule caps the stake itself.
        caps.append(p.equity * cfg.max_risk_per_trade_bps / BPS / each)
    size = _quantized(min(caps), cfg.size_step)
    if size <= 0:
        why = f"size: the caps leave less than one step of {cfg.size_step}"
        return _stop_before_asking(cfg, p.where, result, why)
    stake = size * each
    risk_bps = None if p.equity in (None, 0) else stake / p.equity * BPS
    if cfg.desk.bounds_a_trade and risk_bps is not None:
        held, source = _held(cfg, result)
        found = within_desk(
            cfg.desk, instrument=cfg.instrument, adding_bps=risk_bps, held=held, source=source
        )
        result.book = found
        for reason in found.reasons:
            result.refusals.append(f"desk: {reason}")
        if not found.admitted:
            _finish(cfg, p.where, result)
            return result
    take = analyst.probability
    if market.price_max is not None:
        take = min(take, market.price_max)
    result.intent = {
        "instrument": cfg.instrument,
        "target": "long",
        "setup": "event",
        "size": str(size),
        "risk_bps": None if risk_bps is None else str(risk_bps.quantize(Decimal("0.01"))),
        "confidence": analyst.confidence,
        "probability": str(analyst.probability),
        "reason": analyst.reason,
        "cited": result.cited,
        "edge_bps": str(edge.edge_bps.quantize(Decimal("0.01"))),
        "edge_source": "outcome_assessor",
        "stake": str(stake.quantize(Decimal("0.0001"))),
        "mode": cfg.mode,
        "mid": str(ask if book.bid is None else (book.bid + ask) / 2),
        "at": p.cycle,
        **_touch(p.snapshot),
        "stop_price": None,
        "take_price": str(take),
        "exits": "bounded",
        "channel": cfg.venue_key,
        "entry_role": _channel(cfg).entry_role,
    }
    announce(cfg.watch, "intent", DONE, f"buy {size} at {ask} on a probability of {take}")
    append_line(p.where.path / "intents.jsonl", result.intent, durable=True)
    return _send(cfg, p.where, result)


__all__ = ["decide_on_an_outcome"]
