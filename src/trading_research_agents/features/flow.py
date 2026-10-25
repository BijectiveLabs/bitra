"""Order-flow figures: who took liquidity, how much, and how busy the market was.

- `taker_imbalance_h`: (taker buy - taker sell) / volume, summed over the last h bars.
- `delta_z_24`: the last bar's taker delta against the last 24 bars', in standard deviations.
- `trade_intensity_z_24`: the last bar's trade count against the last 24 bars', the same way.
- `large_trade_share_24`: volume from large trades over all volume in the last 24 bars; absent when
  the bars were built without a large-trade threshold.
"""

from __future__ import annotations

from decimal import Decimal

from trading_research_agents.features.core import (
    Absent,
    FeatureValue,
    Inputs,
    Value,
    last_z,
    window,
)

FLOW_BARS = 24


def taker_imbalance(horizon: int) -> Value:
    def value(inputs: Inputs) -> FeatureValue:
        bars = window(inputs.bars, inputs.at_ns, horizon)
        if isinstance(bars, Absent):
            return bars
        volume = sum((b.volume_base for b in bars), Decimal(0))
        delta = sum((b.delta_base for b in bars), Decimal(0))
        return float(delta / volume)

    return value


def delta_z_24(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, FLOW_BARS)
    if isinstance(bars, Absent):
        return bars
    return last_z([float(b.delta_base) for b in bars])


def trade_intensity_z_24(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, FLOW_BARS)
    if isinstance(bars, Absent):
        return bars
    return last_z([float(b.trade_count) for b in bars])


def large_trade_share_24(inputs: Inputs) -> FeatureValue:
    bars = window(inputs.bars, inputs.at_ns, FLOW_BARS)
    if isinstance(bars, Absent):
        return bars
    large = [b.large_trade_base for b in bars]
    if any(part is None for part in large):
        return Absent("no large-trade threshold was declared when these bars were built")
    volume = sum((b.volume_base for b in bars), Decimal(0))
    return float(sum((part for part in large if part is not None), Decimal(0)) / volume)
