"""The one-minute bar a desk reads order flow from, and how much of it anyone saw.

A candle says where the price went. A flow bar also says who moved it: how much of the volume was
a taker buying and how much a taker selling, how many trades made it, and whether the stream or
archive that built it covered the whole minute. A quiet minute and a minute nobody saw are
different facts, and only the second is a gap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum


class MarketDataError(ValueError):
    """A bar, a point or an archive file that cannot be what it claims to be."""


class GapState(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    MISSING = "missing"


_RANK = {GapState.NONE: 0, GapState.PARTIAL: 1, GapState.MISSING: 2}


def worst(states: Iterable[GapState]) -> GapState:
    """The least covered of `states`; nothing at all is fully covered."""
    found = GapState.NONE
    for state in states:
        if _RANK[state] > _RANK[found]:
            found = state
    return found


@dataclass(frozen=True, slots=True)
class FlowBar:
    venue: str
    instrument: str
    start_ns: int
    end_ns: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume_base: Decimal
    volume_quote: Decimal
    taker_buy_base: Decimal
    taker_sell_base: Decimal
    trade_count: int
    large_trade_base: Decimal | None
    late_trades: int
    gap: GapState
    source: str

    def __post_init__(self) -> None:
        if not self.venue.strip() or not self.instrument.strip():
            raise MarketDataError("a bar names its venue and instrument")
        if self.start_ns < 0 or self.end_ns <= self.start_ns:
            raise MarketDataError("a bar ends after it starts, at a non-negative epoch time")
        if self.trade_count < 0 or self.late_trades < 0:
            raise MarketDataError("trade counts are not negative")
        if self.taker_buy_base + self.taker_sell_base != self.volume_base:
            raise MarketDataError(
                f"taker buy {self.taker_buy_base} and sell {self.taker_sell_base} do not add up "
                f"to the volume {self.volume_base}"
            )
        if self.trade_count == 0:
            self._check_empty()
        else:
            self._check_traded()

    def _check_empty(self) -> None:
        prices = (self.open, self.high, self.low, self.close)
        if any(p is not None for p in prices) or self.volume_base != 0 or self.volume_quote != 0:
            raise MarketDataError("a bar with no trades carries no prices and no volume")
        if self.large_trade_base not in (None, Decimal(0)):
            raise MarketDataError("a bar with no trades has no large-trade volume")

    def _check_traded(self) -> None:
        if self.gap is GapState.MISSING:
            raise MarketDataError("a bar nobody saw has no trades")
        o, h, lo, c = self.open, self.high, self.low, self.close
        if o is None or h is None or lo is None or c is None:
            raise MarketDataError("a bar with trades carries all four prices")
        if not lo <= min(o, c) <= max(o, c) <= h:
            raise MarketDataError("bar prices are inconsistent: low <= open, close <= high")
        if self.volume_base <= 0 or self.volume_quote <= 0:
            raise MarketDataError("a bar with trades has positive volume")
        if self.taker_buy_base < 0 or self.taker_sell_base < 0:
            raise MarketDataError("taker volumes are not negative")
        large = self.large_trade_base
        if large is not None and not Decimal(0) <= large <= self.volume_base:
            raise MarketDataError("large-trade volume lies between zero and the bar's volume")

    @property
    def empty(self) -> bool:
        return self.trade_count == 0

    @property
    def vwap(self) -> Decimal | None:
        """Quote over base. A quotient is not exact, so it is computed when read, never stored."""
        return None if self.empty else self.volume_quote / self.volume_base

    @property
    def delta_base(self) -> Decimal:
        return self.taker_buy_base - self.taker_sell_base

    def ohlc(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """The four prices of a bar that has trades; an empty bar refuses rather than guesses."""
        o, h, lo, c = self.open, self.high, self.low, self.close
        if o is None or h is None or lo is None or c is None:
            raise MarketDataError("an empty bar has no prices")
        return o, h, lo, c

    def with_late(self, count: int) -> FlowBar:
        """This bar after `count` trades arrived for it once it had closed: marked, not rewritten.

        Rewriting a closed bar would make a replay read a minute differently from the moment a
        live decision read it; marking it says the minute was not seen whole.
        """
        if count <= 0:
            return self
        gap = worst((self.gap, GapState.PARTIAL))
        return replace(self, late_trades=self.late_trades + count, gap=gap)


def quiet_bar(
    venue: str,
    instrument: str,
    start_ns: int,
    end_ns: int,
    *,
    source: str,
    large_declared: bool,
    gap: GapState = GapState.NONE,
) -> FlowBar:
    """A minute with no trades: watched and empty, or, with `gap` missing, not watched at all."""
    large = Decimal(0) if large_declared and gap is not GapState.MISSING else None
    return FlowBar(
        venue=venue,
        instrument=instrument,
        start_ns=start_ns,
        end_ns=end_ns,
        open=None,
        high=None,
        low=None,
        close=None,
        volume_base=Decimal(0),
        volume_quote=Decimal(0),
        taker_buy_base=Decimal(0),
        taker_sell_base=Decimal(0),
        trade_count=0,
        large_trade_base=large,
        late_trades=0,
        gap=gap,
        source=source,
    )
