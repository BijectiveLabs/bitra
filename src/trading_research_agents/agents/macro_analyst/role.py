"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.llm.form import (
    _enum,
    _list,
    _text,
    parse_object,
)

ROLE = "macro_analyst"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "regime": {"type": "string", "enum": ["risk-on", "risk-off", "neutral"]},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "when": {"type": "string"},
                    "what": {"type": "string"},
                    "expected_impact": {"type": "string"},
                },
                "required": ["when", "what", "expected_impact"],
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
        "data_as_of": {"type": "string"},
    },
    "required": ["regime", "events", "reason", "data_as_of"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class MacroAnalyst:
    regime: str
    events: list[Any]
    reason: str
    data_as_of: str


def parse(text: str) -> MacroAnalyst:
    data = parse_object(text, SCHEMA)
    regime = _enum(data.get("regime"), "regime", ("risk-on", "risk-off", "neutral"))
    events = _list(data.get("events"), "events")
    reason = _text(data.get("reason"), "reason")
    data_as_of = _text(data.get("data_as_of"), "data_as_of")
    return MacroAnalyst(regime=regime, events=events, reason=reason, data_as_of=data_as_of)
