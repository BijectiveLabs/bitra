"""What a policy sees at a decision and what it may answer: an idea, settled later by the harness.

A view never holds a bar that had not closed or a derivatives value not yet published; it refuses
one when it is made, which makes a look-ahead defect a failure rather than a result. A policy
answers with an idea or nothing, never with an order: entry, settlement and costs belong to the
harness, so every row is settled the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from trading_research_agents.data.schema import Direction
from trading_research_agents.features.core import FeatureValue, FeatureVector, LookAheadError
from trading_research_agents.market_data.bars import FlowBar, MarketDataError


@dataclass(frozen=True)
class TradeIdea:
    """A side, the distances to the stop and the take in basis points of the entry (None is no
    barrier), and the longest hold in bars of the decision interval."""

    side: Direction
    playbook: str
    stop_bps: Decimal | None
    take_bps: Decimal | None
    max_hold_bars: int
    reason: str

    def __post_init__(self) -> None:
        if self.max_hold_bars <= 0:
            raise MarketDataError("an idea is held for at least one bar")
        for name, level in (("stop", self.stop_bps), ("take", self.take_bps)):
            if level is not None and (not level.is_finite() or level <= 0):
                raise MarketDataError(f"the {name} distance is positive, or None for no barrier")


@dataclass(frozen=True)
class DecisionView:
    instrument: str
    at_ns: int
    primary: str
    bars: Sequence[FlowBar]
    features: Mapping[str, FeatureVector]
    context: Mapping[str, Mapping[str, FeatureVector]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        late = [b.end_ns for b in self.bars if b.end_ns > self.at_ns]
        if late:
            raise LookAheadError(
                f"{self.instrument}: a bar closing at {late[0]} in a view at {self.at_ns}"
            )
        if self.primary not in self.features:
            raise MarketDataError(f"the view has no {self.primary} features")

    def feature(self, name: str, timeframe: str | None = None) -> FeatureValue:
        return self.features[timeframe or self.primary][name]


class Policy(Protocol):
    name: str
    knowledge_cutoff: date | None

    def decide(self, view: DecisionView) -> TradeIdea | None: ...


@runtime_checkable
class Scored(Protocol):
    """A policy that also exposes its side score at every decision, for the rank IC."""

    def score(self, view: DecisionView) -> float | None: ...
