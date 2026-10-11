"""What the screener is handed per instrument: figures from the dataset, and a regime the code
proposes from two of them, which the model may accept or overrule with a reason.

The regime rule is deliberately plain and written down: a variance ratio (1-minute against
10-minute returns) above one with positive first-lag autocorrelation reads as trending, below
one with negative autocorrelation as ranging; a realised volatility above the operator's band
reads as volatile; too few prints as illiquid. It is a label for a screen, not a forecast.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any

from trading_research_agents.data.schema import Trade

NS_PER_MINUTE = 60 * 1_000_000_000


@dataclass(frozen=True)
class ScreenFigures:
    instrument: str
    rows: int
    minutes: float
    prints_per_minute: float
    realised_vol_bps: float
    autocorr_lag1: float | None
    variance_ratio_1_10: float | None
    largest_gap_s: float
    last_hour_volume_ratio: float | None
    turnover: Decimal
    proposed_regime: str
    regime_reason: str

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "rows": self.rows,
            "minutes": round(self.minutes, 1),
            "prints_per_minute": round(self.prints_per_minute, 3),
            "realised_vol_bps": round(self.realised_vol_bps, 3),
            "autocorr_lag1": None if self.autocorr_lag1 is None else round(self.autocorr_lag1, 4),
            "variance_ratio_1_10": (
                None if self.variance_ratio_1_10 is None else round(self.variance_ratio_1_10, 4)
            ),
            "largest_gap_s": round(self.largest_gap_s, 1),
            "last_hour_volume_ratio": (
                None
                if self.last_hour_volume_ratio is None
                else round(self.last_hour_volume_ratio, 3)
            ),
            "turnover": str(self.turnover),
            "proposed_regime": self.proposed_regime,
            "regime_reason": self.regime_reason,
        }


def _autocorr(returns: Sequence[float]) -> float | None:
    n = len(returns)
    if n < 3:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns)
    if var <= 0:
        return None
    cov = sum((a - mean) * (b - mean) for a, b in pairwise(returns))
    return cov / var


def _variance_ratio(returns: Sequence[float], q: int) -> float | None:
    """var(q-step return) / (q * var(1-step return)); one under a random walk."""
    if len(returns) < 3 * q:
        return None
    mean = sum(returns) / len(returns)
    var1 = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var1 <= 0:
        return None
    aggregated = [sum(returns[i : i + q]) for i in range(0, len(returns) - q + 1, q)]
    mq = sum(aggregated) / len(aggregated)
    varq = sum((r - mq) ** 2 for r in aggregated) / max(len(aggregated) - 1, 1)
    return varq / (q * var1)


def screen_figures(
    instrument: str,
    trades: Iterable[Trade],
    *,
    min_prints_per_minute: float,
    volatile_above_bps: float,
) -> ScreenFigures:
    """One pass over the trades. Both thresholds are the operator's: passed in, never set here."""
    rows = 0
    first = last = None
    turnover = Decimal(0)
    closes: dict[int, float] = {}
    volume_by_minute: dict[int, Decimal] = {}
    largest_gap = 0
    previous = None
    for t in trades:
        rows += 1
        first = t.ts_ns if first is None else first
        last = t.ts_ns
        turnover += t.qty
        minute = t.ts_ns // NS_PER_MINUTE
        closes[minute] = float(t.price)
        volume_by_minute[minute] = volume_by_minute.get(minute, Decimal(0)) + t.qty
        if previous is not None:
            largest_gap = max(largest_gap, t.ts_ns - previous)
        previous = t.ts_ns
    if rows == 0 or first is None or last is None:
        return ScreenFigures(
            instrument, 0, 0.0, 0.0, 0.0, None, None, 0.0, None, Decimal(0), "illiquid", "no trades"
        )
    minutes = max((last - first) / NS_PER_MINUTE, 1.0)
    series = [closes[k] for k in sorted(closes)]
    returns = [math.log(b / a) for a, b in pairwise(series) if a > 0 and b > 0]
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        vol = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) * 1e4
    else:
        vol = 0.0
    ac = _autocorr(returns)
    vr = _variance_ratio(returns, 10)
    minutes_sorted = sorted(volume_by_minute)
    last_hour = minutes_sorted[-60:]
    hour_volume = sum((volume_by_minute[m] for m in last_hour), Decimal(0))
    avg_hour = turnover / Decimal(max(minutes / 60, 1.0))
    hour_ratio = None if avg_hour <= 0 else float(hour_volume / avg_hour)
    rate = rows / minutes
    if rate < min_prints_per_minute:
        regime, why = "illiquid", f"{rate:.2f} prints/min below {min_prints_per_minute}"
    elif vol > volatile_above_bps:
        regime, why = "volatile", f"realised vol {vol:.1f} bps above {volatile_above_bps}"
    elif vr is not None and ac is not None and vr > 1.0 and ac > 0:
        regime, why = "trending", f"variance ratio {vr:.2f} > 1 with autocorr {ac:+.2f}"
    elif vr is not None and ac is not None and vr < 1.0 and ac < 0:
        regime, why = "ranging", f"variance ratio {vr:.2f} < 1 with autocorr {ac:+.2f}"
    else:
        regime, why = "ranging", "no trend signature in variance ratio and autocorrelation"
    return ScreenFigures(
        instrument=instrument,
        rows=rows,
        minutes=minutes,
        prints_per_minute=rate,
        realised_vol_bps=vol,
        autocorr_lag1=ac,
        variance_ratio_1_10=vr,
        largest_gap_s=largest_gap / 1e9,
        last_hour_volume_ratio=hour_ratio,
        turnover=turnover,
        proposed_regime=regime,
        regime_reason=why,
    )
