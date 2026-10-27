"""The schema a reviewer fills and the object the gate gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.execution.confluence import STANCES, VOTES
from trading_research_agents.llm.form import FormError, _enum, _text, parse_object

ROLE = "reviewer"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": list(STANCES)},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "figure": {"type": "string"},
                    "value": {"type": "string"},
                    "says": {"type": "string"},
                },
                "required": ["figure", "value", "says"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": list(VOTES)},
        "reason": {"type": "string"},
    },
    "required": ["stance", "claims", "verdict", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Claim:
    """One thing a reviewer says, tied to a figure it was given and that figure's value."""

    figure: str
    value: str
    says: str

    def record(self) -> dict[str, Any]:
        return {"figure": self.figure, "value": self.value, "says": self.says}


@dataclass(frozen=True)
class Reviewer:
    stance: str
    claims: tuple[Claim, ...]
    verdict: str
    reason: str


def parse(text: str) -> Reviewer:
    data = parse_object(text, SCHEMA)
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise FormError("claims must be a list")
    return Reviewer(
        stance=_enum(data.get("stance"), "stance", STANCES),
        claims=tuple(_claim(row, number) for number, row in enumerate(claims, start=1)),
        verdict=_enum(data.get("verdict"), "verdict", VOTES),
        reason=_text(data.get("reason"), "reason"),
    )


def _claim(row: Any, number: int) -> Claim:
    if not isinstance(row, dict):
        raise FormError(f"claim {number} must be an object")
    return Claim(
        figure=_text(row.get("figure"), f"claim {number} figure"),
        value=_text(row.get("value"), f"claim {number} value"),
        says=_text(row.get("says"), f"claim {number} says"),
    )
