"""The sentiment reading, and what it says when there is nothing to read.

Sentiment is the easiest figure in this business to invent, so this brief is deliberately narrow.
It reads only what the operator mapped in: dated scores through the same series engine as macro
and on-chain data, and dated text through the same calendar reader. Nothing is scraped, nothing
is inferred from a model's memory of the internet, and a series nobody declared produces the
state `not declared` rather than a comfortable `neutral`.

Every figure carries its basis, the same as everywhere else: the value known at the time and when
it became known, the operator's threshold when there is one, the change over a window, and the
correlation with the instrument's returns measured on the operator's own data with its n.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_research_agents.data.series import (
    Event,
    Series,
    correlation,
    threshold_state,
    zscore,
)

NS_PER_S = 1_000_000_000


def sentiment_brief(
    series: Sequence[Series],
    *,
    at_ns: int,
    times_ns: Sequence[int] = (),
    returns: Sequence[float] = (),
    notes: Sequence[Event] = (),
    look_back: timedelta = timedelta(days=7),
    zscore_points: int = 30,
    notes_window: timedelta = timedelta(hours=24),
    max_notes: int = 40,
) -> dict[str, Any]:
    """Scores and dated notes as of one moment. No source declared is a state, not a zero."""
    window = f"{len(times_ns)} steps" if times_ns else "no steps"
    rows = []
    for s in series:
        state = threshold_state(s, at_ns)
        earlier = s.as_of(at_ns - int(look_back.total_seconds() * NS_PER_S))
        latest = s.as_of(at_ns)
        change = None
        if earlier is not None and latest is not None:
            change = float(latest.value) - float(earlier.value)
        rows.append(
            {
                **state,
                "source": s.spec.source,
                "points_known": sum(1 for p in s.points if p.published_ns < at_ns),
                "change_over_window": None if change is None else round(change, 4),
                "window": str(look_back),
                "zscore_vs_recent": zscore(s, at_ns, window_points=zscore_points).record(),
                "correlation_measured": (
                    correlation(s, times_ns, returns, window=window).record() if times_ns else None
                ),
            }
        )
    # Headlines age by the hour, not the week; the newest of the last day are what is read. Each is
    # named, so an answer can point at the one it rests on and code can check it.
    since = at_ns - int(min(look_back, notes_window).total_seconds() * NS_PER_S)
    fresh = [e for e in notes if since <= e.when_ns <= at_ns][-max_notes:]
    recent = [{"name": f"note_{n}", **e.record()} for n, e in enumerate(fresh, start=1)]
    return {
        "as_of": datetime.fromtimestamp(at_ns / NS_PER_S, tz=UTC).isoformat(),
        "basis": (
            "scores are the last known before as_of; states come from operator-declared "
            "thresholds; correlations are measured on the operator's data and carry n; notes are "
            "dated text from the feeds and files the operator declared, and nothing else"
        ),
        "declared": bool(series or recent),
        "series": rows,
        "notes": recent,
    }
