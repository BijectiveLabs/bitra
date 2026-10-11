"""One session of the seven roles, in the sandbox, in a fixed order, everything written down.

    figures (code) -> screener -> [macro] -> [onchain] -> analyst runs (one per kept instrument)
                   -> risk manager -> executor (allocation) -> auditor

No role talks to another; each gets what code computed plus what the previous roles wrote,
and each answer is a typed object recorded in the session directory. The analyst is the only
role that decides inside a run, as the decision core of a replay over recorded trades. The
risk manager's decision is applied through the action surface, which can only make the next
session safer. The executor allocates; it does not send. The auditor reads it all, including
what every call cost.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents import (
    auditor,
    executor,
    macro_analyst,
    market_screener,
    onchain_analyst,
    risk_manager,
)
from trading_research_agents.agents.auditor.tools import audit_brief
from trading_research_agents.agents.macro_analyst.tools import macro_brief, returns_from_closes
from trading_research_agents.agents.market_screener.tools import ScreenFigures, screen_figures
from trading_research_agents.agents.onchain_analyst.tools import onchain_brief
from trading_research_agents.agents.react import ToolLoopError, run_tool_loop
from trading_research_agents.agents.risk_manager.tools import risk_brief
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.agents.strategy import MAX_FAILURES_IN_A_ROW, AgentStrategy
from trading_research_agents.chain import cost_floor, load_observation, observe
from trading_research_agents.data.parquet import read_trades
from trading_research_agents.data.series import SeriesError, load_all, read_events
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.cost import FundingSchedule
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import BudgetSpentError, UsageLedger
from trading_research_agents.surface import (
    ladder_show,
    ledger_search,
    ledger_show,
    run_report,
    runs_index,
)
from trading_research_agents.surface.actions import (
    ActionRefusedError,
    arm_kill_switch,
    demote,
    tighten_limits,
)
from trading_research_agents.surface.confine import confine_all, roots_of
from trading_research_agents.trading import (
    Controls,
    KillSwitch,
    Ladder,
    RecordedFeed,
    RunReport,
    SimulatedVenue,
    StrategyConfig,
    TradingConfig,
    run,
)
from trading_research_agents.trading.controls import Limits

NS_PER_MINUTE = 60 * 1_000_000_000


class SessionError(ValueError):
    """The session cannot start as configured."""


@dataclass(frozen=True)
class Figures:
    """What code measures about a dataset before any model sees it."""

    instrument: str
    rows: int
    first_ts_ns: int
    last_ts_ns: int
    prints_per_minute: float
    realised_vol_bps: float
    turnover: Decimal
    floor_outcome: str
    floor_margin_bps: str
    screen: ScreenFigures | None = None
    observation: dict[str, Any] | None = None

    def record(self) -> dict[str, Any]:
        base = {} if self.screen is None else self.screen.record()
        out = {
            **base,
            "instrument": self.instrument,
            "rows": self.rows,
            "prints_per_minute": round(self.prints_per_minute, 3),
            "realised_vol_bps": round(self.realised_vol_bps, 3),
            "turnover": str(self.turnover),
            "cost_floor": self.floor_outcome,
            "floor_margin_bps": self.floor_margin_bps,
        }
        if self.observation is not None:
            out["observation"] = self.observation
        return out


@dataclass(frozen=True)
class ScreenSettings:
    """The screener's two thresholds, the operator's; `[agents.screen]` or these defaults."""

    min_prints_per_minute: float = 1.0
    volatile_above_bps: float = 50.0


@dataclass(frozen=True)
class SessionConfig:
    strategy: StrategyConfig
    envelope: Envelope
    datasets: Mapping[str, Path]
    out_dir: Path
    completer_for: Callable[[str], Completer]
    usage: UsageLedger
    magnitude_bps: Decimal
    ladder_path: Path
    kill_file: Path | None = None
    macro_series: Path | None = None
    onchain_series: Path | None = None
    events: Path | None = None
    screen: ScreenSettings = field(default_factory=ScreenSettings)
    observation: Path | None = None
    resume: bool = False
    auditor_steps: int = 6
    # Told at each step: (step, status, summary). It observes; it decides nothing.
    on_step: Callable[[str, str, str], None] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.strategy.agents is None:
            raise SessionError("a session needs [strategy].agent and [agents] in strategy.toml")
        if not self.datasets:
            raise SessionError("a session needs at least one dataset")
        if self.magnitude_bps <= 0:
            raise SessionError("magnitude_bps is a positive number of basis points")


@dataclass
class SessionResult:
    session_id: str
    figures: list[Figures]
    kept: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    macro: dict[str, Any] | None
    onchain: dict[str, Any] | None
    reports: dict[str, RunReport]
    risk: dict[str, Any] | None
    limits_after: Limits
    allocation: list[dict[str, Any]]
    audit: dict[str, Any] | None
    refusals: list[str]
    stopped_by: str | None


def figures_for(
    instrument: str,
    path: Path,
    envelope: Envelope,
    venue: str,
    magnitude_bps: Decimal,
    *,
    screen: ScreenSettings,
    observation: Path | None = None,
) -> Figures:
    figures = screen_figures(
        instrument,
        read_trades(path),
        min_prints_per_minute=screen.min_prints_per_minute,
        volatile_above_bps=screen.volatile_above_bps,
    )
    if figures.rows == 0:
        raise SessionError(f"{instrument}: the dataset has no trades")
    check = cost_floor(envelope, magnitude_bps, venue=venue)
    observed = None
    if observation is not None:
        # Layer 1 on the same prints: the screener sees whether the declared event recurs
        # here, with its counts and response, before it labels a regime.
        report = observe(load_observation(observation), read_trades(path), dataset=str(path))
        observed = {
            "subject": report.observation.subject,
            "recurrence": report.recurrence.value,
            "events": report.events,
            "measured": report.measured,
            "events_per_subperiod": list(report.events_per_subperiod),
            "response_bps": report.response_bps,
            "horizon_bars": report.observation.horizon_bars,
        }
    return Figures(
        instrument=instrument,
        rows=figures.rows,
        first_ts_ns=0,
        last_ts_ns=0,
        prints_per_minute=figures.prints_per_minute,
        realised_vol_bps=figures.realised_vol_bps,
        turnover=figures.turnover,
        floor_outcome=check.outcome.value,
        floor_margin_bps=str(check.margin_bps),
        screen=figures,
        observation=observed,
    )


def _minute_closes(path: Path) -> tuple[list[int], list[float]]:
    closes: dict[int, float] = {}
    for trade in read_trades(path):
        closes[trade.ts_ns // NS_PER_MINUTE] = float(trade.price)
    minutes = sorted(closes)
    return [m * NS_PER_MINUTE for m in minutes], [closes[m] for m in minutes]


class _Session:
    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg
        self.started = cfg.clock()
        self.session_id = self.started.strftime("%Y%m%dT%H%M%S")
        self.out = cfg.out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.refusals: list[str] = []
        self.stopped_by: str | None = None
        self.calls = 0
        self._failures_in_a_row = 0

    def ask(self, role: str, user: dict[str, Any], schema: dict[str, Any]) -> str | None:
        """One call, recorded. A refusal of form or budget is a None the caller handles."""
        if self.stopped_by is not None:
            return None
        recorded = self._recorded(role)
        if recorded is not None:
            self.calls += 1
            return recorded
        completer = self.cfg.completer_for(role)
        text_user = json.dumps(user, sort_keys=True, default=str)
        try:
            done = completer.complete(system=prompt(role), user=text_user, schema=schema)
        except BudgetSpentError as exc:
            self.stopped_by = str(exc)
            self.refusals.append(f"{role}: {exc}")
            return None
        except (RoleError, RuntimeError, OSError, ValueError) as exc:
            self.refusals.append(f"{role}: {exc.__class__.__name__}: {exc}")
            self._failures_in_a_row += 1
            if self._failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                self.stopped_by = f"{self._failures_in_a_row} failed calls in a row"
            return None
        self._failures_in_a_row = 0
        self.calls += 1
        (self.out / f"{self.calls:02d}-{role}.json").write_text(
            json.dumps(
                {"role": role, "user": user, "response": done.text, "model": done.model},
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        return done.text

    def _recorded(self, role: str) -> str | None:
        """With --resume, an answer already on disk for this step is reused, not asked again."""
        if not self.cfg.resume:
            return None
        path = self.out / f"{self.calls + 1:02d}-{role}.json"
        if not path.is_file():
            return None
        try:
            saved = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if saved.get("role") != role or not isinstance(saved.get("response"), str):
            return None
        self.refusals.append(f"{role}: reused the recorded answer ({path.name})")
        return str(saved["response"])

    def screen(self, figures: list[Figures]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        text = self.ask(
            market_screener.ROLE,
            {"candidates": [f.record() for f in figures]},
            market_screener.SCHEMA,
        )
        if text is None:
            return [], [
                {"instrument": f.instrument, "reason": "screener unavailable"} for f in figures
            ]
        try:
            out = market_screener.parse(text)
        except RoleError as exc:
            self.refusals.append(f"market_screener: {exc}")
            return [], [{"instrument": f.instrument, "reason": str(exc)} for f in figures]
        known = {f.instrument for f in figures}
        kept = [k for k in out.kept if isinstance(k, dict) and k.get("instrument") in known]
        invented = [k for k in out.kept if isinstance(k, dict) and k.get("instrument") not in known]
        for k in invented:
            self.refusals.append(f"market_screener: kept an instrument it was not given: {k}")
        # A candidate the screener did not mention is excluded, not silently kept.
        named = {k["instrument"] for k in kept} | {
            e.get("instrument") for e in out.excluded if isinstance(e, dict)
        }
        excluded = [e for e in out.excluded if isinstance(e, dict)]
        for f in figures:
            if f.instrument not in named:
                excluded.append(
                    {"instrument": f.instrument, "reason": "not mentioned by the screener"}
                )
        return kept, excluded

    def context(self, role: str, series_path: Path | None, reference: Path | None) -> Any:
        """A measured brief over the operator's series, aligned as-of to a reference dataset."""
        if series_path is None:
            return None
        try:
            series = load_all(series_path)
        except SeriesError as exc:
            raise SessionError(f"{role}: {exc}") from exc
        times, closes = _minute_closes(reference) if reference is not None else ([], [])
        at_ns = times[-1] if times else int(self.started.timestamp() * 1_000_000_000)
        returns = returns_from_closes(times, closes) if times else []
        parse: Callable[[str], Any]
        if role == macro_analyst.ROLE:
            events = read_events(self.cfg.events) if self.cfg.events is not None else ()
            brief = macro_brief(series, at_ns=at_ns, times_ns=times, returns=returns, events=events)
            schema, parse = macro_analyst.SCHEMA, macro_analyst.parse
        else:
            brief = onchain_brief(series, at_ns=at_ns, times_ns=times, returns=returns)
            schema, parse = onchain_analyst.SCHEMA, onchain_analyst.parse
        (self.out / f"{role}-brief.json").write_text(
            json.dumps(brief, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        text = self.ask(role, brief, schema)
        if text is None:
            return None
        try:
            return parse(text)
        except RoleError as exc:
            self.refusals.append(f"{role}: {exc}")
            return None

    def analyse(self, instrument: str, path: Path, limits: Limits) -> RunReport:
        s = self.cfg.strategy
        a = s.agents
        if a is None:  # pragma: no cover - SessionConfig refuses this
            raise SessionError("no [agents]")
        channel = next((c for c in self.cfg.envelope.channels if c.key == s.run.venue_key), None)
        if channel is None:
            raise SessionError(f"venue_key {s.run.venue_key!r} is not a channel in the envelope")
        out = self.out / instrument
        if self.cfg.resume and (out / "run.json").is_file():
            # A run that reached the end of its data is a checkpoint; one that stopped early
            # (a spent budget, five failed calls, a kill switch) is run again, and said so.
            recorded = _report_from_disk(out, instrument)
            if recorded.stopped_by is None:
                self.refusals.append(f"{instrument}: reused the recorded run")
                return recorded
            self.refusals.append(
                f"{instrument}: recorded run stopped by {recorded.stopped_by}; run again"
            )
        core = AgentStrategy(
            self.cfg.completer_for("technical_analyst"),
            mechanism=a.mechanism,
            lookback_bars=a.lookback_bars,
            min_confidence=a.min_confidence,
            decide_every_bars=a.decide_every_bars,
        )
        report = replay_instrument(
            s,
            channel,
            instrument=instrument,
            path=path,
            core=core,
            limits=limits,
            kill_file=self.cfg.kill_file,
            out=out,
            label="session-replay",
        )
        if report.stopped_by and "token budget" in report.stopped_by:
            self.stopped_by = report.stopped_by
        return report

    def risk(
        self, reports: dict[str, RunReport], limits: Limits, ladder: Ladder
    ) -> tuple[Any, Limits]:
        s = self.cfg.strategy
        user = {
            "tier": ladder.tier.name,
            "limits": {
                "max_risk_per_trade_bps": str(limits.max_risk_per_trade_bps),
                "max_position": str(limits.max_position),
                "max_orders_per_minute": limits.max_orders_per_minute,
                "daily_loss_limit_bps": str(limits.daily_loss_limit_bps),
            },
            "declared_latency_ms": str(s.run.latency_ms),
            "registration_magnitude_bps": str(self.cfg.magnitude_bps),
            "measured": risk_brief(
                [(name, self.out / name) for name in reports],
                declared_ms=s.run.latency_ms,
                claimed_bps=self.cfg.magnitude_bps,
            ),
            "runs": {
                name: {
                    "prints": r.prints,
                    "bars": r.bars,
                    "intents": r.intents,
                    "refused": r.refused,
                    "fills": r.fills,
                    "round_trips": r.round_trips,
                    "net_bps_total": str(r.net_bps_total),
                    "stopped_by": r.stopped_by,
                    "refusals": r.refusals[:5],
                }
                for name, r in reports.items()
            },
        }
        text = self.ask(risk_manager.ROLE, user, risk_manager.SCHEMA)
        if text is None:
            return None, limits
        try:
            decision = risk_manager.parse(text)
        except RoleError as exc:
            self.refusals.append(f"risk_manager: {exc}")
            return None, limits
        after = limits
        try:
            if decision.decision == "tighten":
                tightened = tighten_limits(limits, decision.limits)
                after = tightened.after
                if not tightened.changed:
                    self.refusals.append("risk_manager: tighten changed nothing")
            elif decision.decision == "demote":
                demote(ladder, reason=decision.reason)
            elif decision.decision == "halt":
                arm_kill_switch(KillSwitch(), reason=decision.reason, touch=self.cfg.kill_file)
                self.stopped_by = self.stopped_by or f"risk manager halt: {decision.reason}"
        except ActionRefusedError as exc:
            self.refusals.append(f"risk_manager: {exc}")
        return decision, after

    def allocate(self, kept: list[dict[str, Any]], limits: Limits) -> list[dict[str, Any]]:
        if not kept:
            return []
        user = {
            "instruments": [k["instrument"] for k in kept],
            "max_position": str(limits.max_position),
            "declared_size": str(self.cfg.strategy.run.fixed_size()),
            "gross_exposure_cap": "1.0",
        }
        text = self.ask(executor.ROLE, user, executor.SCHEMA)
        if text is None:
            return []
        try:
            out = executor.parse(text)
        except RoleError as exc:
            self.refusals.append(f"executor: {exc}")
            return []
        names = {k["instrument"] for k in kept}
        rows = [r for r in out.allocations if isinstance(r, dict) and r.get("instrument") in names]
        total = sum(float(r.get("fraction", 0)) for r in rows)
        if total > 1.0 + 1e-9:
            self.refusals.append(f"executor: allocations sum to {total:.3f}; capped at 1")
            return []
        return rows

    def audit(
        self,
        reports: dict[str, RunReport],
        risk: Any,
        allocation: list[dict[str, Any]],
        ladder: Ladder,
    ) -> dict[str, Any] | None:
        usage = self.cfg.usage
        by_role = usage.by_role()
        round_trips = sum(r.round_trips for r in reports.values())
        measured = audit_brief(
            usage_path=usage.path,
            round_trips=round_trips,
            run_dir=None,
            replay_dir=None,
            ladder_path=self.cfg.ladder_path,
        )
        user = {
            "measured": measured,
            "runs": {
                name: {
                    "round_trips": r.round_trips,
                    "net_bps_total": str(r.net_bps_total),
                    "refused": r.refused,
                    "stopped_by": r.stopped_by,
                }
                for name, r in reports.items()
            },
            "usage_by_role": by_role,
            "tokens_total": usage.tokens,
            "cost_total": str(usage.cost),
            "tokens_per_round_trip": None if round_trips == 0 else usage.tokens / round_trips,
            "risk_decision": None if risk is None else risk.decision,
            "allocation": allocation,
            "ladder_history": list(ladder.history),
            "refusals": self.refusals,
        }
        out = self._audit_with_tools(user)
        if out is None:
            return None
        record = {
            "verdict": out.verdict,
            "summary": out.summary,
            "findings": out.findings,
            "tokens_total": usage.tokens,
            "cost_total": str(usage.cost),
            "usage_by_role": by_role,
        }
        (self.out / "audit.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        return record

    def _audit_with_tools(self, user: dict[str, Any]) -> Any:
        """The auditor may read any run, the ledger and the ladder before it answers."""
        if self.stopped_by is not None:
            return None
        recorded = self._recorded(auditor.ROLE)
        if recorded is not None:
            self.calls += 1
            try:
                return auditor.parse(recorded)
            except RoleError:
                return None
        # The model writes the paths these tools open, so they are held to the project, the
        # session and the ladder's own directory, and never to a hidden file inside any of them.
        tools = confine_all(
            {
                "run_report": run_report,
                "ledger_show": ledger_show,
                "ledger_search": ledger_search,
                "runs_index": runs_index,
                "ladder_show": ladder_show,
            },
            roots_of((Path.cwd(), self.out, self.cfg.ladder_path.parent)),
        )
        try:
            loop = run_tool_loop(
                completer=self.cfg.completer_for(auditor.ROLE),
                system=prompt(auditor.ROLE),
                question={
                    **user,
                    "session_dir": str(self.out),
                    "ladder_path": str(self.cfg.ladder_path),
                },
                tools=tools,
                answer_schema=auditor.SCHEMA,
                max_steps=self.cfg.auditor_steps,
            )
        except ToolLoopError as exc:
            self.refusals.append(f"auditor: {exc}")
            return None
        except (RoleError, RuntimeError, OSError, ValueError) as exc:
            self.refusals.append(f"auditor: {exc.__class__.__name__}: {exc}")
            return None
        self.calls += 1
        response = json.dumps(loop.answer, sort_keys=True)
        (self.out / f"{self.calls:02d}-{auditor.ROLE}.json").write_text(
            json.dumps(
                {
                    "role": auditor.ROLE,
                    "user": user,
                    "response": response,
                    "steps": [s.record() for s in loop.steps],
                    "calls": loop.calls,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            return auditor.parse(response)
        except RoleError as exc:
            self.refusals.append(f"auditor: {exc}")
            return None


def replay_instrument(
    s: StrategyConfig,
    channel: Any,
    *,
    instrument: str,
    path: Path,
    core: Any,
    limits: Limits,
    kill_file: Path | None,
    out: Path,
    label: str,
) -> RunReport:
    """The replay every session and eval runs: recorded prints, simulated venue, one core."""
    config = TradingConfig(
        feed=RecordedFeed.from_parquet(path, instrument=instrument),
        venue=SimulatedVenue(
            channel=channel,
            funding=s.funding or FundingSchedule(interval_s=s.run.settlement_interval_s, rates={}),
            latency_ns=int(s.run.latency_ms * 1_000_000),
            slippage_bps=s.run.slippage_bps,
        ),
        strategy=core,
        exit_rule=s.exit_rule,
        controls=Controls(limits, KillSwitch(kill_file)),
        out_dir=out,
        bar_interval_ns=s.run.bar_interval_s * 1_000_000_000,
        size=s.run.fixed_size(),
        spread_cost_bps=s.run.spread_cost_bps,
        equity=s.run.equity,
        label=label,
    )
    return run(config)


def _report_from_disk(out: Path, instrument: str) -> RunReport:
    summary = json.loads((out / "run.json").read_text("utf-8"))
    report = RunReport(
        label=str(summary.get("label", "session-replay")),
        instrument=instrument,
        started_at=datetime.fromisoformat(str(summary["started_at"])),
    )
    report.finished_at = datetime.fromisoformat(str(summary["finished_at"]))
    for key in ("prints", "bars", "intents", "refused", "fills", "round_trips"):
        setattr(report, key, int(summary.get(key, 0)))
    report.net_bps_total = Decimal(str(summary.get("net_bps_total", "0")))
    report.stopped_by = summary.get("stopped_by")
    report.open_position = summary.get("open_position")
    return report


def run_session(cfg: SessionConfig) -> SessionResult:
    s = _Session(cfg)
    venue = cfg.strategy.run.venue_key.partition("/")[0]

    def tell(step: str, status: str, summary: str = "") -> None:
        if cfg.on_step is not None:
            cfg.on_step(step, status, summary)

    tell("figures", "running", f"{len(cfg.datasets)} datasets")
    figures = [
        figures_for(
            name,
            path,
            cfg.envelope,
            venue,
            cfg.magnitude_bps,
            screen=cfg.screen,
            observation=cfg.observation,
        )
        for name, path in cfg.datasets.items()
    ]
    (s.out / "figures.json").write_text(
        json.dumps([f.record() for f in figures], indent=2, sort_keys=True) + "\n", "utf-8"
    )
    tell("figures", "done", f"{len(figures)} measured")
    tell("market_screener", "running")
    kept, excluded = s.screen(figures)
    tell("market_screener", "done", f"{len(kept)} kept, {len(excluded)} excluded")
    reference = next(iter(cfg.datasets.values()))
    tell("macro_analyst", "running" if cfg.macro_series else "skipped")
    macro = s.context(macro_analyst.ROLE, cfg.macro_series, reference)
    if cfg.macro_series:
        tell("macro_analyst", "done", "" if macro is None else str(macro.regime))
    tell("onchain_analyst", "running" if cfg.onchain_series else "skipped")
    onchain = s.context(onchain_analyst.ROLE, cfg.onchain_series, reference)
    if cfg.onchain_series:
        tell("onchain_analyst", "done", "" if onchain is None else str(onchain.signal))
    limits = cfg.strategy.limits
    ladder = Ladder(cfg.ladder_path)
    reports: dict[str, RunReport] = {}
    for k in kept:
        if s.stopped_by is not None:
            break
        name = str(k["instrument"])
        tell(f"run:{name}", "running")
        reports[name] = s.analyse(name, Path(cfg.datasets[name]), limits)
        r = reports[name]
        tell(f"run:{name}", "done", f"{r.round_trips} round trips, net {r.net_bps_total} bps")
    tell("risk_manager", "running" if reports else "skipped")
    risk, limits_after = s.risk(reports, limits, ladder) if reports else (None, limits)
    if reports:
        tell("risk_manager", "done", "refused" if risk is None else str(risk.decision))
    tell("executor", "running")
    allocation = s.allocate(kept, limits_after)
    tell("executor", "done", f"{len(allocation)} allocations")
    tell("auditor", "running")
    audit = s.audit(reports, risk, allocation, ladder)
    tell("auditor", "done", "refused" if audit is None else str(audit.get("verdict")))
    result = SessionResult(
        session_id=s.session_id,
        figures=figures,
        kept=kept,
        excluded=excluded,
        macro=None if macro is None else macro.__dict__,
        onchain=None if onchain is None else onchain.__dict__,
        reports=reports,
        risk=None if risk is None else risk.__dict__,
        limits_after=limits_after,
        allocation=allocation,
        audit=audit,
        refusals=s.refusals,
        stopped_by=s.stopped_by,
    )
    summary = {
        "session_id": result.session_id,
        "started_at": s.started.isoformat(),
        "finished_at": cfg.clock().isoformat(),
        "kept": kept,
        "excluded": excluded,
        "macro": result.macro,
        "onchain": result.onchain,
        "runs": {
            name: {"round_trips": r.round_trips, "net_bps_total": str(r.net_bps_total)}
            for name, r in reports.items()
        },
        "risk": result.risk,
        "limits_after": {
            "max_risk_per_trade_bps": str(limits_after.max_risk_per_trade_bps),
            "max_position": str(limits_after.max_position),
            "max_orders_per_minute": limits_after.max_orders_per_minute,
            "daily_loss_limit_bps": str(limits_after.daily_loss_limit_bps),
        },
        "allocation": allocation,
        "audit_verdict": None if audit is None else audit["verdict"],
        "tokens_total": cfg.usage.tokens,
        "cost_total": str(cfg.usage.cost),
        "refusals": s.refusals,
        "stopped_by": s.stopped_by,
    }
    (s.out / "session.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return result
