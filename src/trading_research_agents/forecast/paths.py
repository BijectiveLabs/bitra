"""Paths: the instrument's own candles drawn forward, read at the levels a trade names.

The reference is Kronos (arXiv 2508.02739). Three of its choices are kept because they held up: the
unit is the whole candle (open, high, low, close against the previous close), not the close alone;
each context window is normalised on its own volatility and clipped at five deviations; and the
next candle is drawn conditionally on a coarse and a fine token of the one before it, many times, so
the answer is a distribution of paths. What its audit found wanting is not carried over: there is no
transformer to train or ship (its crypto direction skill was a rank correlation near 0.03, its
volatility skill real), every path is kept instead of averaged, every draw is seeded, and the
question asked of the paths is not the next price but how likely this take is reached before this
stop within this horizon. A candle that reaches both levels counts as the stop, as the arena's own
settlement counts it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

BPS = 10_000.0
SIDES = ("long", "short")
OPEN, HIGH, LOW, CLOSE = range(4)


class PathError(ValueError):
    """The candles given cannot carry a reading: too few, or a market that did not move."""


@dataclass(frozen=True)
class PathSettings:
    """How many paths, from how much history, and how the candles are tokenised.

    `context` is the window the candle pools are built from and normalised on; `recent` the bars
    whose volatility scales every draw, so a quiet hour is not read with a loud week's swings.
    `prior` is how many sightings a token needs before its own pool counts as much as the pools
    above it.
    """

    paths: int = 1000
    context: int = 512
    recent: int = 64
    prior: int = 30
    coarse: int = 5
    fine: int = 3
    clip: float = 5.0
    seed: int = 0
    min_bars: int = 30

    def __post_init__(self) -> None:
        if self.paths < 100:
            raise PathError("a reading needs at least 100 paths")
        if self.coarse < 2 or self.fine < 1 or self.prior < 1:
            raise PathError("at least two coarse tokens, one fine token and a prior of one")
        if self.min_bars < 10 or self.recent < 8:
            raise PathError("at least 10 bars of history and 8 recent bars")


DEFAULT = PathSettings()


@dataclass(frozen=True)
class PathReading:
    """What the paths say about one side at its levels. Fractions sum to one; moves are in basis
    points of the entry, signed for the side (positive is in the trade's favour)."""

    side: str
    paths: int
    horizon_bars: int
    context_bars: int
    take_bps: float | None
    stop_bps: float | None
    take_first: float
    stop_first: float
    neither: float
    neither_mean_bps: float
    final_p10_bps: float
    final_p50_bps: float
    final_p90_bps: float
    range_bps: float
    seed: int

    def expected_bps(self, cost_bps: float) -> float:
        """The trade's value over the paths, net of the round trip."""
        take = self.take_bps or 0.0
        stop = self.stop_bps or 0.0
        gross = (
            self.take_first * take - self.stop_first * stop + self.neither * self.neither_mean_bps
        )
        return gross - cost_bps

    def record(self) -> dict[str, Any]:
        return {name: _rounded(value) for name, value in self.__dict__.items()}


def _rounded(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


@dataclass(frozen=True)
class _Window:
    candles: np.ndarray
    tokens: np.ndarray
    coarse: np.ndarray
    scale: float


def _shapes(ohlc: Sequence[Sequence[float]]) -> np.ndarray:
    """Each candle as the log of its open, high, low and close over the previous close."""
    prices = np.asarray(ohlc, dtype=float)
    if prices.ndim != 2 or prices.shape[1] != 4 or not np.all(np.isfinite(prices)):
        raise PathError("candles are open, high, low and close, all finite")
    if np.any(prices <= 0):
        raise PathError("a candle price is not positive")
    previous = prices[:-1, CLOSE][:, None]
    shapes: np.ndarray = np.log(prices[1:] / previous)
    return shapes


def _bins(values: np.ndarray, count: int) -> np.ndarray:
    edges = np.quantile(values, np.linspace(0, 1, count + 1)[1:-1])
    bins: np.ndarray = np.searchsorted(edges, values, side="right")
    return bins


def _unearned_drift(closes: np.ndarray) -> float:
    """The part of the window's mean move its own noise does not support.

    A window's average move is extrapolated over every path, so a drift that is only the luck of
    the sample would tilt every reading. The mean is kept in proportion to its evidence, by
    1 - 1/t^2 floored at zero: a drift ten standard errors out is kept almost whole, one of two
    standard errors is cut to three quarters, and one inside a standard error is dropped.
    """
    mean = float(np.mean(closes))
    error = float(np.std(closes)) / math.sqrt(len(closes))
    if error == 0:
        return 0.0
    kept = mean * max(0.0, 1 - (error / mean) ** 2) if mean else 0.0
    return mean - kept


def _window(ohlc: Sequence[Sequence[float]], settings: PathSettings) -> _Window:
    if len(ohlc) < settings.min_bars:
        raise PathError(f"{len(ohlc)} closed bars; a reading needs at least {settings.min_bars}")
    shapes = _shapes(ohlc[-(settings.context + 1) :])
    spread = float(np.std(shapes[:, CLOSE]))
    recent = float(np.std(shapes[-settings.recent :, CLOSE]))
    if spread <= 0 or recent <= 0:
        raise PathError("the closes did not move over the window; there is nothing to draw from")
    normal = np.clip(shapes / spread, -settings.clip, settings.clip)
    normal = normal - _unearned_drift(normal[:, CLOSE])
    coarse = _bins(normal[:, CLOSE], settings.coarse)
    fine = _bins(normal[:, HIGH] - normal[:, LOW], settings.fine)
    return _Window(normal, coarse * settings.fine + fine, coarse, recent)


@dataclass(frozen=True)
class _Pools:
    """The candles that followed each token, laid end to end so a step draws for every path at
    once, and how far each token's own pool is trusted over the coarser ones.

    A token seen n times is drawn from its own pool with weight n / (n + prior), the rest from its
    coarse token's pool by the same rule, and what is left from the whole window: a thin token
    leans on the market's general behaviour instead of on a handful of candles.
    """

    own: np.ndarray
    own_start: np.ndarray
    own_count: np.ndarray
    own_weight: np.ndarray
    coarse: np.ndarray
    coarse_start: np.ndarray
    coarse_count: np.ndarray
    coarse_weight: np.ndarray
    coarse_of: np.ndarray
    every: np.ndarray


def _laid_out(groups: np.ndarray, follows: np.ndarray, size: int) -> tuple[np.ndarray, ...]:
    order = np.argsort(groups, kind="stable")
    count = np.bincount(groups, minlength=size)
    start = np.concatenate(([0], np.cumsum(count)[:-1]))
    return follows[order], start, count


def _pools(window: _Window, settings: PathSettings) -> _Pools:
    follows = np.arange(1, len(window.tokens))
    tokens = settings.coarse * settings.fine
    own, own_start, own_count = _laid_out(window.tokens[:-1], follows, tokens)
    coarse, coarse_start, coarse_count = _laid_out(window.coarse[:-1], follows, settings.coarse)
    return _Pools(
        own=own,
        own_start=own_start,
        own_count=own_count,
        own_weight=own_count / (own_count + settings.prior),
        coarse=coarse,
        coarse_start=coarse_start,
        coarse_count=coarse_count,
        coarse_weight=coarse_count / (coarse_count + settings.prior),
        coarse_of=np.arange(tokens) // settings.fine,
        every=follows,
    )


def _draw(rng: np.random.Generator, pools: _Pools, state: np.ndarray) -> np.ndarray:
    """One following candle per path, from its token's pool or the pools above it."""
    level = rng.random(len(state))
    place = rng.random(len(state))
    own_weight = pools.own_weight[state]
    coarse = pools.coarse_of[state]
    to_own = level < own_weight
    to_coarse = ~to_own & (level < own_weight + (1 - own_weight) * pools.coarse_weight[coarse])
    drawn: np.ndarray = pools.every[(place * len(pools.every)).astype(int)]
    if to_own.any():
        s = state[to_own]
        at = pools.own_start[s] + (place[to_own] * pools.own_count[s]).astype(int)
        drawn[to_own] = pools.own[at]
    if to_coarse.any():
        c = coarse[to_coarse]
        at = pools.coarse_start[c] + (place[to_coarse] * pools.coarse_count[c]).astype(int)
        drawn[to_coarse] = pools.coarse[at]
    return drawn


def _barriers(side: str, take_bps: float | None, stop_bps: float | None) -> tuple[float, float]:
    """The upper and the lower barrier as log moves of the price; an absent one never touches."""
    rise = {"long": take_bps, "short": stop_bps}[side]
    fall = {"long": stop_bps, "short": take_bps}[side]
    upper = math.inf if rise is None else math.log1p(rise / BPS)
    lower = -math.inf if fall is None else math.log1p(-fall / BPS)
    return upper, lower


def read_paths(
    ohlc: Sequence[Sequence[float]],
    *,
    side: str,
    take_bps: float | None,
    stop_bps: float | None,
    horizon_bars: int,
    settings: PathSettings = DEFAULT,
) -> PathReading:
    """Draw the paths from the last closed candle and read them at this side's take and stop."""
    if side not in SIDES:
        raise PathError(f"side {side!r} is not long or short")
    if horizon_bars < 1:
        raise PathError("a reading looks at least one bar ahead")
    for name, level in (("take", take_bps), ("stop", stop_bps)):
        if level is not None and not 0 < level < BPS:
            raise PathError(f"the {name} is a positive distance under 10000 bps, or None")
    window = _window(ohlc, settings)
    upper, lower = _barriers(side, take_bps, stop_bps)
    walk = _walk(window, settings, horizon_bars, (upper, lower), stop_up=side == "short")
    return _reading(walk, side, take_bps, stop_bps, horizon_bars, len(window.tokens), settings)


@dataclass
class _Walk:
    level: np.ndarray
    high: np.ndarray
    low: np.ndarray
    # 1 the upper barrier first, -1 the lower first, 0 neither inside the horizon.
    hit: np.ndarray


def _walk(
    window: _Window,
    settings: PathSettings,
    horizon: int,
    barriers: tuple[float, float],
    *,
    stop_up: bool,
) -> _Walk:
    rng = np.random.default_rng(settings.seed)
    n = settings.paths
    pools = _pools(window, settings)
    state = np.full(n, window.tokens[-1])
    walk = _Walk(np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n, dtype=int))
    for _ in range(horizon):
        drawn = _draw(rng, pools, state)
        _step(walk, window.candles[drawn] * window.scale, barriers, stop_up=stop_up)
        state = window.tokens[drawn]
    return walk


def _step(walk: _Walk, moves: np.ndarray, barriers: tuple[float, float], *, stop_up: bool) -> None:
    """One candle on every path; a path already settled keeps its outcome."""
    upper, lower = barriers
    high = walk.level + moves[:, HIGH]
    low = walk.level + moves[:, LOW]
    open_now = walk.hit == 0
    up = open_now & (high >= upper)
    down = open_now & (low <= lower)
    # Inside one candle the order of its high and low is not known; the stop is assumed first.
    both = up & down
    walk.hit[up & ~both] = 1
    walk.hit[down & ~both] = -1
    walk.hit[both] = 1 if stop_up else -1
    walk.high = np.maximum(walk.high, high)
    walk.low = np.minimum(walk.low, low)
    walk.level = walk.level + moves[:, CLOSE]


def _reading(
    walk: _Walk,
    side: str,
    take_bps: float | None,
    stop_bps: float | None,
    horizon: int,
    context: int,
    settings: PathSettings,
) -> PathReading:
    sign = 1.0 if side == "long" else -1.0
    final = sign * np.expm1(walk.level) * BPS
    favour = 1 if side == "long" else -1
    took = walk.hit == favour
    stopped = walk.hit == -favour
    neither = walk.hit == 0
    quantiles = np.quantile(final, [0.1, 0.5, 0.9])
    return PathReading(
        side=side,
        paths=settings.paths,
        horizon_bars=horizon,
        context_bars=context,
        take_bps=take_bps,
        stop_bps=stop_bps,
        take_first=float(took.mean()),
        stop_first=float(stopped.mean()),
        neither=float(neither.mean()),
        neither_mean_bps=float(final[neither].mean()) if neither.any() else 0.0,
        final_p10_bps=float(quantiles[0]),
        final_p50_bps=float(quantiles[1]),
        final_p90_bps=float(quantiles[2]),
        range_bps=float(np.mean(np.expm1(walk.high) - np.expm1(walk.low)) * BPS),
        seed=settings.seed,
    )


def adverse_bps(
    ohlc: Sequence[Sequence[float]],
    *,
    side: str,
    horizon_bars: int,
    quantile: float,
    settings: PathSettings = DEFAULT,
) -> float:
    """How far against the side the paths reach inside the hold, at this quantile, in bps.

    A stop placed here is reached by noise on `1 - quantile` of the paths: the range the paths
    draw, wicks included, rather than an average true range scaled by the square root of time.
    """
    if side not in SIDES:
        raise PathError(f"side {side!r} is not long or short")
    if not 0 < quantile < 1:
        raise PathError("the quantile is between 0 and 1")
    if horizon_bars < 1:
        raise PathError("a reading looks at least one bar ahead")
    window = _window(ohlc, settings)
    walk = _walk(window, settings, horizon_bars, (math.inf, -math.inf), stop_up=False)
    against = -np.expm1(walk.low) if side == "long" else np.expm1(walk.high)
    return float(np.quantile(np.maximum(against, 0.0), quantile) * BPS)
