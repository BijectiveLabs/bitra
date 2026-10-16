"""Live venues. The runtime sees a `Venue`; it never learns which one.

Every exchange ccxt knows is a live venue through `ccxt_live`, on the same client and hand that
venue mode sends with. A plugin registered under the `trading_research_agents.live_venues` entry
point, or through `register`, takes precedence for its name. Either way the runtime calls
`preflight()` before the first print, and a venue that cannot pass its own checks (clock, account
mode, key permissions where the venue reports them, an open position it was not told about)
refuses to start.
"""

from trading_research_agents.trading.venues.registry import (
    LiveVenue,
    LiveVenueFactory,
    LiveVenueSettings,
    UnknownLiveVenueError,
    live_venue,
    live_venues,
    register,
)

__all__ = [
    "LiveVenue",
    "LiveVenueFactory",
    "LiveVenueSettings",
    "UnknownLiveVenueError",
    "live_venue",
    "live_venues",
    "register",
]
