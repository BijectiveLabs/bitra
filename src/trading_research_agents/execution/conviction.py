"""How much capital a cycle puts behind an idea, decided by a rule the operator wrote.

The matrix answers whether the context agrees with the direction. This answers the other half of
the question a desk actually has: how much. Four analysts and three validators are only worth their
token bill if what they say changes the size of the trade, and not merely whether it happens.

The rule is code, and the weights are the operator's. That is deliberate. A model asked "how
confident are you, and how big should this be" will answer differently on Tuesday than on Monday
from the same figures, and a size that depends on which sentence was written last cannot be
backtested, reproduced or audited. So a score is computed from what each role declared, what the
validators voted after their claims were checked, and the deciding analyst's own confidence; the
score picks a tier off a ladder the operator wrote; the tier multiplies the base size; and every cap
in the project still applies afterwards. Nothing here can make a trade larger than the operator
already allowed - it can only make it smaller.

Leverage is not a lever a role gets to pull. It is what the notional works out to against the
equity, and `[conviction].max_leverage` is the ceiling on that, applied as a notional cap.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_research_agents.execution.confluence import declared_of
from trading_research_agents.execution.sizing import (
    Sizing,
    leverage_of,
    notional_cap,
    size_for,
)

AGREES = {"long": "bullish", "short": "bearish"}
OPPOSES = {"long": "bearish", "short": "bullish"}
DECIDING = "technical_analyst"


class ConvictionError(ValueError):
    """The conviction rule as written cannot be applied."""


@dataclass(frozen=True)
class Tier:
    """One rung of the ladder: a score at or above `at_least` trades `size_multiple` of the base."""

    at_least: Decimal
    size_multiple: Decimal

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.at_least <= Decimal(1):
            raise ConvictionError("a ladder rung sits between 0 and 1")
        if self.size_multiple <= 0:
            raise ConvictionError("a size multiple is positive; use min_score to refuse instead")

    def record(self) -> dict[str, Any]:
        return {"at_least": str(self.at_least), "size_multiple": str(self.size_multiple)}


@dataclass(frozen=True)
class Conviction:
    """[conviction]: what each voice is worth, the floor, and the ladder from score to size."""

    weights: Mapping[str, Decimal] = field(default_factory=dict)
    validator_weight: Decimal = Decimal(0)
    min_score: Decimal = Decimal(0)
    ladder: tuple[Tier, ...] = ()
    max_leverage: Decimal | None = None

    def __post_init__(self) -> None:
        for role, weight in self.weights.items():
            if weight <= 0:
                raise ConvictionError(f"[conviction].weights.{role} is positive")
        if self.validator_weight < 0:
            raise ConvictionError("[conviction].validator_weight is not negative")
        if not Decimal(0) <= self.min_score <= Decimal(1):
            raise ConvictionError("[conviction].min_score sits between 0 and 1")
        if self.max_leverage is not None and self.max_leverage <= 0:
            raise ConvictionError("[conviction].max_leverage is positive")
        rungs = sorted(self.ladder, key=lambda t: t.at_least, reverse=True)
        if list(rungs) != list(self.ladder):
            object.__setattr__(self, "ladder", tuple(rungs))

    @property
    def idle(self) -> bool:
        return not self.weights and not self.ladder and self.max_leverage is None

    def tier_for(self, score: Decimal) -> Tier | None:
        """The first rung the score reaches. Nothing means no rung covers it."""
        for rung in self.ladder:
            if score >= rung.at_least:
                return rung
        return None

    def record(self) -> dict[str, Any]:
        return {
            "weights": {role: str(w) for role, w in sorted(self.weights.items())},
            "validator_weight": str(self.validator_weight),
            "min_score": str(self.min_score),
            "ladder": [t.record() for t in self.ladder],
            "max_leverage": None if self.max_leverage is None else str(self.max_leverage),
        }


@dataclass(frozen=True)
class Score:
    """What the desk's own voices add up to, and who said what to get there."""

    score: Decimal
    raw: Decimal
    possible: Decimal
    agreed: tuple[str, ...] = ()
    against: tuple[str, ...] = ()
    silent: tuple[str, ...] = ()
    support: int = 0
    opposition: int = 0
    contributions: tuple[tuple[str, str], ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "score": str(self.score),
            "raw": str(self.raw),
            "possible": str(self.possible),
            "agreed": list(self.agreed),
            "against": list(self.against),
            "silent": list(self.silent),
            "support": self.support,
            "opposition": self.opposition,
            "contributions": {name: value for name, value in self.contributions},
        }


@dataclass(frozen=True)
class Stake:
    """The size this cycle is willing to put behind the idea, and why it is that size."""

    admitted: bool
    size: Decimal
    multiple: Decimal
    score: Score
    capped_by: str = ""
    leverage: Decimal | None = None
    reasons: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "size": str(self.size),
            "multiple": str(self.multiple),
            "capped_by": self.capped_by,
            "leverage": None if self.leverage is None else str(self.leverage),
            "reasons": list(self.reasons),
            "score": self.score.record(),
        }


def score_of(
    rule: Conviction,
    *,
    target: str,
    states: Mapping[str, Mapping[str, Any]],
    confidence: float,
    votes: Sequence[str] = (),
) -> Score:
    """Add up the desk's voices for this direction.

    A role that declared nothing contributes nothing: it is neither agreement nor disagreement, and
    counting silence as either is how a desk talks itself into a trade. The deciding analyst's own
    weight is scaled by the confidence it gave, because that is the one number it is asked for.
    """
    if target not in AGREES:
        raise ConvictionError(f"target is long or short, not {target!r}")
    agreed: list[str] = []
    against: list[str] = []
    silent: list[str] = []
    parts: list[tuple[str, str]] = []
    raw = Decimal(0)
    possible = Decimal(0)

    for role, weight in sorted(rule.weights.items()):
        possible += weight
        if role == DECIDING:
            share = (weight * Decimal(str(confidence))).quantize(Decimal("0.001"))
            raw += share
            agreed.append(role)
            parts.append((role, str(share)))
            continue
        word = declared_of(states.get(role))
        if word == AGREES[target]:
            raw += weight
            agreed.append(role)
            parts.append((role, str(weight)))
        elif word == OPPOSES[target]:
            raw -= weight
            against.append(role)
            parts.append((role, str(-weight)))
        else:
            silent.append(role)
            parts.append((role, "0"))

    support = sum(1 for vote in votes if vote == "support")
    opposition = sum(1 for vote in votes if vote == "oppose")
    if rule.validator_weight > 0 and votes:
        possible += rule.validator_weight * len(votes)
        raw += rule.validator_weight * (support - opposition)
        parts.append(("validators", str(rule.validator_weight * (support - opposition))))

    if possible <= 0:
        return Score(score=Decimal(0), raw=raw, possible=Decimal(0))
    share = max(raw, Decimal(0)) / possible
    return Score(
        score=share.quantize(Decimal("0.001")),
        raw=raw,
        possible=possible,
        agreed=tuple(agreed),
        against=tuple(against),
        silent=tuple(silent),
        support=support,
        opposition=opposition,
        contributions=tuple(parts),
    )


def stake_for(
    rule: Conviction,
    score: Score,
    *,
    base_size: Decimal,
    price: Decimal,
    max_position: Decimal,
    equity: Decimal | None = None,
    step: Decimal = Decimal("0.000001"),
) -> Stake:
    """The size the ladder allows, after every cap the operator already declared."""
    reasons: list[str] = []
    if score.score < rule.min_score:
        reasons.append(f"conviction {score.score} is below the declared floor {rule.min_score}")
        return Stake(
            admitted=False,
            size=Decimal(0),
            multiple=Decimal(0),
            score=score,
            reasons=tuple(reasons),
        )
    rung = rule.tier_for(score.score)
    if rung is None:
        reasons.append(f"no rung of the ladder covers a conviction of {score.score}")
        return Stake(
            admitted=False,
            size=Decimal(0),
            multiple=Decimal(0),
            score=score,
            reasons=tuple(reasons),
        )

    wanted = base_size * rung.size_multiple
    sized: Sizing = size_for(
        wanted=wanted,
        price=price,
        max_position=max_position,
        max_notional=notional_cap(equity=equity, max_leverage=rule.max_leverage),
        step=step,
    )
    if sized.size <= 0:
        reasons.append(f"nothing left to trade after {sized.capped_by}")
        return Stake(
            admitted=False,
            size=Decimal(0),
            multiple=rung.size_multiple,
            score=score,
            capped_by=sized.capped_by,
            reasons=tuple(reasons),
        )
    if sized.size < wanted:
        reasons.append(f"{wanted} cut to {sized.size} by {sized.capped_by}")
    return Stake(
        admitted=True,
        size=sized.size,
        multiple=rung.size_multiple,
        score=score,
        capped_by=sized.capped_by,
        leverage=leverage_of(size=sized.size, price=price, equity=equity),
        reasons=tuple(reasons),
    )
