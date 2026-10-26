"""The order book a minute had, from samples of it, kept as a one-minute bar.

A book cannot be bought back later: whatever was not sampled while it happened is gone. So the
recorder samples it every second, and a minute keeps what a decision would ask of it: how wide the
spread was, which side held more of the first ten levels, and how much rested within ten basis
points of the mid. A minute with no sample is empty, never a book that was flat.

Spread and imbalance are statistics of the samples and are floats. Resting depth is a sum of
quantities and stays exact; its mean is computed when read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from trading_research_agents.market_data.bars import GapState, MarketDataError
from trading_research_agents.market_data.days import NS_PER_MINUTE

TOP_LEVELS = 10
DEPTH_BAND = Decimal("0.001")
BPS = 10_000.0

Level = tuple[Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class BookSample:
    """One look at the book: levels as (price, quantity in the base), best first."""

    at_ns: int
    bids: Sequence[Level]
    asks: Sequence[Level]

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise MarketDataError("a book sample has both sides")
        if self.bids[0][0] >= self.asks[0][0]:
            raise MarketDataError(
                f"a crossed book: best bid {self.bids[0][0]} against best ask {self.asks[0][0]}"
            )

    @property
    def mid(self) -> Decimal:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    def spread_bps(self) -> float:
        return float((self.asks[0][0] - self.bids[0][0]) / self.mid) * BPS

    def imbalance(self) -> float:
        bid = sum((qty for _, qty in self.bids[:TOP_LEVELS]), Decimal(0))
        ask = sum((qty for _, qty in self.asks[:TOP_LEVELS]), Decimal(0))
        return float((bid - ask) / (bid + ask))

    def depth(self) -> tuple[Decimal, Decimal]:
        """Resting quantity priced within ten basis points of the mid, each side."""
        low, high = self.mid * (1 - DEPTH_BAND), self.mid * (1 + DEPTH_BAND)
        bid = sum((qty for price, qty in self.bids if price >= low), Decimal(0))
        ask = sum((qty for price, qty in self.asks if price <= high), Decimal(0))
        return bid, ask


@dataclass(frozen=True, slots=True)
class BookBar:
    venue: str
    instrument: str
    start_ns: int
    end_ns: int
    samples: int
    spread_bps_mean: float | None
    spread_bps_max: float | None
    imbalance_top10_mean: float | None
    depth_bid_10bps_sum_base: Decimal | None
    depth_ask_10bps_sum_base: Decimal | None
    mid_close: Decimal | None
    gap: GapState

    def __post_init__(self) -> None:
        if self.end_ns <= self.start_ns or self.samples < 0:
            raise MarketDataError("a book bar ends after it starts and counts its samples")
        figures = (
            self.spread_bps_mean,
            self.spread_bps_max,
            self.imbalance_top10_mean,
            self.depth_bid_10bps_sum_base,
            self.depth_ask_10bps_sum_base,
            self.mid_close,
        )
        if (self.samples == 0) != all(f is None for f in figures):
            raise MarketDataError("a book bar carries figures if and only if it has samples")

    @property
    def depth_bid_10bps_base(self) -> Decimal | None:
        total = self.depth_bid_10bps_sum_base
        return None if total is None else total / self.samples

    @property
    def depth_ask_10bps_base(self) -> Decimal | None:
        total = self.depth_ask_10bps_sum_base
        return None if total is None else total / self.samples


@dataclass
class _Minute:
    spreads: list[float] = field(default_factory=list)
    imbalances: list[float] = field(default_factory=list)
    bid_depth: Decimal = Decimal(0)
    ask_depth: Decimal = Decimal(0)
    mid: Decimal | None = None

    def add(self, sample: BookSample) -> None:
        self.spreads.append(sample.spread_bps())
        self.imbalances.append(sample.imbalance())
        bid, ask = sample.depth()
        self.bid_depth += bid
        self.ask_depth += ask
        self.mid = sample.mid


class BookBarBuilder:
    """Fold one market's book samples into one-minute bars, closed by the time of the samples."""

    def __init__(self, *, venue: str, instrument: str, start_ns: int) -> None:
        if start_ns % NS_PER_MINUTE:
            raise MarketDataError("the first book bar starts on a minute boundary")
        self.venue = venue
        self.instrument = instrument
        self.late = 0
        self._next = start_ns
        self._open = _Minute()

    def push(self, sample: BookSample) -> list[BookBar]:
        """Add one sample; return every minute it closes, empty ones included."""
        if sample.at_ns < self._next:
            self.late += 1
            return []
        closed = self.advance(sample.at_ns)
        self._open.add(sample)
        return closed

    def advance(self, now_ns: int) -> list[BookBar]:
        """Close every minute that ended at or before `now_ns`."""
        closed: list[BookBar] = []
        while self._next + NS_PER_MINUTE <= now_ns:
            closed.append(self._emit(GapState.NONE))
        return closed

    def finish(self, until_ns: int, *, gap: GapState) -> list[BookBar]:
        """Close every minute before `until_ns`, each marked with `gap`."""
        if until_ns % NS_PER_MINUTE:
            raise MarketDataError("a book builder finishes on a minute boundary")
        closed: list[BookBar] = []
        while self._next < until_ns:
            closed.append(self._emit(gap))
        return closed

    def _emit(self, gap: GapState) -> BookBar:
        minute, self._open = self._open, _Minute()
        start = self._next
        self._next += NS_PER_MINUTE
        count = len(minute.spreads)
        return BookBar(
            venue=self.venue,
            instrument=self.instrument,
            start_ns=start,
            end_ns=start + NS_PER_MINUTE,
            samples=count,
            spread_bps_mean=sum(minute.spreads) / count if count else None,
            spread_bps_max=max(minute.spreads) if count else None,
            imbalance_top10_mean=sum(minute.imbalances) / count if count else None,
            depth_bid_10bps_sum_base=minute.bid_depth if count else None,
            depth_ask_10bps_sum_base=minute.ask_depth if count else None,
            mid_close=minute.mid,
            gap=gap,
        )
