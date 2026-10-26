"""Dated text as it was first seen: headlines and notices kept so a replay reads what was known.

A headline's own timestamp says when its publisher dated it, which can be earlier than when anyone
could read it. So the stream is ordered and selected by the moment the recorder first saw each
item, and the publisher's time is kept beside it for reading, never for deciding what was known.
A headline seen again on a later poll is not a new fact and is kept once. The text arrives already
reduced to a plain line by `data.news`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from trading_research_agents.data.series import Event
from trading_research_agents.market_data.archive import (
    HOUR_NS,
    META,
    SCHEMA_VERSION,
    TEXT,
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

# Text is not a market's: the archive keeps it under this venue, one folder per declared feed.
TEXT_VENUE = "text"
# How far back a restarted recorder reads its own archive to know what it has already kept.
REMEMBER = timedelta(days=7)

__all__ = ["TEXT", "TextItem", "TextLog", "read_text_range", "write_text_day", "write_text_part"]


@dataclass(frozen=True, slots=True)
class TextItem:
    feed: str
    first_seen_ns: int
    published_ns: int | None
    text: str
    source: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ArchiveError("a text item carries text")

    @property
    def key(self) -> tuple[int | None, str]:
        return self.published_ns, self.text


class TextLog:
    """What one feed has said, kept once per item, at the moment it was first seen."""

    def __init__(self, root: Path, feed: str, *, now_ns: int) -> None:
        self.root = root
        self.feed = feed
        today = day_of(now_ns)
        earlier = read_text_range(root, feed, today - REMEMBER, today)
        self._seen = {item.key for item in earlier}

    def observe(self, events: Iterable[Event], *, now_ns: int) -> list[TextItem]:
        """The items in `events` not seen before, stamped with `now_ns`."""
        fresh: list[TextItem] = []
        for event in events:
            item = TextItem(self.feed, now_ns, event.when_ns, event.what, event.source)
            if item.key in self._seen:
                continue
            self._seen.add(item.key)
            fresh.append(item)
        return fresh


def write_text_part(
    items: Sequence[TextItem], root: Path, *, feed: str, hour_start_ns: int
) -> DayManifest:
    day, hour = hour_of(hour_start_ns)
    outside = [i for i in items if not hour_start_ns <= i.first_seen_ns < hour_start_ns + HOUR_NS]
    if outside:
        raise ArchiveError(f"{len(outside)} item(s) were not first seen in hour {hour:02d}")
    path = part_file(root, TEXT_VENUE, feed, TEXT, day, hour)
    return _write(items, path, feed, day, hour, ())


def write_text_day(
    items: Sequence[TextItem], root: Path, *, feed: str, day: date, sources: Sequence[SourceFile]
) -> DayManifest:
    outside = [i for i in items if day_of(i.first_seen_ns) != day]
    if outside:
        raise ArchiveError(f"{len(outside)} item(s) were not first seen on {day}")
    path = day_file(root, TEXT_VENUE, feed, TEXT, day)
    return _write(items, path, feed, day, None, sources)


def _write(
    items: Sequence[TextItem],
    path: Path,
    feed: str,
    day: date,
    hour: int | None,
    sources: Sequence[SourceFile],
) -> DayManifest:
    arrow, _ = arrow_modules()
    ordered = sorted(items, key=lambda i: (i.first_seen_ns, i.text))
    stamp = arrow.timestamp("ns", tz="UTC")
    schema = arrow.schema(
        [
            arrow.field("first_seen", stamp, nullable=False),
            arrow.field("published", stamp, nullable=True),
            arrow.field("text", arrow.string(), nullable=False),
            arrow.field("source", arrow.string(), nullable=False),
        ],
        metadata={META + "schema": SCHEMA_VERSION, META + "stream": TEXT},
    )
    columns = {
        "first_seen": [i.first_seen_ns for i in ordered],
        "published": [i.published_ns for i in ordered],
        "text": [i.text for i in ordered],
        "source": [i.source for i in ordered],
    }
    manifest = DayManifest(
        stream=TEXT,
        venue=TEXT_VENUE,
        instrument=feed,
        day=day,
        rows=len(ordered),
        sha256="",
        partial_minutes=0,
        missing_minutes=0,
        sources=tuple(sources),
        hour=hour,
    )
    return write_unit(arrow.table(columns, schema=schema), path, manifest)


def read_text_file(path: Path, feed: str) -> list[TextItem]:
    verified(path)
    arrow, parquet = arrow_modules()
    table = parquet.read_table(path)
    seen = table.column("first_seen").cast(arrow.int64()).to_pylist()
    published = table.column("published").cast(arrow.int64()).to_pylist()
    texts = table.column("text").to_pylist()
    sources = table.column("source").to_pylist()
    return [TextItem(feed, seen[i], published[i], texts[i], sources[i]) for i in range(len(seen))]


def read_text_range(root: Path, feed: str, start: date, end: date) -> list[TextItem]:
    """Every item first seen from `start` to `end` inclusive, in the order they were seen."""
    out: list[TextItem] = []
    day = start
    while day <= end:
        units = day_units(root, TEXT_VENUE, feed, TEXT, day)
        for hour in sorted(units, key=lambda h: -1 if h is None else h):
            out.extend(read_text_file(units[hour], feed))
        day += timedelta(days=1)
    return out
