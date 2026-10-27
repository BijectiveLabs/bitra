"""`tra run`: one cycle or a schedule of them on live data, with every gate the desk declared."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
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
from trading_research_agents.agents.roles import prompt
from trading_research_agents.cli import shutdown
from trading_research_agents.cli.common import (
    _DISCOVERED,
    CLEARED,
    REFUSED,
    REPORT_HEADROOM,
    _audit_lines,
    _completer_for_role,
    _desk_data,
    _discovered_universe,
    _execution_client,
    _fill_account_fees,
    _load_env,
    _metered_roles,
    _on_the_execution_venue,
    _refresh_news,
    _symbol_for,
    _ui,
    _venue_hand,
    _watching,
    emit,
    plain_text,
)
from trading_research_agents.cli.text import (
    desk_judge,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.readings import READINGS
from trading_research_agents.records import append_line, read_lines, write_json
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)
from trading_research_agents.trading.config import RunSettings


def screen_options_for(cycle_cfg: Any) -> dict[str, dict[str, Any]]:
    """What the pass pulls per instrument: data.toml's options, on the cycle's own bars.

    Read off data.toml's candle settings, the pass judged a 15-minute desk's market from four hours
    of one-minute bars, and the screener and the analyst saw two regimes for one instrument.
    """
    options = {kind: dict(opts) for kind, opts in cycle_cfg.data_options.items()}
    options.setdefault("ohlcv", {}).update(
        {"interval_s": cycle_cfg.interval_s, "limit": max(cycle_cfg.lookback_bars, 2)}
    )
    return options


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


@dataclass
class Auditing:
    """Waking the auditors between cycles: on an alarm, on a count, and on a clock.

    Something breaking is an event and belongs to the technical auditor. Whether the desk is
    earning what it costs belongs to the quant auditor, which wakes on a count of cycles, on a
    clock, and when the account says so: the daily loss limit reached, or a run of losing round
    trips. Neither can act on what it finds, and neither may end a run: an auditor that fails is
    a line in the report.
    """

    policy: Any
    settings: Any
    config: Path
    runs: Path
    watch: Any = None
    clock: Callable[[], float] = wall_clock
    limits: Any = None
    kill_switch: Path | None = None

    def __post_init__(self) -> None:
        from trading_research_agents.agents.alarms import Watch

        if self.watch is None:
            self.watch = Watch(cooldown_s=self.policy.alarm_cooldown_s)

    def due(self, record: Mapping[str, Any], ran: int) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        """Which auditors to ask after this cycle, and the alarms that asked for one."""
        from trading_research_agents.agents.alarms import (
            DAILY_KINDS,
            QUANT_KINDS,
            Reported,
            alarms_of,
        )

        now = self.clock()
        reported = Reported(self.runs.parent / "audits" / "reported.json")
        state = reported.read()
        who: list[str] = []
        fired: tuple[Any, ...] = ()
        if self.policy.on_alarm:
            found = [a for a in alarms_of(record) if self._first_time(a, state, now)]
            streak = self._streak(record, state)
            if streak is not None:
                found.append(streak)
            fired = self.watch.due(tuple(found), now)
            if any(a.kind not in QUANT_KINDS for a in fired):
                who.append("tech_auditor")
            if any(a.kind in QUANT_KINDS for a in fired):
                who.append("quant_auditor")
            for alarm in fired:
                if alarm.kind in DAILY_KINDS:
                    reported.write(**{f"{alarm.kind}_day": _utc_day(now)})
                elif alarm.kind == "losing_streak":
                    reported.write(streak_trip=alarm.at)
        every = self.policy.quant_every_cycles
        if every is not None and ran % every == 0 and "quant_auditor" not in who:
            who.append("quant_auditor")
        clock = self.policy.quant_every_s
        if clock is not None:
            last = state.get("quant_at")
            if not isinstance(last, int | float):
                # The first run starts the clock rather than paying for a look at nothing.
                reported.write(quant_at=now)
            elif now - last >= clock and "quant_auditor" not in who:
                who.append("quant_auditor")
        if "quant_auditor" in who and clock is not None:
            # Any look restarts the clock: a review that just happened is the one the clock wanted.
            reported.write(quant_at=now)
        if "quant_auditor" in who and getattr(self.policy, "risk_manager", False):
            # The auditor reports; the risk manager is the one who can act on the same figures.
            who.insert(0, "risk_manager")
        return tuple(who), fired

    def _risk(self, out: Path, fired: tuple[Any, ...]) -> list[str]:
        return _risk_decision(self, out, fired)

    @staticmethod
    def _first_time(alarm: Any, state: Mapping[str, Any], now: float) -> bool:
        """A loss limit is reported once per UTC day; any other kind is left to the cooldown."""
        from trading_research_agents.agents.alarms import DAILY_KINDS

        return alarm.kind not in DAILY_KINDS or state.get(f"{alarm.kind}_day") != _utc_day(now)

    def _streak(self, record: Mapping[str, Any], state: Mapping[str, Any]) -> Any:
        """An alarm when the newest round trip took a losing run to the threshold or past it."""
        from trading_research_agents.agents.alarms import LOSING_STREAK, Alarm, losing_streak
        from trading_research_agents.trading.positions import ROUND_TRIPS

        threshold = self.policy.losing_streak
        if threshold < 1:
            return None
        count, newest = losing_streak(self.runs / ROUND_TRIPS)
        if count < threshold or newest == state.get("streak_trip"):
            return None
        return Alarm(
            kind=LOSING_STREAK,
            detail=f"{count} losing round trips in a row, the newest at {newest.partition('|')[0]}",
            at=newest,
        )

    def after(self, record: Mapping[str, Any], ran: int, *, as_json: bool = False) -> list[str]:
        """Ask whoever is due, and say what they found. Never raises into the run."""
        who, fired = self.due(record, ran)
        if not who:
            return []
        from trading_research_agents.agents.audit import AuditConfig, run_audit
        from trading_research_agents.llm.usage import UsageLedger

        out = self.runs.parent / "audits"
        said = [
            f"audit: {', '.join(who)} asked"
            + (f" on {', '.join(a.kind for a in fired)}" if fired else f" after {ran} cycle(s)")
        ]
        for alarm in fired:
            said.append(f"  alarm {alarm.kind}: {alarm.detail}")
        if "risk_manager" in who:
            said.extend(self._risk(out, fired))
            who = tuple(w for w in who if w != "risk_manager")
        if not who:
            if not as_json:
                print(plain_text("\n".join(said)), flush=True)
            return said
        try:
            usage = UsageLedger(out / "usage.jsonl")
            audit = AuditConfig(
                runs=self.runs,
                completer_for=_metered_roles(self.settings, usage, floor=REPORT_HEADROOM),
                usage=usage,
                root=out,
                config=self.config,
                auditors=who,
            )
            if not as_json and _watching():
                with _ui().console().status(f"asking {', '.join(who)}", spinner="line"):
                    result = run_audit(audit)
            else:
                result = run_audit(audit)
        except Exception as exc:
            # A failing auditor is a thing to report, never a thing that ends a run.
            said.append(f"  the audit could not run: {exc.__class__.__name__}: {exc}")
            if not as_json:
                print(plain_text("\n".join(said)), flush=True)
            return said
        said.extend(f"  {line}" for line in _audit_lines(result, []))
        if not as_json:
            print(plain_text("\n".join(said)), flush=True)
        return said


def _risk_decision(auditing: Any, out: Path, fired: tuple[Any, ...]) -> list[str]:
    """Ask the risk manager, put what it decided in force one way only, and say so."""
    from trading_research_agents.agents.risk_manager import run as risk
    from trading_research_agents.llm.usage import UsageLedger

    now = datetime.fromtimestamp(auditing.clock(), tz=UTC)
    held = risk.Overrides.load(auditing.runs, now)
    try:
        usage = UsageLedger(out / "usage.jsonl")
        ask = _metered_roles(auditing.settings, usage, floor=REPORT_HEADROOM)(risk.ROLE)
        done = ask.complete(
            system=prompt(risk.PROMPT),
            user=json.dumps(
                risk.brief(
                    auditing.runs,
                    limits=auditing.limits,
                    overrides=held,
                    alarms=[a.record() for a in fired],
                    now=now,
                ),
                default=str,
            ),
            schema=risk.SCHEMA,
        )
        decision = risk.parse(done.text)
    except Exception as exc:
        # A risk manager that cannot answer leaves the client's limits exactly where they were.
        return [f"  risk manager: no decision ({exc.__class__.__name__}: {exc}); limits unchanged"]
    append_line(
        auditing.runs / "risk_decisions.jsonl",
        {
            "at": now.isoformat(),
            "decision": decision.decision,
            "size_multiple": None
            if decision.size_multiple is None
            else str(decision.size_multiple),
            "pause_instruments": list(decision.pause_instruments),
            "hold_hours": decision.hold_hours,
            "reason": decision.reason,
            "triggers": list(decision.triggers),
        },
        durable=True,
    )
    said = [f"  risk manager: {decision.decision}, {decision.reason}"]
    if decision.decision == "halt":
        if auditing.kill_switch is not None:
            auditing.kill_switch.write_text(
                f"armed by the risk manager at {now.isoformat()}: {decision.reason}\n",
                encoding="utf-8",
            )
            said.append(f"  the kill switch at {auditing.kill_switch} is armed: flatten and stop")
        else:
            # No kill switch declared: the nearest one-way step is to take no new entry anywhere.
            pause_all = risk.RunDecision(
                "pause", None, ("*",), decision.hold_hours, decision.reason, decision.triggers
            )
            said.extend(f"  {line}" for line in held.apply(pause_all, now))
        return said
    said.extend(f"  {line}" for line in held.apply(decision, now))
    return said


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).date().isoformat()


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


def _sweep(
    cfg: Any,
    cycle_cfg: Any,
    args: argparse.Namespace,
    *,
    chains: Mapping[str, Any],
    out: Path,
    ask: Callable[[str], Any],
) -> tuple[tuple[str, ...], Any]:
    """Which instruments this pass runs a cycle on, and the screen that chose them.

    A desk that watches one instrument runs it, as it always did. A desk that declared a universe
    takes the cheap look first - one data pull each, the same trigger applied per instrument - and
    spends its cycles on what that admitted.
    """
    from trading_research_agents.agents.screen import asking, sweep
    from trading_research_agents.execution.trigger import Trigger
    from trading_research_agents.store.snapshot import cycle_id

    universe = cfg.universe
    if args.instrument or universe.idle:
        return (cycle_cfg.instrument,), None
    universe, discovered, discovery_said = _discovered_universe(cfg)
    now = cycle_cfg.clock()
    # --now overrides the trigger for the cycle, so it overrides it for the pass that feeds the
    # cycle too: an operator who asked anyway should not be told nothing was worth looking at.
    trigger = Trigger() if args.now else cfg.trigger
    screener = None
    if universe.screener:
        screener = asking(ask("market_screener"), mechanism=cycle_cfg.mechanism, at=cycle_id(now))

    open_now: set[str] = set()
    if cfg.execution.places_orders and not cfg.execution.adopt_open_position:
        reader = cycle_cfg.held
        if reader is not None:
            try:
                rows, _source = reader()
                open_now = {row.instrument for row in rows}
            except Exception:
                # The cycle reads the book again and refuses on its own if it cannot; the pass only
                # uses this to avoid asking the roster about something already open.
                open_now = set()

    cap = cfg.desk.max_open_positions
    full = (
        f"the desk holds {len(open_now)} position(s), its declared [desk].max_open_positions"
        if cap is not None and open_now and len(open_now) >= cap
        else ""
    )

    screen_options = screen_options_for(cycle_cfg)

    def look(watch: Any) -> Any:
        return sweep(
            universe,
            chains,
            trigger=trigger,
            root=out,
            now=now,
            max_round_trips_per_day=cfg.targets.max_round_trips_per_day,
            options=screen_options,
            ask=screener,
            desk=cfg.desk,
            held=cycle_cfg.held,
            watch=watch,
            open_now=open_now,
            full=full,
            listed={row["instrument"]: row for row in (discovered or {}).get("kept", [])},
            rule=cfg.regime,
            screens=out.parent / "screens",
        )

    if args.json or not _watching():
        screened = look(lambda _step: None)
    else:
        # A pass pulls one snapshot per instrument before any cycle starts; on a slow venue that
        # is seconds per name, and a blank terminal for that long reads as a hung process.
        steps = [*universe.instruments, *(["market_screener"] if universe.screener else [])]
        with _ui().CycleView(cycle_cfg.usage, steps, title="screen") as view:
            screened = look(view.on_step)
    screened.discovered = discovered
    screened.refusals[:0] = discovery_said
    where = out.parent / "screens"
    where.mkdir(parents=True, exist_ok=True)
    write_json(where / f"{screened.at}.json", screened.record())
    return screened.chosen, screened


def _screen_report(screened: Any) -> tuple[dict[str, Any], list[str]]:
    """One pass over the universe as a record and as the lines a person reads."""
    from trading_research_agents.execution.universe import why_no_cycle

    payload = screened.record()
    # Admitted by the system's own measures, before the screener narrowed them.
    admitted = sum(1 for c in screened.candidates if c.admitted)
    lines = [
        f"screen {screened.at}: {len(screened.candidates)} instrument(s) looked at, "
        f"{admitted} admitted, {len(screened.chosen)} taken"
    ]
    for candidate in screened.candidates:
        state = (
            "taken"
            if candidate.instrument in screened.chosen
            else ("admitted" if candidate.taken else "held")
        )
        lines.append(
            f"  {candidate.instrument:<12} {state:<9} "
            f"moved {candidate.moved_bps if candidate.moved_bps is not None else '-'} bps, "
            f"spread {candidate.spread_bps if candidate.spread_bps is not None else '-'} bps, "
            f"pressure {candidate.pressure if candidate.pressure is not None else '-'}"
        )
    for refusal in screened.refusals[:8]:
        lines.append(f"  held {refusal}")
    if not screened.chosen:
        lines.append(
            f"no cycle this pass: {why_no_cycle(payload.get('candidates') or [])}"
            + (
                "; only the screener was asked"
                if screened.asked_screener
                else "; no model was asked"
            )
        )
    return payload, lines


def _screen_view(payload: Mapping[str, Any]) -> Any:
    return _ui().screen_view(payload)


def _keeper(
    args: argparse.Namespace, cfg: Any, project: Path, out: Path, instrument: str, rate: Any
) -> Any:
    """What looks after positions between decisions: time exits, venue exits, the kill switch."""
    from trading_research_agents.trading.positions import Keeper

    hands: dict[str, Any] = {}

    def hand_for(name: str) -> Any:
        if name not in hands:
            hands[name] = _venue_hand(args, cfg, project, name, rate)
        return hands[name]

    return Keeper(
        root=out,
        hand_for=hand_for,
        instruments=tuple(cfg.universe.instruments) or (instrument,),
        bar_interval_s=cfg.run.bar_interval_s,
        # Discovered instruments come and go from the list; a position on one is looked after
        # for as long as the records say it was entered.
        follow_records=True,
        timeout_bars=cfg.exit_rule.timeout_bars,
        hold_limit=cfg.execution.hold_limit,
    )


def _held_reader(cfg: Any, out: Path, mode: str, args: argparse.Namespace, project: Path) -> Any:
    """Where a cycle looks to find out what this desk is already carrying.

    In venue mode the venue is the book and nothing else is: it knows what filled, and what the
    attached exits closed while nobody was watching. Everywhere else the intents on disk are all
    there is, and the verdict says so rather than implying a venue was asked.
    """
    from trading_research_agents.execution.desk import Open, filled_from_records, from_records

    if cfg.desk.idle:
        return None
    if mode != "venue":
        return lambda: from_records(out, at=datetime.now(UTC))

    def ask_the_venue() -> tuple[tuple[Open, ...], str]:
        ours = filled_from_records(out)
        declared = cfg.universe.instruments or (args.instrument or cfg.run.instrument,)
        # An instrument discovery once took, and that this desk filled, is still the desk's to
        # count even when this pass did not list it.
        names = tuple(dict.fromkeys([*declared, *ours]))
        held: list[Open] = []
        for name in names:
            size = _venue_hand(args, cfg, project, name).position()
            if not size:
                continue
            # The venue says the position is there; this desk's own intent says what it has at
            # risk, which the venue has no way to know.
            mine = ours.get(name)
            side = "buy" if size > 0 else "sell"
            if mine is not None and mine.side and mine.side != side:
                # The venue holds the other side from what this desk filled last: not ours to state.
                mine = None
            held.append(
                Open(
                    instrument=name,
                    side=side,
                    risk_bps=None if mine is None else mine.risk_bps,
                    notional=None if mine is None else mine.notional,
                )
            )
        return tuple(held), "the venue, with what each one risks from this desk's own intents"

    return ask_the_venue


def _cycle_steps(cfg: Any) -> list[str]:
    """The pipeline this project configured, in order, so the live table starts complete.

    An operator watching a cycle should see every step that can happen, including the ones that are
    not configured - a table that grows as it goes tells you less than one that was there first.
    """
    steps = ["data"]
    if not cfg.trigger.idle:
        steps.append("trigger")
    if cfg.size_quote is not None:
        steps.append("size")
    if cfg.account is not None:
        steps.append("capital")
    steps.extend(["regime", "technical"])
    # The members beside the analyst, by the names their rows report under.
    steps.extend({"technical_peer": "technical_peer", "flow_analyst": "flow"}[m] for m in cfg.team)
    if cfg.judge is not None:
        steps.append("judge")
    # Every reading has a row, read or not; the row says which.
    steps.extend(["macro", "onchain", "sentiment"])
    steps.extend(["cost", "exit"])
    steps.extend(f"validator_{stance}" for stance in cfg.validators)
    if not cfg.matrix.idle:
        steps.append("confluence")
    if not cfg.conviction.idle:
        steps.append("conviction")
    if cfg.max_risk_per_trade_bps is not None:
        steps.append("risk")
    if cfg.desk.bounds_a_trade:
        steps.append("desk")
    steps.append("cost gate")
    if not cfg.targets.idle:
        steps.append("targets")
    steps.append("intent")
    if cfg.mode == "venue":
        steps.append("venue")
    return steps


def _one_cycle(cfg: Any, usage: Any, spine: list[str], args: argparse.Namespace) -> Any:
    """One cycle, with the steps drawn as they happen when a person is watching."""
    from trading_research_agents.agents.cycle import run_cycle

    if args.json or not _watching():
        return run_cycle(cfg)
    with _ui().CycleView(usage, spine) as view:
        return run_cycle(replace(cfg, watch=view.on_step))


def order_outcome(placed: Mapping[str, Any]) -> str:
    """What became of a sent order, in one phrase, from the record the venue hand wrote."""
    if placed.get("interrupted"):
        return str(placed.get("note") or "interrupted")
    filled = placed.get("filled")
    if placed.get("note"):
        return str(placed["note"])
    if filled not in (None, "", "0", "0.0"):
        return f"filled {filled}" + (f" at {placed['average']}" if placed.get("average") else "")
    return str(placed.get("status") or "sent; the venue reported no state")


def _cycle_view(payload: Mapping[str, Any]) -> Any:
    return _ui().cycle_view(payload)


def _team_lines(context: Mapping[str, Any]) -> list[str]:
    """The members beside the analyst, and the judge's reading of each structure reason."""
    lines = []
    peer = context.get("technical_peer")
    if isinstance(peer, Mapping) and peer.get("target"):
        lines.append(
            f"peer:      {peer['target']} on {peer.get('setup')} at {peer.get('confidence')}: "
            f"{peer.get('reason')}"
        )
    flow = context.get("flow_analyst")
    if isinstance(flow, Mapping):
        said = flow.get("error") or f"{flow.get('target')} at {flow.get('confidence')}"
        lines.append(f"flow:      {said}" + (f": {flow['reason']}" if flow.get("reason") else ""))
    for member in ("technical_analyst", "technical_peer"):
        check = context.get(f"{member}_check")
        if isinstance(check, Mapping):
            lines.append(
                f"judge:     {member} describes the measurements at {check.get('describes')} "
                f"({check.get('model')})"
            )
    return lines


def _cycle_report(result: Any, usage: Any, mode: str) -> tuple[dict[str, Any], list[str]]:
    """One cycle as a record and as the lines a person reads."""
    payload = result.record()
    payload["tokens"] = usage.tokens
    payload["out"] = str(result.path)
    spent = f"{result.fan.seconds}s of agents"
    if result.debate is not None:
        spent += f", {result.debate.seconds}s of validators"
    lines = [f"cycle {result.cycle} {result.instrument}: {spent}"]
    data = result.briefs.get("data") or {}
    for kind, value in sorted((data.get("values") or {}).items()):
        lines.append(f"  {kind:<14} {value['source']:<18} age {value['age_s']}s")
    for row in result.briefs.get("series") or []:
        lines.append(
            f"  {row['series']:<14} {row['value']} as of {str(row['observed_at'])[:16]}, "
            f"{row['age_h']}h old, {row['state']}; {row['origin']}"
        )
    for name, why in sorted((result.briefs.get("not_read") or {}).items()):
        lines.append(f"{name:<10} {why}")
    for name, said in sorted(result.context.items()):
        if name not in READINGS:
            continue
        declared = said.get("declared_state") or {}
        word = declared.get("declared", "undeclared")
        spoken = declared.get("in_its_words")
        state = f"{word} ({spoken})" if spoken else word
        lines.append(f"{name:<10} read by code: {state}")
    lines.extend(_team_lines(result.context))
    if result.analyst is not None:
        lines.append(
            f"technical: {result.analyst.target} on {result.analyst.setup} at "
            f"{result.analyst.confidence:.2f} over {result.analyst.horizon_bars} bars: "
            f"{result.analyst.reason}"
        )
        if result.cited:
            read = ", ".join(f"{f['figure']}={f['value']}" for f in result.cited)
            lines.append(f"read: {read}")
    if result.wake is not None and not result.wake.ask:
        lines.append(f"held: {result.wake.reasons[0]}")
        if result.wake.moved_bps is not None:
            lines.append(
                f"the mid moved {result.wake.moved_bps} bps against the "
                f"{result.wake.required_bps} bps this desk asks for before it looks"
            )
    for role, said in sorted(result.context.items()):
        if not role.startswith("validator"):
            continue
        verdict = said.get("verdict") or f"failed: {said.get('error', '')}"
        cited = len(said.get("claims") or ())
        dropped_claims = len(said.get("discarded") or ())
        counted = f"{cited} cited figure(s)"
        if dropped_claims:
            counted += f", {dropped_claims} discarded"
        lines.append(f"{role.replace('_', ' '):<18} {verdict} on {counted}")
    if result.confluence is not None:
        agreement = result.confluence
        states = ", ".join(
            f"{role.replace('_analyst', '')} {state}"
            for role, state in sorted(agreement.states.items())
        )
        counted = f"support {agreement.support}, opposition {agreement.opposition}"
        lines.append(f"confluence: {states or 'no role required'}; {counted}")
    if result.stake is not None:
        stake = result.stake
        score = stake.score
        most = dict(score.weights)
        voices = ", ".join(
            f"{role.replace('_analyst', '')} {value}"
            + (f" of {most[role]}" if role in most else "")
            for role, value in score.contributions
        )
        lines.append(f"conviction: {score.score} of 1.0 ({voices})")
        if stake.admitted:
            leverage = "" if stake.leverage is None else f", {stake.leverage}x equity"
            lines.append(f"stake: {stake.size} at {stake.multiple} of the base size{leverage}")
    if result.risk is not None and result.risk.risk_bps is not None:
        risk = result.risk
        lines.append(
            f"risk: {risk.risk_bps} bps of equity at the stop against the declared "
            f"{risk.allowed_bps} bps, size {risk.size}"
        )
    if result.exits:
        shown = result.exits
        said = [f"exits: {shown['side']} at about {shown['entry']}"]
        for label, name in (("take profit", "take"), ("stop loss", "stop")):
            if f"{name}_price" in shown:
                pnl = shown.get(f"{name}_pnl_bps", shown[f"{name}_bps"])
                said.append(f"{label} {shown[f'{name}_price']} (position {pnl} bps)")
        lines.append(", ".join(said))
        even = (result.intent or {}).get("break_even_win_rate")
        if even is not None:
            lines.append(
                f"break even: {Decimal(even) * 100:.1f}% of trades like this must reach the take "
                "profit to cover the stops and the round trip"
            )
        measured = shown.get("measured")
        if measured:
            held = f", held at {measured['clamped_to']}" if measured["clamped_to"] else ""
            if measured.get("source") == "analyst":
                lines.append(
                    f"exits from the analyst: stop named {measured['proposed_stop_bps']} bps away"
                    f"{held}, take profit {measured['take_bps']} bps; the market's own range "
                    f"(ATR {measured['atr_bps']} bps) would have put the stop at "
                    f"{measured['measured_stop_bps']} bps"
                )
            else:
                lines.append(
                    f"exits measured: ATR {measured['atr_bps']} bps over "
                    f"{measured['horizon_bars']} bar(s) put the stop at "
                    f"{measured['measured_stop_bps']} bps{held}"
                )
            for note in measured.get("notes") or ():
                lines.append(f"  {note}")
    if result.targets is not None:
        met = result.targets
        lines.append(
            f"targets: net {met.net_bps.quantize(Decimal('0.01'))} bps, "
            f"reward-to-risk {met.reward_risk}, {met.round_trips_today} round trip(s) today"
        )
        if met.edge_multiple is not None:
            lines.append(f"edge over cost: {met.edge_multiple}x the round trip")
        if met.research_bps is not None:
            lines.append(
                f"research: {met.research_bps} bps of this trade's notional in model calls "
                "since the last intent"
            )
    if result.verdict is not None:
        cost = result.verdict.cost
        total = cost.round_trip_bps.quantize(Decimal("0.01"))
        guessed = cost.estimated_bps.quantize(Decimal("0.01"))
        lines.append(f"cost: {total} bps round trip, {guessed} of it estimated")
        lines.append(f"gate: {result.verdict.reason}")
        lines.append(
            f"the edge is {result.verdict.edge_certainty.value}: {result.verdict.edge_source}"
        )
        for component in cost.components:
            row = component.record()
            lines.append(f"  {row['name']:<18} {row['bps']:>10} bps  {row['certainty']}")
    if result.intent is not None:
        lines.append(f"intent: {result.intent['target']} {result.intent['size']} ({mode})")
        placed = result.placed or {}
        if placed.get("sent"):
            at = "" if not placed.get("average") else f" at {placed['average']}"
            lines.append(
                f"placed: {placed.get('side')} {placed.get('size')} on {placed.get('venue')}"
                f"{at}, stop {placed.get('stop_price')}, take {placed.get('take_price')}"
            )
            lines.append(f"  timing: {execution_timing(placed)}")
            # Sent is not filled. What became of the order is the line an operator acts on.
            lines.append(f"  outcome: {order_outcome(placed)}")
        elif placed.get("refused"):
            lines.append(f"not placed: {placed['refused']}")
    for refusal in result.refusals[:6]:
        lines.append(f"  refused {refusal}")
    if result.stopped_by:
        lines.append(f"stopped by: {result.stopped_by}")
    lines.append(f"snapshot.json, briefs, answers and cycle.json in {result.path}")
    return payload, lines


def execution_timing(placed: Mapping[str, Any]) -> str:
    """How old the decision was when sent, how long the venue took, and what the fill cost."""
    said = []
    if placed.get("decision_age_s") is not None:
        said.append(f"sent {placed['decision_age_s']}s after the cycle began")
    if placed.get("send_ms") is not None:
        said.append(f"the venue answered in {placed['send_ms']} ms")
    if placed.get("slippage_bps") is not None:
        said.append(f"filled {placed['slippage_bps']} bps from the mid it was priced at")
    return "; ".join(said) or "not measured"


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
