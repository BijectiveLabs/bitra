from __future__ import annotations

import math
import random

import pytest

from trading_research_agents.forecast.paths import PathError, PathSettings, read_paths


def walk(n: int, *, drift: float = 0.0, vol: float = 0.004, seed: int = 1) -> list[list[float]]:
    """Candles of a random walk: each opens at the last close and reaches a little past it."""
    rng = random.Random(seed)  # noqa: S311 - a test fixture, not a secret
    close = 100.0
    out = []
    for _ in range(n):
        move = rng.gauss(drift, vol)
        new = close * math.exp(move)
        reach = abs(rng.gauss(0, vol / 2))
        out.append([close, max(close, new) * (1 + reach), min(close, new) * (1 - reach), new])
        close = new
    return out


SETTINGS = PathSettings(paths=2000, seed=3)


def test_the_same_candles_and_seed_give_the_same_reading() -> None:
    bars = walk(400)
    first = read_paths(
        bars, side="long", take_bps=80, stop_bps=80, horizon_bars=24, settings=SETTINGS
    )
    again = read_paths(
        bars, side="long", take_bps=80, stop_bps=80, horizon_bars=24, settings=SETTINGS
    )
    assert first == again
    assert math.isclose(first.take_first + first.stop_first + first.neither, 1.0)


def test_a_walk_without_drift_reads_symmetric_levels_as_a_coin() -> None:
    bars = walk(500)
    long = read_paths(
        bars, side="long", take_bps=60, stop_bps=60, horizon_bars=48, settings=SETTINGS
    )
    short = read_paths(
        bars, side="short", take_bps=60, stop_bps=60, horizon_bars=48, settings=SETTINGS
    )
    assert abs(long.take_first - long.stop_first) < 0.12
    assert abs(long.take_first - short.stop_first) < 0.12


def test_a_market_that_keeps_rising_favours_the_long_and_not_the_short() -> None:
    bars = walk(500, drift=0.002)
    long = read_paths(
        bars, side="long", take_bps=60, stop_bps=60, horizon_bars=48, settings=SETTINGS
    )
    short = read_paths(
        bars, side="short", take_bps=60, stop_bps=60, horizon_bars=48, settings=SETTINGS
    )
    assert long.take_first > long.stop_first + 0.3
    assert short.stop_first > short.take_first + 0.3
    assert long.final_p50_bps > 0 > short.final_p50_bps


def test_the_paths_are_read_at_the_levels_named() -> None:
    bars = walk(500)
    near = read_paths(
        bars, side="long", take_bps=30, stop_bps=90, horizon_bars=48, settings=SETTINGS
    )
    far = read_paths(
        bars, side="long", take_bps=150, stop_bps=90, horizon_bars=48, settings=SETTINGS
    )
    assert near.take_first > far.take_first
    assert near.expected_bps(10) != far.expected_bps(10)


def test_a_candle_that_reaches_both_levels_counts_as_the_stop() -> None:
    bars = [[100.0, 103.0, 97.0, 100.0 + (0.01 if i % 2 else -0.01)] for i in range(200)]
    for side in ("long", "short"):
        read = read_paths(
            bars, side=side, take_bps=5, stop_bps=5, horizon_bars=5, settings=SETTINGS
        )
        assert read.take_first == 0.0 and read.stop_first == 1.0


def test_without_levels_every_path_runs_to_the_horizon() -> None:
    read = read_paths(
        walk(300), side="long", take_bps=None, stop_bps=None, horizon_bars=12, settings=SETTINGS
    )
    assert read.neither == 1.0 and read.final_p10_bps < read.final_p50_bps < read.final_p90_bps
    assert read.range_bps > 0


def test_a_reading_refuses_what_it_cannot_carry() -> None:
    with pytest.raises(PathError, match="at least 30"):
        read_paths(walk(20), side="long", take_bps=50, stop_bps=50, horizon_bars=5)
    flat = [[100.0, 100.0, 100.0, 100.0]] * 100
    with pytest.raises(PathError, match="did not move"):
        read_paths(flat, side="long", take_bps=50, stop_bps=50, horizon_bars=5)
    with pytest.raises(PathError, match="not long or short"):
        read_paths(walk(100), side="flat", take_bps=50, stop_bps=50, horizon_bars=5)
    with pytest.raises(PathError, match="positive distance"):
        read_paths(walk(100), side="long", take_bps=0, stop_bps=50, horizon_bars=5)


def test_the_adverse_reach_widens_with_the_hold_and_the_quantile() -> None:
    from trading_research_agents.forecast.paths import adverse_bps

    bars = walk(500)
    short_hold = adverse_bps(bars, side="long", horizon_bars=4, quantile=0.9, settings=SETTINGS)
    long_hold = adverse_bps(bars, side="long", horizon_bars=36, quantile=0.9, settings=SETTINGS)
    looser = adverse_bps(bars, side="long", horizon_bars=36, quantile=0.5, settings=SETTINGS)
    assert 0 < short_hold < long_hold and looser < long_hold
    # A random walk's reach grows roughly with the square root of the hold.
    assert 2.0 < long_hold / short_hold < 4.5
    with pytest.raises(PathError, match="between 0 and 1"):
        adverse_bps(bars, side="long", horizon_bars=4, quantile=1.0)
