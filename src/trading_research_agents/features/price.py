"""Return, momentum, volatility and path figures from one market's closed bars.

With c the closes and r_i = ln(c_i / c_(i-1)) the one-bar log returns:

- `ret_bps_h`: (c_t / c_(t-h) - 1) * 10^4.
- `vol_bps`: sample standard deviation of r over the last 96 bars, * 10^4.
- `mom_z_h`: (r_(t-h+1) + ... + r_t) / (sd(r over 96 bars) * sqrt(h)).
- `range_vol_bps`: Parkinson, sqrt(sum of ln(H/L)^2 / (4 n ln 2)) over 96 bars, * 10^4.
- `atr_bps`: Wilder's average true range of 14 bars, smoothed over the last 96, over c_t, * 10^4.

The path figures keep the definitions the v1 technical analyst reads, on its 60-bar window, so a v1
record and a replay say the same thing about the same bars: `zscore`, `vwap_gap_bps` (closes
weighted by base volume, as v1 has it), `position_in_range`, `window_move_bps`, `trend_strength`,
`variance_ratio_4`, and `regime` from the default regime rule.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

from trading_research_agents.execution.regime import RegimeRule, label
from trading_research_agents.execution.stops import StopError, average_true_range
from trading_research_agents.features.core import (
    Absent,
    FeatureValue,
    Inputs,
    Value,
    closes,
    last_z,
    sample_sd,
    window,
)

BPS = 1e4
VOL_BARS = 96
ATR_BARS = 14
PATH_BARS = 60
VARIANCE_RATIO_BARS = 4
DEFAULT_REGIME = RegimeRule()


def move_bps(inputs: Inputs, horizon: int) -> float | Absent:
    """The return over the last `horizon` bars, in basis points."""
    bars = window(inputs.bars, inputs.at_ns, horizon + 1)
    if isinstance(bars, Absent):
        return bars
    c = closes(bars)
    return (c[-1] / c[0] - 1) * BPS


def ret_bps(horizon: int) -> Value:
    return lambda inputs: move_bps(inputs, horizon)


def _steps(inputs: Inputs) -> list[float] | Absent:
    bars = window(inputs.bars, inputs.at_ns, VOL_BARS + 1)
    if isinstance(bars, Absent):
        return bars
    return [math.log(b / a) for a, b in pairwise(closes(bars))]


def vol_bps(inputs: Inputs) -> FeatureValue:
    steps = _steps(inputs)
    if isinstance(steps, Absent):
        return steps
    return sample_sd(steps) * BPS


def mom_z(horizon: int) -> Value:
    def value(inputs: Inputs) -> FeatureValue:
        steps = _steps(inputs)
        if isinstance(steps, Absent):
            return steps
        scale = sample_sd(steps) * math.sqrt(horizon)
        if scale <= 0:
            return Absent("the window did not move, so there is nothing to scale the move by")
        return sum(steps[-horizon:]) / scale

    return value


def range_vol_bps(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, VOL_BARS)
    if isinstance(bars, Absent):
        return bars
    squares = []
    for bar in bars:
        _, high, low, _ = bar.ohlc()
        squares.append(math.log(float(high) / float(low)) ** 2)
    return math.sqrt(sum(squares) / (4 * len(squares) * math.log(2))) * BPS


def atr_bps(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, VOL_BARS)
    if isinstance(bars, Absent):
        return bars
    try:
        atr = average_true_range(bars, ATR_BARS)
    except StopError as exc:
        return Absent(str(exc))
    return float(atr / bars[-1].ohlc()[3]) * BPS


def _path_closes(inputs: Inputs) -> list[float] | Absent:
    bars = window(inputs.bars, inputs.at_ns, PATH_BARS)
    return bars if isinstance(bars, Absent) else closes(bars)


def zscore(inputs: Inputs) -> FeatureValue:
    c = _path_closes(inputs)
    if isinstance(c, Absent):
        return c
    found = last_z(c)
    return Absent("the closes did not vary") if isinstance(found, Absent) else found


def vwap_gap_bps(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, PATH_BARS)
    if isinstance(bars, Absent):
        return bars
    c = closes(bars)
    volume = [float(b.volume_base) for b in bars]
    vwap = sum(x * v for x, v in zip(c, volume, strict=True)) / sum(volume)
    return (c[-1] / vwap - 1) * BPS


def position_in_range(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, PATH_BARS)
    if isinstance(bars, Absent):
        return bars
    top = max(float(b.ohlc()[1]) for b in bars)
    bottom = min(float(b.ohlc()[2]) for b in bars)
    if top <= bottom:
        return Absent("the window has no range")
    return (closes(bars)[-1] - bottom) / (top - bottom)


def window_move_bps(inputs: Inputs) -> FeatureValue:
    c = _path_closes(inputs)
    if isinstance(c, Absent):
        return c
    return (c[-1] / c[0] - 1) * BPS


def trend_strength_of(inputs: Inputs) -> FeatureValue:
    c = _path_closes(inputs)
    if isinstance(c, Absent):
        return c
    found = trend_strength(c)
    return Absent("the window walked no path") if found is None else found


def variance_ratio_of(inputs: Inputs) -> FeatureValue:
    c = _path_closes(inputs)
    if isinstance(c, Absent):
        return c
    found = variance_ratio(c, VARIANCE_RATIO_BARS)
    return Absent("too few steps, or one-bar returns that did not vary") if found is None else found


def regime_of(inputs: Inputs) -> FeatureValue:
    c = _path_closes(inputs)
    if isinstance(c, Absent):
        return c
    return label(trend_strength(c), (c[-1] / c[0] - 1) * BPS, DEFAULT_REGIME)


def variance_ratio(closes: Sequence[float], bars: int = VARIANCE_RATIO_BARS) -> float | None:
    """Whether the window's moves carried on or turned back: the variance of returns over `bars`
    bars against `bars` times the variance of one-bar returns (Lo and MacKinlay). Near 1 is a
    random walk; below 1 a move tended to be given back, above 1 to be followed by more."""
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
    """Net move against the path walked, scaled by the square root of the steps, so a random walk
    sits near 1 on any window length."""
    steps = [b - a for a, b in pairwise(closes)]
    walked = sum(abs(x) for x in steps)
    if len(steps) < 2 or walked <= 0:
        return None
    return abs(closes[-1] - closes[0]) / walked * math.sqrt(len(steps))
