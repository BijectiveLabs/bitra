"""Where trades come from, and how they become bars.

A feed is an iterator of canonical trades in time order. `RecordedFeed` replays a dataset;
the live feed lives in its own module behind an optional dependency. `BarBuilder` turns
trades into fixed intervals and marks an interval with no trades as empty rather than carrying
the previous close forward, because a filled-in bar is a defaulted value with a price on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from trading_research_agents.data.schema import Trade
from trading_research_agents.strategy.core import Bar


class FeedError(ValueError):
    """The feed cannot be read as a time-ordered stream of one instrument."""


class Feed(Protocol):
    @property
    def instrument(self) -> str: ...

    def __iter__(self) -> Iterator[Trade]: ...


@dataclass
class RecordedFeed:
    """Replay trades already on disk or in memory. Refuses to go backwards in time."""

    instrument: str
    trades: Iterable[Trade]

    def __iter__(self) -> Iterator[Trade]:
        last = None
        for t in self.trades:
            if t.instrument != self.instrument:
                raise FeedError(f"trade for {t.instrument!r} on a feed for {self.instrument!r}")
            if last is not None and t.ts_ns < last:
                raise FeedError("recorded trades are not in time order")
            last = t.ts_ns
            yield t

    @classmethod
    def from_parquet(cls, path: Path, *, instrument: str) -> RecordedFeed:
        from trading_research_agents.data.parquet import read_trades

        return cls(instrument=instrument, trades=read_trades(path))


class BarBuilder:
    """Fold trades into bars of `interval_ns`, aligned to the epoch."""

    def __init__(self, interval_ns: int) -> None:
        if interval_ns <= 0:
            raise FeedError("bar interval must be positive")
        self.interval_ns = interval_ns
        self._start: int | None = None
        self._open: Decimal | None = None
        self._high: Decimal | None = None
        self._low: Decimal | None = None
        self._close: Decimal | None = None
        self._volume = Decimal(0)
        self._count = 0

    def push(self, trade: Trade) -> list[Bar]:
        """Add one trade; return every bar that closed before it, empty intervals included."""
        closed: list[Bar] = []
        start = trade.ts_ns - (trade.ts_ns % self.interval_ns)
        if self._start is None:
            self._start = start
        while start > self._start:
            closed.append(self._flush())
            self._start += self.interval_ns
        if start < self._start:
            raise FeedError("trade arrived before the bar being built; feed is out of order")
        if self._count == 0:
            self._open = self._high = self._low = trade.price
        else:
            if self._high is None or self._low is None:  # pragma: no cover - count > 0 guarantees
                raise FeedError("bar state is inconsistent")
            self._high = max(self._high, trade.price)
            self._low = min(self._low, trade.price)
        self._close = trade.price
        self._volume += trade.qty
        self._count += 1
        return closed

    def flush(self) -> Bar | None:
        """Close the bar in progress, if any trade has been seen."""
        if self._start is None:
            return None
        return self._flush()

    def _flush(self) -> Bar:
        if self._start is None:  # pragma: no cover - callers check
            raise FeedError("nothing to flush")
        bar = Bar(
            start_ns=self._start,
            end_ns=self._start + self.interval_ns,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
            trades=self._count,
        )
        self._open = self._high = self._low = self._close = None
        self._volume = Decimal(0)
        self._count = 0
        return bar
