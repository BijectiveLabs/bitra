"""One analyst, one dataset, several models: what each earned and what each cost.

An eval here is not a benchmark score. It is the replay every session runs, once per model,
reduced to the figures a trader compares: net basis points after costs, round trips, calls,
tokens, latency at the median and the 90th percentile, and tokens per basis point earned. A
model that earned nothing has no tokens-per-bps figure; the column stays empty rather than
dividing by zero. Every model sees the same bars in the same order under the same controls,
so the only thing that differs between rows is the model.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents.session import SessionError
from trading_research_agents.agents.session_replay import replay_instrument
from trading_research_agents.agents.strategy import AgentStrategy
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import (
    BudgetLimits,
    MeteredCompleter,
    Pricing,
    UsageLedger,
    UsageRecord,
)
from trading_research_agents.trading import RunReport, StrategyConfig

ROLE = "technical_analyst"


@dataclass(frozen=True)
class ModelSpec:
    """`PROVIDER:MODEL` on the command line; the model may itself contain a colon or a slash."""

    provider: str
    model: str

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        provider, sep, model = text.partition(":")
        if not sep or not provider.strip() or not model.strip():
            raise ValueError(f"--model takes PROVIDER:MODEL, got {text!r}")
        return cls(provider.strip(), model.strip())

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{self.provider}-{self.model}").strip("-")

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class EvalRow:
    provider: str
    model: str
    instrument: str
    bars: int
    intents: int
    refused: int
    fills: int
    round_trips: int
    net_bps_total: Decimal
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost: Decimal | None
    latency_median_ms: int
    latency_p90_ms: int
    tokens_per_bps: Decimal | None
    stopped_by: str | None
    out: Path

    def record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "instrument": self.instrument,
            "bars": self.bars,
            "intents": self.intents,
            "refused": self.refused,
            "fills": self.fills,
            "round_trips": self.round_trips,
            "net_bps_total": str(self.net_bps_total),
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": None if self.cost is None else str(self.cost),
            "latency_median_ms": self.latency_median_ms,
            "latency_p90_ms": self.latency_p90_ms,
            "tokens_per_bps": None if self.tokens_per_bps is None else str(self.tokens_per_bps),
            "stopped_by": self.stopped_by,
            "out": str(self.out),
        }


@dataclass(frozen=True)
class EvalConfig:
    strategy: StrategyConfig
    envelope: Envelope
    instrument: str
    dataset: Path
    models: tuple[ModelSpec, ...]
    completer_for: Callable[[ModelSpec], Completer]
    out_dir: Path
    kill_file: Path | None = None

    def __post_init__(self) -> None:
        if self.strategy.agents is None:
            raise SessionError("an eval needs [strategy].agent and [agents] in strategy.toml")
        if not self.models:
            raise ValueError("an eval compares at least one model")
        if len({m.slug for m in self.models}) != len(self.models):
            raise ValueError("the same model is named twice")
        if not self.dataset.is_file():
            raise FileNotFoundError(self.dataset)


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile; an empty list reads as zero, not as an error."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def summarise(spec: ModelSpec, report: RunReport, records: list[UsageRecord], out: Path) -> EvalRow:
    latencies = [r.latency_ms for r in records]
    tokens = sum(r.input_tokens + r.output_tokens for r in records)
    costs = [r.cost for r in records if r.cost is not None]
    per_bps = None
    if report.net_bps_total > 0 and tokens:
        per_bps = (Decimal(tokens) / report.net_bps_total).quantize(Decimal("0.1"))
    return EvalRow(
        provider=spec.provider,
        model=spec.model,
        instrument=report.instrument,
        bars=report.bars,
        intents=report.intents,
        refused=report.refused,
        fills=report.fills,
        round_trips=report.round_trips,
        net_bps_total=report.net_bps_total,
        calls=len(records),
        failed_calls=sum(1 for r in records if r.outcome != "ok"),
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        cost=sum(costs, Decimal(0)) if costs else None,
        latency_median_ms=percentile(latencies, 0.5),
        latency_p90_ms=percentile(latencies, 0.9),
        tokens_per_bps=per_bps,
        stopped_by=report.stopped_by,
        out=out,
    )


def evaluate(cfg: EvalConfig) -> list[EvalRow]:
    """Run the analyst once per model over the dataset; write `eval.json`; return the rows."""
    s = cfg.strategy
    a = s.agents
    if a is None:  # pragma: no cover - EvalConfig refuses this
        raise SessionError("no [agents]")
    channel = next((c for c in cfg.envelope.channels if c.key == s.run.venue_key), None)
    if channel is None:
        raise SessionError(f"venue_key {s.run.venue_key!r} is not a channel in the envelope")
    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {m: Pricing(i, o) for m, (i, o) in a.pricing.items()}
    rows: list[EvalRow] = []
    for spec in cfg.models:
        out = cfg.out_dir / spec.slug
        out.mkdir(parents=True, exist_ok=True)
        # Each model has its own ledger and its own budget: one that runs out does not starve
        # the next, and the rows stay comparable.
        usage = UsageLedger(out / "usage.jsonl")
        metered = MeteredCompleter(
            cfg.completer_for(spec),
            role=ROLE,
            provider=spec.provider,
            limits=limits,
            ledger=usage,
            pricing=pricing,
        )
        core = AgentStrategy(
            metered,
            mechanism=a.mechanism,
            lookback_bars=a.lookback_bars,
            min_confidence=a.min_confidence,
            decide_every_bars=a.decide_every_bars,
        )
        report = replay_instrument(
            s,
            channel,
            instrument=cfg.instrument,
            path=cfg.dataset,
            core=core,
            limits=s.limits,
            kill_file=cfg.kill_file,
            out=out,
            label=f"eval-{spec.slug}",
        )
        rows.append(summarise(spec, report, usage.records, out))
    (cfg.out_dir / "eval.json").write_text(
        json.dumps(
            {
                "instrument": cfg.instrument,
                "dataset": str(cfg.dataset),
                "bar_interval_s": s.run.bar_interval_s,
                "decide_every_bars": a.decide_every_bars,
                "rows": [r.record() for r in rows],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return rows


def table(rows: list[EvalRow]) -> list[str]:
    """The rows as fixed-width text, one line per model, in the order they were run."""
    columns = "{:<34} {:>9} {:>4} {:>6} {:>5} {:>9} {:>7} {:>7} {:>9} {:>8}"
    lines = [
        columns.format(
            "model",
            "net bps",
            "rt",
            "calls",
            "fail",
            "tokens",
            "med ms",
            "p90 ms",
            "tok/bps",
            "cost",
        )
    ]
    for r in rows:
        name = str(ModelSpec(r.provider, r.model))
        if len(name) > 34:
            name = name[:31] + "..."
        lines.append(
            columns.format(
                name,
                str(r.net_bps_total),
                r.round_trips,
                r.calls,
                r.failed_calls,
                r.input_tokens + r.output_tokens,
                r.latency_median_ms,
                r.latency_p90_ms,
                "" if r.tokens_per_bps is None else str(r.tokens_per_bps),
                "" if r.cost is None else str(r.cost),
            )
        )
    return lines
