"""`tra run`: one cycle or a schedule of them on live data, with every gate the desk declared."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from time import sleep
from time import time as wall_clock
from typing import Any

from trading_research_agents.agents.context import (
    declared_in,
)
from trading_research_agents.agents.cycle import budget_spent
from trading_research_agents.agents.risk_manager.run import Overrides as RiskOverrides
from trading_research_agents.cli import shutdown
from trading_research_agents.cli.auditing import Auditing
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _completer_for_role,
    _desk_data,
    _load_env,
    _refresh_news,
    _ui,
    _watching,
    emit,
)
from trading_research_agents.cli.cycle_report import _cycle_report, _cycle_steps, _cycle_view
from trading_research_agents.cli.fees import _fill_account_fees
from trading_research_agents.cli.text import (
    desk_judge,
)
from trading_research_agents.cli.universe_pass import (
    _held_reader,
    _keeper,
    _screen_report,
    _screen_view,
    _sweep,
)
from trading_research_agents.cli.venue_access import (
    _DISCOVERED,
    _execution_client,
    _on_the_execution_venue,
    _symbol_for,
    _venue_hand,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.records import read_lines
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)
from trading_research_agents.trading.config import RunSettings


def held_back_by(cfg: Any, out: Path, now: datetime) -> str:
    """Why this pass takes no new entry and asks no model, or empty. Each is the client's own line,
    counted per UTC day from the records on disk, so a restart cannot reset it."""
    from trading_research_agents.execution.targets import intents_today
    from trading_research_agents.llm.usage import spent_since

    if cfg.pause_switch is not None and cfg.pause_switch.exists():
        why = cfg.pause_switch.read_text(encoding="utf-8", errors="replace").strip()
        return (
            f"paused by {cfg.pause_switch}"
            + (f" ({why[:120]})" if why else "")
            + ("; `tra pause --off` resumes")
        )
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    a = cfg.agents
    if a is not None and (a.daily_token_budget or a.daily_cost_budget):
        ledgers = (out / "usage.jsonl", out.parent / "audits" / "usage.jsonl")
        tokens, cost = spent_since(ledgers, midnight.timestamp())
        if a.daily_token_budget and tokens >= a.daily_token_budget:
            return f"daily token budget spent: {tokens} of {a.daily_token_budget} since 00:00 UTC"
        if a.daily_cost_budget and cost >= a.daily_cost_budget:
            return f"daily cost budget spent: {cost:.4f} of {a.daily_cost_budget} since 00:00 UTC"
    cap = cfg.limits.max_trades_per_day
    if cap is not None:
        sent = intents_today(out, at=now, instrument=None)
        if sent >= cap:
            return (
                f"{sent} trade(s) today across every instrument; [controls].max_trades_per_day "
                f"is {cap}"
            )
    streak_cap = cfg.limits.max_losing_streak
    if streak_cap is not None:
        streak = losing_streak_today(out / "round_trips.jsonl", midnight)
        if streak >= streak_cap:
            return (
                f"{streak} losing round trips in a row today; [controls].max_losing_streak is "
                f"{streak_cap}"
            )
    return ""


def losing_streak_today(path: Path, midnight: datetime) -> int:
    """Losing round trips in a row, newest first, closed since `midnight`. A trip with no result is
    passed over; a winner ends the count."""

    streak = 0
    for trip in reversed(read_lines(path) if path.is_file() else []):
        try:
            closed = datetime.fromisoformat(str(trip.get("at")))
        except ValueError:
            continue
        if closed < midnight:
            break
        if trip.get("net") is None:
            continue
        if Decimal(str(trip["net"])) >= 0:
            break
        streak += 1
    return streak


@dataclass
class Schedule:
    """When the next cycle runs, and what ends the run.

    A desk does not want a cycle every few seconds; it wants one when a bar has closed and the
    figures have changed. So the wait lands on the interval's own boundary, and everything that
    should end a run - the budget, the kill switch, the count asked for, a keyboard - ends it by
    name rather than by the process stopping quietly.
    """

    every_s: float | None = None
    cycles: int | None = None
    kill_switch: Path | None = None
    clock: Callable[[], float] = wall_clock
    rest: Callable[[float], None] = sleep

    @property
    def looping(self) -> bool:
        return self.every_s is not None

    @property
    def every_label(self) -> str:
        every = int(self.every_s or 0)
        for size, unit in ((3600, "h"), (60, "m")):
            if every >= size and every % size == 0:
                return f"{every // size}{unit}"
        return f"{every}s"

    def stop_reason(self, result: Any, ran: int) -> str:
        if not self.looping:
            return "one cycle"
        # A pass that chose nothing has no result to read, and that is not a reason to stop.
        stopped = getattr(result, "stopped_by", None)
        if stopped and budget_spent(str(stopped)):
            return f"the budget: {stopped}"
        if self.kill_switch is not None and self.kill_switch.exists():
            return f"the kill switch at {self.kill_switch}"
        if self.cycles is not None and ran >= self.cycles:
            return f"the {self.cycles} pass(es) asked for"
        return ""

    def left(self) -> float:
        """Seconds to the next boundary, so a cycle reads a bar that has finished happening."""
        every = self.every_s or 0.0
        if every <= 0:
            return 0.0
        return round(every - (self.clock() % every), 3)

    def announce(self, left: float) -> str:
        """The wait as an operator reads it: when it ends on the clock, and why it is short."""
        ends = datetime.fromtimestamp(self.clock() + left, tz=UTC).strftime("%H:%M:%S")
        minutes, seconds = divmod(int(left + 0.999), 60)
        hours, minutes = divmod(minutes, 60)
        shown = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"
        return (
            f"scheduler: next pass at {ends} UTC, in {shown}. "
            f"Passes land on the clock, every {self.every_label}, "
            "not a full interval after the last one"
        )

    def wait(self) -> float:
        """Sleep to the next boundary and say how long that was."""
        left = self.left()
        if left > 0:
            self.rest(left)
        return left


def run_cycle_command(args: argparse.Namespace) -> int:
    """One cycle, end to end, from the files in the project."""
    from trading_research_agents.agents.cycle import CycleConfig
    from trading_research_agents.execution.targets import reward_risk
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        RateWindow,
        UsageLedger,
    )

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("a cycle needs [strategy].agent and [agents] in strategy.toml")
    filled, _ = _fill_account_fees(args.envelope, cfg, dict(os.environ), datetime.now(UTC))
    if filled and not args.json:
        sys.stdout.write("\n".join(filled) + "\n")
    a = cfg.agents
    magnitude = a.magnitude_bps
    if magnitude is None:
        raise ConfigError("the claimed edge: [agents].magnitude_bps in strategy.toml")
    data_cfg = _desk_data(args.data_config, cfg, args.mode or cfg.execution.mode)
    chains = {declared.kind: data_cfg.chain(declared.kind) for declared in data_cfg.kinds}
    instrument = args.instrument or cfg.run.instrument
    out = args.out or Path("runs") / "cycles"
    beside = args.config.resolve().parent
    overrides = {
        name: path
        for name, path in (
            ("macro", args.macro),
            ("onchain", args.onchain),
            ("sentiment", args.sentiment),
            ("derivatives", args.derivatives),
            ("news", args.news),
        )
        if path is not None
    }
    declared = declared_in(beside, overrides=overrides)
    series = {role: tuple(paths) for role, paths in declared.series.items()}
    news = tuple(declared.text)
    if not news and (beside / "events.csv").is_file():
        news = (beside / "events.csv",)
    # A declared roster wins; otherwise every reading with something to read is read.
    readings = a.readings or declared.readings
    mode = args.mode or cfg.execution.mode
    # A hand for the instrument this run starts on, so a misconfigured mandate is refused before any
    # data is pulled. A pass over a universe builds another one for whichever instrument it chose:
    # one hand per instrument, because a hand carries the symbol an order goes out under.
    hand = _venue_hand(args, cfg, beside, instrument) if mode == "venue" else None
    # One order log for the whole run, so [controls].max_orders_per_minute is the desk's.
    from trading_research_agents.trading.dispatch import OrderRate

    rate = OrderRate(cfg.limits.max_orders_per_minute)
    keeper = _keeper(args, cfg, beside, out, instrument, rate) if mode == "venue" else None
    usage = UsageLedger(out / "usage.jsonl")
    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {model: Pricing(i, o) for model, (i, o) in a.pricing.items()}
    metered: dict[str, MeteredCompleter] = {}
    window = RateWindow()

    def completer_for(role: str) -> MeteredCompleter:
        # One budget and one rate window for the whole desk, one client per role: a role may run
        # on a cheaper model without the others paying for it.
        if role not in metered:
            settings = a.for_role(role)
            metered[role] = MeteredCompleter(
                _completer_for_role(a, role),
                role=role,
                provider=settings.provider,
                limits=limits,
                ledger=usage,
                pricing=pricing,
                window=window,
            )
        return metered[role]

    facts = _market_facts(cfg, beside, instrument, hand)
    cycle_cfg = CycleConfig(
        instrument=instrument,
        envelope=load_envelope(args.envelope),
        venue_key=cfg.run.venue_key,
        chains=chains,
        completer_for=completer_for,
        usage=usage,
        root=out,
        mechanism=a.mechanism,
        size=cfg.run.size,
        size_quote=cfg.run.size_quote,
        magnitude_bps=magnitude,
        bar_interval_s=cfg.run.bar_interval_s,
        lookback_bars=a.lookback_bars,
        interval_s=cfg.run.bar_interval_s,
        settlement_interval_s=cfg.run.settlement_interval_s,
        latency_slippage_bps=cfg.run.slippage_bps,
        margin_bps=args.margin if args.margin is not None else Decimal(0),
        min_confidence=a.min_confidence,
        mode=mode,
        place=hand,
        spot=facts[0],
        asset_class=facts[1],
        market=facts[2],
        equity_session=cfg.execution.equity_session,
        equity_closed=tuple(date.fromisoformat(d) for d in cfg.execution.equity_closed),
        matrix=cfg.confluence,
        conviction=cfg.conviction,
        max_position=cfg.limits.max_position,
        max_position_quote=cfg.limits.max_position_quote,
        equity=cfg.run.equity,
        size_step=cfg.run.step_for(instrument),
        validators=a.validators,
        on_missing_validator=a.on_missing_validator,
        context_intervals=cfg.run.context_intervals,
        hold_limit=cfg.execution.hold_limit,
        timeout_bars=cfg.exit_rule.timeout_bars,
        regime=cfg.regime,
        targets=cfg.targets,
        desk=cfg.desk,
        held=_held_reader(cfg, out, mode, args, beside),
        wait_while_open=mode == "venue" and not cfg.execution.adopt_open_position,
        daily_loss_limit_bps=cfg.limits.daily_loss_limit_bps if mode == "venue" else None,
        max_drawdown_bps=cfg.limits.max_drawdown_bps if mode == "venue" else None,
        sizing=cfg.sizing,
        # Flattening is the kill switch's job; the cycle only arms it.
        drawdown_kill=cfg.kill_switch if cfg.limits.on_drawdown == "flatten" else None,
        trigger=cfg.trigger,
        forced=bool(args.now),
        max_risk_per_trade_bps=cfg.limits.max_risk_per_trade_bps,
        reward_to_risk=reward_risk(
            stop_frac=cfg.exit_rule.stop_frac,
            take_frac=cfg.exit_rule.take_profit_frac,
        ),
        stop_frac=cfg.exit_rule.stop_frac,
        take_frac=cfg.exit_rule.take_profit_frac,
        volatility_exit=cfg.exit_rule.volatility,
        data_options={declared.kind: dict(declared.options) for declared in data_cfg.kinds},
        series=series,
        news=news,
        readings=readings,
        team=a.team,
        judge=desk_judge(args.data_config),
    )
    schedule = Schedule(every_s=args.every, cycles=args.cycles, kill_switch=cfg.kill_switch)
    spine = _cycle_steps(cycle_cfg)
    # The auditors keep their own ledger, so what the desk spent deciding stays separable from what
    # it spent looking at itself.
    audits = Auditing(
        policy=cfg.audit,
        settings=a,
        config=args.config,
        runs=out,
        limits=cfg.limits,
        kill_switch=cfg.kill_switch,
    )
    ran = 0
    cleared = CLEARED
    # What the models have already cost since this desk last wrote an intent, per instrument. A
    # refused cycle's tokens are not free: they are carried forward and charged to whatever trade
    # that instrument eventually produces.
    carried: dict[str, Decimal] = {}
    mid_cycle = False
    passes = 0
    lock = shutdown.claim(out) if mode == "venue" else None
    shutdown.journal(
        out,
        "started",
        every_s=args.every,
        cycles=args.cycles,
        mode=mode,
        instruments=list(cfg.universe.instruments) or [instrument],
    )
    try:
        with shutdown.as_interrupt():
            while True:
                if keeper is not None:
                    armed = cfg.kill_switch is not None and cfg.kill_switch.exists()
                    kept = keeper.run(kill=armed)
                    if kept and not args.json:
                        sys.stdout.write("\n".join(kept) + "\n")
                        sys.stdout.flush()
                    if armed:
                        stop = f"the kill switch at {cfg.kill_switch}, after flattening"
                        emit(
                            {"cycles": ran, "passes": passes, "stopped_by": stop},
                            args.json,
                            [f"scheduler: stopped by {stop}"],
                        )
                        shutdown.journal(out, "stopped", stopped_by=stop, cycles=ran)
                        return cleared
                refreshed = _refresh_sources(declared.classes.values())
                if refreshed and not args.json:
                    sys.stdout.write("\n".join(refreshed) + "\n")
                    sys.stdout.flush()
                held_back = held_back_by(cfg, out, datetime.now(UTC))
                chosen: tuple[str, ...] = ()
                screened: Any = None
                if held_back:
                    emit(
                        {"held_back": held_back},
                        args.json,
                        [f"desk: {held_back}; open positions keep their exits, nothing is asked"],
                    )
                else:
                    chosen, screened = _sweep(
                        cfg, cycle_cfg, args, chains=chains, out=out, ask=completer_for
                    )
                if screened is not None:
                    found, said = _screen_report(screened)
                    emit(found, args.json, said, partial(_screen_view, found))
                result: Any = None
                stop = ""
                for instrument in chosen:
                    moment = datetime.now(UTC)
                    guard = RiskOverrides.load(out, moment)
                    paused = guard.pause_of(instrument, moment)
                    if paused is not None:
                        emit(
                            {"instrument": instrument, "paused_until": paused.isoformat()},
                            args.json,
                            [f"risk manager: {instrument} paused until {paused:%H:%M}Z; not asked"],
                        )
                        continue
                    multiple = guard.multiple(moment)
                    before = usage.cost
                    held = carried.get(instrument, Decimal(0))
                    sends = (
                        _venue_hand(args, cfg, beside, instrument, rate)
                        if mode == "venue"
                        else None
                    )
                    mid_cycle = True
                    chosen_facts = _market_facts(cfg, beside, instrument, sends)
                    result = _one_cycle(
                        replace(
                            cycle_cfg,
                            instrument=instrument,
                            spot=chosen_facts[0],
                            asset_class=chosen_facts[1],
                            market=chosen_facts[2],
                            size_step=_step_for(cfg, instrument),
                            # The risk manager's cut, if one is in force; the client's caps apply
                            # after it as always.
                            size=cycle_cfg.size * multiple,
                            size_quote=(
                                None
                                if cycle_cfg.size_quote is None
                                else cycle_cfg.size_quote * multiple
                            ),
                            # A unit cap was written for [run].instrument; with a quote cap
                            # declared, other instruments are held to that one alone.
                            max_position=(
                                cfg.limits.max_position
                                if instrument == cfg.run.instrument
                                or cfg.limits.max_position_quote is None
                                else None
                            ),
                            carried_spend=held,
                            place=sends,
                            account=None if sends is None else sends.account,
                        ),
                        usage,
                        spine,
                        args,
                    )
                    mid_cycle = False
                    carried[instrument] = (
                        Decimal(0) if result.acted else held + (usage.cost - before)
                    )
                    payload, lines = _cycle_report(result, usage, mode)
                    emit(payload, args.json, lines, partial(_cycle_view, payload))
                    ran += 1
                    audits.after(payload, ran, as_json=args.json)
                    cleared = CLEARED if result.stopped_by is None else REFUSED
                    # Inside a pass only the budget and the kill switch end the run.
                    stop = schedule.stop_reason(result, 0)
                    if stop:
                        break
                # A pass counts whether or not the trigger let a cycle through: on a quiet market
                # `--cycles 6` would otherwise run for as long as the market stays quiet.
                passes += 1
                if not stop:
                    stop = schedule.stop_reason(result, passes)
                if stop:
                    if keeper is not None and stop.startswith("the kill switch"):
                        # The switch exists to get flat, not only to stop deciding.
                        kept = keeper.run(kill=True)
                        if kept and not args.json:
                            sys.stdout.write("\n".join(kept) + "\n")
                    if schedule.looping:
                        emit(
                            {"cycles": ran, "passes": passes, "stopped_by": stop},
                            args.json,
                            [
                                f"scheduler: stopped by {stop}, after {passes} pass(es) and "
                                f"{ran} cycle(s)"
                            ],
                        )
                    shutdown.journal(out, "stopped", stopped_by=stop, cycles=ran, passes=passes)
                    return cleared
                # Said before the wait, not after it: an operator looking at the screen in the
                # meantime needs to know the desk is waiting rather than stuck.
                left = schedule.left()
                if not args.json:
                    sys.stdout.write(schedule.announce(left) + "\n")
                    sys.stdout.flush()
                if _watching(args.json) and left > 0:
                    _ui().countdown(left)
                else:
                    schedule.rest(left)
    except KeyboardInterrupt as exc:
        # Ctrl-C, a service manager's SIGTERM, a dropped SSH session: one way out, said by name and
        # written down. Whatever a cycle had already put on disk stays there.
        why = shutdown.stopped_by(exc)
        if mid_cycle:
            why = f"{why}, in the middle of a cycle"
        emit(
            {"cycles": ran, "stopped_by": why},
            args.json,
            [f"scheduler: stopped by {why}, after {ran} cycle(s)"],
        )
        shutdown.journal(out, "stopped", stopped_by=why, cycles=ran)
        # Stopping between cycles is the ordinary way a desk ends; stopping inside one is not.
        return REFUSED if mid_cycle else cleared
    finally:
        if lock is not None:
            shutdown.release(lock)


def _step_for(cfg: Any, instrument: str) -> Decimal:
    """The venue's size step: declared first, then what discovery read from the venue."""
    if instrument in cfg.run.size_steps:
        return Decimal(cfg.run.size_steps[instrument])
    found = _DISCOVERED.get(instrument)
    if found is not None:
        return Decimal(found["step"])
    if cfg.run.size_step == RunSettings.__dataclass_fields__["size_step"].default:
        # Nothing declared for this instrument: ask the venue orders go to what its step is.
        stated = _venue_step(cfg, instrument)
        if stated is not None:
            return stated
    return Decimal(cfg.run.step_for(instrument))


def _venue_step(cfg: Any, instrument: str) -> Decimal | None:
    """The step in the base the execution venue states for this instrument, or nothing."""
    from trading_research_agents.data.sources.discover import market_step

    try:
        symbol = _on_the_execution_venue(cfg, instrument)
        client = _execution_client(cfg)
        tick = getattr(client, "precisionMode", 4) == 4
        return market_step(client.markets[symbol], tick=tick)
    except Exception:
        return None


def _one_cycle(cfg: Any, usage: Any, spine: list[str], args: argparse.Namespace) -> Any:
    """One cycle, with the steps drawn as they happen when a person is watching."""
    from trading_research_agents.agents.cycle import run_cycle

    if args.json or not _watching():
        return run_cycle(cfg)
    with _ui().CycleView(usage, spine) as view:
        return run_cycle(replace(cfg, watch=view.on_step))


def _market_facts(
    cfg: Any, project: Path, instrument: str, hand: Any = None
) -> tuple[bool, str, Any]:
    """Whether the instrument is spot on the venue orders go to, and what its underlying is.

    In venue mode the hand that will send says so. Elsewhere the venue's market list is asked,
    keyless, and only by a desk that lists spot or prediction, or whose universe admits more than
    crypto; a list that cannot be read leaves the cycle as it always was, since nothing is sent
    outside venue mode. An instrument named in [execution].equities is an equity whatever the
    venue says.
    """
    from trading_research_agents.data.sources.ccxt_client import market_of
    from trading_research_agents.trading.market import Market

    markets = tuple(cfg.execution.markets)
    declared = instrument.upper() in tuple(cfg.execution.equities)
    market = None
    if hand is not None:
        market = hand.market()
    elif "spot" in markets or "prediction" in markets or not cfg.universe.crypto_only:
        try:
            client = _execution_client(cfg)
            symbol = _symbol_for(cfg, project, instrument)
            venue = cfg.execution.venue or cfg.run.venue_key.partition("/")[0]
            market = Market.from_ccxt(venue, symbol, market_of(client, symbol))
        except Exception:
            market = None
    spot = "spot" in markets and market is not None and market.held_as_balance
    asset = "equity" if declared else (market.asset_class if market is not None else "crypto")
    outcome = market if market is not None and market.outcome and "prediction" in markets else None
    return spot, asset, outcome


def _refresh_sources(declarations: Iterable[Path]) -> list[str]:
    """Pull every fetched series that is due, before the pass reads it. Never stops the desk."""
    from trading_research_agents.data.pull import PullError, due, pull
    from trading_research_agents.data.series import SeriesError, load_series_map

    said: list[str] = []
    now = datetime.now(UTC)
    for path in declarations:
        try:
            specs = load_series_map(path)
        except SeriesError:
            said.extend(_refresh_news(path, now))
            continue
        base = path.resolve().parent
        for spec in specs:
            if not due(spec, base, now):
                continue
            try:
                got = pull(spec, base)
            except PullError as exc:
                said.append(
                    f"sources: {exc}; the file on disk is read as it is, and a value past its "
                    "max_age is not counted"
                )
            else:
                said.append(f"sources: {got.series} refreshed, {got.added} new, last {got.last}")
    return said
