"""Calibration: a model's score mapped onto the rate at which outcomes actually happened.

- `Platt`: a logistic curve on the score's logit. Two parameters, so it holds on few outcomes.
- `Isotonic`: the pool-adjacent-violators fit, a non-decreasing step curve read between its steps.
  It follows any monotone shape and needs more outcomes not to follow noise.

`choose_calibrator` fits each on one half of the outcomes and scores it on the other, both ways,
and keeps the lower Brier score, then fits the choice on all of them. A calibrated probability is
kept inside [0.001, 0.999]: certainty is never something a fit on a sample can show.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from trading_research_agents.forecast.logistic import Array, ForecastError, Logistic

EPSILON = 1e-3
PLATT_ROWS = 12


class Calibrator(Protocol):
    @property
    def name(self) -> str: ...

    def map(self, score: Array) -> Array: ...


def _logit(p: Array) -> Array:
    clipped = np.clip(p, EPSILON, 1 - EPSILON)
    out: Array = np.log(clipped / (1 - clipped))
    return out


def _bounded(p: Array) -> Array:
    out: Array = np.clip(p, EPSILON, 1 - EPSILON)
    return out


@dataclass(frozen=True)
class Platt:
    curve: Logistic
    name: str = "platt"

    @classmethod
    def fit(cls, score: Array, y: Array) -> Platt:
        # Two parameters: a dozen outcomes with both results in them can place a curve.
        return cls(Logistic.fit(_logit(score).reshape(-1, 1), y, l2=1e-3, min_rows=PLATT_ROWS))

    def map(self, score: Array) -> Array:
        return _bounded(self.curve.predict(_logit(score).reshape(-1, 1)))


@dataclass(frozen=True)
class Isotonic:
    knots: Array
    levels: Array
    name: str = "isotonic"

    @classmethod
    def fit(cls, score: Array, y: Array) -> Isotonic:
        if len(score) != len(y) or len(score) == 0:
            raise ForecastError("scores and outcomes do not line up")
        order = np.argsort(score, kind="stable")
        means: list[float] = []
        weights: list[float] = []
        centres: list[float] = []
        for s, outcome in zip(score[order], y[order], strict=True):
            means.append(float(outcome))
            weights.append(1.0)
            centres.append(float(s))
            while len(means) > 1 and means[-2] > means[-1]:
                total = weights[-2] + weights[-1]
                means[-2] = (means[-2] * weights[-2] + means[-1] * weights[-1]) / total
                centres[-2] = (centres[-2] * weights[-2] + centres[-1] * weights[-1]) / total
                weights[-2] = total
                del means[-1], weights[-1], centres[-1]
        return cls(knots=np.array(centres), levels=np.array(means))

    def map(self, score: Array) -> Array:
        return _bounded(np.interp(score, self.knots, self.levels))


def _brier(calibrator: Calibrator, score: Array, y: Array) -> float:
    return float(np.mean((calibrator.map(score) - y) ** 2))


def choose_calibrator(score: Array, y: Array) -> Calibrator:
    """The calibrator with the lower out-of-sample Brier score, fitted on every outcome.

    A candidate that cannot be fitted on a half is out of the running; with neither left there is
    no calibration, and so no forecast.
    """
    halves = (np.arange(len(score)) % 2 == 0, np.arange(len(score)) % 2 == 1)
    fits: dict[str, Callable[[Array, Array], Calibrator]] = {
        "platt": Platt.fit,
        "isotonic": Isotonic.fit,
    }
    errors: dict[str, float] = {}
    for name, fit in fits.items():
        try:
            errors[name] = sum(
                _brier(fit(score[a], y[a]), score[b], y[b]) for a, b in (halves, halves[::-1])
            )
        except ForecastError:
            continue
    if not errors:
        raise ForecastError("no calibrator could be fitted on these outcomes")
    # A tie goes to the curve with two parameters, which is listed first.
    chosen = min(errors, key=lambda name: errors[name])
    return fits[chosen](score, y)


__all__ = ["Calibrator", "Isotonic", "Platt", "choose_calibrator"]
