"""The expected value of a side, and the one decision: the better side, if it clears the margin.

With `p` the calibrated probability that the take is reached before the stop, an idea is worth

    p x take - (1 - p) x stop - round trip        in basis points of the entry

An idea that runs out of time is counted as a stop by the model's label, which makes the value
conservative: it can understate a side, never overstate it.
"""

from __future__ import annotations


def expected_bps(p: float, *, take_bps: float, stop_bps: float, cost_bps: float) -> float:
    return p * take_bps - (1 - p) * stop_bps - cost_bps


def best_side(
    long_bps: float | None, short_bps: float | None, *, margin_bps: float
) -> tuple[str, float] | None:
    """`long` or `short` with its value, when the better one is worth more than the margin."""
    known = [(n, v) for n, v in (("long", long_bps), ("short", short_bps)) if v is not None]
    if not known:
        return None
    name, value = max(known, key=lambda side: side[1])
    return (name, value) if value > margin_bps else None


__all__ = ["best_side", "expected_bps"]
