"""Sources by name, so a chain is a line in a file and not a line of code.

A name is `scheme:target`: `ccxt:bybit` is public data from that venue through ccxt,
`file:data/` is the project's own directory. Anything else a client needs registers under the
`trading_research_agents.sources` entry point and is then usable in a chain by name, which is
how a paid on-chain or derivatives vendor arrives without this package depending on it.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from trading_research_agents.data.sources.base import Source, SourceError

ENTRY_POINT_GROUP = "trading_research_agents.sources"

Factory = Callable[[str, dict[str, Any]], Source]

_registry: dict[str, Factory] = {}
_discovered = False


class UnknownSourceError(KeyError):
    """The chain names a scheme nothing is registered for."""


def register(scheme: str, factory: Factory) -> None:
    name = scheme.strip().lower()
    if not name:
        raise ValueError("a source scheme names itself")
    _registry[name] = factory


def _ccxt(target: str, settings: dict[str, Any]) -> Source:
    from trading_research_agents.data.sources.ccxt_source import CcxtSource

    if not target:
        raise SourceError("ccxt needs a venue: ccxt:bybit")
    # `ccxt:okx@testnet` reads the venue's practice environment rather than its live one.
    venue, _, environment = target.partition("@")
    return CcxtSource(
        venue,
        environment=environment.strip().lower(),
        tier=str(settings.get("tier", "free")),
        symbols=dict(settings.get("symbols", {})),
        settings=dict(settings.get("ccxt", {})),
    )


def _file(target: str, settings: dict[str, Any]) -> Source:
    from trading_research_agents.data.sources.file_source import FileSource

    root = Path(settings.get("root", ".")) / (target or "data")
    return FileSource(root)


def _bundled() -> None:
    register("ccxt", _ccxt)
    register("file", _file)


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    _bundled()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        register(ep.name, ep.load())


def schemes() -> list[str]:
    _discover()
    return sorted(_registry)


def build(name: str, settings: dict[str, Any] | None = None) -> Source:
    """`ccxt:bybit` or `file:data/` to a source, with the settings the config declared."""
    _discover()
    scheme, _, target = name.strip().partition(":")
    key = scheme.strip().lower()
    if key not in _registry:
        raise UnknownSourceError(f"no source scheme {key!r} is registered; have {schemes()}")
    return _registry[key](target.strip(), dict(settings or {}))
