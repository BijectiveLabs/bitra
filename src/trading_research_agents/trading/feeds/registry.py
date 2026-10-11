from __future__ import annotations

import threading
from importlib import import_module
from importlib.metadata import entry_points
from typing import Protocol

from trading_research_agents.trading.feed import Feed

ENTRY_POINT_GROUP = "trading_research_agents.feeds"
BUNDLED = ("trading_research_agents.trading.feeds.bybit",)


class UnknownFeedError(KeyError):
    """No live feed plugin with that name is registered."""


class LiveFeedFactory(Protocol):
    @property
    def name(self) -> str: ...

    def __call__(
        self, instrument: str, *, stop: threading.Event, max_reconnects: int | None
    ) -> Feed: ...


_registry: dict[str, LiveFeedFactory] = {}
_discovered = False


def register(factory: LiveFeedFactory) -> None:
    name = factory.name.strip().lower()
    if not name:
        raise ValueError("a feed plugin names itself")
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


def live_feed(name: str) -> LiveFeedFactory:
    _discover()
    try:
        return _registry[name.strip().lower()]
    except KeyError:
        raise UnknownFeedError(
            f"no live feed named {name!r}; registered: {', '.join(sorted(_registry)) or 'none'}"
        ) from None


def live_feeds() -> tuple[str, ...]:
    _discover()
    return tuple(sorted(_registry))
