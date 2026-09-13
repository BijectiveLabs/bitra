"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agent.agent.form import (
    _enum,
    _text,
    parse_object,
)

ROLE = "onchain_analyst"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signal": {"type": "string", "enum": ["accumulation", "distribution", "neutral"]},
        "reason": {"type": "string"},
        "data_as_of": {"type": "string"},
    },
    "required": ["signal", "reason", "data_as_of"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class OnchainAnalyst:
    signal: str
    reason: str
    data_as_of: str


def parse(text: str) -> OnchainAnalyst:
    data = parse_object(text, SCHEMA)
    signal = _enum(data.get("signal"), "signal", ("accumulation", "distribution", "neutral"))
    reason = _text(data.get("reason"), "reason")
    data_as_of = _text(data.get("data_as_of"), "data_as_of")
    return OnchainAnalyst(signal=signal, reason=reason, data_as_of=data_as_of)
