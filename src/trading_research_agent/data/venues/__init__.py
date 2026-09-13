"""Venues are plugins. The core never names one.

An archive venue knows where its public trade archives live and how its rows become canonical
trades. Two ship with the package; a third is a module that satisfies `ArchiveVenue`, registered
under the `trading_research_agent.venues` entry point or by calling `register`. Nothing else in
the package changes when a venue is added.
"""

from trading_research_agent.data.venues.registry import (
    ArchiveVenue,
    UnknownVenueError,
    archive_venue,
    archive_venues,
    register,
)

__all__ = ["ArchiveVenue", "UnknownVenueError", "archive_venue", "archive_venues", "register"]
