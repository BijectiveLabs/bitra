"""Trades into flow bars, the same way from a venue's archive and from its stream.

There is one builder because there must be one answer. A bar built tomorrow from the venue's
archive and the bar the recorder built from the stream at the time are the same minute; made by
two pieces of code, they would differ for reasons nobody could see.

A stream delivers trades slightly out of order and sometimes twice. So a bar stays open for a
grace after its end, open and close follow the venue's time rather than arrival, and a trade id
seen in the last ten minutes is dropped. The archive is the stream with a grace of zero.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError, quiet_bar
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S

DEDUP_WINDOW_NS = 10 * 60 * NS_PER_S


@dataclass
class _Accumulator:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    open_ns: int
    close_ns: int
    volume_base: Decimal = Decimal(0)
    volume_quote: Decimal = Decimal(0)
    buy_base: Decimal = Decimal(0)
    sell_base: Decimal = Decimal(0)
    count: int = 0
    large_base: Decimal = Decimal(0)

    @classmethod
    def first(cls, trade: Trade, large: bool) -> _Accumulator:
        p = trade.price
        made = cls(open=p, high=p, low=p, close=p, open_ns=trade.ts_ns, close_ns=trade.ts_ns)
        made.take(trade, large)
        return made

    def add(self, trade: Trade, large: bool) -> None:
        # Venue time, not arrival: a stream that swaps two trades inside the grace still builds
        # the bar the archive builds. On a tie, open keeps the first pushed, close the last.
        if trade.ts_ns < self.open_ns:
            self.open, self.open_ns = trade.price, trade.ts_ns
        if trade.ts_ns >= self.close_ns:
            self.close, self.close_ns = trade.price, trade.ts_ns
        self.take(trade, large)

    def take(self, trade: Trade, large: bool) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.volume_base += trade.qty
        self.volume_quote += trade.qty * trade.price
        if trade.side is Side.BUY:
            self.buy_base += trade.qty
        else:
            self.sell_base += trade.qty
        self.count += 1
        if large:
            self.large_base += trade.qty


class FlowBarBuilder:
    """Fold one market's trades into bars of `interval_ns`, aligned to the epoch."""

    def __init__(
        self,
        *,
        venue: str,
        instrument: str,
        source: str,
        start_ns: int | None = None,
        interval_ns: int = NS_PER_MINUTE,
        grace_ns: int = 0,
        large_trade_quote: Decimal | None = None,
    ) -> None:
        if interval_ns <= 0 or grace_ns < 0:
            raise MarketDataError("the interval is positive and the grace is not negative")
        if start_ns is not None and start_ns % interval_ns:
            raise MarketDataError("the first bar starts on an interval boundary")
        if large_trade_quote is not None and large_trade_quote <= 0:
            raise MarketDataError("the large-trade threshold is a positive quote amount")
        self.venue = venue
        self.instrument = instrument
        self.source = source
        self.interval_ns = interval_ns
        self.grace_ns = grace_ns
        self.large_trade_quote = large_trade_quote
        self.duplicates = 0
        self._next = start_ns
        self._watermark: int | None = None
        self._open: dict[int, _Accumulator] = {}
        self._seen: set[str] = set()
        self._order: deque[tuple[int, str]] = deque()
        self._late: dict[int, int] = {}

    def push(self, trade: Trade) -> list[FlowBar]:
        """Add one trade; return every bar its time closes, quiet minutes included."""
        if trade.venue != self.venue or trade.instrument != self.instrument:
            raise MarketDataError(
                f"a {trade.venue} {trade.instrument} trade pushed into the "
                f"{self.venue} {self.instrument} builder"
            )
        if self._duplicate(trade):
            return []
        start = trade.ts_ns - trade.ts_ns % self.interval_ns
        if self._next is None:
            self._next = start
        if start < self._next:
            self._late[start] = self._late.get(start, 0) + 1
            return []
        large = (
            self.large_trade_quote is not None and trade.qty * trade.price >= self.large_trade_quote
        )
        held = self._open.get(start)
        if held is None:
            self._open[start] = _Accumulator.first(trade, large)
        else:
            held.add(trade, large)
        return self.advance(trade.ts_ns)

    def advance(self, watermark_ns: int) -> list[FlowBar]:
        """Close every bar whose end plus the grace lies at or before the watermark."""
        if self._watermark is None or watermark_ns > self._watermark:
            self._watermark = watermark_ns
        closed: list[FlowBar] = []
        while self._next is not None and self._closable(self._next):
            closed.append(self._emit(self._next, GapState.NONE))
            self._next += self.interval_ns
        return closed

    def finish(self, until_ns: int, *, gap: GapState = GapState.NONE) -> list[FlowBar]:
        """Close every bar that starts before `until_ns`, each marked with `gap`.

        An archive day ends at midnight with nothing missing; a stream that stops mid-minute closes
        what it holds as partial. A trade at or after the end is refused, never dropped.
        """
        if until_ns % self.interval_ns:
            raise MarketDataError("a builder finishes on an interval boundary")
        beyond = [start for start in self._open if start >= until_ns]
        if beyond:
            raise MarketDataError(f"{len(beyond)} bar(s) hold trades at or after the end")
        closed: list[FlowBar] = []
        while self._next is not None and self._next < until_ns:
            closed.append(self._emit(self._next, gap))
            self._next += self.interval_ns
        return closed

    def take_late(self) -> dict[int, int]:
        """Trades that arrived for bars already closed, by bar start; cleared as they are taken."""
        late, self._late = self._late, {}
        return late

    def _closable(self, start: int) -> bool:
        if self._watermark is None:
            return False
        return start + self.interval_ns + self.grace_ns <= self._watermark

    def _duplicate(self, trade: Trade) -> bool:
        if trade.trade_id in self._seen:
            self.duplicates += 1
            return True
        self._seen.add(trade.trade_id)
        self._order.append((trade.ts_ns, trade.trade_id))
        cutoff = trade.ts_ns - DEDUP_WINDOW_NS
        while self._order and self._order[0][0] < cutoff:
            self._seen.discard(self._order.popleft()[1])
        return False

    def _emit(self, start: int, gap: GapState) -> FlowBar:
        end = start + self.interval_ns
        held = self._open.pop(start, None)
        if held is None:
            declared = self.large_trade_quote is not None
            return quiet_bar(
                self.venue,
                self.instrument,
                start,
                end,
                source=self.source,
                large_declared=declared,
                gap=gap,
            )
        return FlowBar(
            venue=self.venue,
            instrument=self.instrument,
            start_ns=start,
            end_ns=end,
            open=held.open,
            high=held.high,
            low=held.low,
            close=held.close,
            volume_base=held.volume_base,
            volume_quote=held.volume_quote,
            taker_buy_base=held.buy_base,
            taker_sell_base=held.sell_base,
            trade_count=held.count,
            large_trade_base=held.large_base if self.large_trade_quote is not None else None,
            late_trades=0,
            gap=gap,
            source=self.source,
        )
