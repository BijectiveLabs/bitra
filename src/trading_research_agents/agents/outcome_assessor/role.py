"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.llm.form import FormError, _decimal, _text, _unit, parse_object

ROLE = "outcome_assessor"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The chance this outcome wins, in [0, 1], read from the event rather than from its price.
        "probability": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "figures_used": FIGURES_SCHEMA,
    },
    "required": ["probability", "confidence", "reason", "figures_used"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class OutcomeAssessor:
    probability: Decimal
    confidence: float
    reason: str
    figures_used: tuple[Figure, ...]


def parse(text: str) -> OutcomeAssessor:
    data = parse_object(text, SCHEMA)
    probability = _decimal(data.get("probability"), "probability")
    if not Decimal(0) <= probability <= Decimal(1):
        raise FormError("probability is a number in [0, 1]")
    return OutcomeAssessor(
        probability=probability,
        confidence=_unit(data.get("confidence"), "confidence"),
        reason=_text(data.get("reason"), "reason"),
        figures_used=figures_used(data.get("figures_used")),
    )
