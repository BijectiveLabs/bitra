from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, Protocol

from trading_research_agent.envelope.model import Channel
from trading_research_agent.execution.cost import FundingSchedule
from trading_research_agent.trading.venue import Venue

ENTRY_POINT_GROUP = "trading_research_agent.live_venues"
BUNDLED = ("trading_research_agent.trading.venues.bybit",)


class UnknownLiveVenueError(KeyError):
    """No live venue plugin with that name is registered."""


@dataclass(frozen=True)
class LiveVenueSettings:
    """What every live venue is told. Keys are not here; the factory reads them from `env`."""

    instrument: str
    channel: Channel
    funding: FundingSchedule
    environment: str
    run_id: str
    key_ip_unbound_ok: bool = False
    adopt_open_position: bool = False
    poll_interval_ms: int = 250
    options: Mapping[str, Any] | None = None


class LiveVenue(Venue, Protocol):
    """A `Venue` that also checks itself before the first print."""

    def preflight(self) -> dict[str, Any]: ...


class LiveVenueFactory(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def env_keys(self) -> tuple[str, ...]:
        """Environment variables the factory needs, so the CLI can say which one is missing."""
        ...

    @property
    def environments(self) -> tuple[str, ...]: ...

    def __call__(self, settings: LiveVenueSettings, env: Mapping[str, str]) -> LiveVenue: ...


_registry: dict[str, LiveVenueFactory] = {}
_discovered = False


def register(factory: LiveVenueFactory) -> None:
    name = factory.name.strip().lower()
    if not name:
        raise ValueError("a live venue plugin names itself")
    _registry[name] = factory


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


def live_venue(name: str) -> LiveVenueFactory:
    _discover()
    try:
        return _registry[name.strip().lower()]
    except KeyError:
        raise UnknownLiveVenueError(
            f"no live venue named {name!r}; registered: {', '.join(sorted(_registry)) or 'none'}"
        ) from None


def live_venues() -> tuple[str, ...]:
    _discover()
    return tuple(sorted(_registry))


__all__ = [
    "LiveVenue",
    "LiveVenueFactory",
    "LiveVenueSettings",
    "UnknownLiveVenueError",
    "live_venue",
    "live_venues",
    "register",
]
