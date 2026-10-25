"""Walk-forward windows: fit on one span, leave an embargo, test on the next, and step by the test.

The embargo is at least the longest hold, so no idea opened in a fit window is still open, and still
being paid, when the test window starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_research_agents.market_data.days import DAY_NS, midnight_ns


@dataclass(frozen=True)
class Split:
    fit_start_ns: int
    fit_end_ns: int
    test_start_ns: int
    test_end_ns: int


def walk_forward(
    start: date,
    end: date,
    *,
    fit_days: int,
    test_days: int,
    embargo_bars: int,
    interval_ns: int,
) -> list[Split]:
    """Ordered (fit, test) windows inside the days `start` to `end`, both inclusive."""
    if fit_days <= 0 or test_days <= 0 or embargo_bars < 0 or interval_ns <= 0:
        raise ValueError("fit and test days are positive, the embargo is not negative")
    stop = midnight_ns(end) + DAY_NS
    embargo = embargo_bars * interval_ns
    out: list[Split] = []
    fit_start = midnight_ns(start)
    while True:
        fit_end = fit_start + fit_days * DAY_NS
        test_start = fit_end + embargo
        test_end = test_start + test_days * DAY_NS
        if test_end > stop:
            return out
        out.append(Split(fit_start, fit_end, test_start, test_end))
        fit_start += test_days * DAY_NS
