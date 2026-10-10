"""Stops sized by the market's own movement.

One fraction of price is a wide stop on a quiet instrument and a narrow one on a fast one. Here
the stop is a number of average true ranges on the decision timeframe, grown with the square root
of the bars the trade is meant to last, and held between a floor and a ceiling the operator
declares. The take profit is the stop times the declared reward to risk.

Code measures the range and places both prices. No model is asked where a stop belongs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any

from trading_research_agents.data.schema import Candle

BPS = Decimal(10_000)
MODES = ("fixed", "volatility")


class StopError(ValueError):
    """The stop cannot be placed from these numbers."""


@dataclass(frozen=True)
class VolatilityExit:
    """[exit] with mode = "volatility"."""

    stop_atr: Decimal = Decimal("1.5")
    reward_to_risk: Decimal = Decimal("2.0")
    min_stop_frac: Decimal = Decimal("0.002")
    max_stop_frac: Decimal = Decimal("0.03")
    atr_bars: int = 14

    def __post_init__(self) -> None:
        for label in ("stop_atr", "reward_to_risk", "min_stop_frac", "max_stop_frac"):
            value = getattr(self, label)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise StopError(f"[exit].{label} is a positive number, got {value!r}")
        if self.min_stop_frac > self.max_stop_frac:
            raise StopError(
                f"[exit].min_stop_frac {self.min_stop_frac} is above "
                f"max_stop_frac {self.max_stop_frac}"
            )
        if self.max_stop_frac >= 1:
            raise StopError("[exit].max_stop_frac is a fraction of price, below 1")
        if not isinstance(self.atr_bars, int) or self.atr_bars < 2:
            raise StopError("[exit].atr_bars is a whole number of bars, at least 2")

    def place(self, candles: Sequence[Candle], *, horizon_bars: int) -> Placement:
        atr = average_true_range(candles, self.atr_bars)
        last = candles[-1].close
        raw = self.stop_atr * atr / last * Decimal(max(horizon_bars, 1)).sqrt()
        clamped = None
        stop = raw
        if raw < self.min_stop_frac:
            stop, clamped = self.min_stop_frac, "min_stop_frac"
        elif raw > self.max_stop_frac:
            stop, clamped = self.max_stop_frac, "max_stop_frac"
        return Placement(
            stop_frac=stop,
            take_frac=stop * self.reward_to_risk,
            atr=atr,
            atr_bps=atr / last * BPS,
            horizon_bars=horizon_bars,
            measured_frac=raw,
            clamped_to=clamped,
        )


@dataclass(frozen=True)
class Placement:
    """Where one trade's exits sit, and the figures that put them there."""

    stop_frac: Decimal
    take_frac: Decimal
    atr: Decimal
    atr_bps: Decimal
    horizon_bars: int
    measured_frac: Decimal
    clamped_to: str | None

    def record(self) -> dict[str, Any]:
        cents = Decimal("0.01")
        return {
            "mode": "volatility",
            "atr": str(self.atr),
            "atr_bps": str(self.atr_bps.quantize(cents)),
            "horizon_bars": self.horizon_bars,
            "measured_stop_bps": str((self.measured_frac * BPS).quantize(cents)),
            "stop_bps": str((self.stop_frac * BPS).quantize(cents)),
            "take_bps": str((self.take_frac * BPS).quantize(cents)),
            "clamped_to": self.clamped_to,
        }


def average_true_range(candles: Sequence[Candle], bars: int) -> Decimal:
    """Wilder's average true range over the closed candles given, the last one included.

    The first value is the plain mean of the first `bars` true ranges and each later one is
    smoothed as (previous * (bars - 1) + range) / bars, which is how the indicator is published
    on every charting terminal, so the figure in the record can be checked against one.
    """
    if bars < 2:
        raise StopError("an average true range needs at least 2 bars")
    if len(candles) < bars + 1:
        raise StopError(
            f"{len(candles)} closed candle(s); an average true range over {bars} bars needs "
            f"{bars + 1}, so raise [agents].lookback_bars or lower [exit].atr_bars"
        )
    ranges = []
    for before, now in pairwise(candles):
        ranges.append(
            max(now.high - now.low, abs(now.high - before.close), abs(now.low - before.close))
        )
    atr = sum(ranges[:bars], Decimal(0)) / bars
    for value in ranges[bars:]:
        atr = (atr * (bars - 1) + value) / bars
    if atr <= 0:
        raise StopError("the candles did not move, so there is no range to size a stop from")
    return atr
