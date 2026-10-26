"""A regularised logistic model, fitted by Newton's method so the same rows give the same weights.

Columns are standardised on the rows the model is fitted on, and a column that never moved is
left at zero rather than divided by zero. The penalty is an L2 on every weight but the intercept,
which keeps a model fitted on a few hundred outcomes from leaning on noise.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]
MIN_ROWS = 50
L2 = 1.0
MAX_ITERATIONS = 100
TOLERANCE = 1e-9


class ForecastError(ValueError):
    """A forecast that cannot be fitted or applied; the caller has no forecast, not a guess."""


@dataclass(frozen=True)
class Logistic:
    # The intercept first, then one weight per standardised column.
    weights: Array
    means: Array
    scales: Array

    @classmethod
    def fit(cls, x: Array, y: Array, *, l2: float = L2) -> Logistic:
        rows, _ = _checked(x, y)
        means = x.mean(axis=0)
        spread = x.std(axis=0)
        scales = np.where(spread > 0, spread, 1.0)
        design = np.column_stack([np.ones(rows), (x - means) / scales])
        penalty = np.full(design.shape[1], l2)
        penalty[0] = 0.0
        weights = np.zeros(design.shape[1])
        for _ in range(MAX_ITERATIONS):
            p = _sigmoid(design @ weights)
            gradient = design.T @ (p - y) + penalty * weights
            hessian = (design.T * (p * (1 - p))) @ design + np.diag(penalty)
            step = np.linalg.solve(hessian + 1e-12 * np.eye(len(weights)), gradient)
            weights = weights - step
            if float(np.max(np.abs(step))) < TOLERANCE:
                break
        return cls(weights=weights, means=means, scales=scales)

    def predict(self, x: Array) -> Array:
        if x.ndim != 2 or x.shape[1] != len(self.means):
            raise ForecastError(f"a forecast reads {len(self.means)} columns, given {x.shape}")
        design = np.column_stack([np.ones(len(x)), (x - self.means) / self.scales])
        return _sigmoid(design @ self.weights)


def _checked(x: Array, y: Array) -> tuple[int, int]:
    if x.ndim != 2 or len(x) != len(y):
        raise ForecastError(f"rows and outcomes do not line up: {x.shape} against {y.shape}")
    if len(x) < MIN_ROWS:
        raise ForecastError(f"{len(x)} rows; a fit needs at least {MIN_ROWS}")
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        raise ForecastError("a value that is not finite is in the rows")
    if len(np.unique(y)) < 2:
        raise ForecastError("the outcomes need both outcomes, a take and a miss, to fit on")
    return int(x.shape[0]), int(x.shape[1])


def _sigmoid(z: Array) -> Array:
    out: Array = 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))
    return out


__all__ = ["Array", "ForecastError", "Logistic"]
