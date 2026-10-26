"""One market's archive as a replay reads it: minutes for settlement, coarser bars and derivatives
for decisions, and the views built from them.

The minutes after a decision are kept apart from what a view is built from: settlement reads them,
a policy never does. Each feature vector is computed once per market, interval and moment.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from trading_research_agents.evaluation.policy import DecisionView
from trading_research_agents.features import FeatureVector, Inputs, compute_features
from trading_research_agents.features.catalog import MAX_WINDOW_BARS
from trading_research_agents.market_data.archive import (
    BOOK,
    DERIV,
    FLOW,
    DayManifest,
    day_units,
    manifest_file,
    read_flow_range,
)
from trading_research_agents.market_data.archive_book import read_book_range
from trading_research_agents.market_data.archive_deriv import read_deriv_range
from trading_research_agents.market_data.bars import FlowBar, MarketDataError
from trading_research_agents.market_data.book import BookBar, resample_book
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S
from trading_research_agents.market_data.deriv import LEVELS, DerivPoint, Series, Timeline
from trading_research_agents.market_data.resample import resample


def label_of(interval_ns: int) -> str:
    seconds = interval_ns // NS_PER_S
    return f"{seconds // 3600}h" if seconds % 3600 == 0 else f"{seconds // 60}m"


class MinuteTape:
    """Contiguous one-minute bars of one market, read by the minute's start."""

    def __init__(self, bars: Sequence[FlowBar]) -> None:
        self.bars = tuple(bars)
        self.first_ns = self.bars[0].start_ns if self.bars else 0
        for i, bar in enumerate(self.bars):
            if bar.start_ns != self.first_ns + i * NS_PER_MINUTE:
                raise MarketDataError("a minute tape holds contiguous one-minute bars")
            if bar.end_ns - bar.start_ns != NS_PER_MINUTE:
                raise MarketDataError("a minute tape holds one-minute bars")

    def span(self, start_ns: int, end_ns: int) -> tuple[FlowBar, ...] | None:
        """The minutes from `start_ns` up to `end_ns`; None when the tape does not hold them all."""
        if start_ns % NS_PER_MINUTE or end_ns % NS_PER_MINUTE or end_ns <= start_ns:
            raise MarketDataError("a span starts and ends on minute boundaries, in order")
        first = (start_ns - self.first_ns) // NS_PER_MINUTE
        last = (end_ns - self.first_ns) // NS_PER_MINUTE
        if first < 0 or last > len(self.bars):
            return None
        return self.bars[first:last]


class MarketHistory:
    """One market's minutes, its bars at each interval a run reads, and its derivatives."""

    def __init__(
        self,
        instrument: str,
        minutes: Sequence[FlowBar],
        points: Sequence[DerivPoint],
        intervals_ns: Sequence[int],
        *,
        book: Sequence[BookBar] = (),
    ) -> None:
        self.instrument = instrument
        self.tape = MinuteTape(minutes)
        self.points = tuple(points)
        intervals = sorted(set(intervals_ns))
        self._bars = {i: resample(self.tape.bars, i) for i in intervals}
        self._ends = {i: [b.end_ns for b in bars] for i, bars in self._bars.items()}
        self._book = {i: resample_book(book, i) for i in intervals}
        self._book_ends = {i: [b.end_ns for b in bars] for i, bars in self._book.items()}
        self.events = tuple(p for p in self.points if p.series is Series.LIQUIDATION)
        self._event_times = [p.as_of_ns for p in self.events]
        self.timelines: dict[Series, Timeline] = {}
        for series in LEVELS:
            found = [p for p in self.points if p.series is series]
            if found:
                self.timelines[series] = Timeline(series, found)

    def closed(
        self, interval_ns: int, at_ns: int, count: int = MAX_WINDOW_BARS
    ) -> tuple[FlowBar, ...]:
        """The last `count` bars of `interval_ns` that had closed by `at_ns`."""
        stop = bisect_right(self._ends[interval_ns], at_ns)
        return tuple(self._bars[interval_ns][max(0, stop - count) : stop])

    def closed_book(self, interval_ns: int, at_ns: int, count: int = 1) -> tuple[BookBar, ...]:
        """The last `count` book bars of `interval_ns` that had closed by `at_ns`."""
        stop = bisect_right(self._book_ends[interval_ns], at_ns)
        return tuple(self._book[interval_ns][max(0, stop - count) : stop])

    def events_until(self, at_ns: int, since_ns: int) -> tuple[DerivPoint, ...]:
        """Liquidations published in (since_ns, at_ns]."""
        low = bisect_right(self._event_times, since_ns)
        high = bisect_right(self._event_times, at_ns)
        return self.events[low:high]

    def next_return(self, interval_ns: int, at_ns: int) -> float | None:
        """The return of the bar closing one interval after `at_ns`: for scoring, never a view."""
        ends = self._ends[interval_ns]
        i = bisect_right(ends, at_ns)
        if i == 0 or i >= len(ends) or ends[i - 1] != at_ns:
            return None
        before, after = self._bars[interval_ns][i - 1], self._bars[interval_ns][i]
        if before.empty or after.empty:
            return None
        return float(after.ohlc()[3] / before.ohlc()[3] - 1)


class Views:
    """Decision views over a set of markets, each feature vector computed once."""

    def __init__(
        self,
        histories: Mapping[str, MarketHistory],
        *,
        primary_ns: int,
        context_ns: int,
        context: Sequence[str],
    ) -> None:
        self.histories = histories
        self.primary_ns = primary_ns
        self.context_ns = context_ns
        self.context = tuple(name for name in context if name in histories)
        self._vectors: dict[tuple[str, int, int], FeatureVector] = {}

    def vector(self, instrument: str, interval_ns: int, at_ns: int) -> FeatureVector:
        key = (instrument, interval_ns, at_ns)
        found = self._vectors.get(key)
        if found is None:
            history = self.histories[instrument]
            inputs = Inputs(
                at_ns=at_ns,
                interval_ns=interval_ns,
                bars=history.closed(interval_ns, at_ns),
                deriv=history.timelines,
                book=history.closed_book(interval_ns, at_ns),
                events=history.events_until(at_ns, at_ns - MAX_WINDOW_BARS * interval_ns),
                context={n: self.histories[n].closed(interval_ns, at_ns) for n in self.context},
            )
            found = self._vectors[key] = compute_features(inputs)
        return found

    def view(self, instrument: str, at_ns: int) -> DecisionView:
        primary = label_of(self.primary_ns)
        intervals = (self.primary_ns, self.context_ns)
        features = {label_of(i): self.vector(instrument, i, at_ns) for i in intervals}
        context = {
            name: {primary: self.vector(name, self.primary_ns, at_ns)}
            for name in self.context
            if name != instrument
        }
        return DecisionView(
            instrument=instrument,
            at_ns=at_ns,
            primary=primary,
            bars=self.histories[instrument].closed(self.primary_ns, at_ns),
            features=features,
            context=context,
        )


@dataclass(frozen=True)
class Loaded:
    history: MarketHistory
    manifests: dict[str, str]


def load_history(
    root: Path,
    venue: str,
    instrument: str,
    first: date,
    last: date,
    intervals_ns: Sequence[int],
) -> Loaded:
    """Every minute and derivatives point from `first` to `last`, and the sha256 of each day file
    read, so a result names the exact archive it was computed from."""
    minutes = read_flow_range(root, venue, instrument, first, last)
    points = read_deriv_range(root, venue, instrument, first, last)
    book = read_book_range(root, venue, instrument, first, last)
    manifests: dict[str, str] = {}
    day = first
    while day <= last:
        for stream in (FLOW, DERIV, BOOK):
            for path in day_units(root, venue, instrument, stream, day).values():
                digest = DayManifest.read(manifest_file(path)).sha256
                manifests[path.relative_to(root).as_posix()] = digest
        day += timedelta(days=1)
    recorded = book if any(b.samples for b in book) else ()
    history = MarketHistory(instrument, minutes, points, intervals_ns, book=recorded)
    return Loaded(history, manifests)
