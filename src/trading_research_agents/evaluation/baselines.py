"""The rows every policy is compared against. They are fixed and versioned, not tuned: they are
there to be beaten, and a change to one changes every comparison, so the version is in every report.

- `buy_hold`: long at the window's first decision, held to its end.
- `momentum`: long when `mom_z_24` >= 1, short when <= -1; stop 1.5 x ATR, take 3 x ATR, 24 bars.
- `fade`: the opposite side on the same triggers and exits.
- `random`: a seeded side at the times momentum fires, with its exits; this is the null.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.policy import DecisionView, TradeIdea

BASELINES_VERSION = "1"
MOMENTUM_Z = 1.0
STOP_ATR = Decimal("1.5")
TAKE_ATR = Decimal(3)
HOLD_BARS = 24


def _z(view: DecisionView) -> float | None:
    found = view.feature("mom_z_24")
    return found if isinstance(found, float) else None


def _trigger(z: float | None) -> Direction | None:
    if z is None:
        return None
    if z >= MOMENTUM_Z:
        return Direction.LONG
    if z <= -MOMENTUM_Z:
        return Direction.SHORT
    return None


def _idea(view: DecisionView, side: Direction, playbook: str, z: float) -> TradeIdea | None:
    atr = view.feature("atr_bps")
    if not isinstance(atr, float) or atr <= 0:
        return None
    unit = Decimal(f"{atr:.4f}")
    return TradeIdea(
        side=side,
        playbook=playbook,
        stop_bps=STOP_ATR * unit,
        take_bps=TAKE_ATR * unit,
        max_hold_bars=HOLD_BARS,
        reason=f"mom_z_24 {z:+.3f}",
    )


class Momentum:
    name = "momentum"
    knowledge_cutoff: date | None = None

    def decide(self, view: DecisionView) -> TradeIdea | None:
        z = _z(view)
        side = _trigger(z)
        return None if z is None or side is None else _idea(view, side, "momentum", z)

    def score(self, view: DecisionView) -> float | None:
        return _z(view)


class Fade:
    name = "fade"
    knowledge_cutoff: date | None = None

    def decide(self, view: DecisionView) -> TradeIdea | None:
        z = _z(view)
        side = _trigger(z)
        if z is None or side is None:
            return None
        other = Direction.SHORT if side is Direction.LONG else Direction.LONG
        return _idea(view, other, "fade", z)

    def score(self, view: DecisionView) -> float | None:
        z = _z(view)
        return None if z is None else -z


class RandomSide:
    name = "random"
    knowledge_cutoff: date | None = None

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._draws: dict[str, random.Random] = {}

    def decide(self, view: DecisionView) -> TradeIdea | None:
        z = _z(view)
        if z is None or _trigger(z) is None:
            return None
        draw = self._draws.get(view.instrument)
        if draw is None:
            # A string seed is hashed the same way in every process, so the null is reproducible.
            draw = random.Random(f"{self.seed}:{view.instrument}")  # noqa: S311
            self._draws[view.instrument] = draw
        side = draw.choice((Direction.LONG, Direction.SHORT))
        return _idea(view, side, "random", z)


class BuyHold:
    name = "buy_hold"
    knowledge_cutoff: date | None = None

    def __init__(self, end_ns: int, interval_ns: int) -> None:
        self.end_ns = end_ns
        self.interval_ns = interval_ns
        self._held: set[str] = set()

    def decide(self, view: DecisionView) -> TradeIdea | None:
        if view.instrument in self._held:
            return None
        bars = (self.end_ns - view.at_ns) // self.interval_ns
        if bars <= 0:
            return None
        self._held.add(view.instrument)
        return TradeIdea(
            Direction.LONG, "buy_hold", None, None, bars, "held from the first decision to the end"
        )
