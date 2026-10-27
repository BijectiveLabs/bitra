"""The schema the flow analyst fills and the object the cycle gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.llm.form import _enum, _text, _unit, parse_object

ROLE = "flow_analyst"
TARGETS = ("long", "short", "flat")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": list(TARGETS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "figures_used": FIGURES_SCHEMA,
    },
    "required": ["target", "confidence", "reason", "figures_used"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class FlowAnalyst:
    target: str
    confidence: float
    reason: str
    figures_used: tuple[Figure, ...]


def parse(text: str) -> FlowAnalyst:
    data = parse_object(text, SCHEMA)
    return FlowAnalyst(
        target=_enum(data.get("target"), "target", TARGETS),
        confidence=_unit(data.get("confidence"), "confidence"),
        reason=_text(data.get("reason"), "reason"),
        figures_used=figures_used(data.get("figures_used")),
    )
