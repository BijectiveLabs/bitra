"""Live venues are plugins. The runtime sees a `Venue`; it never learns which one.

A live venue plugin is a factory: given the run's settings and the keys it read from the
environment, it returns an object satisfying `trading.venue.Venue` whose `preflight()` has
not yet been called. The runtime calls it before the first print, and a venue that cannot
pass its own checks (key permissions, clock, an open position it was not told about) refuses
to start. One ships with the package; others register under the
`trading_research_agents.live_venues` entry point or by calling `register`.
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
