"""What the technical analyst is handed on each closed bar: features, computed by code.

Raw bars are what the model would otherwise have to add up itself, badly and at a price per
token. These are the figures a desk analyst would have on the screen: returns over a few
horizons, the close against its window as a z-score, distance from VWAP, the volume against
the window's average, the range against the window's, and where the close sits between the
window's high and low. Every figure is computed from bars already closed; nothing here looks
at the bar being formed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any

from trading_research_agents.execution.stops import StopError, average_true_range
from trading_research_agents.execution.structure import Structure, read
from trading_research_agents.strategy.core import Bar

HORIZONS = (1, 5, 20)
ATR_BARS = 14
# Four bars against one: on 15m decisions, an hour's moves against fifteen minutes'.
VARIANCE_RATIO_BARS = 4


@dataclass(frozen=True)
class Features:
    bars: int
    close: Decimal
    returns_bps: dict[int, float | None]
    zscore: float | None
    vwap_gap_bps: float | None
    volume_ratio: float | None
    range_ratio: float | None
    position_in_range: float | None
    window_move_bps: float | None = None
    trend_strength: float | None = None
    variance_ratio: float | None = None
    # Price levels, so an analyst naming a stop or a target names it against figures it was given.
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    atr: Decimal | None = None
    structure: Structure | None = None

    def record(self) -> dict[str, Any]:
        r = {
            f"ret_{k}_bps": None if v is None else round(v, 2) for k, v in self.returns_bps.items()
        }
        return {
            "bars": self.bars,
            "close": str(self.close),
            **r,
            "zscore": None if self.zscore is None else round(self.zscore, 3),
            "vwap_gap_bps": None if self.vwap_gap_bps is None else round(self.vwap_gap_bps, 2),
            "volume_ratio": None if self.volume_ratio is None else round(self.volume_ratio, 3),
            "range_ratio": None if self.range_ratio is None else round(self.range_ratio, 3),
            "position_in_range": (
                None if self.position_in_range is None else round(self.position_in_range, 3)
            ),
            "window_move_bps": (
                None if self.window_move_bps is None else round(self.window_move_bps, 2)
            ),
            "trend_strength": (
                None if self.trend_strength is None else round(self.trend_strength, 3)
            ),
            "variance_ratio": (
                None if self.variance_ratio is None else round(self.variance_ratio, 3)
            ),
            "range_high": None if self.range_high is None else str(self.range_high),
            "range_low": None if self.range_low is None else str(self.range_low),
            "atr": None if self.atr is None else str(self.atr),
            "market_structure": None if self.structure is None else self.structure.record(),
            "atr_bps": (
                None
                if self.atr is None or self.close <= 0
                else str((self.atr / self.close * 10_000).quantize(Decimal("0.01")))
            ),
        }


def features(window: Sequence[Bar]) -> Features | None:
    """Features from the closed bars in `window`, the last one being the most recent."""
    bars = [b for b in window if not b.empty and b.close is not None]
    if len(bars) < 2:
        return None
    closes = [float(b.close) for b in bars if b.close is not None]
    last = closes[-1]
    returns: dict[int, float | None] = {}
    for k in HORIZONS:
        returns[k] = None if len(closes) <= k else (last / closes[-1 - k] - 1) * 1e4
    n = len(closes)
    mean = sum(closes) / n
    var = sum((c - mean) ** 2 for c in closes) / (n - 1)
    z = None if var <= 0 else (last - mean) / math.sqrt(var)
    volume = [float(b.volume) for b in bars]
    total_volume = sum(volume)
    if total_volume > 0:
        vwap = sum(c * v for c, v in zip(closes, volume, strict=True)) / total_volume
        vwap_gap = (last / vwap - 1) * 1e4 if vwap > 0 else None
    else:
        vwap_gap = None
    avg_volume = total_volume / n
    volume_ratio = None if avg_volume <= 0 else volume[-1] / avg_volume
    ranges = [float(b.high - b.low) for b in bars if b.high is not None and b.low is not None]
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0
    range_ratio = None if avg_range <= 0 or not ranges else ranges[-1] / avg_range
    highs = [float(b.high) for b in bars if b.high is not None]
    lows = [float(b.low) for b in bars if b.low is not None]
    top, bottom = max(highs), min(lows)
    position = None if top <= bottom else (last - bottom) / (top - bottom)
    close = bars[-1].close
    if close is None:  # pragma: no cover - filtered above
        return None
    return Features(
        bars=len(bars),
        close=close,
        returns_bps=returns,
        zscore=z,
        vwap_gap_bps=vwap_gap,
        volume_ratio=volume_ratio,
        range_ratio=range_ratio,
        position_in_range=position,
        window_move_bps=(last / closes[0] - 1) * 1e4 if closes[0] > 0 else None,
        trend_strength=trend_strength(closes),
        variance_ratio=variance_ratio(closes),
        range_high=max(b.high for b in bars if b.high is not None),
        range_low=min(b.low for b in bars if b.low is not None),
        atr=_atr(bars),
        structure=read(bars),
    )


def _atr(bars: Sequence[Bar]) -> Decimal | None:
    """Wilder's average true range over the window, 14 bars or as many as the window holds."""
    if len(bars) < 3:
        return None
    close = bars[-1].close
    try:
        atr = average_true_range(bars, min(ATR_BARS, len(bars) - 1))
    except (StopError, TypeError):
        return None
    if close is None:  # pragma: no cover - filtered by the caller
        return atr
    # Two places finer than the price itself, so the figure reads at the instrument's own scale.
    exponent = close.as_tuple().exponent
    return atr.quantize(Decimal(1).scaleb(int(exponent) - 2)) if isinstance(exponent, int) else atr


def variance_ratio(closes: Sequence[float], bars: int = VARIANCE_RATIO_BARS) -> float | None:
    """Whether the window's moves have carried on or turned back: the variance of returns over
    `bars` bars against `bars` times the variance of one-bar returns (Lo and MacKinlay).

    Near 1 is a random walk. Below 1, a move tended to be given back, which is the market a fade
    is paid in; above 1, a move tended to be followed by more of the same.
    """
    if any(c <= 0 for c in closes):
        return None
    steps = [math.log(b / a) for a, b in pairwise(closes)]
    if len(steps) < 3 * bars:
        return None
    one = _variance(steps)
    sums = [sum(steps[k : k + bars]) for k in range(len(steps) - bars + 1)]
    if one <= 0:
        return None
    return _variance(sums) / (bars * one)


def _variance(values: Sequence[float]) -> float:
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def trend_strength(closes: Sequence[float]) -> float | None:
    """Net move against the path walked, scaled by the square root of the steps.

    The efficiency ratio alone shrinks as a window grows, so a fixed threshold would mean one thing
    on 20 bars and another on 240. Times sqrt(steps), a random walk sits near 1 on any window.
    """
    steps = [b - a for a, b in pairwise(closes)]
    walked = sum(abs(x) for x in steps)
    if len(steps) < 2 or walked <= 0:
        return None
    return abs(closes[-1] - closes[0]) / walked * math.sqrt(len(steps))
