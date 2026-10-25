"""Features: figures computed at a bar's close from what was known at that close, and nothing else.

A feature is a pure function of what one decision may read. When an input is missing, stale or
inside a bar nobody saw, the feature is absent and says why. It is never zero and never carried
forward, because an unknown funding rate read as zero is a wrong number that looks right. Replay
and live compute features through this package alone, so a feature has one implementation.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError
from trading_research_agents.market_data.deriv import Series, Timeline


class LookAheadError(MarketDataError):
    """An input read for a moment before it was known: a defect, never a value."""


@dataclass(frozen=True, slots=True)
class Absent:
    reason: str


FeatureValue = Decimal | float | str | Absent
FeatureVector = dict[str, FeatureValue]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    unit: str
    window_bars: int
    requires: frozenset[str]


@dataclass(frozen=True)
class Inputs:
    """What one decision at `at_ns` may read about one market, on bars of `interval_ns`."""

    at_ns: int
    interval_ns: int
    bars: Sequence[FlowBar]
    deriv: Mapping[Series, Timeline] = field(default_factory=dict)
    context: Mapping[str, Sequence[FlowBar]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, bars in (("bars", self.bars), *self.context.items()):
            late = [b.end_ns for b in bars if b.end_ns > self.at_ns]
            if late:
                raise LookAheadError(f"{name}: a bar closing at {late[0]} read at {self.at_ns}")


Value = Callable[[Inputs], FeatureValue]


@dataclass(frozen=True)
class Feature:
    spec: FeatureSpec
    value: Value


def compute(features: Sequence[Feature], inputs: Inputs) -> FeatureVector:
    """Every feature at `inputs.at_ns`, by name."""
    return {feature.spec.name: feature.value(inputs) for feature in features}


def window(bars: Sequence[FlowBar], at_ns: int, count: int) -> list[FlowBar] | Absent:
    """The last `count` bars, if the last closes at `at_ns` and every one was seen and traded."""
    if len(bars) < count:
        return Absent(f"{len(bars)} closed bar(s); this needs {count}")
    picked = list(bars[-count:])
    if picked[-1].end_ns != at_ns:
        return Absent("the last closed bar does not close at the decision time")
    if any(b.gap is GapState.MISSING for b in picked):
        return Absent("a bar nobody saw lies inside the window")
    if any(b.empty for b in picked):
        return Absent("a bar with no trades lies inside the window")
    return picked


def closes(bars: Sequence[FlowBar]) -> list[float]:
    return [float(b.ohlc()[3]) for b in bars]


def sample_sd(values: Sequence[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def last_z(values: Sequence[float]) -> float | Absent:
    """How far the last value sits from the window's mean, in the window's standard deviations."""
    spread = sample_sd(values)
    if spread <= 0:
        return Absent("the window did not vary")
    return (values[-1] - sum(values) / len(values)) / spread
