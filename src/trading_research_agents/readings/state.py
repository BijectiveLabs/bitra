"""What the operator's own thresholds declare on a reading, with its net graded score.

Each series row of a reading carries the state of the operator's declared threshold; when they all
point the same way, that is the declared state, and when they disagree it is `mixed`. Confluence
reads the word and fails closed on `mixed`; conviction weighs the net score.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from trading_research_agents.readings import MACRO, ONCHAIN, SENTIMENT

# What a declared threshold means in each reading's own vocabulary. The operator declares bullish
# and bearish; a desk says risk-on or accumulation, and these are the same statement.
IN_ITS_WORDS = {
    MACRO: {"bullish": "risk-on", "bearish": "risk-off"},
    ONCHAIN: {"bullish": "accumulation", "bearish": "distribution"},
    SENTIMENT: {"bullish": "positive", "bearish": "negative"},
}


def declared_state(reading: str, brief: Mapping[str, Any]) -> dict[str, Any]:
    """What the operator's own thresholds say on one reading: one word, the count, the net score.

    `undeclared` when no series has a threshold, `mixed` when they disagree, else the one state
    they share, with the reading's own word for it.
    """
    rows = [
        row for row in brief.get("series", []) if row.get("basis") == "operator-declared threshold"
    ]
    states = [str(row.get("state")) for row in rows]
    named = sorted({s for s in states if s in ("bullish", "bearish")})
    score = _net_strength(rows)
    graded = bool(rows) and all(bool(row.get("graded")) for row in rows)
    if not named:
        return {"declared": "undeclared", "series_declared": len(states), "score": "0"}
    if len(named) > 1:
        # The gate still reads `mixed` and fails closed on it; conviction weighs the net.
        return {
            "declared": "mixed",
            "states": named,
            "series_declared": len(states),
            "score": str(score),
            "graded": graded,
        }
    return {
        "declared": named[0],
        "in_its_words": IN_ITS_WORDS.get(reading, {}).get(named[0]),
        "series_declared": len(states),
        "score": str(score),
        "graded": graded,
    }


def _net_strength(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    """The mean signed strength of a role's declared series, between -1 and 1.

    A row written before strengths existed carries only its word, and is read as the full +1 or
    -1 it always meant. A series whose value is unknown says nothing and is left out.
    """
    found: list[Decimal] = []
    for row in rows:
        state = str(row.get("state"))
        if state not in ("bullish", "bearish", "neutral"):
            continue
        raw = row.get("strength")
        if raw is not None:
            value = Decimal(str(raw))
        else:
            value = {"bullish": Decimal(1), "bearish": Decimal(-1)}.get(state, Decimal(0))
        # A series about another asset is divided over the same count but weighed less, so a desk
        # reading only another asset's chain hears a quieter voice, not the same one.
        found.append(value * Decimal(str(row.get("counts", 1))))
    if not found:
        return Decimal(0)
    return (sum(found, Decimal(0)) / len(found)).quantize(Decimal("0.001"))


__all__ = ["IN_ITS_WORDS", "declared_state"]
