"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.cited import FIGURES_SCHEMA, Figure, figures_used
from trading_research_agents.llm.form import (
    FormError,
    _decimal,
    _enum,
    _posint,
    _text,
    _unit,
    parse_object,
)

ROLE = "technical_analyst"
TARGETS = ("long", "short", "flat")
# The named shapes this role is allowed to say it recognised. A closed list, because "where did this
# position come from" has to be answerable by reading the record rather than by reading a paragraph,
# and because a vocabulary is the only part of a sentence a later step can count.
SETUPS = (
    "trend_continuation",
    "pullback_in_trend",
    "range_reversion",
    "range_breakout",
    "momentum_exhaustion",
    "volatility_expansion",
    "no_setup",
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": list(TARGETS)},
        "setup": {"type": "string", "enum": list(SETUPS)},
        "reason": {"type": "string"},
        "figures_used": FIGURES_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "horizon_bars": {"type": "integer", "minimum": 1},
        # Where the idea is wrong and where it is done, as prices; null when the bars show neither.
        "stop_price": {"type": ["number", "null"]},
        "target_price": {"type": ["number", "null"]},
    },
    "required": [
        "target",
        "setup",
        "reason",
        "figures_used",
        "confidence",
        "horizon_bars",
        "stop_price",
        "target_price",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TechnicalAnalyst:
    target: str
    setup: str
    reason: str
    figures_used: tuple[Figure, ...]
    confidence: float
    horizon_bars: int
    stop_price: Decimal | None = None
    target_price: Decimal | None = None


# Strict providers need every field listed as required; an answer that leaves the two levels out is
# still read, as no levels, because the measured stop is the fallback either way.
LEVELS = ("stop_price", "target_price")
READ: dict[str, Any] = {**SCHEMA, "required": [k for k in SCHEMA["required"] if k not in LEVELS]}


def parse(text: str) -> TechnicalAnalyst:
    data = parse_object(text, READ)
    target = _enum(data.get("target"), "target", TARGETS)
    setup = _enum(data.get("setup"), "setup", SETUPS)
    reason = _text(data.get("reason"), "reason")
    cited = figures_used(data.get("figures_used"))
    confidence = _unit(data.get("confidence"), "confidence")
    horizon_bars = _posint(data.get("horizon_bars"), "horizon_bars")
    return TechnicalAnalyst(
        stop_price=_price(data.get("stop_price"), "stop_price"),
        target_price=_price(data.get("target_price"), "target_price"),
        target=target,
        setup=setup,
        reason=reason,
        figures_used=cited,
        confidence=confidence,
        horizon_bars=horizon_bars,
    )


def _price(value: Any, label: str) -> Decimal | None:
    if value is None:
        return None
    price = _decimal(value, label)
    if price <= 0:
        raise FormError(f"{label} is a price above zero, or null")
    return price
