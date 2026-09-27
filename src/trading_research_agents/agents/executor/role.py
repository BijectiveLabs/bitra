"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agent.agent.form import (
    _decimal,
    _list,
    _text,
    parse_object,
)

ROLE = "executor"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "size": {"type": "string"},
        "reason": {"type": "string"},
        "allocations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instrument": {"type": "string"},
                    "fraction": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["instrument", "fraction", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["size", "reason", "allocations"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Executor:
    size: Decimal
    reason: str
    allocations: list[Any]


def parse(text: str) -> Executor:
    data = parse_object(text, SCHEMA)
    size = _decimal(data.get("size"), "size")
    reason = _text(data.get("reason"), "reason")
    allocations = _list(data.get("allocations"), "allocations")
    return Executor(size=size, reason=reason, allocations=allocations)
