"""Derivatives figures, read as of the decision from what the venue had published by then.

- `funding_rate`: the last settled rate, as a fraction.
- `funding_z_30d`: that rate against the settlements of the last 30 days, in standard deviations;
  absent below 10 settlements.
- `basis_bps`: (mark / index - 1) * 10^4.
- `oi_change_pct_h`: (open interest now / open interest h bars ago - 1) * 100.
- `oi_price_state` over 4 bars: `new_longs` (interest and price up), `new_shorts` (interest up,
  price down), `short_covering` (interest down, price up), `long_liquidation` (both down), `flat`
  (interest moved less than 0.25 %).
- `long_short_ratio`: the venue's account long/short ratio.

A value past its age is absent with the timeline's reason; nothing is carried forward.
"""

from __future__ import annotations

from trading_research_agents.features.core import Absent, FeatureValue, Inputs, Value, last_z
from trading_research_agents.features.price import move_bps
from trading_research_agents.market_data.days import DAY_NS
from trading_research_agents.market_data.deriv import Known, Series, Unknown

FUNDING_WINDOW_NS = 30 * DAY_NS
FUNDING_MIN_SETTLEMENTS = 10
OI_FLAT_PCT = 0.25
STATE_BARS = 4


def known(inputs: Inputs, series: Series, at_ns: int | None = None) -> Known | Absent:
    line = inputs.deriv.get(series)
    if line is None:
        return Absent(f"no {series} history for this market")
    found = line.at(inputs.at_ns if at_ns is None else at_ns)
    return Absent(found.reason) if isinstance(found, Unknown) else found


def funding_rate(inputs: Inputs) -> FeatureValue:
    found = known(inputs, Series.FUNDING)
    return found if isinstance(found, Absent) else found.value


def funding_z_30d(inputs: Inputs) -> FeatureValue:
    found = known(inputs, Series.FUNDING)
    if isinstance(found, Absent):
        return found
    since = inputs.at_ns - FUNDING_WINDOW_NS
    points = inputs.deriv[Series.FUNDING].points
    rates = [float(p.value) for p in points if since < p.as_of_ns <= inputs.at_ns]
    if len(rates) < FUNDING_MIN_SETTLEMENTS:
        return Absent(
            f"{len(rates)} settlement(s) in the last 30 days; this needs {FUNDING_MIN_SETTLEMENTS}"
        )
    return last_z(rates)


def basis_bps(inputs: Inputs) -> FeatureValue:
    mark = known(inputs, Series.MARK)
    if isinstance(mark, Absent):
        return mark
    index = known(inputs, Series.INDEX)
    if isinstance(index, Absent):
        return index
    return float((mark.value / index.value - 1) * 10_000)


def oi_change(inputs: Inputs, horizon: int) -> float | Absent:
    now = known(inputs, Series.OPEN_INTEREST)
    if isinstance(now, Absent):
        return now
    before = known(inputs, Series.OPEN_INTEREST, inputs.at_ns - horizon * inputs.interval_ns)
    if isinstance(before, Absent):
        return before
    if before.value == 0:
        return Absent("open interest was zero at the start of the window")
    return float((now.value / before.value - 1) * 100)


def oi_change_pct(horizon: int) -> Value:
    return lambda inputs: oi_change(inputs, horizon)


def oi_price_state(inputs: Inputs) -> FeatureValue:
    change = oi_change(inputs, STATE_BARS)
    if isinstance(change, Absent):
        return change
    move = move_bps(inputs, STATE_BARS)
    if isinstance(move, Absent):
        return move
    if abs(change) < OI_FLAT_PCT:
        return "flat"
    rising = move > 0
    if change > 0:
        return "new_longs" if rising else "new_shorts"
    return "short_covering" if rising else "long_liquidation"


def long_short_ratio(inputs: Inputs) -> FeatureValue:
    found = known(inputs, Series.LONG_SHORT)
    return found if isinstance(found, Absent) else found.value
