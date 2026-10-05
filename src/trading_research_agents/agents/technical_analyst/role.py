"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.llm.form import (
    _enum,
    _posint,
    _text,
    _unit,
    parse_object,
)

ROLE = "technical_analyst"
TARGETS = ("long", "short", "flat")
# The named shapes this role is allowed to say it recognised. A closed list, because "where did this
# position come from" has to be answerable by reading the record rather than by reading a paragraph,
# and because a vocabulary is the only part of a sentence a later step can count.
SETUPS = (
    "trend_continuation",
    "pullback_in_trend",
    "range_reversion",
    "range_breakout",
    "momentum_exhaustion",
    "volatility_expansion",
    "no_setup",
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": list(TARGETS)},
        "setup": {"type": "string", "enum": list(SETUPS)},
        "reason": {"type": "string"},
        "figures_used": FIGURES_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "horizon_bars": {"type": "integer", "minimum": 1},
    },
    "required": ["target", "setup", "reason", "figures_used", "confidence", "horizon_bars"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TechnicalAnalyst:
    target: str
    setup: str
    reason: str
    figures_used: tuple[Figure, ...]
    confidence: float
    horizon_bars: int


def parse(text: str) -> TechnicalAnalyst:
    data = parse_object(text, SCHEMA)
    target = _enum(data.get("target"), "target", TARGETS)
    setup = _enum(data.get("setup"), "setup", SETUPS)
    reason = _text(data.get("reason"), "reason")
    cited = figures_used(data.get("figures_used"))
    confidence = _unit(data.get("confidence"), "confidence")
    horizon_bars = _posint(data.get("horizon_bars"), "horizon_bars")
    return TechnicalAnalyst(
        target=target,
        setup=setup,
        reason=reason,
        figures_used=cited,
        confidence=confidence,
        horizon_bars=horizon_bars,
    )
