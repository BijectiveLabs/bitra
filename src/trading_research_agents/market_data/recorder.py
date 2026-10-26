"""The recorder: one venue's markets kept in the archive while they happen, without a network.

This is the state the recorder carries, fed events by whatever reads the venue
(`market_data/venues/ccxt_stream.py`) and by a clock. Everything it does is decided here and can be
tested by feeding it events: which minute a trade belongs to, when a minute and an hour are closed,
what a disconnect did to coverage, when a closed day is compacted, and what is written to the
events log. It is the only writer of the archive it records.

`data/market/events.jsonl` holds one structured line per event: started, connected, disconnected,
gap_closed, late_trades, late_points, latency, skew, written, compacted, stopped.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.data.schema import Trade
from trading_research_agents.data.series import Event
from trading_research_agents.market_data.archive import HOUR_NS
from trading_research_agents.market_data.book import BookSample
from trading_research_agents.market_data.compact import compact_day
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S, day_of
from trading_research_agents.market_data.deriv import DerivPoint
from trading_research_agents.market_data.gaps import GapMark, record_gap
from trading_research_agents.market_data.record_config import RecordConfig
from trading_research_agents.market_data.text import TEXT_VENUE, TextLog
from trading_research_agents.market_data.tracks import (
    BookTrack,
    FlowTrack,
    PointTrack,
    Scales,
    TextTrack,
)
from trading_research_agents.records import append_line

EVENTS = "events.jsonl"
MINUTE_STREAMS = ("flow_1m", "book_1m", "deriv")
SKEW_MS = 1000
# Derivatives points and headlines are kept open this long past their hour: a venue publishes an
# open-interest bucket after it closes, and a poll can be late.
POINT_GRACE_NS = 5 * 60 * NS_PER_S
# Minutes are held this long past their hour before they are written: a connection that goes
# silent is noticed only when its keepalive gives up (ccxt: up to 90 seconds), and the minutes it
# missed are marked from where the silence began.
HOLD_NS = 3 * 60 * NS_PER_S
NS_PER_MS = 1_000_000

__all__ = ["HOLD_NS", "Recorder", "Scales"]


class Recorder:
    """The recorder's state for one venue and the markets of a `[record]` table."""

    def __init__(
        self,
        config: RecordConfig,
        root: Path,
        *,
        scales: Mapping[str, Scales],
        now_ns: int,
        feeds: Iterable[str] = (),
    ) -> None:
        self.config = config
        self.root = root
        self.venue = config.venue
        grace = config.grace_ms * NS_PER_MS
        close_after = config.close_after_ms * NS_PER_MS
        markets = config.markets
        self.flows = {
            name: FlowTrack(
                root,
                self.venue,
                name,
                scales=scales[name],
                large_trade_quote=config.large_trade_quote.get(name),
                grace_ns=grace,
                close_after_ns=close_after,
                hold_ns=HOLD_NS,
                start_ns=now_ns,
            )
            for name in markets
            if "trades" in config.streams
        }
        self.books = {
            name: BookTrack(
                root, self.venue, name, scales=scales[name], hold_ns=HOLD_NS, start_ns=now_ns
            )
            for name in markets
            if "book" in config.streams
        }
        self.points = {
            name: PointTrack(root, self.venue, name, grace_ns=POINT_GRACE_NS) for name in markets
        }
        names = tuple(feeds) or config.text
        self.texts = {feed: TextTrack(root, feed, grace_ns=POINT_GRACE_NS) for feed in names}
        self.logs = {feed: TextLog(root, feed, now_ns=now_ns) for feed in names}
        self.outage_reasons: dict[tuple[str, str], str] = {}
        self.latencies: dict[str, list[int]] = {}
        self._latency_minute = now_ns // NS_PER_MINUTE
        self.days: set[tuple[str, str, str, date]] = set()
        self._find_leftovers(now_ns)
        self._mark_start(now_ns)
        self._event(now_ns, "started", markets=list(markets), streams=list(config.streams))

    # What arrives from the venue

    def on_trades(self, instrument: str, trades: Iterable[Trade], received_ns: int) -> None:
        batch = list(trades)
        track = self.flows.get(instrument)
        if track is None or not batch:
            return
        late = [max(0, received_ns - t.ts_ns) // NS_PER_MS for t in batch]
        self.latencies.setdefault(instrument, []).extend(late)
        track.push(batch)

    def on_book(self, instrument: str, sample: BookSample) -> None:
        track = self.books.get(instrument)
        if track is not None:
            track.push(sample)

    def on_points(self, instrument: str, points: Iterable[DerivPoint]) -> None:
        self.points[instrument].push(points)

    def on_text(self, feed: str, events: Iterable[Event], now_ns: int) -> None:
        log = self.logs.get(feed)
        if log is not None:
            self.texts[feed].push(log.observe(events, now_ns=now_ns))

    def on_disconnect(
        self,
        instrument: str,
        stream: str,
        since_ns: int,
        reason: str,
        *,
        noticed_ns: int | None = None,
    ) -> None:
        """The stream is unseen from `since_ns`, its last message, though noticed only now."""
        for track in self._tracks_of(instrument, stream):
            track.outages.open(since_ns)
        self.outage_reasons[(instrument, stream)] = reason
        fields: dict[str, Any] = {"instrument": instrument, "stream": stream, "reason": reason}
        if noticed_ns is not None and noticed_ns != since_ns:
            fields["silent_since"] = _iso(since_ns)
        self._event(since_ns if noticed_ns is None else noticed_ns, "disconnected", **fields)

    def on_reconnect(self, instrument: str, stream: str, at_ns: int) -> None:
        started = None
        for track in self._tracks_of(instrument, stream):
            started = track.outages.close(at_ns) or started
        self._event(at_ns, "connected", instrument=instrument, stream=stream)
        if started is not None and started < at_ns:
            why = self.outage_reasons.pop((instrument, stream), "the stream dropped")
            mark = GapMark(self.venue, instrument, stream, started, at_ns, why)
            record_gap(self.root, mark)
            seconds = round((at_ns - started) / NS_PER_S, 3)
            self._event(at_ns, "gap_closed", instrument=instrument, stream=stream, seconds=seconds)

    # The clock

    def tick(self, now_ns: int) -> None:
        """Close what time has closed, write the hours that can no longer change, compact days."""
        for track in self.flows.values():
            track.advance(now_ns)
        for book in self.books.values():
            book.advance(now_ns)
        self._latency(now_ns)
        self._write_due(now_ns, everything=False)
        self._late(now_ns)
        self._compact(now_ns)

    def stop(self, now_ns: int) -> None:
        """Flush open minutes as partial, write every hour held, and mark the rest as unseen."""
        for track in self.flows.values():
            track.finish(now_ns)
        for book in self.books.values():
            book.finish(now_ns)
        self._latency(now_ns, now=True)
        self._write_due(now_ns, everything=True)
        self._late(now_ns)
        end = now_ns - now_ns % HOUR_NS + HOUR_NS
        for name, stream in self._streamed():
            reason = "the recorder stopped"
            record_gap(self.root, GapMark(self.venue, name, stream, now_ns, end, reason))
        self._event(now_ns, "stopped")

    # Inside

    def _tracks_of(self, instrument: str, stream: str) -> list[FlowTrack | BookTrack]:
        if stream == "trades" and instrument in self.flows:
            return [self.flows[instrument]]
        if stream == "book" and instrument in self.books:
            return [self.books[instrument]]
        return []

    def _write_due(self, now_ns: int, *, everything: bool) -> None:
        tracks: list[tuple[str, str, Any]] = [
            *((name, "flow_1m", t) for name, t in self.flows.items()),
            *((name, "book_1m", t) for name, t in self.books.items()),
            *((name, "deriv", t) for name, t in self.points.items()),
        ]
        for name, stream, track in tracks:
            for hour in track.due(now_ns, everything=everything):
                track.write(hour)
                self.days.add((self.venue, name, stream, day_of(hour)))
        for feed, text in self.texts.items():
            for hour in text.due(now_ns, everything=everything):
                text.write(hour)
                self.days.add((TEXT_VENUE, feed, "text", day_of(hour)))

    def _compact(self, now_ns: int) -> None:
        today = day_of(now_ns)
        for venue, name, stream, day in sorted(self.days):
            if day >= today or self._holds(name, stream, day):
                continue
            result = compact_day(self.root, venue, name, stream, day, now_ns=now_ns)
            self.days.discard((venue, name, stream, day))
            if result.written:
                self._event(
                    now_ns,
                    "compacted",
                    instrument=name,
                    stream=stream,
                    day=day.isoformat(),
                    parts=result.removed,
                    missing_minutes=result.missing_minutes,
                )

    def _holds(self, name: str, stream: str, day: date) -> bool:
        holders: dict[str, Any] = {
            "flow_1m": self.flows,
            "book_1m": self.books,
            "deriv": self.points,
            "text": self.texts,
        }
        track = holders.get(stream, {}).get(name)
        return track is not None and any(day_of(h) == day for h in track.hours)

    def _latency(self, now_ns: int, *, now: bool = False) -> None:
        """Latency of the minute just ended: one line per market per minute, not per message."""
        minute = now_ns // NS_PER_MINUTE
        if minute == self._latency_minute and not now:
            return
        self._latency_minute = minute
        for name, seen in self.latencies.items():
            if not seen:
                continue
            figures = {"latency_p50_ms": int(statistics.median(seen)), "latency_max_ms": max(seen)}
            self._event(now_ns, "latency", instrument=name, **figures)
            if figures["latency_max_ms"] > SKEW_MS:
                self._event(now_ns, "skew", instrument=name, **figures)
            seen.clear()

    def _late(self, now_ns: int) -> None:
        for name, track in self.flows.items():
            if track.late_after_writing:
                self._event(now_ns, "late_trades", instrument=name, count=track.late_after_writing)
                track.late_after_writing = 0
        for name, points in self.points.items():
            if points.late_after_writing:
                self._event(now_ns, "late_points", instrument=name, count=points.late_after_writing)
                points.late_after_writing = 0

    def _find_leftovers(self, now_ns: int) -> None:
        """Hour parts of closed days that an earlier run left, so the first tick compacts them."""
        today = day_of(now_ns)
        found = [(self.venue, name, s) for name in self.config.markets for s in MINUTE_STREAMS]
        found += [(TEXT_VENUE, feed, "text") for feed in self.texts]
        for venue, name, stream in found:
            folder = self.root / "market" / venue / name / stream
            if not folder.is_dir():
                continue
            for child in folder.iterdir():
                try:
                    day = date.fromisoformat(child.name)
                except ValueError:
                    continue
                if child.is_dir() and day < today:
                    self.days.add((venue, name, stream, day))

    def _mark_start(self, now_ns: int) -> None:
        hour = now_ns - now_ns % HOUR_NS
        if hour == now_ns:
            return
        for name, stream in self._streamed():
            reason = "the recorder was not running"
            record_gap(self.root, GapMark(self.venue, name, stream, hour, now_ns, reason))

    def _streamed(self) -> list[tuple[str, str]]:
        return [(name, "trades") for name in self.flows] + [(name, "book") for name in self.books]

    def _event(self, at_ns: int, event: str, **fields: Any) -> None:
        row = {"at": _iso(at_ns), "at_ns": at_ns, "event": event, "venue": self.venue, **fields}
        append_line(self.root / "market" / EVENTS, row)


def _iso(at_ns: int) -> str:
    return datetime.fromtimestamp(at_ns / NS_PER_S, tz=UTC).isoformat()
