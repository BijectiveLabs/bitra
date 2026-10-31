"""The cycle from the snapshot to the first question: what is free to know before a model is paid.

Code takes a snapshot, asks the trigger whether anything has moved, checks the session, converts a
size declared in quote currency, and reads the account. Each of these can end the cycle before a
single token is spent, and each says why.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trading_research_agents.agents.alarms import DAILY_LOSS_MARK, DRAWDOWN_MARK
from trading_research_agents.agents.briefs import data_brief
from trading_research_agents.agents.cycle.config import CONTRACT_KINDS, CycleConfig
from trading_research_agents.agents.cycle.decide import DECIDING
from trading_research_agents.agents.cycle.gates import GATES
from trading_research_agents.agents.cycle.outcome import decide_on_an_outcome
from trading_research_agents.agents.cycle.pricing import _held, _prices, _quantized
from trading_research_agents.agents.cycle.record import _finish, _send, _stop_before_asking
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.cycle.state import Pass, through
from trading_research_agents.agents.progress import DONE, HELD, RUNNING, SKIPPED, announce
from trading_research_agents.execution import session
from trading_research_agents.execution.targets import intents_today
from trading_research_agents.execution.trigger import Wake, last_seen, spread_of, worth_asking
from trading_research_agents.store.cycles import CycleDir
from trading_research_agents.store.snapshot import cycle_id, take_snapshot
from trading_research_agents.trading.capital import (
    EQUITY_PEAK,
    day_loss_bps,
    drawdown_bps,
    sizing_equity,
)


def run_cycle(cfg: CycleConfig) -> CycleResult:
    """Take one snapshot, ask the agents, price the trade, and write the cycle down."""
    p = _open(cfg)
    if not through(p, (_trigger, _session, _size, _capital, _wait_while_open)):
        return p.result
    if cfg.market is not None and cfg.market.outcome:
        return decide_on_an_outcome(p, cfg.market)
    if not through(p, (*DECIDING, *GATES)):
        return p.result
    return _send(cfg, p.where, p.result)


def _open(cfg: CycleConfig) -> Pass:
    cycle = cycle_id(cfg.clock(), cfg.instrument)
    where = CycleDir(root=cfg.root, cycle=cycle).make()
    spent_before = cfg.usage.cost
    calls_before = len(cfg.usage.records)
    no_contract = cfg.spot or (cfg.market is not None and cfg.market.outcome)
    kinds = [k for k in cfg.chains if not (no_contract and k in CONTRACT_KINDS)]
    announce(cfg.watch, "data", RUNNING, f"{len(kinds)} kind(s)")
    # What the operator declared per kind in data.toml, then the window this cycle needs. A venue
    # quirk lives in that file, so dropping it here would be dropping the operator's own decision.
    options = {kind: dict(cfg.data_options.get(kind, {})) for kind in kinds}
    options.setdefault("ohlcv", {}).update(
        {"interval_s": cfg.interval_s, "limit": max(cfg.lookback_bars, 2)}
    )
    snapshot = take_snapshot(cfg.instrument, cfg.chains, cycle=cycle, kinds=kinds, options=options)
    snapshot.write(where.snapshot)
    result = CycleResult(cycle=cycle, instrument=cfg.instrument, snapshot=snapshot, path=where.path)
    result.briefs["data"] = data_brief(snapshot).record()
    result.briefs["mode"] = cfg.mode
    for kind, reason in snapshot.refused.items():
        result.refusals.append(f"data {kind}: {reason}")
    for kind in snapshot.stale:
        result.refusals.append(f"data {kind}: used while stale, because the config allows it")
    answered = len(kinds) - len(snapshot.refused)
    announce(cfg.watch, "data", DONE, f"{answered} of {len(kinds)} kind(s) answered")
    bid, ask = _prices(snapshot)
    mid_now, spread_now = spread_of(bid, ask)
    result.seen = {
        "mid": None if mid_now is None else str(mid_now),
        "spread_bps": None if spread_now is None else str(spread_now),
    }
    return Pass(
        cfg=cfg,
        where=where,
        result=result,
        snapshot=snapshot,
        cycle=cycle,
        no_contract=bool(no_contract),
        equity_hours=cfg.asset_class == "equity" and cfg.equity_session == "regular",
        spent_before=spent_before,
        calls_before=calls_before,
        base=cfg.size,
        equity=cfg.equity,
        mid_now=mid_now,
        spread_now=spread_now,
    )


def _trigger(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    if cfg.forced and not cfg.trigger.idle:
        result.wake = Wake(
            ask=True, reasons=("the operator asked with --now, so the trigger was not applied",)
        )
        announce(cfg.watch, "trigger", SKIPPED, "asked with --now")
        return True
    if cfg.trigger.idle:
        return True
    wake = worth_asking(
        cfg.trigger,
        mid=p.mid_now,
        spread_bps=p.spread_now,
        seen=last_seen(cfg.root, instrument=cfg.instrument, min_move_bps=cfg.trigger.min_move_bps),
        now=cfg.clock(),
        round_trips_today=intents_today(cfg.root, at=cfg.clock(), instrument=cfg.instrument),
        max_round_trips_per_day=cfg.targets.max_round_trips_per_day,
    )
    result.wake = wake
    if not wake.ask:
        result.held = True
        for reason in wake.reasons:
            result.refusals.append(f"trigger: {reason}")
        announce(cfg.watch, "trigger", HELD, wake.reasons[0])
        _finish(cfg, p.where, result)
        return False
    announce(
        cfg.watch,
        "trigger",
        DONE,
        "" if wake.moved_bps is None else f"moved {wake.moved_bps} bps",
    )
    return True


def _session(p: Pass) -> bool:
    cfg = p.cfg
    if not p.equity_hours or session.is_open(cfg.clock(), cfg.equity_closed):
        return True
    why = f"session: {session.refusal(cfg.clock(), timedelta(0), cfg.equity_closed)}"
    p.result.refusals.append(why)
    p.result.held = True
    announce(cfg.watch, "session", HELD, "the underlying's exchange is closed")
    _finish(cfg, p.where, p.result)
    return False


def _size(p: Pass) -> bool:
    cfg = p.cfg
    if cfg.size_quote is None:
        return True
    # Converted before anyone is asked: a size that cannot exist is not worth a model call.
    if p.mid_now is None:
        why = (
            "size: [run].size_quote is declared and this snapshot has no two-sided book, so "
            "there is no price to convert it at"
        )
        _stop_before_asking(cfg, p.where, p.result, why)
        return False
    p.base = _quantized(cfg.size_quote / p.mid_now, cfg.size_step)
    p.result.briefs["size"] = {
        "size_quote": str(cfg.size_quote),
        "mid": str(p.mid_now),
        "size": str(p.base),
        "step": str(cfg.size_step),
    }
    if p.base <= 0:
        why = (
            f"size: {cfg.size_quote} of quote at {p.mid_now} is less than one step of "
            f"{cfg.size_step}, so there is no size to trade"
        )
        _stop_before_asking(cfg, p.where, p.result, why)
        return False
    announce(cfg.watch, "size", DONE, f"{cfg.size_quote} of quote is {p.base} at {p.mid_now}")
    return True


def _capital(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    if cfg.account is None:
        return True
    try:
        held_now = cfg.account()
    except Exception as exc:
        _stop_before_asking(cfg, p.where, result, f"capital: {exc}", step="capital")
        return False
    p.equity = sizing_equity(cfg.equity, held_now.equity)
    result.briefs["equity"] = {
        "declared": None if cfg.equity is None else str(cfg.equity),
        "venue": str(held_now.equity),
        "free": str(held_now.free),
        "used": None if p.equity is None else str(p.equity),
    }
    why = _day_loss(p, held_now.equity) or _drawdown(p, held_now.equity)
    if why:
        _stop_before_asking(cfg, p.where, result, why, step="capital")
        return False
    announce(cfg.watch, "capital", DONE, f"equity {held_now.equity:.2f}, used {p.equity}")
    return True


def _day_loss(p: Pass, venue_equity: Decimal) -> str:
    cfg = p.cfg
    if cfg.daily_loss_limit_bps is None:
        return ""
    down = day_loss_bps(cfg.root, cfg.clock(), venue_equity)
    p.result.briefs["equity"]["day_loss_bps"] = str(down)
    if down < cfg.daily_loss_limit_bps:
        return ""
    return (
        f"{DAILY_LOSS_MARK} {down} bps since the day began, at or past "
        f"[controls].daily_loss_limit_bps {cfg.daily_loss_limit_bps}; no new entry "
        "today, and open positions keep their exits"
    )


def _drawdown(p: Pass, venue_equity: Decimal) -> str:
    cfg = p.cfg
    if cfg.max_drawdown_bps is None:
        return ""
    fallen, peak = drawdown_bps(cfg.root, cfg.clock(), venue_equity)
    p.result.briefs["equity"]["drawdown_bps"] = str(fallen)
    p.result.briefs["equity"]["peak"] = str(peak)
    if fallen < cfg.max_drawdown_bps:
        return ""
    then = "open positions keep their exits"
    if cfg.drawdown_kill is not None:
        cfg.drawdown_kill.parent.mkdir(parents=True, exist_ok=True)
        cfg.drawdown_kill.write_text(
            f"armed by [controls].max_drawdown_bps at {cfg.clock().isoformat()}\n",
            encoding="utf-8",
        )
        then = f"the kill switch at {cfg.drawdown_kill} is armed to flatten and stop"
    return (
        f"{DRAWDOWN_MARK} of {fallen} bps from the peak of {peak}, at or past "
        f"[controls].max_drawdown_bps {cfg.max_drawdown_bps}; no new entry until "
        f"the peak file {EQUITY_PEAK} is cleared, and {then}"
    )


def _wait_while_open(p: Pass) -> bool:
    cfg, result = p.cfg, p.result
    if not cfg.wait_while_open or cfg.held is None:
        return True
    rows, _source = _held(cfg, result)
    if not any(row.instrument == cfg.instrument for row in rows):
        return True
    why = (
        f"open: a position on {cfg.instrument} is open at the venue, so the next entry "
        "waits for its exits; nothing was asked"
    )
    result.refusals.append(why)
    result.held = True
    announce(cfg.watch, "desk", HELD, "a position is already open")
    _finish(cfg, p.where, result)
    return False


__all__ = ["run_cycle"]
