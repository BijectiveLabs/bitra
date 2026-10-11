"""Walk the book: the price a size would actually have received, level by level.

The smallest instrument here and one of the most consequential, because it turns a mid-price
abstraction into something an operator could have transacted. When the recorded depth cannot
absorb the intended size it says so. It does not extrapolate the last level and it does not
price a partial fill as if it were complete; that lie would be most attractive on exactly the
thin instruments where a naive backtest looks best.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from trading_research_agents.data.schema import Side

BPS = Decimal(10_000)


class BookError(ValueError):
    """The book cannot answer the question honestly."""


@dataclass(frozen=True)
class Level:
    price: Decimal
    qty: Decimal

    def __post_init__(self) -> None:
        for label, value in (("price", self.price), ("qty", self.qty)):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise BookError(f"level {label} must be a positive finite Decimal, got {value!r}")


def mid_price(bid: Decimal, ask: Decimal) -> Decimal:
    _check_quote(bid, ask)
    return (bid + ask) / 2


def spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    """Quoted spread relative to mid, never to bid.

    A denominator of bid shifts the result on low-priced instruments, which are exactly the
    instruments whose spread in basis points tends to be widest. A locked book is zero, a real
    market state. A crossed book is refused: a snapshot caught mid-update is not a market.
    """
    _check_quote(bid, ask)
    return (ask - bid) / mid_price(bid, ask) * BPS


def half_spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    return spread_bps(bid, ask) / 2


def _check_quote(bid: Decimal, ask: Decimal) -> None:
    for label, value in (("bid", bid), ("ask", ask)):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise BookError(f"{label} must be a positive finite Decimal, got {value!r}")
    if ask < bid:
        raise BookError(f"crossed book: ask {ask} below bid {bid}")


@dataclass(frozen=True)
class WalkResult:
    vwap: Decimal
    slippage_bps: Decimal
    levels_consumed: int
    size: Decimal


def walk_the_book(levels: Sequence[Level], size: Decimal, side: Side, mid: Decimal) -> WalkResult:
    """Consume `levels` from best to worst until `size` is filled; report the VWAP against mid.

    `side` is the taker's side: a buy consumes asks (prices rising), a sell consumes bids
    (prices falling). Slippage is signed so that paying more than mid is a positive cost on
    either side.
    """
    if not isinstance(size, Decimal) or not size.is_finite() or size <= 0:
        raise BookError(f"size must be a positive finite Decimal, got {size!r}")
    if not isinstance(mid, Decimal) or not mid.is_finite() or mid <= 0:
        raise BookError(f"mid must be a positive finite Decimal, got {mid!r}")
    if not levels:
        raise BookError("empty book side")
    _check_ordering(levels, side)

    remaining = size
    notional = Decimal(0)
    consumed = 0
    for level in levels:
        take = min(remaining, level.qty)
        notional += take * level.price
        remaining -= take
        consumed += 1
        if remaining == 0:
            break
    if remaining > 0:
        raise BookError(
            f"insufficient depth: {size - remaining} of {size} available across "
            f"{len(levels)} levels; refusing to extrapolate"
        )
    vwap = notional / size
    sign = Decimal(1) if side is Side.BUY else Decimal(-1)
    return WalkResult(
        vwap=vwap, slippage_bps=sign * (vwap - mid) / mid * BPS, levels_consumed=consumed, size=size
    )


def _check_ordering(levels: Sequence[Level], side: Side) -> None:
    prices = [lvl.price for lvl in levels]
    if side is Side.BUY:
        ok = all(a < b for a, b in pairwise(prices))
        expected = "ascending asks"
    else:
        ok = all(a > b for a, b in pairwise(prices))
        expected = "descending bids"
    if not ok:
        raise BookError(f"levels are not ordered from best: expected {expected}")


@dataclass(frozen=True)
class Snapshot:
    """One recorded book state, with the time it was recorded.

    A snapshot recorded at an event and read at a decision several seconds later is not the
    book that was there; `walk` refuses a snapshot older than the tolerance the caller declares.
    """

    ts_ns: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise BookError("a snapshot carries at least one bid and one ask")
        _check_ordering(self.bids, Side.SELL)
        _check_ordering(self.asks, Side.BUY)
        if self.asks[0].price < self.bids[0].price:
            raise BookError("crossed book")

    @property
    def mid(self) -> Decimal:
        return mid_price(self.bids[0].price, self.asks[0].price)

    @property
    def half_spread_bps(self) -> Decimal:
        return half_spread_bps(self.bids[0].price, self.asks[0].price)

    def walk(self, size: Decimal, side: Side, *, at_ts_ns: int, max_age_ns: int) -> WalkResult:
        if at_ts_ns < self.ts_ns:
            raise BookError("decision time is before the snapshot was recorded")
        if at_ts_ns - self.ts_ns > max_age_ns:
            raise BookError(
                f"stale snapshot: recorded {(at_ts_ns - self.ts_ns) / 1e9:.3f}s before the "
                "decision, beyond the declared tolerance"
            )
        levels = self.asks if side is Side.BUY else self.bids
        return walk_the_book(levels, size, side, self.mid)
