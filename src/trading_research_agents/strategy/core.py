"""What a strategy sees and what it may say."""

from __future__ import annotations

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
