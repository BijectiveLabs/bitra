"""The trading agent: a validated strategy, a feed, a venue, and controls that fail closed.

One code path for replay, paper and shadow. What differs is the feed and the venue, both behind
protocols; the runtime does not know which it was given. No language model is anywhere on the
order path. Every intent, every refusal and every fill is written down as it happens.
"""

from trading_research_agent.trading.controls import (
    Admission,
    ControlError,
    Controls,
    KillSwitch,
    Limits,
)
from trading_research_agent.trading.feed import BarBuilder, Feed, RecordedFeed
from trading_research_agent.trading.ladder import Ladder, LadderError, Tier
from trading_research_agent.trading.runtime import RunReport, TradingConfig, run
from trading_research_agent.trading.venue import (
    Fill,
    Intent,
    SimulatedVenue,
    Venue,
    VenueError,
)

__all__ = [
    "Admission",
    "BarBuilder",
    "ControlError",
    "Controls",
    "Feed",
    "Fill",
    "Intent",
    "KillSwitch",
    "Ladder",
    "LadderError",
    "Limits",
    "RecordedFeed",
    "RunReport",
    "SimulatedVenue",
    "Tier",
    "TradingConfig",
    "Venue",
    "VenueError",
    "run",
]
