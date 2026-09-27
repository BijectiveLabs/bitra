"""Live feeds are plugins, like archive venues. The runtime never names one.

A live feed plugin is a factory: given an instrument and a stop event, it returns a `Feed`.
One ships with the package, behind the `[shadow]` extra; others register under the
`trading_research_agent.feeds` entry point or by calling `register`.
"""

from trading_research_agent.trading.feeds.registry import (
    LiveFeedFactory,
    UnknownFeedError,
    live_feed,
    live_feeds,
    register,
)

__all__ = ["LiveFeedFactory", "UnknownFeedError", "live_feed", "live_feeds", "register"]
