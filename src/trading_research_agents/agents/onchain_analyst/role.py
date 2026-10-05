"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.llm.form import (
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
        "figures_used": FIGURES_SCHEMA,
        "data_as_of": {"type": "string"},
    },
    "required": ["signal", "reason", "figures_used", "data_as_of"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class OnchainAnalyst:
    signal: str
    reason: str
    figures_used: tuple[Figure, ...]
    data_as_of: str


def parse(text: str) -> OnchainAnalyst:
    data = parse_object(text, SCHEMA)
    signal = _enum(data.get("signal"), "signal", ("accumulation", "distribution", "neutral"))
    reason = _text(data.get("reason"), "reason")
    cited = figures_used(data.get("figures_used"))
    data_as_of = _text(data.get("data_as_of"), "data_as_of")
    return OnchainAnalyst(signal=signal, reason=reason, figures_used=cited, data_as_of=data_as_of)
