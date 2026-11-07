"""Example: a long-only time-series momentum rule on daily bars.

This is an example, not a recommendation, and not part of the package. It is the arena's fixed
`momentum_long` baseline written as a rule a desk can run: long when the last 24 bars' move is at
least one standard deviation of a 96-bar window scaled to 24 bars, stop 1.5 average true ranges,
take 3, held at most `hold_bars`. Crypto's published time-series momentum is long-biased (Liu and
Tsyvinski, Review of Financial Studies, 2021). Replay it on your own data before you run it:

    tra arena --rows momentum_long,strategies/momentum_long.py:MomentumLong ...

Point `strategy.toml` at it::

    [strategy]
    file = "strategies/momentum_long.py"
    class = "MomentumLong"
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

from trading_research_agents.execution.stops import average_true_range
from trading_research_agents.strategy.core import Bar, Idea, Target

BPS = 10_000


class MomentumLong:
    name = "example-momentum-long"

    def __init__(
        self,
        *,
        horizon: int = 24,
        vol_bars: int = 96,
        atr_bars: int = 14,
        entry_z: float = 1.0,
        stop_atr: Decimal = Decimal("1.5"),
        take_atr: Decimal = Decimal(3),
        hold_bars: int = 6,
    ) -> None:
        if not 1 <= horizon <= vol_bars or atr_bars < 2 or atr_bars >= vol_bars:
            raise ValueError("1 <= horizon <= vol_bars, and 2 <= atr_bars < vol_bars")
        if hold_bars < 1 or stop_atr <= 0 or take_atr <= 0:
            raise ValueError("the hold, the stop and the take are positive")
        self.horizon = horizon
        self.vol_bars = vol_bars
        self.atr_bars = atr_bars
        self.entry_z = entry_z
        self.stop_atr = Decimal(stop_atr)
        self.take_atr = Decimal(take_atr)
        self.hold_bars = hold_bars

    def decide_on_bars(self, bars: Sequence[Bar]) -> Idea | None:
        if len(bars) < self.vol_bars + 1:
            return None
        z = self._momentum_z(bars[-(self.vol_bars + 1) :])
        if z is None or z < self.entry_z:
            return None
        window = bars[-self.vol_bars :]
        close = window[-1].close
        if close is None:
            return None
        atr_bps = float(average_true_range(window, self.atr_bars) / close) * BPS
        unit = Decimal(f"{atr_bps:.4f}")
        return Idea(
            side=Target.LONG,
            stop_bps=self.stop_atr * unit,
            take_bps=self.take_atr * unit,
            hold_bars=self.hold_bars,
            reason=f"mom_z_{self.horizon} {z:+.3f}",
        )

    def _momentum_z(self, bars: Sequence[Bar]) -> float | None:
        closes = [float(b.close) for b in bars if b.close is not None]
        steps = [math.log(b / a) for a, b in pairwise(closes)]
        mean = sum(steps) / len(steps)
        sd = math.sqrt(sum((s - mean) ** 2 for s in steps) / (len(steps) - 1))
        if sd <= 0:
            return None
        return sum(steps[-self.horizon :]) / (sd * math.sqrt(self.horizon))
