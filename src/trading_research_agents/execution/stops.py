"""Stops sized by the market's own movement, or named by the analyst and held inside bounds.

One fraction of price is a wide stop on a quiet instrument and a narrow one on a fast one. In
volatility mode the stop is a number of average true ranges on the decision timeframe, grown with
the square root of the bars the trade is meant to last, and held between a floor and a ceiling the
operator declares; the take profit is the stop times the declared reward to risk.

In analyst mode the analyst names where its idea is wrong and where it is done. Code checks the
side, holds the stop inside the same floor and ceiling, holds the target inside the declared reward
to risk, and falls back to the measured stop when the analyst names none. Size still comes from
the risk per trade, so a wider stop means a smaller position, never more risk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from typing import Any

from trading_research_agents.data.schema import Candle

BPS = Decimal(10_000)
MODES = ("fixed", "volatility", "analyst")


class StopError(ValueError):
    """The stop cannot be placed from these numbers."""


@dataclass(frozen=True)
class VolatilityExit:
    """[exit] with mode = "volatility" or "analyst"."""

    stop_atr: Decimal = Decimal("1.5")
    reward_to_risk: Decimal = Decimal("2.0")
    min_stop_frac: Decimal = Decimal("0.002")
    max_stop_frac: Decimal = Decimal("0.03")
    atr_bars: int = 14
    analyst_levels: bool = False
    min_reward_to_risk: Decimal = Decimal("1.5")
    max_reward_to_risk: Decimal = Decimal("4.0")

    def __post_init__(self) -> None:
        for label in (
            "stop_atr",
            "reward_to_risk",
            "min_stop_frac",
            "max_stop_frac",
            "min_reward_to_risk",
            "max_reward_to_risk",
        ):
            value = getattr(self, label)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise StopError(f"[exit].{label} is a positive number, got {value!r}")
        if self.min_stop_frac > self.max_stop_frac:
            raise StopError(
                f"[exit].min_stop_frac {self.min_stop_frac} is above "
                f"max_stop_frac {self.max_stop_frac}"
            )
        if self.min_reward_to_risk > self.max_reward_to_risk:
            raise StopError(
                f"[exit].min_reward_to_risk {self.min_reward_to_risk} is above "
                f"max_reward_to_risk {self.max_reward_to_risk}"
            )
        if self.max_stop_frac >= 1:
            raise StopError("[exit].max_stop_frac is a fraction of price, below 1")
        if not isinstance(self.atr_bars, int) or self.atr_bars < 2:
            raise StopError("[exit].atr_bars is a whole number of bars, at least 2")

    @property
    def mode(self) -> str:
        return "analyst" if self.analyst_levels else "volatility"

    def place(
        self,
        candles: Sequence[Candle],
        *,
        horizon_bars: int,
        mid: Decimal | None = None,
        side: str | None = None,
        stop_price: Decimal | None = None,
        target_price: Decimal | None = None,
    ) -> Placement:
        atr = average_true_range(candles, self.atr_bars)
        last = candles[-1].close
        measured = self.stop_atr * atr / last * Decimal(max(horizon_bars, 1)).sqrt()
        stop, clamped = self._held(measured)
        take = stop * self.reward_to_risk
        source, proposed, notes, named_target = "measured", None, [], False
        if self.analyst_levels:
            named = _distance(mid, side, stop_price, against=True)
            if named is None:
                notes.append(
                    "no stop named on the losing side of the price; the measured stop stands"
                )
            else:
                proposed = named
                stop, clamped = self._held(named)
                source = "analyst"
                target = _distance(mid, side, target_price, against=False)
                take = self._target(stop, target, notes)
                named_target = target is not None
        return Placement(
            stop_frac=stop,
            take_frac=take,
            atr=atr,
            atr_bps=atr / last * BPS,
            horizon_bars=horizon_bars,
            measured_frac=measured,
            clamped_to=clamped,
            mode=self.mode,
            source=source,
            proposed_frac=proposed,
            notes=tuple(notes),
            target_named=named_target,
        )

    def _held(self, frac: Decimal) -> tuple[Decimal, str | None]:
        if frac < self.min_stop_frac:
            return self.min_stop_frac, "min_stop_frac"
        if frac > self.max_stop_frac:
            return self.max_stop_frac, "max_stop_frac"
        return frac, None

    def _target(self, stop: Decimal, target: Decimal | None, notes: list[str]) -> Decimal:
        if target is None:
            notes.append(f"no target named on the winning side; take at {self.reward_to_risk}x")
            return stop * self.reward_to_risk
        ratio = target / stop
        if ratio < self.min_reward_to_risk:
            # Moving the analyst's target further out to make the trade fit would be inventing a
            # target nobody named; a trade whose own target is too close is not taken.
            raise StopError(
                f"the analyst's target is {ratio.quantize(Decimal('0.01'))}x its stop and "
                f"[exit].min_reward_to_risk is {self.min_reward_to_risk}"
            )
        if ratio > self.max_reward_to_risk:
            # A far target makes the break-even look easy and is rarely reached; it is brought in.
            notes.append(f"target held to max_reward_to_risk {self.max_reward_to_risk}x")
            return stop * self.max_reward_to_risk
        return target


def _distance(
    mid: Decimal | None, side: str | None, price: Decimal | None, *, against: bool
) -> Decimal | None:
    """How far a named price sits from the mid as a fraction, when it is on the side it belongs."""
    if mid is None or mid <= 0 or price is None or side not in ("long", "short"):
        return None
    up = (price - mid) / mid
    frac = -up if (side == "long") == against else up
    return frac if frac > 0 else None


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
    mode: str = "volatility"
    source: str = "measured"
    proposed_frac: Decimal | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # The take profit is the analyst's own target (possibly brought in), not a multiple of the stop.
    target_named: bool = False

    def record(self) -> dict[str, Any]:
        cents = Decimal("0.01")
        out: dict[str, Any] = {
            "mode": self.mode,
            "source": self.source,
            "atr": str(self.atr),
            "atr_bps": str(self.atr_bps.quantize(cents)),
            "horizon_bars": self.horizon_bars,
            "measured_stop_bps": str((self.measured_frac * BPS).quantize(cents)),
            "stop_bps": str((self.stop_frac * BPS).quantize(cents)),
            "take_bps": str((self.take_frac * BPS).quantize(cents)),
            "clamped_to": self.clamped_to,
        }
        if self.proposed_frac is not None:
            out["proposed_stop_bps"] = str((self.proposed_frac * BPS).quantize(cents))
        out["target_named"] = self.target_named
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def average_true_range(candles: Sequence[Any], bars: int) -> Decimal:
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
