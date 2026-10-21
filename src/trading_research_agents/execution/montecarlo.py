"""What a risk per trade does to the account over the trades ahead, by resampling the desk's own.

Kelly answers how much growth a fraction buys on average. An operator also has to live through the
path, and the question there is how deep the drawdowns run. This draws the next `trades` results,
with replacement, from the desk's finished trades in planned losses, compounds them at a given
risk per trade, and does that `paths` times. Each path is one equity curve; across them the share
that falls as far as the room left under `[controls].max_drawdown_bps` is the chance of reaching
that limit inside the horizon.

Deterministic by construction. The draws come from a generator seeded with a declared number, the
same draws are reused for every risk level tried, and the percentiles are nearest-rank, so the same
record, settings and seed give the same figures on any machine. The risk that is applied is the
largest one, found by bisection below the Kelly ceiling, whose chance of reaching the limit is
within the declared tolerance.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from typing import Any

BPS = 10_000.0
# Bisection to a hundredth of a basis point of risk: finer than any venue's size step resolves.
RESOLUTION_BPS = 0.01


class SimulationError(ValueError):
    """The simulation cannot run on these inputs."""


@dataclass(frozen=True)
class Simulation:
    risk_bps: float
    paths: int
    trades: int
    seed: int
    room_bps: float | None
    reach_share: float | None
    drawdown_median_bps: float
    drawdown_p95_bps: float
    end_median: float
    end_p05: float

    def record(self) -> dict[str, Any]:
        return {
            "risk_bps": f"{self.risk_bps:.2f}",
            "paths": self.paths,
            "trades": self.trades,
            "seed": self.seed,
            "room_bps": None if self.room_bps is None else f"{self.room_bps:.2f}",
            "reach_share": None if self.reach_share is None else f"{self.reach_share:.4f}",
            "drawdown_median_bps": f"{self.drawdown_median_bps:.2f}",
            "drawdown_p95_bps": f"{self.drawdown_p95_bps:.2f}",
            "equity_median": f"{self.end_median:.4f}",
            "equity_p05": f"{self.end_p05:.4f}",
        }


def nearest_rank(ordered: Sequence[float], percent: int) -> float:
    """The nearest-rank percentile of an already sorted sequence, in integer arithmetic so that the
    95th of 2000 is the 1900th value and not the one after it."""
    if not ordered:
        raise SimulationError("no values to rank")
    if not 0 < percent <= 100:
        raise SimulationError("a percentile is above 0 and at most 100")
    rank = -(-percent * len(ordered) // 100)
    return ordered[rank - 1]


@lru_cache(maxsize=2)
def _draws(size: int, paths: int, trades: int, seed: int) -> tuple[tuple[int, ...], ...]:
    """The resampled positions, one tuple per path. Integer draws from the generator's own floats,
    so nothing depends on how a Python version implements `choices`."""
    # A seeded generator is the point: the same seed has to give the same draws. Nothing secret.
    rng = random.Random(seed)  # noqa: S311
    return tuple(tuple(int(rng.random() * size) for _ in range(trades)) for _ in range(paths))


def simulate(
    results: Sequence[float],
    risk_bps: float,
    *,
    paths: int,
    trades: int,
    seed: int,
    room_bps: float | None = None,
) -> Simulation:
    """Compound `trades` resampled results at `risk_bps` per trade, `paths` times."""
    if not results:
        raise SimulationError("there is no finished trade to resample")
    if paths <= 0 or trades <= 0:
        raise SimulationError("paths and trades are positive")
    if risk_bps < 0:
        raise SimulationError("risk per trade is not negative")
    risk = risk_bps / BPS
    values = tuple(results)
    drawdowns: list[float] = []
    ends: list[float] = []
    reached = 0
    limit = None if room_bps is None else room_bps / BPS
    for path in _draws(len(values), paths, trades, seed):
        equity, peak, worst = 1.0, 1.0, 0.0
        for index in path:
            equity *= max(1.0 + risk * values[index], 0.0)
            if equity > peak:
                peak = equity
            fallen = 1.0 - equity / peak
            if fallen > worst:
                worst = fallen
        drawdowns.append(worst)
        ends.append(equity)
        if limit is not None and worst >= limit:
            reached += 1
    drawdowns.sort()
    ends.sort()
    return Simulation(
        risk_bps=risk_bps,
        paths=paths,
        trades=trades,
        seed=seed,
        room_bps=room_bps,
        reach_share=None if limit is None else reached / paths,
        drawdown_median_bps=nearest_rank(drawdowns, 50) * BPS,
        drawdown_p95_bps=nearest_rank(drawdowns, 95) * BPS,
        end_median=nearest_rank(ends, 50),
        end_p05=nearest_rank(ends, 5),
    )


def largest_within(
    results: Sequence[Decimal],
    ceiling_bps: Decimal,
    *,
    room_bps: Decimal,
    tolerance: Decimal,
    paths: int,
    trades: int,
    seed: int,
) -> tuple[Decimal, Simulation]:
    """The largest risk per trade at or below the ceiling whose chance of using up the room left
    under the drawdown limit is within the tolerance, and the simulation at that risk."""
    if ceiling_bps <= 0:
        raise SimulationError("the ceiling is positive")
    if not Decimal(0) < tolerance < Decimal(1):
        raise SimulationError("the tolerance is above 0 and below 1")
    return _largest(tuple(results), ceiling_bps, room_bps, tolerance, paths, trades, seed)


# A desk asks the same question every cycle until its record or its room changes; the answer is a
# pure function of the arguments, so it is kept rather than simulated again.
@lru_cache(maxsize=64)
def _largest(
    results: tuple[Decimal, ...],
    ceiling_bps: Decimal,
    room_bps: Decimal,
    tolerance: Decimal,
    paths: int,
    trades: int,
    seed: int,
) -> tuple[Decimal, Simulation]:
    values = tuple(float(r) for r in results)
    room, share = float(room_bps), float(tolerance)

    def run(risk: float) -> Simulation:
        return simulate(values, risk, paths=paths, trades=trades, seed=seed, room_bps=room)

    if room <= 0:
        return Decimal(0), run(0.0)
    top = run(float(ceiling_bps))
    if top.reach_share is not None and top.reach_share <= share:
        return ceiling_bps, top
    low, high = 0.0, float(ceiling_bps)
    while high - low > RESOLUTION_BPS:
        middle = (low + high) / 2
        tried = run(middle)
        if tried.reach_share is not None and tried.reach_share <= share:
            low = middle
        else:
            high = middle
    # Down to the hundredth, never up past the risk that was found within the tolerance, and the
    # figures reported are the ones simulated at exactly the risk that is applied.
    applied = Decimal(repr(low)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return applied, run(float(applied))
