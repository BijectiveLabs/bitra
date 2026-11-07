"""What a strategy sees and what it may say.

Two shapes of decision core. A `Strategy` is fed bar by bar and holds a target. A `Rule` is asked
once per decision, with the closed bars before it, and answers with one idea or none: the shape
`tra run` and the arena both ask, so a rule replayed and a rule run live are the same code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class Target(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class Bar:
    """One fixed interval of trades. An interval with no trades is empty, never carried forward."""

    start_ns: int
    end_ns: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal
    trades: int

    def __post_init__(self) -> None:
        if self.end_ns <= self.start_ns:
            raise ValueError("a bar ends after it starts")
        prices = (self.open, self.high, self.low, self.close)
        if self.trades == 0:
            if any(p is not None for p in prices) or self.volume != 0:
                raise ValueError("a bar with no trades carries no prices and no volume")
            return
        o, h, lo, c = self.open, self.high, self.low, self.close
        if o is None or h is None or lo is None or c is None:
            raise ValueError("a bar with trades carries all four prices")
        if not lo <= min(o, c) <= max(o, c) <= h:
            raise ValueError("bar prices are inconsistent: low <= open, close <= high must hold")
        if self.volume <= 0:
            raise ValueError("a bar with trades has positive volume")

    @property
    def empty(self) -> bool:
        return self.trades == 0

    def ohl(self) -> tuple[Decimal, Decimal, Decimal]:
        """Open, high, low of a bar that has trades. Refuses an empty bar rather than guessing."""
        if self.open is None or self.high is None or self.low is None:
            raise ValueError("an empty bar has no prices")
        return self.open, self.high, self.low


@dataclass(frozen=True)
class Signal:
    """What the strategy wants, and why, in words the ledger can keep."""

    target: Target
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a signal states its reason")


class Strategy(Protocol):
    """A pure decision core. Internal state is allowed; it is built only from bars seen so far."""

    @property
    def name(self) -> str: ...

    def on_bar(self, bar: Bar) -> Signal | None: ...


@dataclass(frozen=True)
class Idea:
    """A rule's trade: a side, the stop and the take as distances in basis points of the entry
    (None is no level), and the longest hold in bars of the decision interval."""

    side: Target
    stop_bps: Decimal | None
    take_bps: Decimal | None
    hold_bars: int
    reason: str

    def __post_init__(self) -> None:
        if self.side is Target.FLAT:
            raise ValueError("an idea is long or short; no idea is None")
        if self.hold_bars < 1:
            raise ValueError("an idea is held for at least one bar")
        for name, level in (("stop", self.stop_bps), ("take", self.take_bps)):
            if level is not None and (not level.is_finite() or level <= 0):
                raise ValueError(f"the {name} distance is positive, or None for no level")
        if not self.reason.strip():
            raise ValueError("an idea states its reason")


def closed_run(bars: Sequence[Bar], at_ns: int) -> list[Bar]:
    """The bars ending at `at_ns`, back to the first empty bar or break in time: what a rule reads.

    Shared by the arena and `tra run`, so a rule is handed the same bars in a replay and live.
    """
    run: list[Bar] = []
    for bar in reversed(bars):
        if bar.empty:
            break
        if run and bar.end_ns != run[-1].start_ns:
            break
        if not run and bar.end_ns != at_ns:
            break
        run.append(bar)
    return run[::-1]


class Rule(Protocol):
    """A pure decision asked at one moment.

    Precondition: `bars` are the decision interval's closed bars, oldest first, contiguous, every
    one traded and seen, the last closing at the decision. A gap ends the run, so a rule never
    reads across one. Postcondition: an idea, or None for no trade. No state outlives the call.
    """

    @property
    def name(self) -> str: ...

    def decide_on_bars(self, bars: Sequence[Bar]) -> Idea | None: ...
