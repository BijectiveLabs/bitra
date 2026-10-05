"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.llm.form import _enum, _text, parse_object

ROLE = "sentiment_analyst"
STATES = ("negative", "neutral", "positive", "not declared")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": list(STATES)},
        "reason": {"type": "string"},
        "figures_used": FIGURES_SCHEMA,
        "data_as_of": {"type": "string"},
    },
    "required": ["state", "reason", "figures_used", "data_as_of"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SentimentAnalyst:
    state: str
    reason: str
    figures_used: tuple[Figure, ...]
    data_as_of: str

    @property
    def declared(self) -> bool:
        """Whether the operator gave this analyst anything to read at all."""
        return self.state != "not declared"


def parse(text: str) -> SentimentAnalyst:
    data = parse_object(text, SCHEMA)
    state = _enum(data.get("state"), "state", STATES)
    reason = _text(data.get("reason"), "reason")
    cited = figures_used(data.get("figures_used"))
    data_as_of = _text(data.get("data_as_of"), "data_as_of")
    return SentimentAnalyst(state=state, reason=reason, figures_used=cited, data_as_of=data_as_of)
