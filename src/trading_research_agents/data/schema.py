"""The canonical trade, and what a row becomes when it cannot be one."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    """The taker's side. A buy is a trade where the aggressor lifted the offer."""

    BUY = "buy"
    SELL = "sell"


class Direction(StrEnum):
    """The side of a held position, as distinct from the side of the trade that opened it."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


def scale_of(value: Decimal) -> int:
    """Decimal places carried by the value as written: 93530.00 has scale 2, 0.003 has 3."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        return 0
    return max(0, -exponent)


@dataclass(frozen=True, slots=True)
class Trade:
    ts_ns: int
    venue: str
    instrument: str
    price: Decimal
    qty: Decimal
    side: Side
    trade_id: str
    seq: int | None = None

    def __post_init__(self) -> None:
        if self.ts_ns <= 0:
            raise ValueError("ts_ns must be a positive epoch nanosecond value")
        if not self.venue.strip() or not self.instrument.strip():
            raise ValueError("a trade names its venue and instrument")
        for label, value in (("price", self.price), ("qty", self.qty)):
            if not isinstance(value, Decimal):
                raise ValueError(f"{label} must be a Decimal")
            if not value.is_finite() or value <= 0:
                # Zero is not a price and not a quantity. A zero here is a defaulted field.
                raise ValueError(f"{label} must be a positive finite number, got {value}")
        if not self.trade_id.strip():
            raise ValueError("a trade carries its venue-assigned id")

    @property
    def ts(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ns / 1_000_000_000, tz=UTC)


@dataclass(frozen=True, slots=True)
class Candle:
    """One published interval: open, high, low, close, volume, and how many trades made it.

    `start_ns` is the interval's own start in epoch nanoseconds, so the candle carries market
    time rather than the time it was fetched. An interval with no trades is not a candle.
    """

    start_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int = 0

    def __post_init__(self) -> None:
        # The start is the interval the trade fell into, so bucketing a trade at the first
        # nanosecond of the epoch gives a start of zero. That is arithmetic, not a missing field.
        if self.start_ns < 0:
            raise ValueError("start_ns is an epoch nanosecond value, never negative")
        for label, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{label} must be a positive finite Decimal")
        if not isinstance(self.volume, Decimal) or self.volume < 0:
            raise ValueError("volume must be a non-negative Decimal")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("candle prices are inconsistent: low <= open, close <= high must hold")
        if self.trades < 0:
            raise ValueError("a candle counts zero or more trades")

    @property
    def ts(self) -> datetime:
        return datetime.fromtimestamp(self.start_ns / 1_000_000_000, tz=UTC)

    def record(self) -> dict[str, Any]:
        return {
            "start_ns": self.start_ns,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "trades": self.trades,
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    """A source row that did not become a trade, with the line it came from and why."""

    line: int
    reason: str
    raw: str
