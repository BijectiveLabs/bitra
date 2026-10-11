from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib import import_module
from importlib.metadata import entry_points
from typing import Protocol

from trading_research_agents.data.schema import Rejection, Trade

ENTRY_POINT_GROUP = "trading_research_agents.venues"
BUNDLED = (
    "trading_research_agents.data.venues.bybit",
    "trading_research_agents.data.venues.binance",
)


class UnknownVenueError(KeyError):
    """No venue plugin with that name is registered."""


@dataclass(frozen=True)
class FundingRate:
    """One settlement: when it happened (epoch ns) and the rate as a fraction."""

    ts_ns: int
    rate: Decimal


class FundingSource(Protocol):
    """A venue plugin that can also say what funding it charged. Optional."""

    def funding_rates(
        self, instrument: str, *, start_ms: int, end_ms: int
    ) -> list[FundingRate]: ...


class ArchiveVenue(Protocol):
    """What a venue plugin provides for public trade archives."""

    @property
    def name(self) -> str: ...

    @property
    def cadence(self) -> str:
        """`daily` or `monthly`: how the venue slices its archives."""
        ...

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str: ...

    def checksum_url(self, archive_url: str) -> str | None: ...

    def normalise(
        self, lines: Iterable[str], *, instrument: str
    ) -> Iterator[Trade | Rejection]: ...


_registry: dict[str, ArchiveVenue] = {}
_discovered = False


def register(venue: ArchiveVenue) -> None:
    name = venue.name.strip().lower()
    if not name:
        raise ValueError("a venue plugin names itself")
    _registry[name] = venue


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    for module in BUNDLED:
        import_module(module)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        plugin = ep.load()
        register(plugin() if isinstance(plugin, type) else plugin)


def archive_venue(name: str) -> ArchiveVenue:
    _discover()
    try:
        return _registry[name.strip().lower()]
    except KeyError:
        raise UnknownVenueError(
            f"no venue named {name!r}; registered: {', '.join(sorted(_registry)) or 'none'}"
        ) from None


def archive_venues() -> tuple[str, ...]:
    _discover()
    return tuple(sorted(_registry))
