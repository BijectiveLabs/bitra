"""How every record this package keeps is written to disk and read back."""

from trading_research_agents.records.files import (
    append_line,
    read_gzip_json,
    read_lines,
    sha256_of,
    write_gzip_json,
    write_json,
)
from trading_research_agents.records.index import (
    INDEX_DIR,
    append_index,
    build_index,
    has_index,
    index_row,
    recent,
)

__all__ = [
    "INDEX_DIR",
    "append_index",
    "append_line",
    "build_index",
    "has_index",
    "index_row",
    "read_gzip_json",
    "read_lines",
    "recent",
    "sha256_of",
    "write_gzip_json",
    "write_json",
]
