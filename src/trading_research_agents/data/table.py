"""A Client's own Parquet file, read as rows of text the way a CSV is read.

The column map and the series spec name columns and declare units, and they were written for CSV,
where every cell is text. Rather than a second set of rules for Parquet, each cell is written as
the text a CSV would hold, and the declared map reads it exactly as it reads a CSV: the same
refusals, the same rejected rows with their numbers, nothing guessed.

What that means per Parquet type, since a type carries part of the meaning a CSV leaves to the map:

  timestamp with a zone   nanoseconds since the epoch, exact; declare ts_unit = "ns"
  timestamp without one   ISO 8601 without an offset, which every ts_unit refuses as ambiguous
  date                    YYYY-MM-DD, which a series reads as UTC midnight before its publish lag
  decimal                 its digits as stored, scale kept
  float                   the shortest text that reads back as the same float
  null                    an empty cell, which a row needing it rejects
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.data.archive import ArchiveError
from trading_research_agents.extras import install_hint

BATCH_ROWS = 50_000


class TableError(ArchiveError):
    """The file is not a Parquet table this package can read."""


def _file(path: Path) -> Any:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise TableError(f"reading Parquet needs pyarrow; {install_hint('data')}") from exc
    try:
        return pq.ParquetFile(path)
    except (OSError, ValueError) as exc:
        raise TableError(f"{path.name}: not a Parquet file: {exc}") from exc


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def header(path: Path) -> list[str]:
    return list(_file(path).schema_arrow.names)


def rows(path: Path) -> Iterator[list[str]]:
    """Every row as text cells, in the file's column order, a batch at a time."""
    import pyarrow as pa

    table = _file(path)
    schema = table.schema_arrow
    # An instant is kept to the nanosecond, since Python's datetime stops at the microsecond. A
    # time with no zone stays text, the way a CSV would carry it, for the map to refuse.
    kinds = [
        ("instant" if f.type.tz else "local") if pa.types.is_timestamp(f.type) else ""
        for f in schema
    ]
    for batch in table.iter_batches(batch_size=BATCH_ROWS):
        columns = []
        for i, kind in enumerate(kinds):
            column = batch.column(i)
            if kind == "instant":
                column = column.cast(pa.timestamp("ns", tz="UTC")).cast(pa.int64())
            elif kind == "local":
                column = column.cast(pa.string())
            columns.append(column.to_pylist())
        for row in zip(*columns, strict=True):
            yield [cell(value) for value in row]


def records(path: Path) -> Iterator[dict[str, str]]:
    names = header(path)
    for row in rows(path):
        yield dict(zip(names, row, strict=True))
