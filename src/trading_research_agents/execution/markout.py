"""Adverse selection markout: how far the price moves against a passive fill, by horizon.

    markout(h) = side * (mid_{t+h} - fill_price_t) / fill_price_t * 1e4

Negative means the price moved against the maker. It is not a cost term: every other cost is
indifferent to how good your price is, while this one grows with how attractive your quote is,
because the informed flow is the flow that is about to be right. A markout figure is reported
beside the half-spread being earned, never alone; on the control instrument the adverse move at
sixty seconds ran to about four times the spread captured.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_research_agents.data.schema import Side
from trading_research_agents.execution.fill import FillError, Print, _last_at_or_before

BPS = Decimal(10_000)


def markout_bps(fill_price: Decimal, side: Side, mid_after: Decimal) -> Decimal:
    """Signed by the side of the passive position, so negative is always adverse."""
    for label, value in (("fill_price", fill_price), ("mid_after", mid_after)):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise FillError(f"{label} must be a positive finite Decimal, got {value!r}")
    sign = Decimal(1) if side is Side.BUY else Decimal(-1)
    return sign * (mid_after - fill_price) / fill_price * BPS


@dataclass(frozen=True)
class Fill:
    ts_ns: int
    price: Decimal
    side: Side


@dataclass(frozen=True)
class Markout:
    horizon_ns: int
    markout_bps: Decimal
    half_spread_bps: Decimal
    n: int

    @property
    def survives(self) -> bool:
        """The maker keeps more than the market takes back at this horizon."""
        return self.half_spread_bps > abs(self.markout_bps)


def markout_table(
    fills: Sequence[Fill],
    mids: Sequence[Print],
    *,
    horizons_ns: Sequence[int],
    half_spread_bps: Decimal,
) -> tuple[Markout, ...]:
    """Mean markout at each horizon over all fills, each beside the half-spread comparator.

    The mid at `t + h` is the last recorded mid at or before that time. A fill with no mid on
    record at some horizon refuses the table rather than being dropped from it.
    """
    if not fills:
        raise FillError("no fills")
    if not horizons_ns or any(h <= 0 for h in horizons_ns):
        raise FillError("horizons must be positive")
    if not isinstance(half_spread_bps, Decimal) or half_spread_bps < 0:
        raise FillError("half_spread_bps must be a non-negative Decimal")
    if not mids:
        raise FillError("no mids")
    last_mid_ts = mids[-1].ts_ns
    rows: list[Markout] = []
    for h in horizons_ns:
        total = Decimal(0)
        for fill in fills:
            mid = _last_at_or_before(mids, fill.ts_ns + h)
            # The series has to reach the horizon; the last mid before a gap is not the mid then.
            if mid is None or mid.ts_ns < fill.ts_ns or last_mid_ts < fill.ts_ns + h:
                raise FillError(f"no mid on record {h} ns after the fill at {fill.ts_ns}")
            total += markout_bps(fill.price, fill.side, mid.price)
        rows.append(
            Markout(
                horizon_ns=h,
                markout_bps=total / len(fills),
                half_spread_bps=half_spread_bps,
                n=len(fills),
            )
        )
    return tuple(rows)
