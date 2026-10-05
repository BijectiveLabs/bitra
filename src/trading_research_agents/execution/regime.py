"""The regime price structure is in, measured by code, and whether a named setup belongs in it."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

REGIMES = ("trending up", "trending down", "ranging", "mixed")
TREND_SETUPS = ("trend_continuation", "pullback_in_trend")
RANGE_SETUPS = ("range_reversion",)
REGIME_KEYS = ("trend_above", "range_below", "setups_must_fit")


class RegimeError(ValueError):
    """The regime rule as written cannot be applied."""


@dataclass(frozen=True)
class RegimeRule:
    """[regime]: where trending and ranging begin on trend strength, and whether setups must fit."""

    # Trend strength is the net move over the path, scaled so that a random walk lands near 1 on
    # any window length. That keeps one threshold meaningful on 20 bars and on 240.
    trend_above: Decimal = Decimal("2.0")
    range_below: Decimal = Decimal("1.0")
    setups_must_fit: bool = True

    def __post_init__(self) -> None:
        if not Decimal(0) < self.range_below < self.trend_above:
            raise RegimeError("[regime] needs 0 < range_below < trend_above")

    def record(self) -> dict[str, Any]:
        return {
            "trend_above": str(self.trend_above),
            "range_below": str(self.range_below),
            "setups_must_fit": self.setups_must_fit,
        }


def label(strength: float | None, net_bps: float | None, rule: RegimeRule) -> str:
    if strength is None or net_bps is None:
        return "mixed"
    if strength >= float(rule.trend_above):
        return "trending up" if net_bps > 0 else "trending down"
    if strength <= float(rule.range_below):
        return "ranging"
    return "mixed"


def misfit(setup: str, target: str, regime: str) -> str:
    """Why this setup does not belong in this regime, or empty when it does."""
    if regime == "ranging" and setup in TREND_SETUPS:
        return f"{setup} in a market measured as ranging; a trend setup needs a trend"
    if regime in ("trending up", "trending down"):
        way = "long" if regime == "trending up" else "short"
        if setup in RANGE_SETUPS:
            return f"{setup} in a market measured as {regime}; fading a trend is not a range"
        if setup in TREND_SETUPS and target != way:
            return f"{setup} {target} in a market measured as {regime}; that is against the trend"
        if setup == "momentum_exhaustion" and target == way:
            # Exhaustion trades against the move; with it, it is a continuation under another name.
            return f"momentum_exhaustion {target} in a market measured as {regime}; that is with it"
    return ""
