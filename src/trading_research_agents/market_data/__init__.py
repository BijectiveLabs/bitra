"""Market data a desk trades on: order flow, book state and derivatives, minute by minute.

One builder makes every bar, from a venue's archive or from its stream, and every value carries
where it came from and whether the time it covers was seen whole.
"""

from __future__ import annotations

from trading_research_agents.market_data.bars import (
    FlowBar,
    GapState,
    MarketDataError,
    quiet_bar,
    worst,
)
from trading_research_agents.market_data.days import (
    DAY_NS,
    NS_PER_MINUTE,
    NS_PER_S,
    day_of,
    midnight_ns,
)

__all__ = [
    "DAY_NS",
    "NS_PER_MINUTE",
    "NS_PER_S",
    "FlowBar",
    "GapState",
    "MarketDataError",
    "day_of",
    "midnight_ns",
    "quiet_bar",
    "worst",
]
