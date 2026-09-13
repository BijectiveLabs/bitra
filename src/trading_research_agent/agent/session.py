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
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from trading_research_agent.agent import (
    auditor,
    executor,
    macro_analyst,
    market_screener,
    onchain_analyst,
    risk_manager,
)
from trading_research_agent.agent.completer import Completer
from trading_research_agent.agent.roles import RoleError, prompt
from trading_research_agent.agent.strategy import AgentStrategy
from trading_research_agent.agent.usage import BudgetSpentError, UsageLedger
from trading_research_agent.chain import cost_floor
from trading_research_agent.data.parquet import read_trades
from trading_research_agent.envelope.model import Envelope
from trading_research_agent.execution.cost import FundingSchedule
from trading_research_agent.surface.actions import (
    ActionRefusedError,
    arm_kill_switch,
    demote,
    tighten_limits,
)
from trading_research_agent.trading import (
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
from trading_research_agent.trading.controls import Limits

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

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "rows": self.rows,
            "first_ts_ns": self.first_ts_ns,
            "last_ts_ns": self.last_ts_ns,
            "prints_per_minute": round(self.prints_per_minute, 3),
            "realised_vol_bps": round(self.realised_vol_bps, 3),
            "turnover": str(self.turnover),
            "cost_floor": self.floor_outcome,
            "floor_margin_bps": self.floor_margin_bps,
        }


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
    instrument: str, path: Path, envelope: Envelope, venue: str, magnitude_bps: Decimal
) -> Figures:
    rows = 0
    first = last = None
    turnover = Decimal(0)
    closes: dict[int, Decimal] = {}
    for trade in read_trades(path):
        rows += 1
        first = trade.ts_ns if first is None else first
        last = trade.ts_ns
        turnover += trade.qty
        closes[trade.ts_ns // NS_PER_MINUTE] = trade.price
    if rows == 0 or first is None or last is None:
        raise SessionError(f"{instrument}: the dataset has no trades")
    minutes = max((last - first) / NS_PER_MINUTE, 1.0)
    series = [float(closes[k]) for k in sorted(closes)]
    returns = [math.log(b / a) for a, b in pairwise(series) if a > 0 and b > 0]
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        vol_bps = math.sqrt(var) * 1e4
    else:
        vol_bps = 0.0
    check = cost_floor(envelope, magnitude_bps, venue=venue)
    return Figures(
        instrument=instrument,
        rows=rows,
        first_ts_ns=first,
        last_ts_ns=last,
        prints_per_minute=rows / minutes,
        realised_vol_bps=vol_bps,
        turnover=turnover,
        floor_outcome=check.outcome.value,
        floor_margin_bps=str(check.margin_bps),
    )


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

    def ask(self, role: str, user: dict[str, Any], schema: dict[str, Any]) -> str | None:
        """One call, recorded. A refusal of form or budget is a None the caller handles."""
        if self.stopped_by is not None:
            return None
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
            return None
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

    def context(
        self, role: str, series_path: Path | None, schema: dict[str, Any], parse: Any
    ) -> Any:
        if series_path is None:
            return None
        try:
            series = json.loads(series_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"{role}: cannot read {series_path}: {exc}") from exc
        text = self.ask(role, {"series": series}, schema)
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
        core = AgentStrategy(
            self.cfg.completer_for("technical_analyst"),
            mechanism=a.mechanism,
            lookback_bars=a.lookback_bars,
            min_confidence=a.min_confidence,
        )
        out = self.out / instrument
        config = TradingConfig(
            feed=RecordedFeed.from_parquet(path, instrument=instrument),
            venue=SimulatedVenue(
                channel=channel,
                funding=s.funding
                or FundingSchedule(interval_s=s.run.settlement_interval_s, rates={}),
                latency_ns=int(s.run.latency_ms * 1_000_000),
                slippage_bps=s.run.slippage_bps,
            ),
            strategy=core,
            exit_rule=s.exit_rule,
            controls=Controls(limits, KillSwitch(self.cfg.kill_file)),
            out_dir=out,
            bar_interval_ns=s.run.bar_interval_s * 1_000_000_000,
            size=s.run.size,
            spread_cost_bps=s.run.spread_cost_bps,
            equity=s.run.equity,
            label="session-replay",
        )
        report = run(config)
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
            "measured_latency_ms": "not measured in replay",
            "registration_magnitude_bps": str(self.cfg.magnitude_bps),
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
            "declared_size": str(self.cfg.strategy.run.size),
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
        user = {
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
        text = self.ask(auditor.ROLE, user, auditor.SCHEMA)
        if text is None:
            return None
        try:
            out = auditor.parse(text)
        except RoleError as exc:
            self.refusals.append(f"auditor: {exc}")
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


def run_session(cfg: SessionConfig) -> SessionResult:
    s = _Session(cfg)
    venue = cfg.strategy.run.venue_key.partition("/")[0]
    figures = [
        figures_for(name, path, cfg.envelope, venue, cfg.magnitude_bps)
        for name, path in cfg.datasets.items()
    ]
    (s.out / "figures.json").write_text(
        json.dumps([f.record() for f in figures], indent=2, sort_keys=True) + "\n", "utf-8"
    )
    kept, excluded = s.screen(figures)
    macro = s.context(
        macro_analyst.ROLE, cfg.macro_series, macro_analyst.SCHEMA, macro_analyst.parse
    )
    onchain = s.context(
        onchain_analyst.ROLE, cfg.onchain_series, onchain_analyst.SCHEMA, onchain_analyst.parse
    )
    limits = cfg.strategy.limits
    ladder = Ladder(cfg.ladder_path)
    reports: dict[str, RunReport] = {}
    for k in kept:
        if s.stopped_by is not None:
            break
        name = str(k["instrument"])
        reports[name] = s.analyse(name, Path(cfg.datasets[name]), limits)
    risk, limits_after = s.risk(reports, limits, ladder) if reports else (None, limits)
    allocation = s.allocate(kept, limits_after)
    audit = s.audit(reports, risk, allocation, ladder)
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
