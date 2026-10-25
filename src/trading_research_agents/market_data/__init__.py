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
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import (
    DAY_NS,
    NS_PER_MINUTE,
    NS_PER_S,
    day_of,
    midnight_ns,
)
from trading_research_agents.market_data.deriv import (
    LEVELS,
    MAX_AGE_S,
    DerivPoint,
    Known,
    Series,
    Timeline,
    Unknown,
    detail_of,
)
from trading_research_agents.market_data.gaps import GapMark
from trading_research_agents.market_data.resample import resample

__all__ = [
    "DAY_NS",
    "LEVELS",
    "MAX_AGE_S",
    "NS_PER_MINUTE",
    "NS_PER_S",
    "DerivPoint",
    "FlowBar",
    "FlowBarBuilder",
    "GapMark",
    "GapState",
    "Known",
    "MarketDataError",
    "Series",
    "Timeline",
    "Unknown",
    "day_of",
    "detail_of",
    "midnight_ns",
    "quiet_bar",
    "resample",
    "worst",
]
