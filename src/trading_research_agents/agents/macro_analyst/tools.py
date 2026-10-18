"""What the macro analyst is handed: measured figures per series, never a raw file.

For each series the operator mapped in: the value known at the time of the brief and when it
became known, the state against the operator's declared threshold (labelled as declared), the
correlation and lead-lag of the series against the instrument's returns on the operator's own
data, with the sample size, and the scheduled events inside the horizon. The model reads a
table of numbers with their n; it does not read a vendor's opinion of what a number means.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from trading_research_agents.data.series import (
    Event,
    Series,
    correlation,
    lead_lag,
    threshold_state,
    upcoming,
)

NS_PER_S = 1_000_000_000


def returns_from_closes(times_ns: Sequence[int], closes: Sequence[float]) -> list[float]:
    """Simple returns between consecutive closes; the first is zero so lengths match."""
    out = [0.0]
    for a, b in pairwise(closes):
        out.append((b - a) / a if a else 0.0)
    return out


def macro_brief(
    series: Sequence[Series],
    *,
    at_ns: int,
    times_ns: Sequence[int],
    returns: Sequence[float],
    events: Sequence[Event] = (),
    horizon: timedelta = timedelta(days=7),
    max_lag: int = 3,
) -> dict[str, Any]:
    """The whole brief. Every figure carries its basis: measured (with n) or declared."""
    window = (
        f"{datetime.fromtimestamp(times_ns[0] / NS_PER_S, tz=UTC):%Y-%m-%d} to "
        f"{datetime.fromtimestamp(times_ns[-1] / NS_PER_S, tz=UTC):%Y-%m-%d}, {len(times_ns)} steps"
        if times_ns
        else "no steps"
    )
    rows = []
    for s in series:
        state = threshold_state(s, at_ns)
        corr = correlation(s, times_ns, returns, window=window) if times_ns else None
        lags = lead_lag(s, times_ns, returns, max_lag=max_lag, window=window) if times_ns else []
        best = None
        if lags:
            scored = [r for r in lags if r["value"] is not None]
            if scored:
                best = max(scored, key=lambda r: abs(float(r["value"])))
        rows.append(
            {
                **state,
                "source": s.spec.source,
                "points_known": sum(1 for p in s.points if p.published_ns < at_ns),
                "correlation_measured": None if corr is None else corr.record(),
                "strongest_lead_lag": best,
            }
        )
    return {
        "as_of": datetime.fromtimestamp(at_ns / NS_PER_S, tz=UTC).isoformat(),
        "basis": (
            "values are the last known before as_of; states come from operator-declared "
            "thresholds; correlations are measured on the operator's data and carry n"
        ),
        "series": rows,
        "events": [
            {"name": f"event_{n}", **e.record()}
            for n, e in enumerate(upcoming(events, at_ns, horizon=horizon), start=1)
        ],
        "horizon": str(horizon),
    }
