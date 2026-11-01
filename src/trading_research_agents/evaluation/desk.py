"""The desk row: a row's policy asked the way a live desk asks it, one instrument per pass.

A live desk with several instruments does not trade every one of them at every decision. A pass
orders them by room, the range the instrument's own paths draw over the hold against the spread
it costs to cross, and spends its one cycle on the first that is free. If that cycle decides
nothing, the pass ends. The plain rows of the arena ask every market every time, which is a
different set of trades; `desk:<row>` replays the selection, so what is measured is what the desk
would have taken.

Replayed: the ordering by room (the operator's order where room cannot be read) and one cycle per
pass on the first market without an open idea. Not replayed: the trigger, the correlation filter
and the daily intent cap, which depend on the desk's own record of earlier looks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from trading_research_agents.evaluation.paths import ohlc_of
from trading_research_agents.evaluation.policy import DecisionView
from trading_research_agents.evaluation.settle import Costs
from trading_research_agents.forecast.paths import PathError, PathSettings, read_paths

DESK_PREFIX = "desk:"
# What a live pass draws for room (agents/screen.py SCREEN_PATHS and HOLD_BARS).
ROOM_PATHS = 300
ROOM_HOLD_BARS = 24


def base_of(row: str) -> str | None:
    """The row a desk row asks, or None when `row` is not a desk row."""
    return row[len(DESK_PREFIX) :] if row.startswith(DESK_PREFIX) else None


def room(view: DecisionView, costs: Costs) -> Decimal | None:
    """The paths' range over the hold against the full spread, as a live pass orders by it.

    None when the spread is zero or the paths cannot be read, and the market then keeps the place
    the operator gave it.
    """
    if costs.spread_bps <= 0:
        return None
    try:
        reading = read_paths(
            ohlc_of(view.bars),
            side="long",
            take_bps=None,
            stop_bps=None,
            horizon_bars=ROOM_HOLD_BARS,
            settings=PathSettings(paths=ROOM_PATHS, seed=view.at_ns % 2**32),
        )
    except PathError:
        return None
    return (Decimal(f"{reading.range_bps:.2f}") / costs.spread_bps).quantize(Decimal("0.01"))


def chosen(
    views: Sequence[DecisionView],
    costs_for: Callable[[str], Costs],
    is_free: Callable[[str], bool],
) -> DecisionView | None:
    """The market a pass spends its cycle on: most room first, then the declared order."""
    where = {view.instrument: i for i, view in enumerate(views)}

    def key(view: DecisionView) -> tuple[int, Decimal, int]:
        found = room(view, costs_for(view.instrument))
        return (0 if found is not None else 1, -(found or Decimal(0)), where[view.instrument])

    free = [view for view in views if is_free(view.instrument)]
    return min(free, key=key) if free else None


__all__ = ["DESK_PREFIX", "base_of", "chosen", "room"]
