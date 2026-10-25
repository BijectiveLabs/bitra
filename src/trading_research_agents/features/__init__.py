"""Figures a decision reads, computed by code at a bar's close from what was known then.

A figure whose inputs are missing, stale or unseen is `Absent` with the reason.
"""

from __future__ import annotations

from trading_research_agents.features.catalog import (
    DEFAULT_FEATURES,
    MAX_WINDOW_BARS,
    compute_features,
)
from trading_research_agents.features.core import (
    Absent,
    Feature,
    FeatureSpec,
    FeatureValue,
    FeatureVector,
    Inputs,
    LookAheadError,
)

__all__ = [
    "DEFAULT_FEATURES",
    "MAX_WINDOW_BARS",
    "Absent",
    "Feature",
    "FeatureSpec",
    "FeatureValue",
    "FeatureVector",
    "Inputs",
    "LookAheadError",
    "compute_features",
]
