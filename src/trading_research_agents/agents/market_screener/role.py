"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.llm.form import (
    _list,
    parse_object,
)

ROLE = "market_screener"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kept": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instrument": {"type": "string"},
                    "regime": {
                        "type": "string",
                        "enum": ["trending", "ranging", "volatile", "illiquid"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["instrument", "regime", "reason"],
                "additionalProperties": False,
            },
        },
        "excluded": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"instrument": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["instrument", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["kept", "excluded"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class MarketScreener:
    kept: list[Any]
    excluded: list[Any]


def parse(text: str) -> MarketScreener:
    data = parse_object(text, SCHEMA)
    kept = _list(data.get("kept"), "kept")
    excluded = _list(data.get("excluded"), "excluded")
    return MarketScreener(kept=kept, excluded=excluded)
