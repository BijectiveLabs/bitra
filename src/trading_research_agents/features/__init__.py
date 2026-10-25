"""Figures a decision reads, computed by code at a bar's close from what was known then.

A figure whose inputs are missing, stale or unseen is `Absent` with the reason.
"""

from __future__ import annotations

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
    "Absent",
    "Feature",
    "FeatureSpec",
    "FeatureValue",
    "FeatureVector",
    "Inputs",
    "LookAheadError",
]
