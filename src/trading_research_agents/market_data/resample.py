"""Coarser bars from finer ones, exactly.

The archive keeps one-minute bars and every other interval is made from them, so a fifteen-minute
or one-hour bar is the same object whether it was resampled or built directly from the trades,
and a decision on an hour bar can be traced to the sixty minutes inside it. A minute the source
did not see makes the coarse bar partial; a coarse bar nobody saw at all is missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_research_agents.market_data.bars import (
    FlowBar,
    GapState,
    MarketDataError,
    quiet_bar,
    worst,
)


def resample(bars: Sequence[FlowBar], interval_ns: int) -> list[FlowBar]:
    """Bars of `interval_ns` from consecutive bars of one market, aligned to the epoch."""
    if not bars:
        return []
    base = bars[0].end_ns - bars[0].start_ns
    if interval_ns <= 0 or interval_ns % base:
        raise MarketDataError(f"{interval_ns} ns is not a whole number of {base} ns bars")
    groups: dict[int, list[FlowBar]] = {}
    previous: FlowBar | None = None
    for bar in bars:
        _check_next(previous, bar, base)
        groups.setdefault(bar.start_ns - bar.start_ns % interval_ns, []).append(bar)
        previous = bar
    expected = interval_ns // base
    return [_merge(start, start + interval_ns, parts, expected) for start, parts in groups.items()]


def _check_next(previous: FlowBar | None, bar: FlowBar, base: int) -> None:
    if bar.end_ns - bar.start_ns != base or bar.start_ns % base:
        raise MarketDataError("every bar has the same length and starts on its boundary")
    if previous is None:
        return
    if (bar.venue, bar.instrument) != (previous.venue, previous.instrument):
        raise MarketDataError("bars to resample are of one market")
    if bar.start_ns <= previous.start_ns:
        raise MarketDataError("bars to resample are in order, each once")


def _merge(start: int, end: int, parts: list[FlowBar], expected: int) -> FlowBar:
    seen = [p for p in parts if p.gap is not GapState.MISSING]
    unseen = expected - len(seen)
    gap = _gap_of(seen, unseen, expected)
    first = parts[0]
    sources = {p.source for p in parts}
    source = first.source if len(sources) == 1 else "mixed"
    large = _large(seen)
    late = sum(p.late_trades for p in parts)
    traded = [p for p in seen if not p.empty]
    if not traded:
        quiet = quiet_bar(
            first.venue,
            first.instrument,
            start,
            end,
            source=source,
            large_declared=large is not None,
            gap=gap,
        )
        return quiet.with_late(late) if late else quiet
    prices = [p.ohlc() for p in traded]
    return FlowBar(
        venue=first.venue,
        instrument=first.instrument,
        start_ns=start,
        end_ns=end,
        open=prices[0][0],
        high=max(p[1] for p in prices),
        low=min(p[2] for p in prices),
        close=prices[-1][3],
        volume_base=sum((p.volume_base for p in seen), Decimal(0)),
        volume_quote=sum((p.volume_quote for p in seen), Decimal(0)),
        taker_buy_base=sum((p.taker_buy_base for p in seen), Decimal(0)),
        taker_sell_base=sum((p.taker_sell_base for p in seen), Decimal(0)),
        trade_count=sum(p.trade_count for p in seen),
        large_trade_base=large,
        late_trades=late,
        gap=gap,
        source=source,
    )


def _gap_of(seen: list[FlowBar], unseen: int, expected: int) -> GapState:
    if unseen >= expected:
        return GapState.MISSING
    covered = worst(p.gap for p in seen)
    return worst((covered, GapState.PARTIAL)) if unseen else covered


def _large(seen: list[FlowBar]) -> Decimal | None:
    total = Decimal(0)
    for part in seen:
        if part.large_trade_base is None:
            return None
        total += part.large_trade_base
    return total if seen else None
