"""What a live source is, and what it must say about every value it returns.

A source is one way of getting one kind of market data right now. The contract is deliberately
small: a source either returns a `Fetched` carrying the value and where it came from, or it
raises `SourceError`. It never returns a value without provenance, because a figure whose
origin and age are unknown is not usable for a decision about money.

Normalisation belongs to the source: whatever shape the vendor speaks, what comes out is the
package's own shape (bars as `Bar`, funding as a rate keyed by settlement, a book as two
ladders of price and size). Nothing downstream should know which vendor answered.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

NS_PER_S = 1_000_000_000

KINDS = ("ohlcv", "orderbook", "funding", "open_interest")
TIERS = ("free", "paid")


class SourceError(RuntimeError):
    """This source cannot answer right now: rate limited, unreachable, or has no such market."""


@dataclass(frozen=True)
class Fetched:
    """One value with its provenance. `as_of_ns` is market time; `fetched_at` is wall clock."""

    kind: str
    instrument: str
    value: Any
    source: str
    tier: str
    fetched_at: float
    as_of_ns: int
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind is one of {list(KINDS)}, got {self.kind!r}")
        if self.tier not in TIERS:
            raise ValueError(f"tier is one of {list(TIERS)}, got {self.tier!r}")
        if not self.source.strip():
            raise ValueError("a source names itself")
        if self.as_of_ns <= 0 or self.fetched_at <= 0:
            raise ValueError("both clocks are positive")

    def age_s(self, now: float | None = None) -> float:
        """How old the value is by market time, which is the age that matters for a decision."""
        reference = time.time() if now is None else now
        return round(reference - self.as_of_ns / NS_PER_S, 3)

    def record(self, now: float | None = None) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "instrument": self.instrument,
            "source": self.source,
            "tier": self.tier,
            "age_s": self.age_s(now),
            "as_of": self.as_of_ns,
            **dict(self.detail),
        }


class Source(Protocol):
    """One way to get one kind of data. `name` is what the snapshot records."""

    @property
    def name(self) -> str: ...

    @property
    def tier(self) -> str: ...

    def kinds(self) -> tuple[str, ...]: ...

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched: ...


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    """The top of the book, deep enough to walk for the size a decision is about."""

    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise ValueError("a book has both sides")
        if any(level.price <= 0 or level.size <= 0 for level in self.bids + self.asks):
            raise ValueError("book levels carry positive price and size")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("the book is crossed; refuse it rather than trade on it")

    @property
    def mid(self) -> Decimal:
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.asks[0].price - self.bids[0].price) / self.mid * Decimal(10_000)

    def record(self) -> dict[str, Any]:
        return {
            "best_bid": str(self.bids[0].price),
            "best_ask": str(self.asks[0].price),
            "spread_bps": str(self.spread_bps.quantize(Decimal("0.01"))),
            "levels": min(len(self.bids), len(self.asks)),
        }
