"""Dated text as features: what a typed judge said each headline reported, when it was first seen.

A `TextEvent` is one archived headline after the judge read it (`perception/text_events.py`): the
probability of each kind of event, and how much it concerns each asset and the market as a whole.
The features read only events first seen inside the window; a headline's own date is never used.

- `text_count_24`: headlines in the last 24 bars that concern this asset or the whole market, at a
  probability of at least one half.
- `text_<kind>_p_24`: the strongest report of that kind in the last 24 bars, as the probability of
  the kind times how much the headline concerns this asset (or the market, whichever is larger).

With no judged text given at all, both are absent: a desk without a judge and a quiet day must not
look the same. An event whose kind the judge was not confident of counts, and carries no kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from trading_research_agents.features.core import Absent, FeatureValue, Inputs, Value

# The kinds of event a headline is read for, with what each means to the judge.
KINDS: Mapping[str, str] = {
    "listing": "an asset is listed, or a new market for it opens, on an exchange",
    "delisting": "an asset is delisted, or trading in it is suspended, on an exchange",
    "hack": "funds are stolen, or a protocol, bridge or exchange is exploited or breached",
    "outage": "an exchange, chain or protocol halts, stalls or stops withdrawals",
    "regulatory": "a regulator or a court acts on, charges, bans or approves something in crypto",
    "macro": "a scheduled economic release or a central bank decision",
    "none": "none of these",
}
MARKET = "market"
WINDOW_BARS = 24
RELEVANT = 0.5


@dataclass(frozen=True, slots=True)
class TextEvent:
    feed: str
    first_seen_ns: int
    # Probability per kind; empty when the judge was not confident of the kind.
    kinds: Mapping[str, float] = field(default_factory=dict)
    # Probability that the headline concerns each asset, and `market` for the market as a whole.
    concerns: Mapping[str, float] = field(default_factory=dict)
    model: str = ""

    def relevance(self, asset: str) -> float:
        return max(self.concerns.get(asset, 0.0), self.concerns.get(MARKET, 0.0))


def _recent(inputs: Inputs) -> list[TextEvent] | Absent:
    if inputs.text is None:
        return Absent("no judged text: no judge is declared, or none was given")
    since = inputs.at_ns - WINDOW_BARS * inputs.interval_ns
    return [e for e in inputs.text if since < e.first_seen_ns <= inputs.at_ns]


def text_count_24(inputs: Inputs) -> FeatureValue:
    recent = _recent(inputs)
    if isinstance(recent, Absent):
        return recent
    return sum(1 for e in recent if e.relevance(inputs.asset) >= RELEVANT)


def text_kind_p_24(kind: str) -> Value:
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a kind of event; have {list(KINDS)}")

    def value(inputs: Inputs) -> FeatureValue:
        recent = _recent(inputs)
        if isinstance(recent, Absent):
            return recent
        return max(
            (e.kinds.get(kind, 0.0) * e.relevance(inputs.asset) for e in recent), default=0.0
        )

    return value


__all__ = ["KINDS", "MARKET", "TextEvent", "text_count_24", "text_kind_p_24"]
