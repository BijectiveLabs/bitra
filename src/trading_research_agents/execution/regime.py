"""The regime price structure is in, measured by code, and whether a named setup belongs in it."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

REGIMES = ("trending up", "trending down", "ranging", "mixed")
TREND_SETUPS = ("trend_continuation", "pullback_in_trend")
RANGE_SETUPS = ("range_reversion",)
REGIME_KEYS = ("trend_above", "range_below", "setups_must_fit", "fade_below_variance_ratio")


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
    # A window can net out to nothing and still have every move followed by more of the same. A
    # fade is paid only where moves were given back, so it needs the variance ratio below this.
    # Zero turns the check off.
    fade_below_variance_ratio: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        if not Decimal(0) < self.range_below < self.trend_above:
            raise RegimeError("[regime] needs 0 < range_below < trend_above")
        if self.fade_below_variance_ratio < 0:
            raise RegimeError("[regime].fade_below_variance_ratio is zero (off) or above")

    def record(self) -> dict[str, Any]:
        return {
            "trend_above": str(self.trend_above),
            "range_below": str(self.range_below),
            "setups_must_fit": self.setups_must_fit,
            "fade_below_variance_ratio": str(self.fade_below_variance_ratio),
        }


def label(strength: float | None, move_bps: float | None, rule: RegimeRule) -> str:
    if strength is None or move_bps is None:
        return "mixed"
    if strength >= float(rule.trend_above):
        return "trending up" if move_bps > 0 else "trending down"
    if strength <= float(rule.range_below):
        return "ranging"
    return "mixed"


def misfit(
    setup: str,
    target: str,
    regime: str,
    *,
    zscore: float | None = None,
    window_move_bps: float | None = None,
    position_in_range: float | None = None,
    trend_strength: float | None = None,
    variance_ratio: float | None = None,
    fade_below: float = 0.0,
) -> str:
    """Why this setup does not fit what code measured, or empty when it does.

    Each setup is a claim about the market with a direction built into it, and the figures that
    make the claim are in the brief. A setup whose direction contradicts its own figures is a
    named shape on a trade that came from somewhere else, so it is refused by name. A figure
    that was not measured does not refuse anything.
    """
    if setup in TREND_SETUPS and regime not in ("trending up", "trending down"):
        return f"{setup} in a market measured as {regime}; a trend setup needs a measured trend"
    if regime in ("trending up", "trending down"):
        way = "long" if regime == "trending up" else "short"
        if setup in RANGE_SETUPS:
            return f"{setup} in a market measured as {regime}; fading a trend is not a range"
        if setup in TREND_SETUPS and target != way:
            return f"{setup} {target} in a market measured as {regime}; that is against the trend"
        if setup == "momentum_exhaustion" and target == way:
            # Exhaustion trades against the move; with it, it is a continuation under another name.
            return f"momentum_exhaustion {target} in a market measured as {regime}; that is with it"
    if setup in RANGE_SETUPS and regime == "mixed" and trend_strength is not None:
        # The same standard a trend setup is held to: a fade needs a measured range, and a market
        # between a range and a trend has been running often enough to take one out.
        return (
            f"{setup} in a market measured as mixed (trend strength {trend_strength:.2f}); a fade "
            "needs a measured range"
        )
    if (
        setup in RANGE_SETUPS
        and fade_below > 0
        and variance_ratio is not None
        and variance_ratio >= fade_below
    ):
        return (
            f"{setup} with a variance ratio of {variance_ratio:.2f} on the window: its moves "
            f"have carried on rather than turned back, and a fade needs it below {fade_below:g}"
        )
    # A range fade sells what is stretched up and buys what is stretched down.
    if setup in RANGE_SETUPS and zscore is not None and not _points(target, -zscore):
        return (
            f"range_reversion {target} with the close at z-score {zscore:+.3f}; a fade goes "
            "against the stretch"
        )
    if (
        setup == "momentum_exhaustion"
        and window_move_bps is not None
        and not _points(target, -window_move_bps)
    ):
        return (
            f"momentum_exhaustion {target} after a window move of {window_move_bps:+.2f} bps; "
            "exhaustion trades against the move"
        )
    # A breakout trades the side of the range the close has left from.
    if (
        setup == "range_breakout"
        and position_in_range is not None
        and not _points(target, position_in_range - 0.5)
    ):
        return (
            f"range_breakout {target} with the close at {position_in_range:.3f} of the range; "
            "a breakout trades the side the close left from"
        )
    return ""


def _points(target: str, lean: float) -> bool:
    """Whether a long leans up or a short leans down; a lean of zero points nowhere."""
    return (target == "long" and lean > 0) or (target == "short" and lean < 0)
