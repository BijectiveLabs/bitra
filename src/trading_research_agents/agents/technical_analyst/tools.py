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
from typing import Any

from trading_research_agents.strategy.core import Bar

HORIZONS = (1, 5, 20)


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
    )
