"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.agents.roles import RoleError
from trading_research_agents.agents.technical_analyst.role import SETUPS
from trading_research_agents.llm.form import (
    _enum,
    _list,
    _text,
    parse_object,
)

# The setups a pick may name: the technical analyst's own list, without its refusal.
PICKABLE = tuple(s for s in SETUPS if s != "no_setup")

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


# In a live pass the screener chooses: it orders the instruments code found and admitted, names the
# setup it expects on each, and cites the figures that say so. It still cannot add one.
LIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instrument": {"type": "string"},
                    "setup": {"type": "string", "enum": list(PICKABLE)},
                    "reason": {"type": "string"},
                    "figures_used": FIGURES_SCHEMA,
                },
                "required": ["instrument", "setup", "reason", "figures_used"],
                "additionalProperties": False,
            },
        },
        "excluded": SCHEMA["properties"]["excluded"],
    },
    "required": ["picks", "excluded"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Pick:
    instrument: str
    setup: str
    reason: str
    figures_used: tuple[Figure, ...]


@dataclass(frozen=True)
class LiveScreen:
    picks: tuple[Pick, ...]
    excluded: list[Any]


def parse_live(text: str) -> LiveScreen:
    data = parse_object(text, LIVE_SCHEMA)
    picks = []
    for number, row in enumerate(_list(data.get("picks"), "picks"), start=1):
        if not isinstance(row, dict):
            raise RoleError(f"pick {number} is not an object")
        picks.append(
            Pick(
                instrument=_text(row.get("instrument"), "instrument"),
                setup=_enum(row.get("setup"), "setup", PICKABLE),
                reason=_text(row.get("reason"), "reason"),
                figures_used=figures_used(row.get("figures_used"), f"pick {number} figures_used"),
            )
        )
    return LiveScreen(picks=tuple(picks), excluded=_list(data.get("excluded"), "excluded"))
