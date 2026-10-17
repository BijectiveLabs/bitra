from __future__ import annotations

import threading
from importlib import import_module
from importlib.metadata import entry_points
from typing import Protocol

from trading_research_agents.trading.feed import Feed

ENTRY_POINT_GROUP = "trading_research_agents.feeds"


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
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        plugin = ep.load()
        register(plugin() if isinstance(plugin, type) else plugin)


def _streamed() -> tuple[str, ...]:
    try:
        pro = import_module("ccxt.pro")
    except ImportError:
        return ()
    from trading_research_agents.data.sources.ccxt_client import is_prediction, venues

    streamed = {str(name) for name in getattr(pro, "exchanges", ())}
    return tuple(sorted(streamed | {name for name in venues() if is_prediction(name)}))


def live_feed(name: str) -> LiveFeedFactory:
    """A registered plugin by that name, else the venue's public stream through ccxt."""
    _discover()
    key = name.strip().lower()
    if key in _registry:
        return _registry[key]
    if key in _streamed():
        from trading_research_agents.trading.feeds.ccxt_trades import CcxtFeedFactory

        return CcxtFeedFactory(key)
    raise UnknownFeedError(
        f"no live feed named {name!r}: not a registered plugin, and not a venue ccxt streams "
        "(is the ccxt extra installed?)"
    )


def live_feeds() -> tuple[str, ...]:
    _discover()
    return tuple(sorted({*_registry, *_streamed()}))
