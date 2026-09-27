"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.llm.form import (
    _enum,
    _posint,
    _text,
    _unit,
    parse_object,
)

ROLE = "technical_analyst"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": ["long", "short", "flat"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "horizon_bars": {"type": "integer", "minimum": 1},
    },
    "required": ["target", "reason", "confidence", "horizon_bars"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TechnicalAnalyst:
    target: str
    reason: str
    confidence: float
    horizon_bars: int


def parse(text: str) -> TechnicalAnalyst:
    data = parse_object(text, SCHEMA)
    target = _enum(data.get("target"), "target", ("long", "short", "flat"))
    reason = _text(data.get("reason"), "reason")
    confidence = _unit(data.get("confidence"), "confidence")
    horizon_bars = _posint(data.get("horizon_bars"), "horizon_bars")
    return TechnicalAnalyst(
        target=target, reason=reason, confidence=confidence, horizon_bars=horizon_bars
    )
