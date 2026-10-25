"""`tra arena`: every row replayed over the same archive, settled the same way, side by side.

One clock drives every row: decisions at the closes of the decision interval inside the window, one
open idea per market per row, entry at the next minute's open. The v1 record is the only row with
times of its own. A run reads the archive and the declared costs and nothing else, so the same
archive, configuration and seed give the same `arena.json`, byte for byte.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trading_research_agents import __version__
from trading_research_agents.evaluation.baselines import BASELINES_VERSION, HOLD_BARS
from trading_research_agents.evaluation.history import MarketHistory, Views, load_history
from trading_research_agents.evaluation.policy import DecisionView, Policy, Scored, TradeIdea
from trading_research_agents.evaluation.rows import RANDOM, V1_RECORD, make_policy
from trading_research_agents.evaluation.settle import (
    Costs,
    Funding,
    Settlement,
    Unsettled,
    funding_schedule,
    settle,
)
from trading_research_agents.evaluation.splits import walk_forward
from trading_research_agents.evaluation.stats import Window, row_report
from trading_research_agents.evaluation.v1 import recorded_ideas
from trading_research_agents.features import Absent
from trading_research_agents.features.cross import REFERENCE
from trading_research_agents.market_data.bars import GapState, MarketDataError
from trading_research_agents.market_data.days import DAY_NS, NS_PER_S, day_of, midnight_ns
from trading_research_agents.market_data.deriv import LEVELS

WARMUP_DAYS = 31
V1_LABEL = "live record, not replay"


@dataclass(frozen=True)
class ArenaConfig:
    venue: str
    instruments: tuple[str, ...]
    start: date
    end: date
    rows: tuple[str, ...]
    costs: Costs
    interval_s: int = 3600
    context_interval_s: int = 14_400
    context: tuple[str, ...] = (REFERENCE,)
    seed: int = 7
    funding: str = "archive"
    v1_hold_s: int = 14_400
    fit_days: int | None = None
    test_days: int | None = None

    def __post_init__(self) -> None:
        if not self.instruments or len(set(self.instruments)) != len(self.instruments):
            raise MarketDataError("an arena names each instrument once, and at least one")
        if not self.rows or len(set(self.rows)) != len(self.rows):
            raise MarketDataError("an arena names each row once, and at least one")
        if self.end < self.start:
            raise MarketDataError(f"the window ends ({self.end}) before it starts ({self.start})")
        spans = (self.interval_s, self.context_interval_s, self.v1_hold_s)
        if any(seconds <= 0 or seconds % 60 for seconds in spans):
            raise MarketDataError("intervals and holds are positive whole numbers of minutes")
        if self.funding not in ("archive", "none"):
            raise MarketDataError("funding is 'archive' (measured) or 'none' (a market without)")
        if (self.fit_days is None) != (self.test_days is None):
            raise MarketDataError("walk-forward splits need both fit_days and test_days")

    @property
    def interval_ns(self) -> int:
        return self.interval_s * NS_PER_S

    def record(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "instruments": list(self.instruments),
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "rows": list(self.rows),
            "costs": self.costs.record(),
            "interval_s": self.interval_s,
            "context_interval_s": self.context_interval_s,
            "context": list(self.context),
            "seed": self.seed,
            "funding": self.funding,
            "v1_hold_s": self.v1_hold_s,
            "fit_days": self.fit_days,
            "test_days": self.test_days,
        }


@dataclass(frozen=True)
class ArenaResult:
    config: ArenaConfig
    rows: tuple[dict[str, Any], ...]
    ideas: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    manifests: dict[str, str]
    splits: tuple[dict[str, Any], ...]

    def record(self) -> dict[str, Any]:
        return {
            "config": self.config.record(),
            "baselines_version": BASELINES_VERSION,
            "package_version": __version__,
            "manifests": self.manifests,
            "rows": list(self.rows),
            "coverage": self.coverage,
            "splits": list(self.splits),
        }


@dataclass
class _Row:
    name: str
    settled: list[Settlement] = field(default_factory=list)
    unsettled: list[Unsettled] = field(default_factory=list)
    ic: list[tuple[str, float, float]] = field(default_factory=list)
    contaminated: bool = False


class _Run:
    """The state of one run: the markets, the rows, and when each row's market is free again."""

    def __init__(
        self, config: ArenaConfig, histories: dict[str, MarketHistory], window: Window
    ) -> None:
        self.config = config
        self.histories = histories
        self.window = window
        self.rows = {name: _Row(name) for name in config.rows}
        self.funding: dict[str, Funding] = {
            name: None if config.funding == "none" else funding_schedule(histories[name].points)
            for name in config.instruments
        }
        self.policies: dict[str, Policy] = {
            name: make_policy(
                name, seed=config.seed, end_ns=window.end_ns, interval_ns=config.interval_ns
            )
            for name in config.rows
            if name != V1_RECORD
        }
        for name, policy in self.policies.items():
            cutoff = policy.knowledge_cutoff
            self.rows[name].contaminated = cutoff is not None and cutoff > config.start
        self.free_at: dict[tuple[str, str], int] = {}
        self.absent: Counter[tuple[str, str]] = Counter()

    def replay(self, views: Views) -> None:
        step = self.config.interval_ns
        for at_ns in range(self.window.start_ns + step, self.window.end_ns, step):
            for instrument in self.config.instruments:
                view = views.view(instrument, at_ns)
                for feature, value in view.features[view.primary].items():
                    if isinstance(value, Absent):
                        self.absent[(feature, value.reason)] += 1
                for name, policy in self.policies.items():
                    self._decide(self.rows[name], policy, view)

    def _decide(self, row: _Row, policy: Policy, view: DecisionView) -> None:
        if isinstance(policy, Scored):
            score = policy.score(view)
            after = self.histories[view.instrument].next_return(self.config.interval_ns, view.at_ns)
            if score is not None and after is not None:
                row.ic.append((day_of(view.at_ns).isoformat(), score, after))
        if self.free_at.get((row.name, view.instrument), 0) > view.at_ns:
            return
        idea = policy.decide(view)
        if idea is not None:
            hold_ns = idea.max_hold_bars * self.config.interval_ns
            self._settle(row, view.instrument, idea, view.at_ns, hold_ns)

    def _settle(
        self, row: _Row, instrument: str, idea: TradeIdea, decided_ns: int, hold_ns: int
    ) -> None:
        outcome = settle(
            row=row.name,
            instrument=instrument,
            idea=idea,
            decided_ns=decided_ns,
            hold_ns=hold_ns,
            tape=self.histories[instrument].tape,
            costs=self.config.costs,
            funding=self.funding[instrument],
        )
        if isinstance(outcome, Settlement):
            row.settled.append(outcome)
            self.free_at[(row.name, instrument)] = outcome.exit_ns
        else:
            row.unsettled.append(outcome)

    def recorded(self, cycles: Path) -> None:
        row = self.rows[V1_RECORD]
        hold_ns = self.config.v1_hold_s * NS_PER_S
        found = recorded_ideas(
            cycles, self.config.instruments, self.window.start_ns, self.window.end_ns, hold_ns
        )
        for recorded in found:
            if self.free_at.get((V1_RECORD, recorded.instrument), 0) > recorded.decided_ns:
                continue
            self._settle(row, recorded.instrument, recorded.idea, recorded.decided_ns, hold_ns)


def run_arena(config: ArenaConfig, *, root: Path, cycles: Path | None = None) -> ArenaResult:
    """Every row of `config` over the archive under `root`; the v1 record is read from `cycles`."""
    window = Window(
        midnight_ns(config.start), midnight_ns(config.end) + DAY_NS, len(config.instruments)
    )
    histories, manifests = _load(config, root)
    views = Views(
        histories,
        primary_ns=config.interval_ns,
        context_ns=config.context_interval_s * NS_PER_S,
        context=config.context,
    )
    run = _Run(config, histories, window)
    run.replay(views)
    if V1_RECORD in run.rows and cycles is not None:
        run.recorded(cycles)
    return ArenaResult(
        config=config,
        rows=tuple(_reports(config, run.rows, window)),
        ideas=tuple(_ideas(run.rows)),
        coverage=_coverage(config, histories, window, run.absent),
        manifests=manifests,
        splits=tuple(_splits(config, run.rows)),
    )


def _load(config: ArenaConfig, root: Path) -> tuple[dict[str, MarketHistory], dict[str, str]]:
    longest_s = max(HOLD_BARS * config.interval_s, config.v1_hold_s)
    first = config.start - timedelta(days=WARMUP_DAYS)
    last = config.end + timedelta(days=math.ceil(longest_s / 86_400) + 1)
    intervals = (config.interval_ns, config.context_interval_s * NS_PER_S)
    histories: dict[str, MarketHistory] = {}
    manifests: dict[str, str] = {}
    for name in dict.fromkeys((*config.instruments, *config.context)):
        loaded = load_history(root, config.venue, name, first, last, intervals)
        histories[name] = loaded.history
        manifests.update(loaded.manifests)
    return histories, dict(sorted(manifests.items()))


def _reports(
    config: ArenaConfig, rows: dict[str, _Row], window: Window
) -> Iterator[dict[str, Any]]:
    null = rows[RANDOM].settled if RANDOM in rows else None
    for name in config.rows:
        row = rows[name]
        report = row_report(
            name,
            row.settled,
            row.unsettled,
            window,
            null=None if name == RANDOM else null,
            ic=row.ic,
            contaminated=row.contaminated,
            null_row=name == RANDOM,
        )
        if name == V1_RECORD:
            report["label"] = V1_LABEL
        yield report


def _ideas(rows: dict[str, _Row]) -> list[dict[str, Any]]:
    found = [s.record() for row in rows.values() for s in row.settled]
    found += [u.record() for row in rows.values() for u in row.unsettled]
    return sorted(found, key=lambda r: (r["row"], r["instrument"], r["decided_ns"]))


def _coverage(
    config: ArenaConfig,
    histories: dict[str, MarketHistory],
    window: Window,
    absent: Counter[tuple[str, str]],
) -> dict[str, Any]:
    markets: dict[str, Any] = {}
    for name in dict.fromkeys((*config.instruments, *config.context)):
        history = histories[name]
        minutes = history.tape.span(window.start_ns, window.end_ns) or ()
        inside = [p for p in history.points if window.start_ns <= p.as_of_ns < window.end_ns]
        markets[name] = {
            "minutes": len(minutes),
            "missing_minutes": sum(1 for m in minutes if m.gap is GapState.MISSING),
            "partial_minutes": sum(1 for m in minutes if m.gap is GapState.PARTIAL),
            "deriv_points": {s.value: sum(1 for p in inside if p.series is s) for s in LEVELS},
        }
    reasons: dict[str, Counter[str]] = {}
    for (feature, reason), count in absent.items():
        reasons.setdefault(feature, Counter())[reason] += count
    features = {
        feature: {
            "absent": sum(found.values()),
            "reason": sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))[0][0],
        }
        for feature, found in sorted(reasons.items())
    }
    return {"markets": markets, "features_absent": features}


def _splits(config: ArenaConfig, rows: dict[str, _Row]) -> Iterator[dict[str, Any]]:
    if config.fit_days is None or config.test_days is None:
        return
    found = walk_forward(
        config.start,
        config.end,
        fit_days=config.fit_days,
        test_days=config.test_days,
        embargo_bars=HOLD_BARS,
        interval_ns=config.interval_ns,
    )
    for split in found:
        figures: dict[str, Any] = {}
        for name in config.rows:
            nets = [
                float(s.net_bps)
                for s in rows[name].settled
                if split.test_start_ns <= s.decided_ns < split.test_end_ns
            ]
            mean = f"{statistics.mean(nets):.2f}" if nets else None
            figures[name] = {"ideas": len(nets), "mean_net_bps": mean}
        yield {
            "test_from": day_of(split.test_start_ns).isoformat(),
            "test_to": day_of(split.test_end_ns - 1).isoformat(),
            "rows": figures,
        }
