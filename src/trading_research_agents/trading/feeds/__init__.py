"""Live feeds. The runtime never names one.

Every venue ccxt streams is a feed through `ccxt_trades`, with no key. A plugin registered under
the `trading_research_agents.feeds` entry point, or through `register`, takes precedence for its
name, for a venue ccxt does not stream.
"""

from trading_research_agents.trading.feeds.registry import (
    LiveFeedFactory,
    UnknownFeedError,
    live_feed,
    live_feeds,
    register,
)

__all__ = ["LiveFeedFactory", "UnknownFeedError", "live_feed", "live_feeds", "register"]
