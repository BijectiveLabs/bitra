"""Compaction: a closed day's hour parts joined into the one day file, and the parts removed.

A day is compacted only once it has ended. Every part is checked against its manifest first, and an
edited part stops the compaction with nothing removed. The day file is written and verified before
any part is deleted, so a crash at any point leaves either the parts or the day, never neither; a
run that finds both verifies the day and removes the parts it was made from. Hours no part covers
are written as missing minutes and recorded in `gaps.jsonl`, because a recorder that was down saw
nothing, and that is not a quiet market.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from trading_research_agents.market_data.archive import (
    BOOK,
    DERIV,
    FLOW,
    HOUR_NS,
    HOURS_PER_DAY,
    MINUTES_PER_HOUR,
    ArchiveError,
    DayManifest,
    SourceFile,
    day_file,
    manifest_file,
    missing_minutes,
    part_file,
    read_flow_file,
    verified,
    write_flow_day,
)
from trading_research_agents.market_data.archive_book import (
    missing_book,
    read_book_file,
    write_book_day,
)
from trading_research_agents.market_data.archive_deriv import read_deriv_file, write_deriv_day
from trading_research_agents.market_data.bars import FlowBar
from trading_research_agents.market_data.book import BookBar
from trading_research_agents.market_data.days import DAY_NS, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint
from trading_research_agents.market_data.gaps import GapMark, record_gap

Parts = Mapping[int, Path]
Manifests = Mapping[int, DayManifest]
Compactor = Callable[[Path, str, str, date, Parts, Manifests], DayManifest]


@dataclass(frozen=True)
class CompactResult:
    stream: str
    day: date
    written: bool
    removed: int
    missing_minutes: int


def compact_day(
    root: Path, venue: str, instrument: str, stream: str, day: date, *, now_ns: int
) -> CompactResult:
    """Join the closed `day`'s hour parts of one stream into its day file."""
    if midnight_ns(day) + DAY_NS > now_ns:
        raise ArchiveError(f"{day} is still open; a day is compacted once it has ended")
    compactor = COMPACTORS.get(stream)
    if compactor is None:
        raise ArchiveError(f"{stream!r} has no compaction; have {sorted(COMPACTORS)}")
    parts = {
        hour: path
        for hour in range(HOURS_PER_DAY)
        if (path := part_file(root, venue, instrument, stream, day, hour)).is_file()
    }
    if not parts:
        return CompactResult(stream, day, written=False, removed=0, missing_minutes=0)
    whole = day_file(root, venue, instrument, stream, day)
    if whole.is_file():
        done = verified(whole)
        return CompactResult(stream, day, False, _remove(parts), done.missing_minutes)
    manifests = {hour: verified(path) for hour, path in parts.items()}
    written = compactor(root, venue, instrument, day, parts, manifests)
    verified(whole)
    if stream in (FLOW, BOOK):
        _mark_uncovered(root, venue, instrument, stream, day, parts)
    return CompactResult(stream, day, True, _remove(parts), written.missing_minutes)


def _scales(manifests: Manifests) -> tuple[int, int, str | None]:
    found = {(m.price_scale, m.qty_scale, m.large_trade_quote) for m in manifests.values()}
    if len(found) != 1:
        raise ArchiveError(f"the hour parts disagree on their scales: {sorted(map(str, found))}")
    price, qty, large = found.pop()
    if price is None or qty is None:
        raise ArchiveError("an hour part does not name its scales")
    return price, qty, large


def _sources(manifests: Manifests) -> tuple[SourceFile, ...]:
    seen = {s for m in manifests.values() for s in m.sources}
    return tuple(sorted(seen, key=lambda s: (s.path, s.sha256 or "")))


def _compact_flow(
    root: Path, venue: str, instrument: str, day: date, parts: Parts, manifests: Manifests
) -> DayManifest:
    price, qty, large = _scales(manifests)
    bars: list[FlowBar] = []
    for hour in range(HOURS_PER_DAY):
        path = parts.get(hour)
        first = midnight_ns(day) + hour * HOUR_NS
        if path is None:
            bars.extend(missing_minutes(venue, instrument, first, MINUTES_PER_HOUR))
        else:
            bars.extend(read_flow_file(path, venue, instrument))
    return write_flow_day(
        bars,
        root,
        day=day,
        price_scale=price,
        qty_scale=qty,
        sources=_sources(manifests),
        large_trade_quote=None if large is None else Decimal(large),
    )


def _compact_book(
    root: Path, venue: str, instrument: str, day: date, parts: Parts, manifests: Manifests
) -> DayManifest:
    price, qty, _ = _scales(manifests)
    bars: list[BookBar] = []
    for hour in range(HOURS_PER_DAY):
        path = parts.get(hour)
        first = midnight_ns(day) + hour * HOUR_NS
        if path is None:
            bars.extend(missing_book(venue, instrument, first, MINUTES_PER_HOUR))
        else:
            bars.extend(read_book_file(path, venue, instrument))
    return write_book_day(bars, root, day=day, price_scale=price, qty_scale=qty)


def _compact_deriv(
    root: Path, venue: str, instrument: str, day: date, parts: Parts, manifests: Manifests
) -> DayManifest:
    points: list[DerivPoint] = []
    for hour in sorted(parts):
        points.extend(read_deriv_file(parts[hour]))
    return write_deriv_day(
        points, root, venue=venue, instrument=instrument, day=day, sources=_sources(manifests)
    )


COMPACTORS: dict[str, Compactor] = {
    FLOW: _compact_flow,
    BOOK: _compact_book,
    DERIV: _compact_deriv,
}


def _mark_uncovered(
    root: Path, venue: str, instrument: str, stream: str, day: date, parts: Parts
) -> None:
    for hour in range(HOURS_PER_DAY):
        if hour in parts:
            continue
        start = midnight_ns(day) + hour * HOUR_NS
        reason = f"{stream}: hour {hour:02d} of {day} was not recorded"
        record_gap(root, GapMark(venue, instrument, stream, start, start + HOUR_NS, reason))


def _remove(parts: Parts) -> int:
    for path in parts.values():
        manifest_file(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    folder = next(iter(parts.values())).parent
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    return len(parts)
