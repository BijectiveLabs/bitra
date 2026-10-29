"""`tra run`: one cycle or a schedule of them on live data, with every gate the desk declared."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from trading_research_agents.agents.context import (
    declared_in,
)
from trading_research_agents.cli import shutdown
from trading_research_agents.cli.auditing import Auditing
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _completer_for_role,
    _cycle_ui,
    _desk_data,
    _load_env,
    _refresh_news,
    _ui,
    _watching,
    emit,
)
from trading_research_agents.cli.cycle_report import _cycle_report, _cycle_steps, _cycle_view
from trading_research_agents.cli.fees import _fill_account_fees
from trading_research_agents.cli.schedule import Schedule, held_back_by
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
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)
from trading_research_agents.trading.config import RunSettings


def run_cycle_command(args: argparse.Namespace) -> int:
    """One cycle, end to end, from the files in the project."""
    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("a cycle needs [strategy].agent and [agents] in strategy.toml")
    filled, _ = _fill_account_fees(args.envelope, cfg, dict(os.environ), datetime.now(UTC))
    if filled and not args.json:
        sys.stdout.write("\n".join(filled) + "\n")
    if cfg.agents.magnitude_bps is None:
        raise ConfigError("the claimed edge: [agents].magnitude_bps in strategy.toml")
    desk = _open_desk(args, cfg)
    cycle_cfg = _cycle_config(desk)
    schedule = Schedule(every_s=args.every, cycles=args.cycles, kill_switch=cfg.kill_switch)
    spine = _cycle_steps(cycle_cfg)
    # The auditors keep their own ledger, so what the desk spent deciding stays separable from what
    # it spent looking at itself.
    audits = Auditing(
        policy=cfg.audit,
        settings=cfg.agents,
        config=args.config,
        runs=desk.out,
        limits=cfg.limits,
        kill_switch=cfg.kill_switch,
    )
    return _DeskRun(desk, cycle_cfg, schedule, spine, audits).run()


@dataclass
class _Desk:
    """What a run is built from, read once before its first pass."""

    args: argparse.Namespace
    cfg: Any
    instrument: str
    mode: str
    out: Path
    beside: Path
    data_cfg: Any
    chains: dict[str, Any]
    declared: Any
    hand: Any
    rate: Any
    keeper: Any
    usage: Any
    completer_for: Callable[[str], Any]


def _open_desk(args: argparse.Namespace, cfg: Any) -> _Desk:
    """The desk's data, its declared sources, its order log and its metered models."""
    from trading_research_agents.llm.usage import UsageLedger
    from trading_research_agents.trading.hand_state import OrderRate

    mode = args.mode or cfg.execution.mode
    data_cfg = _desk_data(args.data_config, cfg, args.mode or cfg.execution.mode)
    chains = {declared.kind: data_cfg.chain(declared.kind) for declared in data_cfg.kinds}
    instrument = args.instrument or cfg.run.instrument
    out = args.out or Path("runs") / "cycles"
    beside = args.config.resolve().parent
    declared = declared_in(beside, overrides=_overrides(args))
    # A hand for the instrument this run starts on, so a misconfigured mandate is refused before any
    # data is pulled. A pass over a universe builds another one for whichever instrument it chose:
    # one hand per instrument, because a hand carries the symbol an order goes out under.
    hand = _venue_hand(args, cfg, beside, instrument) if mode == "venue" else None
    # One order log for the whole run, so [controls].max_orders_per_minute is the desk's.
    rate = OrderRate(cfg.limits.max_orders_per_minute)
    keeper = _keeper(args, cfg, beside, out, instrument, rate) if mode == "venue" else None
    usage = UsageLedger(out / "usage.jsonl")
    return _Desk(
        args=args,
        cfg=cfg,
        instrument=instrument,
        mode=mode,
        out=out,
        beside=beside,
        data_cfg=data_cfg,
        chains=chains,
        declared=declared,
        hand=hand,
        rate=rate,
        keeper=keeper,
        usage=usage,
        completer_for=_metered_completers(cfg.agents, usage),
    )


def _overrides(args: argparse.Namespace) -> dict[str, Path]:
    """The source files named on the command line, in place of the ones beside strategy.toml."""
    named = (
        ("macro", args.macro),
        ("onchain", args.onchain),
        ("sentiment", args.sentiment),
        ("derivatives", args.derivatives),
        ("news", args.news),
    )
    return {name: path for name, path in named if path is not None}


def _metered_completers(a: Any, usage: Any) -> Callable[[str], Any]:
    """One budget and one rate window for the whole desk, one client per role: a role may run on a
    cheaper model without the others paying for it."""
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        RateWindow,
    )

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

    return completer_for


def _cycle_config(desk: _Desk) -> Any:
    """The cycle as the desk declared it, for the instrument the run starts on."""
    from trading_research_agents.agents.cycle import CycleConfig
    from trading_research_agents.execution.targets import reward_risk

    args, cfg, a, mode = desk.args, desk.cfg, desk.cfg.agents, desk.mode
    news = tuple(desk.declared.text)
    if not news and (desk.beside / "events.csv").is_file():
        news = (desk.beside / "events.csv",)
    facts = _market_facts(cfg, desk.beside, desk.instrument, desk.hand)
    return CycleConfig(
        instrument=desk.instrument,
        envelope=load_envelope(args.envelope),
        venue_key=cfg.run.venue_key,
        chains=desk.chains,
        completer_for=desk.completer_for,
        usage=desk.usage,
        root=desk.out,
        mechanism=a.mechanism,
        size=cfg.run.size,
        size_quote=cfg.run.size_quote,
        magnitude_bps=a.magnitude_bps,
        bar_interval_s=cfg.run.bar_interval_s,
        lookback_bars=a.lookback_bars,
        interval_s=cfg.run.bar_interval_s,
        settlement_interval_s=cfg.run.settlement_interval_s,
        latency_slippage_bps=cfg.run.slippage_bps,
        margin_bps=args.margin if args.margin is not None else Decimal(0),
        min_confidence=a.min_confidence,
        mode=mode,
        place=desk.hand,
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
        size_step=cfg.run.step_for(desk.instrument),
        reviewers=a.reviewers,
        on_missing_reviewer=a.on_missing_reviewer,
        context_intervals=cfg.run.context_intervals,
        hold_limit=cfg.execution.hold_limit,
        timeout_bars=cfg.exit_rule.timeout_bars,
        regime=cfg.regime,
        targets=cfg.targets,
        desk=cfg.desk,
        held=_held_reader(cfg, desk.out, mode, args, desk.beside),
        wait_while_open=mode == "venue" and not cfg.execution.adopt_open_position,
        daily_loss_limit_bps=cfg.limits.daily_loss_limit_bps if mode == "venue" else None,
        max_drawdown_bps=cfg.limits.max_drawdown_bps if mode == "venue" else None,
        sizing=cfg.sizing,
        # Flattening is the kill switch's job; the cycle only arms it.
        drawdown_kill=cfg.kill_switch if cfg.limits.on_drawdown == "flatten" else None,
        max_notional=cfg.execution.max_notional,
        max_trades_per_day=cfg.limits.max_trades_per_day,
        switches=tuple(path for path in (cfg.kill_switch, cfg.pause_switch) if path is not None),
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
        data_options={declared.kind: dict(declared.options) for declared in desk.data_cfg.kinds},
        series={role: tuple(paths) for role, paths in desk.declared.series.items()},
        news=news,
        # A declared roster wins; otherwise every reading with something to read is read.
        readings=a.readings or desk.declared.readings,
        team=a.team,
        judge=desk_judge(args.data_config),
    )


@dataclass
class _DeskRun:
    """Passes over the universe until the schedule, the budget, the kill switch or a signal ends
    the run. Every way out is said by name and written to the journal."""

    desk: _Desk
    cycle_cfg: Any
    schedule: Schedule
    spine: list[str]
    audits: Auditing
    ran: int = 0
    passes: int = 0
    cleared: int = CLEARED
    mid_cycle: bool = False
    # What the models have already cost since this desk last wrote an intent, per instrument. A
    # refused cycle's tokens are not free: they are carried forward and charged to whatever trade
    # that instrument eventually produces.
    carried: dict[str, Decimal] = field(default_factory=dict)

    def run(self) -> int:
        desk = self.desk
        lock = shutdown.claim(desk.out) if desk.mode == "venue" else None
        shutdown.journal(
            desk.out,
            "started",
            every_s=desk.args.every,
            cycles=desk.args.cycles,
            mode=desk.mode,
            instruments=list(desk.cfg.universe.instruments) or [desk.instrument],
        )
        try:
            with shutdown.as_interrupt():
                while True:
                    ended = self._pass()
                    if ended is not None:
                        return ended
        except KeyboardInterrupt as exc:
            return self._interrupted(exc)
        finally:
            if lock is not None:
                shutdown.release(lock)

    def _pass(self) -> int | None:
        """One pass: keep the open positions, choose, run a cycle per instrument, then wait."""
        if self._flattened_by_kill_switch():
            return self.cleared
        self._say(_refresh_sources(self.desk.declared.classes.values()))
        chosen, screened = self._choose()
        if screened is not None:
            found, said = _screen_report(screened)
            emit(found, self.desk.args.json, said, partial(_screen_view, found))
        result, stop = self._cycles(chosen)
        # A pass counts whether or not the trigger let a cycle through: on a quiet market
        # `--cycles 6` would otherwise run for as long as the market stays quiet.
        self.passes += 1
        if not stop:
            stop = self.schedule.stop_reason(result, self.passes)
        if stop:
            return self._stopped(stop)
        self._wait()
        return None

    def _say(self, lines: list[str]) -> None:
        if lines and not self.desk.args.json:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()

    def _flattened_by_kill_switch(self) -> bool:
        desk = self.desk
        if desk.keeper is None:
            return False
        armed = desk.cfg.kill_switch is not None and desk.cfg.kill_switch.exists()
        self._say(desk.keeper.run(kill=armed))
        if not armed:
            return False
        stop = f"the kill switch at {desk.cfg.kill_switch}, after flattening"
        emit(
            {"cycles": self.ran, "passes": self.passes, "stopped_by": stop},
            desk.args.json,
            [f"scheduler: stopped by {stop}"],
        )
        shutdown.journal(desk.out, "stopped", stopped_by=stop, cycles=self.ran)
        return True

    def _choose(self) -> tuple[tuple[str, ...], Any]:
        desk = self.desk
        held_back = held_back_by(desk.cfg, desk.out, datetime.now(UTC))
        if held_back:
            emit(
                {"held_back": held_back},
                desk.args.json,
                [f"desk: {held_back}; open positions keep their exits, nothing is asked"],
            )
            return (), None
        return _sweep(
            desk.cfg,
            self.cycle_cfg,
            desk.args,
            chains=desk.chains,
            out=desk.out,
            ask=desk.completer_for,
        )

    def _cycles(self, chosen: tuple[str, ...]) -> tuple[Any, str]:
        result: Any = None
        for instrument in chosen:
            ran = self._cycle(instrument)
            if ran is None:
                continue
            result = ran
            # Inside a pass only the budget and the kill switch end the run.
            stop = self.schedule.stop_reason(result, 0)
            if stop:
                return result, stop
        return result, ""

    def _cycle(self, instrument: str) -> Any:
        """One cycle on one instrument."""
        desk = self.desk
        before = desk.usage.cost
        held = self.carried.get(instrument, Decimal(0))
        sends = (
            _venue_hand(desk.args, desk.cfg, desk.beside, instrument, desk.rate)
            if desk.mode == "venue"
            else None
        )
        self.mid_cycle = True
        cfg = self._config_for(instrument, held, sends)
        result = _one_cycle(cfg, desk.usage, self.spine, desk.args)
        self.mid_cycle = False
        self.carried[instrument] = Decimal(0) if result.acted else held + (desk.usage.cost - before)
        payload, lines = _cycle_report(result, desk.usage, desk.mode)
        emit(payload, desk.args.json, lines, partial(_cycle_view, payload))
        self.ran += 1
        self.audits.after(payload, self.ran, as_json=desk.args.json)
        self.cleared = CLEARED if result.stopped_by is None else REFUSED
        return result

    def _config_for(self, instrument: str, held: Decimal, sends: Any) -> Any:
        cfg = self.desk.cfg
        base = self.cycle_cfg
        facts = _market_facts(cfg, self.desk.beside, instrument, sends)
        return replace(
            base,
            instrument=instrument,
            spot=facts[0],
            asset_class=facts[1],
            market=facts[2],
            size_step=_step_for(cfg, instrument),
            # A unit cap was written for [run].instrument; with a quote cap declared, other
            # instruments are held to that one alone.
            max_position=(
                cfg.limits.max_position
                if instrument == cfg.run.instrument or cfg.limits.max_position_quote is None
                else None
            ),
            carried_spend=held,
            place=sends,
            account=None if sends is None else sends.account,
        )

    def _stopped(self, stop: str) -> int:
        desk = self.desk
        if desk.keeper is not None and stop.startswith("the kill switch"):
            # The switch exists to get flat, not only to stop deciding.
            kept = desk.keeper.run(kill=True)
            if kept and not desk.args.json:
                sys.stdout.write("\n".join(kept) + "\n")
        if self.schedule.looping:
            emit(
                {"cycles": self.ran, "passes": self.passes, "stopped_by": stop},
                desk.args.json,
                [
                    f"scheduler: stopped by {stop}, after {self.passes} pass(es) and "
                    f"{self.ran} cycle(s)"
                ],
            )
        shutdown.journal(desk.out, "stopped", stopped_by=stop, cycles=self.ran, passes=self.passes)
        return self.cleared

    def _wait(self) -> None:
        # Said before the wait, not after it: an operator looking at the screen in the meantime
        # needs to know the desk is waiting rather than stuck.
        left = self.schedule.left()
        if not self.desk.args.json:
            sys.stdout.write(self.schedule.announce(left) + "\n")
            sys.stdout.flush()
        if _watching(self.desk.args.json) and left > 0:
            _ui().countdown(left)
        else:
            self.schedule.rest(left)

    def _interrupted(self, exc: KeyboardInterrupt) -> int:
        # Ctrl-C, a service manager's SIGTERM, a dropped SSH session: one way out, said by name and
        # written down. Whatever a cycle had already put on disk stays there.
        why = shutdown.stopped_by(exc)
        if self.mid_cycle:
            why = f"{why}, in the middle of a cycle"
        emit(
            {"cycles": self.ran, "stopped_by": why},
            self.desk.args.json,
            [f"scheduler: stopped by {why}, after {self.ran} cycle(s)"],
        )
        shutdown.journal(self.desk.out, "stopped", stopped_by=why, cycles=self.ran)
        # Stopping between cycles is the ordinary way a desk ends; stopping inside one is not.
        return REFUSED if self.mid_cycle else self.cleared


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
    with _cycle_ui().CycleView(usage, spine) as view:
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
