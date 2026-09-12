"""The canonical trade, and what a row becomes when it cannot be one."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    """The taker's side. A buy is a trade where the aggressor lifted the offer."""

    BUY = "buy"
    SELL = "sell"


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
class Rejection:
    """A source row that did not become a trade, with the line it came from and why."""

    line: int
    reason: str
    raw: str
