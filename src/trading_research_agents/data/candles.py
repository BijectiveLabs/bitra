"""Candles: what a venue publishes per interval, and how we fold a tape into the same shape.

Two roads lead to a candle. A vendor hands one over already aggregated; a recorded tape is
folded into candles here. Both end in the same object, so nothing downstream needs to know
which road it came by.

A candle is not the runtime's bar. The bar in `strategy.core` carries an explicit end and the
rule that an interval without trades stays empty, because a decision core must be told that
nothing happened. A candle is the published aggregate: an interval with no trades is absent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from trading_research_agents.data.schema import Candle, Trade


class CandleError(ValueError):
    """The tape cannot be folded: the interval is not positive, or the trades are out of order."""


@dataclass
class _Forming:
    start_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int

    def add(self, trade: Trade) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        self.volume += trade.qty
        self.trades += 1

    def close_out(self) -> Candle:
        return Candle(
            start_ns=self.start_ns,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            trades=self.trades,
        )


def candles_of(trades: Iterable[Trade], interval_ns: int) -> list[Candle]:
    """Fold a tape into candles aligned to the epoch. Intervals with no trades are absent."""
    if interval_ns <= 0:
        raise CandleError("interval must be positive")
    out: list[Candle] = []
    forming: _Forming | None = None
    last_ts = -1
    for trade in trades:
        if trade.ts_ns < last_ts:
            raise CandleError("trades are out of order; audit the dataset first")
        last_ts = trade.ts_ns
        bucket = trade.ts_ns - (trade.ts_ns % interval_ns)
        if forming is None or forming.start_ns != bucket:
            if forming is not None:
                out.append(forming.close_out())
            forming = _Forming(
                start_ns=bucket,
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                volume=trade.qty,
                trades=1,
            )
            continue
        forming.add(trade)
    if forming is not None:
        out.append(forming.close_out())
    return out
