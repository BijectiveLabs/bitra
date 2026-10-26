"""Book bars on disk: a market's one-minute book statistics per UTC day, or per hour while recorded.

Statistics of the samples (spread, imbalance) are float64. Resting depth is stored as the exact sum
over the samples at the quantity scale, and the mid at one decimal place finer than the price, since
the mid of two prices can need it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trading_research_agents.market_data.archive import (
    BOOK,
    HOUR_NS,
    HOURS_PER_DAY,
    META,
    MINUTES_PER_DAY,
    MINUTES_PER_HOUR,
    PRECISION,
    SCHEMA_VERSION,
    ArchiveError,
    DayManifest,
    arrow_modules,
    check_minutes,
    check_scales,
    day_file,
    day_units,
    fit_scale,
    hour_of,
    part_file,
    verified,
    write_unit,
)
from trading_research_agents.market_data.bars import GapState
from trading_research_agents.market_data.book import BookBar
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns

FLOATS = ("spread_bps_mean", "spread_bps_max", "imbalance_top10_mean")
DEPTHS = ("depth_bid_10bps_sum_base", "depth_ask_10bps_sum_base")

__all__ = ["BOOK", "read_book_range", "write_book_day", "write_book_part"]


def write_book_day(
    bars: Sequence[BookBar], root: Path, *, day: date, price_scale: int, qty_scale: int
) -> DayManifest:
    _check(bars, f"a book day of {day}", midnight_ns(day), MINUTES_PER_DAY)
    path = day_file(root, bars[0].venue, bars[0].instrument, BOOK, day)
    return _write(bars, path, day, None, price_scale, qty_scale)


def write_book_part(
    bars: Sequence[BookBar], root: Path, *, price_scale: int, qty_scale: int
) -> DayManifest:
    if len(bars) != MINUTES_PER_HOUR:
        raise ArchiveError(f"an hour part holds {MINUTES_PER_HOUR} book bars, got {len(bars)}")
    day, hour = hour_of(bars[0].start_ns)
    _check(bars, f"book hour {hour:02d} of {day}", bars[0].start_ns, MINUTES_PER_HOUR)
    path = part_file(root, bars[0].venue, bars[0].instrument, BOOK, day, hour)
    return _write(bars, path, day, hour, price_scale, qty_scale)


def _check(bars: Sequence[BookBar], what: str, first_ns: int, count: int) -> None:
    check_minutes([b.start_ns for b in bars], first_ns, count, what)
    markets = {(b.venue, b.instrument) for b in bars}
    if len(markets) != 1:
        raise ArchiveError(f"{what} holds one market, got {sorted(markets)}")


def _write(
    bars: Sequence[BookBar],
    path: Path,
    day: date,
    hour: int | None,
    price_scale: int,
    qty_scale: int,
) -> DayManifest:
    check_scales(price_scale + 1, qty_scale)
    arrow, _ = arrow_modules()
    mid = arrow.decimal128(PRECISION, price_scale + 1)
    depth = arrow.decimal128(PRECISION, qty_scale)
    fields = [
        arrow.field("start", arrow.timestamp("ns", tz="UTC"), nullable=False),
        arrow.field("samples", arrow.int64(), nullable=False),
        *(arrow.field(name, arrow.float64(), nullable=True) for name in FLOATS),
        *(arrow.field(name, depth, nullable=True) for name in DEPTHS),
        arrow.field("mid_close", mid, nullable=True),
        arrow.field("gap", arrow.string(), nullable=False),
    ]
    metadata = {
        META + "schema": SCHEMA_VERSION,
        META + "stream": BOOK,
        META + "price_scale": str(price_scale),
        META + "qty_scale": str(qty_scale),
    }
    columns: dict[str, list[Any]] = {
        "start": [b.start_ns for b in bars],
        "samples": [b.samples for b in bars],
        **{name: [getattr(b, name) for b in bars] for name in FLOATS},
        **{
            name: [fit_scale(getattr(b, name), qty_scale, name, b.start_ns) for b in bars]
            for name in DEPTHS
        },
        "mid_close": [fit_scale(b.mid_close, price_scale + 1, "mid", b.start_ns) for b in bars],
        "gap": [b.gap.value for b in bars],
    }
    table = arrow.table(columns, schema=arrow.schema(fields, metadata=metadata))
    manifest = DayManifest(
        stream=BOOK,
        venue=bars[0].venue,
        instrument=bars[0].instrument,
        day=day,
        rows=len(bars),
        sha256="",
        partial_minutes=sum(1 for b in bars if b.gap is GapState.PARTIAL),
        missing_minutes=sum(1 for b in bars if b.gap is GapState.MISSING),
        sources=(),
        price_scale=price_scale,
        qty_scale=qty_scale,
        hour=hour,
    )
    return write_unit(table, path, manifest)


def read_book_file(path: Path, venue: str, instrument: str) -> list[BookBar]:
    verified(path)
    arrow, parquet = arrow_modules()
    table = parquet.read_table(path)
    starts = table.column("start").cast(arrow.int64()).to_pylist()
    names = ("samples", *FLOATS, *DEPTHS, "mid_close", "gap")
    columns = {name: table.column(name).to_pylist() for name in names}
    return [
        BookBar(
            venue=venue,
            instrument=instrument,
            start_ns=start,
            end_ns=start + NS_PER_MINUTE,
            samples=columns["samples"][i],
            spread_bps_mean=columns["spread_bps_mean"][i],
            spread_bps_max=columns["spread_bps_max"][i],
            imbalance_top10_mean=columns["imbalance_top10_mean"][i],
            depth_bid_10bps_sum_base=columns["depth_bid_10bps_sum_base"][i],
            depth_ask_10bps_sum_base=columns["depth_ask_10bps_sum_base"][i],
            mid_close=columns["mid_close"][i],
            gap=GapState(columns["gap"][i]),
        )
        for i, start in enumerate(starts)
    ]


def read_book_range(
    root: Path, venue: str, instrument: str, start: date, end: date
) -> list[BookBar]:
    """Every minute's book from `start` to `end` inclusive; a minute no file covers is missing."""
    out: list[BookBar] = []
    day = start
    while day <= end:
        units = day_units(root, venue, instrument, BOOK, day)
        if None in units:
            out.extend(read_book_file(units[None], venue, instrument))
        else:
            for hour in range(HOURS_PER_DAY):
                first = midnight_ns(day) + hour * HOUR_NS
                path = units.get(hour)
                if path is None:
                    out.extend(missing_book(venue, instrument, first, MINUTES_PER_HOUR))
                else:
                    out.extend(read_book_file(path, venue, instrument))
        day += timedelta(days=1)
    return out


def missing_book(venue: str, instrument: str, first_ns: int, count: int) -> list[BookBar]:
    return [
        BookBar(
            venue=venue,
            instrument=instrument,
            start_ns=first_ns + i * NS_PER_MINUTE,
            end_ns=first_ns + (i + 1) * NS_PER_MINUTE,
            samples=0,
            spread_bps_mean=None,
            spread_bps_max=None,
            imbalance_top10_mean=None,
            depth_bid_10bps_sum_base=None,
            depth_ask_10bps_sum_base=None,
            mid_close=None,
            gap=GapState.MISSING,
        )
        for i in range(count)
    ]
