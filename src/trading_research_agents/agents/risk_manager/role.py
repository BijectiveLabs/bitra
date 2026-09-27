"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agent.agent.form import (
    _dict,
    _enum,
    _list,
    _text,
    parse_object,
)

ROLE = "risk_manager"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["continue", "tighten", "demote", "halt"]},
        "limits": {
            "type": "object",
            "properties": {
                "max_risk_per_trade_bps": {"type": "string"},
                "max_position": {"type": "string"},
                "max_orders_per_minute": {"type": "integer", "minimum": 1},
                "daily_loss_limit_bps": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        "reason": {"type": "string"},
        "triggers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "limits", "reason", "triggers"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class RiskManager:
    decision: str
    limits: dict[str, Any]
    reason: str
    triggers: list[Any]


def parse(text: str) -> RiskManager:
    data = parse_object(text, SCHEMA)
    decision = _enum(data.get("decision"), "decision", ("continue", "tighten", "demote", "halt"))
    limits = _dict(data.get("limits"), "limits")
    reason = _text(data.get("reason"), "reason")
    triggers = _list(data.get("triggers"), "triggers")
    return RiskManager(decision=decision, limits=limits, reason=reason, triggers=triggers)
