"""A reference decision core, so the runtime can be exercised end to end. It claims nothing.

Trailing z-score of the close over a lookback window: enter against the deviation at
`entry_z`, ask to flatten at `exit_z`. Every parameter is declared at construction and fixed;
the window is strictly the bars already seen. The research this package descends from
falsified z-score reversion on its substrate, which is exactly why this core is a reference
and not a recommendation: the gates exist to say so again, on the reader's data.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from trading_research_agent.strategy.core import Bar, Signal, Target


class TrailingZScore:
    name = "trailing-zscore"

    def __init__(self, *, lookback: int, entry_z: Decimal, exit_z: Decimal) -> None:
        if lookback < 3:
            raise ValueError("lookback must be at least 3 bars")
        if not 0 <= exit_z < entry_z:
            raise ValueError("thresholds must satisfy 0 <= exit_z < entry_z")
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self._closes: deque[Decimal] = deque(maxlen=lookback)
        self._held: Target = Target.FLAT

    def on_bar(self, bar: Bar) -> Signal | None:
        if bar.empty or bar.close is None:
            return None
        window = list(self._closes)
        self._closes.append(bar.close)
        if len(window) < self.lookback:
            return None
        mean = sum(window, Decimal(0)) / len(window)
        variance = sum(((c - mean) ** 2 for c in window), Decimal(0)) / (len(window) - 1)
        if variance <= 0:
            return None
        z = (bar.close - mean) / variance.sqrt()
        if self._held is Target.FLAT:
            if z >= self.entry_z:
                self._held = Target.SHORT
                return Signal(Target.SHORT, f"z {z:.2f} above entry {self.entry_z}")
            if z <= -self.entry_z:
                self._held = Target.LONG
                return Signal(Target.LONG, f"z {z:.2f} below -entry {self.entry_z}")
            return None
        if abs(z) <= self.exit_z:
            self._held = Target.FLAT
            return Signal(Target.FLAT, f"z {z:.2f} inside exit {self.exit_z}")
        return None

    def observe_flat(self) -> None:
        """The runtime tells the core when a position closed without its say (stop, kill)."""
        self._held = Target.FLAT
