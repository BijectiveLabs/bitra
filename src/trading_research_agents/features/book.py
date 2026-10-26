"""Book figures from the last closed book bar of the decision interval.

- `spread_bps`: the mean quoted spread over the bar's samples, in basis points of the mid.
- `book_imbalance`: the mean of (bid - ask) / (bid + ask) over the first ten levels, from -1 to 1.
- `depth_ratio_10bps`: resting bid size over resting ask size within ten basis points of the mid.

A book cannot be recorded after the fact, so before recording began these are absent, and they say
so; a bar the recorder covered without a single sample is absent too.
"""

from __future__ import annotations

from trading_research_agents.features.core import Absent, FeatureValue, Inputs
from trading_research_agents.market_data.book import BookBar


def last_book(inputs: Inputs) -> BookBar | Absent:
    if not inputs.book or inputs.book[-1].end_ns != inputs.at_ns:
        return Absent("no book was recorded for the bar closing at the decision")
    bar = inputs.book[-1]
    if not bar.samples:
        return Absent("the book bar holds no sample")
    return bar


def spread_bps(inputs: Inputs) -> FeatureValue:
    bar = last_book(inputs)
    if isinstance(bar, Absent):
        return bar
    return bar.spread_bps_mean if bar.spread_bps_mean is not None else Absent("no spread")


def book_imbalance(inputs: Inputs) -> FeatureValue:
    bar = last_book(inputs)
    if isinstance(bar, Absent):
        return bar
    found = bar.imbalance_top10_mean
    return found if found is not None else Absent("no imbalance")


def depth_ratio_10bps(inputs: Inputs) -> FeatureValue:
    bar = last_book(inputs)
    if isinstance(bar, Absent):
        return bar
    bid, ask = bar.depth_bid_10bps_sum_base, bar.depth_ask_10bps_sum_base
    if bid is None or ask is None or ask == 0:
        return Absent("nothing rested on the ask within ten basis points")
    return float(bid / ask)
