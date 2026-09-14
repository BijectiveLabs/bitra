"""What the on-chain analyst is handed: the same series engine, with flow and z-score figures.

An on-chain series (exchange net flow, supply held, transaction counts) arrives through the
same column map as a macro one, and the brief carries the same basis line: values known at
the time, thresholds declared by the operator, correlations measured with n. Two figures are
added because they are what flow series are read for: the net over a window, and the last
known value against its own recent history.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_research_agent.data.series import (
    Series,
    correlation,
    net_flow,
    threshold_state,
    zscore,
)

NS_PER_S = 1_000_000_000


def onchain_brief(
    series: Sequence[Series],
    *,
    at_ns: int,
    times_ns: Sequence[int],
    returns: Sequence[float],
    flow_window: timedelta = timedelta(days=7),
    zscore_points: int = 30,
) -> dict[str, Any]:
    window = f"{len(times_ns)} steps" if times_ns else "no steps"
    rows = []
    for s in series:
        state = threshold_state(s, at_ns)
        rows.append(
            {
                **state,
                "source": s.spec.source,
                "net_over_window": net_flow(s, at_ns, window=flow_window).record(),
                "zscore_vs_recent": zscore(s, at_ns, window_points=zscore_points).record(),
                "correlation_measured": (
                    correlation(s, times_ns, returns, window=window).record() if times_ns else None
                ),
            }
        )
    return {
        "as_of": datetime.fromtimestamp(at_ns / NS_PER_S, tz=UTC).isoformat(),
        "basis": (
            "values are the last known before as_of; net and z-score use only points known "
            "then; thresholds are operator-declared; correlations are measured and carry n"
        ),
        "series": rows,
        "flow_window": str(flow_window),
    }
