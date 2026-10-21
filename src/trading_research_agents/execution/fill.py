"""Whether a passive order would have filled, and what the ones that do not fill cost.

A passive strategy was backtested against spread data showing regular widening. Deployed for
18 hours it produced 823 triggers and zero fills. The backtest had asked whether the spread
widened; the market answered whether a quote posted behind an existing queue would be reached
before the spread closed. Fill probability is measured here as its own quantity, before a
strategy is evaluated.

An earlier matcher counted only discrete events at the resting level and missed every trade
that swept past it without printing there: 55.7 percent of adverse fills at one instrument.
Every missed fill was adverse, so the gross figures had been an optimistic bound. The matcher
here counts a print through the level as a fill.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from trading_research_agents.data.schema import Side

BPS = Decimal(10_000)


class FillError(ValueError):
    """The prints cannot answer the question for at least one order. Nothing is dropped."""


@dataclass(frozen=True)
class Print:
    ts_ns: int
    price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.price, Decimal) or not self.price.is_finite() or self.price <= 0:
            raise FillError(f"print price must be a positive finite Decimal, got {self.price!r}")


class FillMode(StrEnum):
    """Two bounds on the fill rate, because queue position is not recorded.

    A print exactly at the limit may have filled somebody ahead of us in the queue. Counting it
    (`AT_OR_THROUGH`) is an upper bound on the fill rate; counting only prints strictly past the
    level (`THROUGH`) is a lower bound. The truth sits between, and both are reported.
    """

    AT_OR_THROUGH = "at_or_through"
    THROUGH = "through"


def _check_sorted(prints: Sequence[Print]) -> None:
    for a, b in pairwise(prints):
        if b.ts_ns < a.ts_ns:
            raise FillError("prints are not in time order")


def passive_fill(
    prints: Sequence[Print],
    *,
    limit: Decimal,
    side: Side,
    t0_ns: int,
    budget_ns: int,
    mode: FillMode = FillMode.AT_OR_THROUGH,
) -> int | None:
    """Timestamp at which a passive order resting from `t0_ns` fills within the budget, or None.

    `side` is our side: a BUY rests a bid and fills when somebody sells down to it, so a print
    at or below the limit; a SELL rests an ask and fills on a print at or above it. Prints before
    `t0_ns` are ignored; they happened before the order existed.
    """
    if budget_ns <= 0:
        raise FillError("wait budget must be positive")
    if not isinstance(limit, Decimal) or not limit.is_finite() or limit <= 0:
        raise FillError(f"limit must be a positive finite Decimal, got {limit!r}")
    _check_sorted(prints)
    end = t0_ns + budget_ns
    for p in prints:
        if p.ts_ns < t0_ns:
            continue
        if p.ts_ns > end:
            break
        if side is Side.BUY:
            hit = p.price < limit if mode is FillMode.THROUGH else p.price <= limit
        else:
            hit = p.price > limit if mode is FillMode.THROUGH else p.price >= limit
        if hit:
            return p.ts_ns
    return None


def entry_after_latency(prints: Sequence[Print], *, signal_ts_ns: int, latency_ns: int) -> Print:
    """The first print at or after the signal plus the measured latency.

    A backtest that fills at the tick that fired the signal assumes an order that arrived before
    it was sent. Real execution arrives after network latency, and the price at arrival is not
    the price that fired.
    """
    if latency_ns < 0:
        raise FillError("latency cannot be negative")
    _check_sorted(prints)
    arrival = signal_ts_ns + latency_ns
    for p in prints:
        if p.ts_ns >= arrival:
            return p
    raise FillError("no print at or after the order's arrival; the series does not cover it")


def _last_at_or_before(prints: Sequence[Print], ts_ns: int) -> Print | None:
    found: Print | None = None
    for p in prints:
        if p.ts_ns > ts_ns:
            break
        found = p
    return found


@dataclass(frozen=True)
class IntendedOrder:
    ts_ns: int
    side: Side
    limit: Decimal


@dataclass(frozen=True)
class EffectiveCost:
    """What the intended trades cost once the ones that did not fill are counted.

        c_eff = p_fill * c_passive + (1 - p_fill) * (c_cross + drift_unfilled)

    The orders that fail to fill are not a random subset: they are the names moving away, which
    is why they did not fill, and their drift compounds while the order rests. Extending the wait
    budget makes this worse, not better: the longer it was, the further unfilled names had moved.
    """

    mode: FillMode
    n_filled: int
    n_unfilled: int
    c_passive_bps: Decimal
    c_cross_bps: Decimal
    drift_unfilled_bps: Decimal
    c_effective_bps: Decimal

    @property
    def p_fill(self) -> Decimal:
        return Decimal(self.n_filled) / Decimal(self.n_filled + self.n_unfilled)


def effective_cost(
    orders: Sequence[IntendedOrder],
    prints: Sequence[Print],
    *,
    budget_ns: int,
    c_passive_bps: Decimal,
    c_cross_bps: Decimal,
    mode: FillMode = FillMode.AT_OR_THROUGH,
) -> EffectiveCost:
    """Blend the passive and the crossing cost by the measured fill rate at this budget.

    `c_passive_bps` is what a filled passive round trip costs and `c_cross_bps` what crossing
    after the budget expires costs; both come from the cost identity and are declared by the
    caller. Drift on an unfilled order is the adverse move from the last print at placement to
    the last print at expiry. An order whose drift cannot be computed refuses the whole report:
    dropping it would remove exactly the observations that make the number informative.
    """
    if not orders:
        raise FillError("no intended orders")
    for label, value in (("c_passive_bps", c_passive_bps), ("c_cross_bps", c_cross_bps)):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise FillError(f"{label} must be a finite Decimal")
    _check_sorted(prints)
    filled = 0
    drifts: list[Decimal] = []
    for order in orders:
        hit = passive_fill(
            prints,
            limit=order.limit,
            side=order.side,
            t0_ns=order.ts_ns,
            budget_ns=budget_ns,
            mode=mode,
        )
        if hit is not None:
            filled += 1
            continue
        at_placement = _last_at_or_before(prints, order.ts_ns)
        at_expiry = _last_at_or_before(prints, order.ts_ns + budget_ns)
        if at_placement is None or at_expiry is None or at_expiry.ts_ns < order.ts_ns:
            raise FillError(
                f"drift on the unfilled order at {order.ts_ns} is not computable from the prints; "
                "refusing to drop it"
            )
        sign = Decimal(1) if order.side is Side.BUY else Decimal(-1)
        drifts.append(sign * (at_expiry.price - at_placement.price) / at_placement.price * BPS)

    n_unfilled = len(drifts)
    drift = sum(drifts, Decimal(0)) / n_unfilled if n_unfilled else Decimal(0)
    n = filled + n_unfilled
    p_fill = Decimal(filled) / Decimal(n)
    effective = p_fill * c_passive_bps + (1 - p_fill) * (c_cross_bps + drift)
    return EffectiveCost(
        mode=mode,
        n_filled=filled,
        n_unfilled=n_unfilled,
        c_passive_bps=c_passive_bps,
        c_cross_bps=c_cross_bps,
        drift_unfilled_bps=drift,
        c_effective_bps=effective,
    )
