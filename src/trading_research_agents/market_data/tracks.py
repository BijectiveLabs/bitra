"""What the recorder keeps for one market and stream while it runs: open bars, the hour being
filled, and the time the stream did not cover.

Each track turns what arrives into the archive's own units and writes an hour part once the hour
can no longer change. A minute the stream did not cover is never written as a quiet one: covered in
part it is partial, not covered at all it is missing. A part already on disk for the same hour, from
an earlier run, keeps every minute it saw; this run fills only the minutes it was missing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

from trading_research_agents.data.schema import Trade
from trading_research_agents.market_data.archive import (
    FLOW,
    HOUR_NS,
    SourceFile,
    day_file,
    hour_of,
    missing_minutes,
    part_file,
    read_flow_file,
    write_flow_part,
)
from trading_research_agents.market_data.archive_book import (
    BOOK,
    missing_book,
    read_book_file,
    write_book_part,
)
from trading_research_agents.market_data.archive_deriv import read_deriv_file, write_deriv_part
from trading_research_agents.market_data.bars import FlowBar, GapState, quiet_bar, worst
from trading_research_agents.market_data.book import BookBar, BookBarBuilder, BookSample
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import NS_PER_MINUTE, day_of
from trading_research_agents.market_data.deriv import DerivPoint
from trading_research_agents.market_data.text import TextItem, write_text_part

M = NS_PER_MINUTE


@dataclass(frozen=True)
class Scales:
    """Decimal places of the market's prices and of its quantities in the base."""

    price: int
    qty: int


@dataclass
class Outages:
    """Spans a stream did not cover, `[start, end)`; an open one has no end yet."""

    spans: list[tuple[int, int | None]] = field(default_factory=list)

    def open(self, at_ns: int) -> None:
        if not self.spans or self.spans[-1][1] is not None:
            self.spans.append((at_ns, None))

    def close(self, at_ns: int) -> int | None:
        """End the open span; its start, or None when nothing was open."""
        if not self.spans or self.spans[-1][1] is not None:
            return None
        start = self.spans[-1][0]
        self.spans[-1] = (start, at_ns)
        return start

    def cover(self, start_ns: int, end_ns: int) -> GapState:
        """How much of `[start, end)` the stream saw: whole, in part, or not at all."""
        state = GapState.NONE
        for low, high in self.spans:
            top = end_ns if high is None else high
            if low <= start_ns and top >= end_ns:
                return GapState.MISSING
            if low < end_ns and top > start_ns:
                state = GapState.PARTIAL
        return state


def _hour_start(at_ns: int) -> int:
    return at_ns - at_ns % HOUR_NS


Minute = TypeVar("Minute", FlowBar, BookBar)


def _seen_first(earlier: Sequence[Minute], fresh: Sequence[Minute]) -> list[Minute]:
    """Minute by minute, what an earlier run saw, else what this run saw."""
    return [
        old if old.gap is not GapState.MISSING else new
        for old, new in zip(earlier, fresh, strict=True)
    ]


class FlowTrack:
    """Trades of one market into one-minute flow bars and hour parts."""

    def __init__(
        self,
        root: Path,
        venue: str,
        instrument: str,
        *,
        scales: Scales,
        large_trade_quote: Decimal | None,
        grace_ns: int,
        close_after_ns: int,
        hold_ns: int,
        start_ns: int,
    ) -> None:
        self.root = root
        self.venue = venue
        self.instrument = instrument
        self.scales = scales
        self.large_trade_quote = large_trade_quote
        self.grace_ns = grace_ns
        self.close_after_ns = close_after_ns
        self.hold_ns = hold_ns
        self.started_ns = start_ns
        self.stopped_ns: int | None = None
        self.builder = FlowBarBuilder(
            venue=venue,
            instrument=instrument,
            source=f"stream:{venue}",
            start_ns=start_ns - start_ns % M,
            grace_ns=grace_ns,
            large_trade_quote=large_trade_quote,
        )
        self.outages = Outages()
        self.hours: dict[int, dict[int, FlowBar]] = {}
        self.late_after_writing = 0

    def push(self, trades: Iterable[Trade]) -> None:
        for trade in trades:
            self._keep(self.builder.push(trade))
        self._apply_late()

    def advance(self, now_ns: int) -> None:
        self._keep(self.builder.advance(now_ns - self.close_after_ns + self.grace_ns))
        self._apply_late()

    def finish(self, now_ns: int) -> None:
        """Close every minute up to the one `now_ns` falls in, that one as partial."""
        self.stopped_ns = now_ns
        self._keep(self.builder.finish(now_ns - now_ns % M + M, gap=GapState.NONE))
        self._apply_late()

    def due(self, now_ns: int, *, everything: bool = False) -> list[int]:
        return [h for h in sorted(self.hours) if everything or h + HOUR_NS + self.hold_ns <= now_ns]

    def write(self, hour_start: int) -> None:
        held = self.hours.pop(hour_start)
        bars = []
        for minute in range(hour_start, hour_start + HOUR_NS, M):
            bar = held.get(minute)
            if bar is None:
                bars.append(missing_minutes(self.venue, self.instrument, minute, 1)[0])
            else:
                bars.append(self._marked(bar))
        day, hour = hour_of(hour_start)
        path = part_file(self.root, self.venue, self.instrument, FLOW, day, hour)
        if path.is_file():
            earlier = read_flow_file(path, self.venue, self.instrument)
            bars = _seen_first(earlier, bars)
        write_flow_part(
            bars,
            self.root,
            price_scale=self.scales.price,
            qty_scale=self.scales.qty,
            sources=(SourceFile(f"stream:{self.venue}", None),),
            large_trade_quote=self.large_trade_quote,
        )

    def _keep(self, closed: Sequence[FlowBar]) -> None:
        # Coverage is judged when the hour is written, not here: a silence can be noticed late.
        for bar in closed:
            if bar.end_ns > self.started_ns:
                self.hours.setdefault(_hour_start(bar.start_ns), {})[bar.start_ns] = bar

    def _marked(self, bar: FlowBar) -> FlowBar:
        seen = self.outages.cover(bar.start_ns, bar.end_ns)
        stopped_ns = self.stopped_ns
        if bar.start_ns < self.started_ns or (stopped_ns is not None and bar.end_ns > stopped_ns):
            seen = worst((seen, GapState.PARTIAL))
        if seen is GapState.MISSING and bar.empty:
            return quiet_bar(
                self.venue,
                self.instrument,
                bar.start_ns,
                bar.end_ns,
                source=bar.source,
                large_declared=False,
                gap=GapState.MISSING,
            )
        if seen is GapState.NONE:
            return bar
        return replace(bar, gap=worst((bar.gap, GapState.PARTIAL)))

    def _apply_late(self) -> None:
        for start, count in self.builder.take_late().items():
            held = self.hours.get(_hour_start(start), {})
            if start in held:
                held[start] = held[start].with_late(count)
            else:
                self.late_after_writing += count


class BookTrack:
    """Book samples of one market into one-minute book bars and hour parts."""

    def __init__(
        self,
        root: Path,
        venue: str,
        instrument: str,
        *,
        scales: Scales,
        hold_ns: int,
        start_ns: int,
    ) -> None:
        self.root = root
        self.venue = venue
        self.instrument = instrument
        self.scales = scales
        self.hold_ns = hold_ns
        self.started_ns = start_ns
        self.builder = BookBarBuilder(
            venue=venue, instrument=instrument, start_ns=start_ns - start_ns % M
        )
        self.outages = Outages()
        self.hours: dict[int, dict[int, BookBar]] = {}

    def push(self, sample: BookSample) -> None:
        self._keep(self.builder.push(sample))

    def advance(self, now_ns: int) -> None:
        self._keep(self.builder.advance(now_ns))

    def finish(self, now_ns: int) -> None:
        self._keep(self.builder.finish(now_ns - now_ns % M + M, gap=GapState.PARTIAL))

    def due(self, now_ns: int, *, everything: bool = False) -> list[int]:
        return [h for h in sorted(self.hours) if everything or h + HOUR_NS + self.hold_ns <= now_ns]

    def write(self, hour_start: int) -> None:
        held = self.hours.pop(hour_start)
        bars = [
            self._marked(held[m])
            if m in held
            else missing_book(self.venue, self.instrument, m, 1)[0]
            for m in range(hour_start, hour_start + HOUR_NS, M)
        ]
        day, hour = hour_of(hour_start)
        path = part_file(self.root, self.venue, self.instrument, BOOK, day, hour)
        if path.is_file():
            earlier = read_book_file(path, self.venue, self.instrument)
            bars = _seen_first(earlier, bars)
        write_book_part(bars, self.root, price_scale=self.scales.price, qty_scale=self.scales.qty)

    def _keep(self, closed: Sequence[BookBar]) -> None:
        for bar in closed:
            if bar.end_ns > self.started_ns:
                self.hours.setdefault(_hour_start(bar.start_ns), {})[bar.start_ns] = bar

    def _marked(self, bar: BookBar) -> BookBar:
        seen = self.outages.cover(bar.start_ns, bar.end_ns)
        if bar.start_ns < self.started_ns:
            seen = worst((seen, GapState.PARTIAL))
        if seen is GapState.MISSING and bar.samples:
            seen = GapState.PARTIAL
        return replace(bar, gap=worst((bar.gap, seen)))


class PointTrack:
    """Derivatives points of one market, levels and liquidations, into hour parts.

    A level polled every minute often repeats the venue's last bucket; a repeat is kept once.
    """

    def __init__(self, root: Path, venue: str, instrument: str, *, grace_ns: int) -> None:
        self.root = root
        self.venue = venue
        self.instrument = instrument
        self.grace_ns = grace_ns
        self.hours: dict[int, dict[tuple[str, int, str], DerivPoint]] = {}
        self.written: set[int] = set()
        self.late_after_writing = 0

    def push(self, points: Iterable[DerivPoint]) -> None:
        for point in points:
            hour = _hour_start(point.as_of_ns)
            # An hour already written is written again, merged, until its day is compacted.
            if hour in self.written and self._compacted(hour):
                self.late_after_writing += 1
                continue
            key = (point.series.value, point.as_of_ns, str(point.value))
            self.hours.setdefault(hour, {}).setdefault(key, point)

    def due(self, now_ns: int, *, everything: bool = False) -> list[int]:
        return [
            h for h in sorted(self.hours) if everything or h + HOUR_NS + self.grace_ns <= now_ns
        ]

    def _compacted(self, hour_start: int) -> bool:
        whole = day_file(self.root, self.venue, self.instrument, "deriv", day_of(hour_start))
        return whole.is_file()

    def write(self, hour_start: int) -> None:
        points = list(self.hours.pop(hour_start).values())
        day, hour = hour_of(hour_start)
        path = part_file(self.root, self.venue, self.instrument, "deriv", day, hour)
        if path.is_file():
            earlier = read_deriv_file(path)
            keys = {(p.series.value, p.as_of_ns, str(p.value)) for p in points}
            points += [p for p in earlier if (p.series.value, p.as_of_ns, str(p.value)) not in keys]
        write_deriv_part(
            points,
            self.root,
            venue=self.venue,
            instrument=self.instrument,
            hour_start_ns=hour_start,
            sources=(SourceFile(f"stream:{self.venue}", None),),
        )
        self.written.add(hour_start)


class TextTrack:
    """New headlines of one feed into hour parts, stamped when first seen."""

    def __init__(self, root: Path, feed: str, *, grace_ns: int) -> None:
        self.root = root
        self.feed = feed
        self.grace_ns = grace_ns
        self.hours: dict[int, list[TextItem]] = {}

    def push(self, items: Iterable[TextItem]) -> None:
        for item in items:
            self.hours.setdefault(_hour_start(item.first_seen_ns), []).append(item)

    def due(self, now_ns: int, *, everything: bool = False) -> list[int]:
        return [
            h for h in sorted(self.hours) if everything or h + HOUR_NS + self.grace_ns <= now_ns
        ]

    def write(self, hour_start: int) -> None:
        write_text_part(
            self.hours.pop(hour_start), self.root, feed=self.feed, hour_start_ns=hour_start
        )
