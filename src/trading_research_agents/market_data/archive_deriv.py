"""Derivatives on disk: the points of one market and UTC day, levels and liquidations together.

Values are stored as their exact decimal text: a funding rate, an open interest, a mark price and a
liquidated quantity have scales no single decimal column fits without rounding one of them. Each
file is sorted by series, then by the venue's time. An empty day is written, because asking the
venue and hearing nothing is itself a fact.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.market_data.archive import (
    DERIV,
    HOUR_NS,
    META,
    SCHEMA_VERSION,
    ArchiveError,
    DayManifest,
    SourceFile,
    arrow_modules,
    day_file,
    day_units,
    hour_of,
    part_file,
    verified,
    write_unit,
)
from trading_research_agents.market_data.days import day_of
from trading_research_agents.market_data.deriv import DerivPoint, Series


def write_deriv_day(
    points: Sequence[DerivPoint],
    root: Path,
    *,
    venue: str,
    instrument: str,
    day: date,
    sources: Sequence[SourceFile],
) -> DayManifest:
    """One UTC day of derivatives points."""
    outside = [p for p in points if day_of(p.as_of_ns) != day]
    if outside:
        raise ArchiveError(f"{len(outside)} point(s) fall outside {day}")
    path = day_file(root, venue, instrument, DERIV, day)
    return _write(points, path, venue, instrument, day, None, sources)


def write_deriv_part(
    points: Sequence[DerivPoint],
    root: Path,
    *,
    venue: str,
    instrument: str,
    hour_start_ns: int,
    sources: Sequence[SourceFile],
) -> DayManifest:
    """One hour of derivatives points as a part of its day."""
    day, hour = hour_of(hour_start_ns)
    outside = [p for p in points if not hour_start_ns <= p.as_of_ns < hour_start_ns + HOUR_NS]
    if outside:
        raise ArchiveError(f"{len(outside)} point(s) fall outside hour {hour:02d} of {day}")
    path = part_file(root, venue, instrument, DERIV, day, hour)
    return _write(points, path, venue, instrument, day, hour, sources)


def _write(
    points: Sequence[DerivPoint],
    path: Path,
    venue: str,
    instrument: str,
    day: date,
    hour: int | None,
    sources: Sequence[SourceFile],
) -> DayManifest:
    arrow, _ = arrow_modules()
    ordered = sorted(points, key=lambda p: (p.series.value, p.as_of_ns, p.received_ns))
    stamp = arrow.timestamp("ns", tz="UTC")
    schema = arrow.schema(
        [
            arrow.field("series", arrow.string(), nullable=False),
            arrow.field("as_of", stamp, nullable=False),
            arrow.field("received", stamp, nullable=False),
            arrow.field("value", arrow.string(), nullable=False),
            arrow.field("detail", arrow.string(), nullable=False),
        ],
        metadata={META + "schema": SCHEMA_VERSION, META + "stream": DERIV},
    )
    columns = {
        "series": [p.series.value for p in ordered],
        "as_of": [p.as_of_ns for p in ordered],
        "received": [p.received_ns for p in ordered],
        "value": [str(p.value) for p in ordered],
        "detail": [
            json.dumps(dict(p.detail), sort_keys=True, separators=(",", ":")) for p in ordered
        ],
    }
    manifest = DayManifest(
        stream=DERIV,
        venue=venue,
        instrument=instrument,
        day=day,
        rows=len(ordered),
        sha256="",
        partial_minutes=0,
        missing_minutes=0,
        sources=tuple(sources),
        hour=hour,
    )
    return write_unit(arrow.table(columns, schema=schema), path, manifest)


def read_deriv_file(path: Path) -> list[DerivPoint]:
    verified(path)
    arrow, parquet = arrow_modules()
    table = parquet.read_table(path)
    as_of = table.column("as_of").cast(arrow.int64()).to_pylist()
    received = table.column("received").cast(arrow.int64()).to_pylist()
    series = table.column("series").to_pylist()
    values = table.column("value").to_pylist()
    details: list[Any] = table.column("detail").to_pylist()
    return [
        DerivPoint(
            Series(series[i]),
            as_of[i],
            received[i],
            Decimal(values[i]),
            tuple(sorted(json.loads(details[i]).items())),
        )
        for i in range(len(as_of))
    ]


def read_deriv_day(root: Path, venue: str, instrument: str, day: date) -> list[DerivPoint]:
    """The day's points from its day file, or from its hour parts in hour order; none if neither."""
    units = day_units(root, venue, instrument, DERIV, day)
    out: list[DerivPoint] = []
    for hour in sorted(units, key=lambda h: -1 if h is None else h):
        out.extend(read_deriv_file(units[hour]))
    return out


def read_deriv_range(
    root: Path, venue: str, instrument: str, start: date, end: date
) -> list[DerivPoint]:
    out: list[DerivPoint] = []
    day = start
    while day <= end:
        out.extend(read_deriv_day(root, venue, instrument, day))
        day += timedelta(days=1)
    return out
