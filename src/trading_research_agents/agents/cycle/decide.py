"""The cycle from the briefs to a priced proposal: the readings, the analyst, the cost, the exits.

The readings come first, read by code. The structure lead answers inside its schema, and code
checks every figure it cited, the setup against the measured regime, and its confidence. The round
trip is priced on the snapshot's own book, and the stop and the take profit are placed inside the
operator's bounds before any reviewer is paid to argue about the trade.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from trading_research_agents.agents import structure_lead
from trading_research_agents.agents.briefs import BriefError, cost_brief, structure_brief
from trading_research_agents.agents.cited import survived
from trading_research_agents.agents.cycle.asking import _ask_readable, _within_scope
from trading_research_agents.agents.cycle.briefing import (
    _context_frames,
    _context_of,
    _previous_open_interest,
    _previous_reading,
    _reading_state,
    _readings,
    _regime_line,
)
from trading_research_agents.agents.cycle.config import TARGETS
from trading_research_agents.agents.cycle.paths import read_the_paths
from trading_research_agents.agents.cycle.pricing import (
    _break_even,
    _channel,
    _claimed_edge,
    _exit_view,
    _exits,
    _held_bars,
)
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.reviewers import _before_votes, _run_reviewers
from trading_research_agents.agents.cycle.state import Pass, Step
from trading_research_agents.agents.cycle.team import check_reasons, combine, split, tasks
from trading_research_agents.agents.parallel import Task, run_tasks
from trading_research_agents.agents.progress import DONE, REFUSED, announce, timed
from trading_research_agents.agents.reviewer.tools import figures_of
from trading_research_agents.agents.roles import RoleError
from trading_research_agents.agents.scope import crossings
from trading_research_agents.execution import session
from trading_research_agents.execution.regime import misfit
from trading_research_agents.execution.stops import StopError
from trading_research_agents.records import write_json


def _stop(p: Pass, refusal: str, step: str, detail: str, *, stopped: bool = False) -> bool:
    """Refuse with a reason, announce the step, write the cycle down; the pass ends here."""
    p.result.refusals.append(refusal)
    if stopped:
        p.result.stopped_by = refusal
    announce(p.cfg.watch, step, REFUSED, detail)
    _finish(p.cfg, p.where, p.result)
    return False


def _brief(p: Pass) -> bool:
    """The readings, by code, then the structure brief the analyst decides from."""
    cfg, result = p.cfg, p.result
    p.reading_briefs = _readings(cfg, p.snapshot, result)
    context = _context_frames(cfg, result)
    result.frames = context
    try:
        p.structure = structure_brief(
            p.snapshot,
            mechanism=cfg.mechanism,
            interval_s=cfg.interval_s,
            lookback_bars=cfg.lookback_bars,
            rule=cfg.regime,
            context=context,
            previous_open_interest=_previous_open_interest(cfg.root, cfg.instrument, p.cycle),
        )
    except BriefError as exc:
        result.stopped_by = f"no structure brief: {exc}"
        _finish(cfg, p.where, result)
        return False
    shown = p.structure.record()
    last = _previous_reading(
        cfg.root,
        cfg.instrument,
        p.cycle,
        shown.get("close"),
        now=cfg.clock(),
        within_s=cfg.lookback_bars * cfg.interval_s,
    )
    if last is not None:
        shown["previous"] = last
    structure, p.flow_brief = split(shown, cfg.team)
    p.asked = _within_scope(structure_lead.ROLE, structure, result)
    write_json(p.where.brief("structure_lead"), p.asked)
    return True


def _ask_lead(p: Pass) -> bool:
    """The structure lead and its team, in parallel; the readings are written down meanwhile."""
    cfg, result = p.cfg, p.result
    states = {n: _reading_state(n, brief) for n, brief in p.reading_briefs.items()}
    announce(cfg.watch, "regime", DONE, _regime_line(cfg, p.structure, states))
    asked = p.asked
    task = Task(
        name="structure_lead",
        work=timed(
            cfg.watch,
            "structure_lead",
            lambda: _ask_readable(
                cfg,
                structure_lead.ROLE,
                asked,
                structure_lead.SCHEMA,
                structure_lead.parse,
            ),
        ),
        path=p.where.answer("structure_lead"),
    )
    members = tasks(p)
    for name, brief in p.reading_briefs.items():
        write_json(p.where.brief(name), brief)
        announce(cfg.watch, name, DONE, f"read by code: {states[name]['declared']}")
    fan = run_tasks([task, *members], max_workers=cfg.max_workers)
    result.fan = fan
    p.answers = dict(fan.answers)
    result.context = _context_of(p.reading_briefs)
    for name, error in fan.failed.items():
        result.refusals.append(f"{name}: {error}")
        if "BudgetSpentError" in error:
            result.stopped_by = f"{name}: {error}"
    if "structure_lead" not in fan.answers:
        result.stopped_by = result.stopped_by or "the structure lead did not answer"
        _finish(cfg, p.where, result)
        return False
    try:
        p.analyst = structure_lead.parse(str(fan.answers["structure_lead"]))
    except RoleError as exc:
        result.refusals.append(f"structure_lead: {exc}")
        result.stopped_by = f"structure_lead: {exc}"
        _finish(cfg, p.where, result)
        return False
    result.analyst = p.analyst
    return True


def _check_answer(p: Pass) -> bool:
    """What code checks in the answer before anything is priced."""
    cfg, result, analyst = p.cfg, p.result, p.analyst
    kept, lost = survived(analyst.figures_used, figures_of(p.asked))
    result.cited = [figure.record() for figure in kept]
    for gone in lost:
        result.refusals.append(f"structure_lead: discarded a citation, {gone}")
    for finding in crossings(structure_lead.ROLE, {"reason": analyst.reason}):
        result.refusals.append(f"structure_lead: {finding}; no rule is read from an answer")
    if analyst.target == "flat":
        result.refusals.append("structure_lead: flat, so there is nothing to price")
        announce(cfg.watch, "structure_lead", DONE, "flat")
        _finish(cfg, p.where, result)
        return False
    if cfg.spot and analyst.target == "short":
        why = (
            f"market: {cfg.instrument} is a spot market; a short would sell coins the account "
            "does not hold, so it is not taken"
        )
        return _stop(p, why, "structure_lead", "short on a spot market")
    if not kept:
        # The role that decides is the one role whose citations have teeth. A direction that cannot
        # point at a figure in its own brief is a direction that came from somewhere else.
        why = (
            "structure_lead: not one figure from its own brief survived the check, so the "
            "direction rests on nothing this cycle measured"
        )
        return _stop(p, why, "structure_lead", "cited nothing")
    if analyst.setup == "no_setup":
        why = (
            f"structure_lead: {analyst.target} with no setup named; a direction without a shape "
            "behind it is not something this desk can act on or review afterwards"
        )
        return _stop(p, why, "structure_lead", "no setup")
    return True


def _fits_regime(p: Pass) -> bool:
    cfg, result, analyst, technical = p.cfg, p.result, p.analyst, p.structure
    features = technical.features
    misfit_why = misfit(
        analyst.setup,
        analyst.target,
        technical.regime,
        zscore=features.zscore,
        window_move_bps=features.window_move_bps,
        position_in_range=features.position_in_range,
        trend_strength=features.trend_strength,
        variance_ratio=features.variance_ratio,
        fade_below=float(cfg.regime.fade_below_variance_ratio),
        structure=None if features.structure is None else features.structure.record(),
    )
    if misfit_why and cfg.regime.setups_must_fit:
        why = f"regime: {misfit_why} ([regime].setups_must_fit)"
        return _stop(p, why, "structure_lead", "the setup does not fit the regime")
    if misfit_why:
        result.refusals.append(f"regime: {misfit_why}; allowed, setups_must_fit is false")
    if analyst.confidence < cfg.min_confidence:
        floor = cfg.min_confidence
        why = f"structure_lead: confidence {analyst.confidence} below the declared {floor}"
        return _stop(p, why, "structure_lead", "below the confidence floor")
    return True


def _price(p: Pass) -> bool:
    """The round trip on the snapshot's own book, at the base size."""
    cfg, result, snapshot = p.cfg, p.result, p.snapshot
    # After the book, which is the first thing a price needs and the first refusal to name.
    if not p.no_contract and "orderbook" in snapshot.values and "funding" not in snapshot.values:
        # A perpetual pays funding while it is held. Priced without the rate, the round trip would
        # look cheaper than it is, so the trade is refused rather than priced on a missing term.
        why = snapshot.refused.get("funding", "no funding chain is declared in data.toml")
        result.refusals.append(
            f"cost: the funding rate for {cfg.instrument} could not be read ({why}); a contract's "
            "round trip is not priced without it"
        )
        result.stopped_by = "cost: no funding rate"
        announce(cfg.watch, "cost", REFUSED, "no funding rate to price the hold")
        _finish(cfg, p.where, result)
        return False
    try:
        p.costs = price_at(p, p.base)
    except BriefError as exc:
        result.refusals.append(f"cost: {exc}")
        result.stopped_by = f"cost: {exc}"
        _finish(cfg, p.where, result)
        return False
    result.briefs["cost"] = p.costs.record()
    write_json(p.where.brief("cost"), p.costs.record())
    announce(cfg.watch, "cost", DONE, f"round trip {p.costs.cost.round_trip_bps:.2f} bps")
    return True


def price_at(p: Pass, size: Decimal) -> Any:
    """The round trip of this cycle's proposal at `size`; BriefError when it cannot be priced."""
    cfg = p.cfg
    return cost_brief(
        p.snapshot,
        channel=_channel(cfg),
        size=size,
        direction=TARGETS[p.analyst.target],
        horizon_bars=_held_bars(cfg, p.analyst.horizon_bars),
        bar_interval_s=cfg.bar_interval_s,
        settlement_interval_s=cfg.settlement_interval_s,
        latency_slippage_bps=cfg.latency_slippage_bps,
    )


def _place_exits(p: Pass) -> bool:
    """Where the trade is wrong and where it is done, settled before anyone votes on it."""
    cfg, result, analyst = p.cfg, p.result, p.analyst
    mid = p.costs.cost.mid
    try:
        p.exits, p.placement = _exits(
            cfg,
            mid,
            analyst.target,
            snapshot=p.snapshot,
            horizon_bars=_held_bars(cfg, analyst.horizon_bars),
            stop_price=analyst.stop_price,
            target_price=analyst.target_price,
        )
    except StopError as exc:
        # A stop that cannot be placed inside the operator's bounds is not replaced by a guess.
        result.refusals.append(f"exit: {exc}")
        announce(cfg.watch, "exit", REFUSED, str(exc))
        _finish(cfg, p.where, result)
        return False
    if p.equity_hours:
        hold = timedelta(seconds=_held_bars(cfg, analyst.horizon_bars) * cfg.bar_interval_s)
        late = session.refusal(cfg.clock(), hold, cfg.equity_closed)
        if late:
            return _stop(p, f"session: {late}", "session", "the hold would run past the close")
    view: dict[str, Any] = dict(_exit_view(mid, analyst.target, p.exits))
    if p.placement is not None:
        view["measured"] = p.placement.record()
    result.exits = view
    return _exits_pay(p, view)


def _exits_pay(p: Pass, view: dict[str, Any]) -> bool:
    """A take profit inside the round trip, or a break even no desk reaches, refuses."""
    cfg, result = p.cfg, p.result
    round_trip = p.costs.cost.round_trip_bps
    take_pnl = view.get("take_pnl_bps")
    if take_pnl is not None and Decimal(take_pnl) <= round_trip:
        # A take profit inside the round trip turns the trade's best outcome into a loss.
        why = (
            f"exit: the take profit is {take_pnl} bps away and the round trip costs "
            f"{round_trip:.2f} bps, so a winner would lose money"
        )
        return _stop(p, why, "exit", "take profit inside the round trip")
    even = _break_even(view, round_trip)
    bound = cfg.targets.max_break_even_win_rate
    if even is not None and Decimal(even) > bound:
        why = (
            f"exit: {Decimal(even) * 100:.1f}% of trades like this would have to reach the take "
            f"profit to cover the stops and the {round_trip:.2f} bps round trip; "
            f"[targets].max_break_even_win_rate is {bound * 100:.0f}%"
        )
        return _stop(p, why, "exit", f"break even at {Decimal(even) * 100:.1f}%")
    p.edge, p.edge_source = _claimed_edge(cfg, p.placement)
    result.briefs["edge"] = {"edge_bps": str(p.edge), "source": p.edge_source}
    if view.get("stop_pnl_bps") is not None:
        announce(
            cfg.watch,
            "exit",
            DONE,
            f"stop {view['stop_pnl_bps']} bps, take {view.get('take_pnl_bps')} bps"
            + (f", break even {Decimal(even) * 100:.0f}%" if even is not None else ""),
        )
    return True


def _validate(p: Pass) -> bool:
    """The declared stances, unless code is already certain to refuse whatever they say."""
    cfg, result = p.cfg, p.result
    if not cfg.reviewers:
        return True
    early = _before_votes(cfg, result, p.costs, p.edge)
    if early:
        # Refused by code that does not read a single vote, so asking three models first
        # would only pay for an answer nothing can use.
        result.refusals.append(f"{early}; the reviewers were not asked")
        announce(cfg.watch, early.partition(":")[0], REFUSED, early.partition(": ")[2])
        _finish(cfg, p.where, result)
        return False
    _run_reviewers(cfg, p.where, result, asked=p.asked, analyst=p.analyst)
    silent = [
        name
        for name in (f"reviewer_{stance}" for stance in cfg.reviewers)
        if "error" in (result.context.get(name) or {"error": "not asked"})
    ]
    if silent and cfg.on_missing_reviewer == "refuse" and result.stopped_by is None:
        why = (
            f"reviewers: {', '.join(silent)} did not answer, and "
            "[agents].on_missing_reviewer is refuse; a check you declared did not happen"
        )
        return _stop(p, why, "reviewers", f"{', '.join(silent)} did not answer", stopped=True)
    if result.stopped_by is not None:
        _finish(cfg, p.where, result)
        return False
    return True


DECIDING: tuple[Step, ...] = (
    _brief,
    _ask_lead,
    _check_answer,
    combine,
    _fits_regime,
    check_reasons,
    _price,
    _place_exits,
    read_the_paths,
    _validate,
)

__all__ = ["DECIDING", "price_at"]
