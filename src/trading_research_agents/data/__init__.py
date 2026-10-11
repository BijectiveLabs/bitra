"""The data gate: any trade data in, a canonical trade stream and an audit verdict out.

A wrong number is more dangerous than a missing number. A missing number stops the pipeline;
a wrong one produces a confident verdict that propagates. Everything here fails closed: a row
that cannot be read is counted and shown, never silently dropped or defaulted. Venues are
plugins (`data.venues`); any other source arrives through a declared column map
(`data.mapping`); both end in the same audit, manifest and Parquet contract.
"""

from trading_research_agents.data.archive import ArchiveError, open_archive_lines, sha256_of
from trading_research_agents.data.audit import AuditReport, Finding, audit
from trading_research_agents.data.fetch import FetchError, fetch, fetch_checksum
from trading_research_agents.data.manifest import (
    Manifest,
    ManifestError,
    Source,
    manifest_from_audit,
    read_manifest,
    write_manifest,
)
from trading_research_agents.data.mapping import (
    ColumnMap,
    MappingError,
    load_mapping,
    mapped_trades,
    parse_mapping,
)
from trading_research_agents.data.normalise import NORMALISER_VERSION, ArchiveFormatError
from trading_research_agents.data.schema import Direction, Rejection, Side, Trade, scale_of
from trading_research_agents.data.venues import (
    ArchiveVenue,
    UnknownVenueError,
    archive_venue,
    archive_venues,
)

__all__ = [
    "NORMALISER_VERSION",
    "ArchiveError",
    "ArchiveFormatError",
    "ArchiveVenue",
    "AuditReport",
    "ColumnMap",
    "Direction",
    "FetchError",
    "Finding",
    "Manifest",
    "ManifestError",
    "MappingError",
    "Rejection",
    "Side",
    "Source",
    "Trade",
    "UnknownVenueError",
    "archive_venue",
    "archive_venues",
    "audit",
    "fetch",
    "fetch_checksum",
    "load_mapping",
    "manifest_from_audit",
    "mapped_trades",
    "open_archive_lines",
    "parse_mapping",
    "read_manifest",
    "scale_of",
    "sha256_of",
    "write_manifest",
]
