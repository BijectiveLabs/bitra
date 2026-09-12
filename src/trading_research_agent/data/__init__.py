"""The data gate: public archives in, a canonical trade stream and an audit verdict out.

A wrong number is more dangerous than a missing number. A missing number stops the pipeline;
a wrong one produces a confident verdict that propagates. Everything here fails closed: a row
that cannot be read is counted and shown, never silently dropped or defaulted.
"""

from trading_research_agent.data.archive import (
    ArchiveError,
    binance_spot_trades_url,
    bybit_trades_url,
    open_archive_lines,
    sha256_of,
)
from trading_research_agent.data.audit import AuditReport, Finding, audit
from trading_research_agent.data.fetch import FetchError, fetch, fetch_checksum
from trading_research_agent.data.manifest import (
    Manifest,
    ManifestError,
    Source,
    manifest_from_audit,
    read_manifest,
    write_manifest,
)
from trading_research_agent.data.normalise import (
    NORMALISER_VERSION,
    ArchiveFormatError,
    binance_trades,
    bybit_trades,
)
from trading_research_agent.data.schema import Direction, Rejection, Side, Trade, scale_of

__all__ = [
    "NORMALISER_VERSION",
    "ArchiveError",
    "ArchiveFormatError",
    "AuditReport",
    "Direction",
    "FetchError",
    "Finding",
    "Manifest",
    "ManifestError",
    "Rejection",
    "Side",
    "Source",
    "Trade",
    "audit",
    "binance_spot_trades_url",
    "binance_trades",
    "bybit_trades",
    "bybit_trades_url",
    "fetch",
    "fetch_checksum",
    "manifest_from_audit",
    "open_archive_lines",
    "read_manifest",
    "scale_of",
    "sha256_of",
    "write_manifest",
]
