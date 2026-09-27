"""One cycle: what the engine saw, what the agents said, what the cost gate decided.

The order is fixed and short. Code takes a snapshot. Code builds the briefs. Agents read the
briefs, in parallel, and answer inside a schema. Code prices the round trip on the book it is
holding and compares it with the edge the analyst claimed. Only then is there an intent, and in
the default mode that intent goes to a file rather than to a venue.

Everything the cycle touched stays in the cycle's directory, so an auditor can replay the
reasoning without asking anyone to remember it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents import technical_analyst
from trading_research_agents.agents.briefs import (
    BriefError,
    cost_brief,
    data_brief,
    technical_brief,
)
from trading_research_agents.agents.parallel import FanOut, Task, run_tasks
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.data.schema import Direction
from trading_research_agents.data.sources.chain import SourceChain
from trading_research_agents.envelope.model import Channel, Envelope
from trading_research_agents.execution.pretrade import PreTradeVerdict, admit
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import BudgetSpentError, UsageLedger
from trading_research_agents.store.cycles import CycleDir
from trading_research_agents.store.snapshot import Snapshot, cycle_id, take_snapshot

MODES = ("sandbox", "signals")
TARGETS = {"long": Direction.LONG, "short": Direction.SHORT}


class CycleError(ValueError):
    """The cycle cannot run as configured, or cannot run at all on what the engine returned."""


@dataclass(frozen=True)
class CycleConfig:
    """Everything one cycle needs, and nothing it should decide for itself."""

    instrument: str
    envelope: Envelope
    venue_key: str
    chains: Mapping[str, SourceChain]
    completer_for: Callable[[str], Completer]
    usage: UsageLedger
    root: Path
    mechanism: str
    size: Decimal
    magnitude_bps: Decimal
    bar_interval_s: int = 60
    lookback_bars: int = 60
    interval_s: int = 60
    settlement_interval_s: int = 28_800
    latency_slippage_bps: Decimal = Decimal(0)
    margin_bps: Decimal = Decimal(0)
    min_confidence: float = 0.6
    mode: str = "sandbox"
    resume: bool = False
    max_workers: int = 4
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise CycleError("a cycle names its instrument")
        if self.mode not in MODES:
            raise CycleError(f"mode is one of {list(MODES)}; venue execution is not in this build")
        if not isinstance(self.size, Decimal) or self.size <= 0:
            raise CycleError("size is a positive Decimal")
        if not isinstance(self.magnitude_bps, Decimal) or self.magnitude_bps <= 0:
            raise CycleError("magnitude_bps is the edge the operator claims, in bps, positive")
        if self.channel is None:
            raise CycleError(f"venue_key {self.venue_key!r} is not a channel in the envelope")

    @property
    def channel(self) -> Channel | None:
        return next((c for c in self.envelope.channels if c.key == self.venue_key), None)


@dataclass
class CycleResult:
    """What the cycle did, in the order a reader wants it."""

    cycle: str
    instrument: str
    snapshot: Snapshot
    briefs: dict[str, Any] = field(default_factory=dict)
    fan: FanOut = field(default_factory=FanOut)
    analyst: Any = None
    verdict: PreTradeVerdict | None = None
    intent: dict[str, Any] | None = None
    refusals: list[str] = field(default_factory=list)
    stopped_by: str | None = None
    path: Path | None = None

    @property
    def acted(self) -> bool:
        return self.intent is not None

    def record(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle,
            "instrument": self.instrument,
            "data": self.briefs.get("data"),
            "agents": self.fan.record(),
            "analyst": None if self.analyst is None else vars(self.analyst),
            "cost": None if self.verdict is None else self.verdict.record(),
            "intent": self.intent,
            "refusals": self.refusals,
            "stopped_by": self.stopped_by,
            "mode": self.briefs.get("mode"),
        }


def _ask(
    cfg: CycleConfig, role: str, question: Mapping[str, Any], schema: Mapping[str, Any]
) -> str:
    completer = cfg.completer_for(role)
    done = completer.complete(
        system=prompt(role),
        user=json.dumps(question, sort_keys=True, default=str),
        schema=dict(schema),
    )
    return done.text


def run_cycle(cfg: CycleConfig) -> CycleResult:
    """Take one snapshot, ask the agents, price the trade, and write the cycle down."""
    cycle = cycle_id(cfg.clock())
    where = CycleDir(root=cfg.root, cycle=cycle).make()
    kinds = list(cfg.chains)
    options = {
        "ohlcv": {"interval_s": cfg.interval_s, "limit": max(cfg.lookback_bars, 2)},
    }
    snapshot = take_snapshot(cfg.instrument, cfg.chains, cycle=cycle, kinds=kinds, options=options)
    snapshot.write(where.snapshot)
    result = CycleResult(cycle=cycle, instrument=cfg.instrument, snapshot=snapshot, path=where.path)
    result.briefs["data"] = data_brief(snapshot).record()
    result.briefs["mode"] = cfg.mode
    for kind, reason in snapshot.refused.items():
        result.refusals.append(f"data {kind}: {reason}")
    for kind in snapshot.stale:
        result.refusals.append(f"data {kind}: used while stale, because the config allows it")

    try:
        technical = technical_brief(
            snapshot,
            mechanism=cfg.mechanism,
            interval_s=cfg.interval_s,
            lookback_bars=cfg.lookback_bars,
        )
    except BriefError as exc:
        result.stopped_by = f"no technical brief: {exc}"
        _finish(cfg, where, result)
        return result
    where.brief("technical").write_text(
        json.dumps(technical.record(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    fan = run_tasks(
        [
            Task(
                name="technical",
                work=lambda: _ask(
                    cfg, technical_analyst.ROLE, technical.record(), technical_analyst.SCHEMA
                ),
                path=where.answer("technical"),
            )
        ],
        max_workers=cfg.max_workers,
        resume=cfg.resume,
    )
    result.fan = fan
    for name, error in fan.failed.items():
        result.refusals.append(f"{name}: {error}")
        if "BudgetSpentError" in error:
            result.stopped_by = f"{name}: {error}"
    if "technical" not in fan.answers:
        result.stopped_by = result.stopped_by or "the technical analyst did not answer"
        _finish(cfg, where, result)
        return result

    try:
        analyst = technical_analyst.parse(str(fan.answers["technical"]))
    except RoleError as exc:
        result.refusals.append(f"technical: {exc}")
        result.stopped_by = f"technical: {exc}"
        _finish(cfg, where, result)
        return result
    result.analyst = analyst

    if analyst.target == "flat":
        result.refusals.append("technical: flat, so there is nothing to price")
        _finish(cfg, where, result)
        return result
    if analyst.confidence < cfg.min_confidence:
        result.refusals.append(
            f"technical: confidence {analyst.confidence} below the declared {cfg.min_confidence}"
        )
        _finish(cfg, where, result)
        return result

    direction = TARGETS[analyst.target]
    try:
        costs = cost_brief(
            snapshot,
            channel=_channel(cfg),
            size=cfg.size,
            direction=direction,
            horizon_bars=analyst.horizon_bars,
            bar_interval_s=cfg.bar_interval_s,
            settlement_interval_s=cfg.settlement_interval_s,
            latency_slippage_bps=cfg.latency_slippage_bps,
        )
    except BriefError as exc:
        result.refusals.append(f"cost: {exc}")
        result.stopped_by = f"cost: {exc}"
        _finish(cfg, where, result)
        return result
    result.briefs["cost"] = costs.record()
    where.brief("cost").write_text(
        json.dumps(costs.record(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    verdict = admit(
        edge_bps=cfg.magnitude_bps,
        cost=costs.cost,
        margin_bps=cfg.margin_bps,
        edge_source="[agents].magnitude_bps, declared by the operator and not measured here",
    )
    result.verdict = verdict
    if not verdict.admitted:
        result.refusals.append(f"cost gate: {verdict.reason}")
        _finish(cfg, where, result)
        return result

    result.intent = {
        "instrument": cfg.instrument,
        "target": analyst.target,
        "size": str(cfg.size),
        "horizon_bars": analyst.horizon_bars,
        "confidence": analyst.confidence,
        "reason": analyst.reason,
        "edge_bps": str(cfg.magnitude_bps),
        "edge_source": verdict.edge_source,
        "net_bps": str(verdict.net_bps),
        "mode": cfg.mode,
        "mid": str(costs.cost.mid),
        "at": cycle,
    }
    with (where.path / "intents.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.intent, sort_keys=True) + "\n")
    _finish(cfg, where, result)
    return result


def _channel(cfg: CycleConfig) -> Channel:
    channel = cfg.channel
    if channel is None:  # pragma: no cover - CycleConfig refuses this
        raise CycleError(f"venue_key {cfg.venue_key!r} is not a channel in the envelope")
    return channel


def _finish(cfg: CycleConfig, where: CycleDir, result: CycleResult) -> None:
    payload = result.record()
    payload["tokens"] = cfg.usage.tokens
    payload["cost_declared"] = str(cfg.usage.cost)
    payload["finished_at"] = cfg.clock().isoformat()
    where.write_summary(payload)


def budget_spent(error: str) -> bool:
    """Whether a failure was the budget refusing, which ends a cycle rather than an agent."""
    return BudgetSpentError.__name__ in error
