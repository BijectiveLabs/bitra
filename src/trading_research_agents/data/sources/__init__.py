"""Live sources: the only place in the package that asks a venue for data right now.

Everything here answers with provenance attached, and everything here can fail without ending a
run: a chain moves on, and the snapshot records what it tried. Agents never reach this package;
they read the snapshot a chain produced.
"""

from trading_research_agents.data.sources.base import (
    KINDS,
    TIERS,
    BookLevel,
    Fetched,
    OrderBook,
    Source,
    SourceError,
)
from trading_research_agents.data.sources.chain import (
    Attempt,
    ChainExhaustedError,
    ChainResult,
    SourceChain,
    chains_by_kind,
)
from trading_research_agents.data.sources.config import (
    DataConfig,
    DataConfigError,
    KindSettings,
    load_data_config,
    parse_duration,
)
from trading_research_agents.data.sources.file_source import FileSource
from trading_research_agents.data.sources.registry import (
    UnknownSourceError,
    build,
    register,
    schemes,
)

__all__ = [
    "KINDS",
    "TIERS",
    "Attempt",
    "BookLevel",
    "ChainExhaustedError",
    "ChainResult",
    "DataConfig",
    "DataConfigError",
    "Fetched",
    "FileSource",
    "KindSettings",
    "OrderBook",
    "Source",
    "SourceChain",
    "SourceError",
    "UnknownSourceError",
    "build",
    "chains_by_kind",
    "load_data_config",
    "parse_duration",
    "register",
    "schemes",
]
