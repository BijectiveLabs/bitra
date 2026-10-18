"""Market structure: the swings price has made, what they say about the trend, and what broke.

A swing high is a bar whose high stands above the bars on each side of it; a swing low the same,
below. Swings alternate: two highs with no low between them are one high, the higher of the two,
and two lows the lower. Each swing is labelled against the one before it of its own kind: a higher
or lower high (HH, LH), a higher or lower low (HL, LL).

From the last high and the last low:

    HH and HL   uptrend      the low that held is the level whose loss changes the trend
    LH and LL   downtrend    the high that held is the level whose loss changes the trend
    LH and HL   range        contracting
    HH and LL   range        expanding

Then what the closes did since. In an uptrend, a close above the last high continues the trend
(break of structure) and a close below the last low is the first sign it is over (change of
character); a downtrend the mirror. In a range, a close outside either edge is a break of the
range. Only closes count: a wick through a level and back is not a break.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

SWING_BARS = 2
LABELLED = 6
BPS = Decimal(10_000)


class Priced(Protocol):
    @property
    def high(self) -> Decimal | None: ...

    @property
    def low(self) -> Decimal | None: ...

    @property
    def close(self) -> Decimal | None: ...


@dataclass(frozen=True)
class Swing:
    kind: str
    price: Decimal
    index: int
    label: str = ""

    def record(self, last: int) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label or None,
            "price": str(self.price),
            "bars_ago": last - self.index,
        }


@dataclass(frozen=True)
class Structure:
    trend: str
    swings: tuple[Swing, ...] = ()
    last: int = 0
    event: str | None = None
    event_level: Decimal | None = None
    event_bars_ago: int | None = None
    invalidation: Decimal | None = None
    support: Decimal | None = None
    resistance: Decimal | None = None
    close: Decimal | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def record(self) -> dict[str, Any]:
        return {
            "trend": self.trend,
            "swings": [s.record(self.last) for s in self.swings[-LABELLED:]],
            "event": self.event,
            "event_level": None if self.event_level is None else str(self.event_level),
            "event_bars_ago": self.event_bars_ago,
            "invalidation": None if self.invalidation is None else str(self.invalidation),
            "support": None if self.support is None else str(self.support),
            "support_bps": _away(self.support, self.close),
            "resistance": None if self.resistance is None else str(self.resistance),
            "resistance_bps": _away(self.resistance, self.close),
        }


def _away(level: Decimal | None, close: Decimal | None) -> str | None:
    if level is None or close is None or close <= 0:
        return None
    return str((abs(level - close) / close * BPS).quantize(Decimal("0.01")))


def swings(bars: Sequence[Priced], side: int = SWING_BARS) -> list[Swing]:
    """Alternating swing highs and lows, oldest first, each labelled against its predecessor.

    The last `side` bars cannot be a swing yet: what follows them has not closed."""
    found: list[Swing] = []
    for i in range(side, len(bars) - side):
        around = [b for j, b in enumerate(bars[i - side : i + side + 1]) if j != side]
        high, low = bars[i].high, bars[i].low
        if high is not None and all(b.high is not None and high > b.high for b in around):
            found.append(Swing("high", high, i))
        if low is not None and all(b.low is not None and low < b.low for b in around):
            found.append(Swing("low", low, i))
    alternating: list[Swing] = []
    for swing in found:
        if alternating and alternating[-1].kind == swing.kind:
            kept = alternating[-1]
            higher = swing.price > kept.price
            if (swing.kind == "high" and higher) or (swing.kind == "low" and not higher):
                alternating[-1] = swing
            continue
        alternating.append(swing)
    labelled: list[Swing] = []
    previous: dict[str, Decimal] = {}
    for swing in alternating:
        before = previous.get(swing.kind)
        label = ""
        if before is not None:
            up = swing.price > before
            label = ("HH" if up else "LH") if swing.kind == "high" else ("HL" if up else "LL")
        labelled.append(Swing(swing.kind, swing.price, swing.index, label))
        previous[swing.kind] = swing.price
    return labelled


def read(bars: Sequence[Priced], side: int = SWING_BARS) -> Structure:
    """The structure of `bars`, the last one being the most recent closed bar."""
    closes = [b.close for b in bars]
    if len(bars) < 2 * side + 3 or any(c is None for c in closes):
        return Structure(trend="unclear", notes=("too few bars for a swing",))
    last = len(bars) - 1
    close = closes[-1]
    found = swings(bars, side)
    highs = [s for s in found if s.kind == "high"]
    lows = [s for s in found if s.kind == "low"]
    if not highs or not lows or not highs[-1].label or not lows[-1].label:
        return Structure(trend="unclear", swings=tuple(found), last=last, close=close)
    high, low = highs[-1], lows[-1]
    pair = (high.label, low.label)
    trend = {("HH", "HL"): "uptrend", ("LH", "LL"): "downtrend"}.get(pair, "range")
    since = max(high.index, low.index)
    event, level, ago = _event(trend, high, low, closes, since)
    # A level a later close went through no longer holds anything up or down.
    above = [
        s.price
        for s in highs
        if close is not None and s.price > close and not _closed_past(closes, s, up=True)
    ]
    below = [
        s.price
        for s in lows
        if close is not None and s.price < close and not _closed_past(closes, s, up=False)
    ]
    return Structure(
        trend=trend,
        swings=tuple(found),
        last=last,
        event=event,
        event_level=level,
        event_bars_ago=ago,
        invalidation={"uptrend": low.price, "downtrend": high.price}.get(trend),
        support=max(below) if below else None,
        resistance=min(above) if above else None,
        close=close,
    )


def _closed_past(closes: Sequence[Decimal | None], swing: Swing, *, up: bool) -> bool:
    later = [c for c in closes[swing.index + 1 :] if c is not None]
    return any(c > swing.price for c in later) if up else any(c < swing.price for c in later)


def _event(
    trend: str, high: Swing, low: Swing, closes: Sequence[Decimal | None], since: int
) -> tuple[str | None, Decimal | None, int | None]:
    """The first close past the last high or low after both were made, and what it means."""
    last = len(closes) - 1
    for i in range(since + 1, last + 1):
        c = closes[i]
        if c is None:
            continue
        if c > high.price:
            name = {
                "uptrend": "bullish break of structure",
                "downtrend": "bullish change of character",
            }.get(trend, "break above the range")
            return name, high.price, last - i
        if c < low.price:
            name = {
                "downtrend": "bearish break of structure",
                "uptrend": "bearish change of character",
            }.get(trend, "break below the range")
            return name, low.price, last - i
    return None, None, None


FRESH_BARS = 3


def fading_a_break(target: str, structure: Mapping[str, Any] | None) -> str:
    """Why a fade points into a break that has just happened, or empty: a close past a swing in the
    last few bars is the move a fade would be standing in front of."""
    if not structure:
        return ""
    event, ago = str(structure.get("event") or ""), structure.get("event_bars_ago")
    if "break" not in event or not isinstance(ago, int) or ago > FRESH_BARS:
        return ""
    up = "bullish" in event or "above" in event
    if (up and target == "short") or (not up and target == "long"):
        return f"a {event} {ago} bar(s) ago"
    return ""


def against(setup_way: str, structure: Mapping[str, Any] | None) -> str:
    """Why a trade with a trend built into it points against the structure, or empty."""
    if not structure:
        return ""
    trend = structure.get("trend")
    event = str(structure.get("event") or "")
    # A change of character is the structure turning; until a new swing confirms it, the old trend
    # is no longer something to lean on either way.
    if "change of character" in event:
        return ""
    if trend == "uptrend" and setup_way == "short":
        return "the structure is an uptrend (a higher high and a higher low)"
    if trend == "downtrend" and setup_way == "long":
        return "the structure is a downtrend (a lower high and a lower low)"
    return ""
