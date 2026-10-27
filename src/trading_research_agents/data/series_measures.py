"""What a series says against the instrument: as-of alignment, correlation, lead and lag, the
declared state, z-scores and net flows.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from trading_research_agents.data.series import NS_PER_S, Measurement, Series, moment


def align_asof(series: Series, times_ns: Sequence[int]) -> list[Decimal | None]:
    """For each time, the value known then. Never the value observed later."""
    return [None if (p := series.as_of(t)) is None else p.value for t in times_ns]


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def correlation(
    series: Series, times_ns: Sequence[int], returns: Sequence[float], *, window: str
) -> Measurement:
    """Pearson between the series' known value (as of each time) and the return at that time."""
    known = align_asof(series, times_ns)
    pairs = [(float(v), r) for v, r in zip(known, returns, strict=True) if v is not None]
    if len(pairs) < 3:
        return Measurement(None, len(pairs), window, "fewer than three aligned samples")
    if len({v for v in known if v is not None}) < 3:
        # A daily print held flat across a window of minutes is one number, not a sample; its float
        # mean is what made a constant series read as "correlation 0.0 on 60" (seen 2026-10-05).
        return Measurement(
            None,
            len(pairs),
            window,
            "slower than the bars: fewer than three values in the window",
        )
    xs, ys = zip(*pairs, strict=True)
    return Measurement(_pearson(xs, ys), len(pairs), window)


def lead_lag(
    series: Series,
    times_ns: Sequence[int],
    returns: Sequence[float],
    *,
    max_lag: int,
    window: str,
) -> list[dict[str, Any]]:
    """Correlation of the series' change with returns shifted by k steps, k in [-max_lag, max_lag].

    Positive k: the series moves first. The series is differenced so that a slow level does not
    pass for a signal, and it is aligned as-of at every step.
    """
    known = align_asof(series, times_ns)
    changes: list[float | None] = [None]
    for a, b in pairwise(known):
        changes.append(None if a is None or b is None else float(b - a))
    out: list[dict[str, Any]] = []
    for k in range(-max_lag, max_lag + 1):
        pairs = []
        for i, c in enumerate(changes):
            j = i + k
            if c is None or j < 0 or j >= len(returns):
                continue
            pairs.append((c, returns[j]))
        if len(pairs) < 3:
            out.append({"lag": k, "value": None, "n": len(pairs), "window": window})
            continue
        xs, ys = zip(*pairs, strict=True)
        r = _pearson(xs, ys)
        out.append(
            {
                "lag": k,
                "value": None if r is None else round(r, 4),
                "n": len(pairs),
                "window": window,
            }
        )
    return out


def threshold_state(series: Series, at_ns: int) -> dict[str, Any]:
    """The declared state at a time, from the value known then. Labelled as a declaration."""
    point = series.as_of(at_ns)
    spec = series.spec
    if point is None:
        return {"series": spec.name, "state": "unknown", "value": None, "known_since": None}
    age = timedelta(seconds=(at_ns - point.observed_ns) / NS_PER_S)
    origin = f"fetched from {spec.fetch['from']}" if spec.fetch else "a file kept by hand"
    observed = moment(point.observed_ns).isoformat()
    if age > spec.max_age:
        # An old reading counted as today's is a made-up reading. It is shown, so the operator
        # sees the file has gone stale, and it is never a state anyone weighs.
        return {
            "series": spec.name,
            "unit": spec.unit,
            "value": str(point.value),
            "observed_at": observed,
            "origin": origin,
            "age_h": round(age.total_seconds() / 3600, 1),
            "max_age": _span(spec.max_age),
            "state": "stale",
            "stale": True,
        }
    state = spec.threshold.state(point.value) if spec.threshold.declared else "undeclared"
    return {
        "series": spec.name,
        "unit": spec.unit,
        "value": str(point.value),
        "observed_at": observed,
        "known_since": moment(point.published_ns).isoformat(),
        "origin": origin,
        "age_h": round(age.total_seconds() / 3600, 1),
        "state": state,
        "basis": "operator-declared threshold"
        if spec.threshold.declared
        else "no threshold declared",
        # Seen live on 2026-10-04: a model read "bullish" on a yield series as yields rising, and
        # called it risk-off. The state is the operator's call for the traded instrument, and the
        # rule beside it shows which side of the series that call sits on.
        **(
            {
                "state_for": "the traded instrument, as the operator declared it",
                "threshold": spec.threshold.describe(),
                # How far past the line, in widths of the neutral band: +0.15 is barely bullish,
                # +1.0 is a full band beyond it. This is the figure conviction weighs.
                "strength": str(spec.threshold.strength(point.value)),
                "graded": spec.threshold.graded,
            }
            if spec.threshold.declared
            else {}
        ),
    }


def _span(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def zscore(series: Series, at_ns: int, *, window_points: int) -> Measurement:
    """The known value against the last `window_points` known values before it."""
    known = [p for p in series.points if p.published_ns < at_ns]
    if len(known) < 3:
        return Measurement(None, len(known), f"{window_points} points", "fewer than three points")
    tail = known[-window_points:]
    values = [float(p.value) for p in tail]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return Measurement(None, len(tail), f"{window_points} points", "no variance")
    return Measurement((values[-1] - mean) / math.sqrt(var), len(tail), f"{window_points} points")


def net_flow(series: Series, at_ns: int, *, window: timedelta) -> Measurement:
    """Sum of the known values whose observation falls inside the window ending at at_ns."""
    start = at_ns - int(window.total_seconds() * NS_PER_S)
    inside = [p for p in series.points if start <= p.observed_ns < at_ns and p.published_ns < at_ns]
    if not inside:
        return Measurement(None, 0, str(window), "no points known in the window")
    return Measurement(float(sum(p.value for p in inside)), len(inside), str(window))
