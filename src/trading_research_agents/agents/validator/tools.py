"""The brief a validator gets, and the check code runs over the claims it comes back with.

A validator's words only count when they rest on a figure it was actually given. So every claim
names a figure by the brief's own name and repeats that figure's value, and code compares the two:
a figure that is not in the brief is discarded, a value that does not match is discarded, and both
discards are recorded. A verdict with no surviving claim becomes `abstain`, because a verdict
resting on nothing is nothing.

The comparison is fair to a model that rounds. "-1.63" against a brief's "-1.631" is the same
citation said with fewer digits, and it is kept; "-2.0" is a different number, and it is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.agents.validator.role import Claim, Validator

# Figures come from the brief the deciding analyst was given, so a validator argues over the same
# numbers rather than over a summary of them.
SKIP = ("mechanism", "basis")


def figures_of(brief: Mapping[str, Any]) -> dict[str, str]:
    """Every scalar figure in a brief, by the brief's own name, as a string.

    Containers are left out: a claim cites one figure, and a list is not a figure. Nothing is
    renamed here, because a citation that has to be translated is not a citation.
    """
    out: dict[str, str] = {}
    for key, value in brief.items():
        if key in SKIP or value is None or isinstance(value, dict | list | tuple):
            continue
        out[str(key)] = str(value)
    return out


@dataclass(frozen=True)
class Checked:
    """What survived the check, what did not, and the verdict that is left."""

    stance: str
    verdict: str
    kept: tuple[Claim, ...]
    discarded: tuple[str, ...]
    reason: str

    def record(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "verdict": self.verdict,
            "claims": [c.record() for c in self.kept],
            "discarded": list(self.discarded),
            "reason": self.reason,
        }


def checked(said: Validator, figures: Mapping[str, str]) -> Checked:
    """Keep the claims that cite a figure in the brief with that figure's value, drop the rest."""
    kept: list[Claim] = []
    discarded: list[str] = []
    for claim in said.claims:
        if claim.figure not in figures:
            discarded.append(f"{claim.figure!r} is not a figure in the brief")
            continue
        actual = figures[claim.figure]
        if not _same(claim.value, actual):
            discarded.append(f"{claim.figure} was {actual}, not {claim.value}")
            continue
        kept.append(claim)
    verdict = said.verdict if kept else "abstain"
    reason = said.reason
    if not kept:
        reason = f"{reason} (no claim cited a figure in the brief, so the verdict is abstain)"
    return Checked(
        stance=said.stance,
        verdict=verdict,
        kept=tuple(kept),
        discarded=tuple(discarded),
        reason=reason,
    )


def validator_brief(
    *,
    instrument: str,
    stance: str,
    proposal: Mapping[str, Any],
    figures: Mapping[str, str],
    declared_states: Mapping[str, Any],
    cost_bps: str | None = None,
) -> dict[str, Any]:
    """One validator's brief: the proposal, the figures it was built from, and the declarations."""
    return {
        "instrument": instrument,
        "stance": stance,
        "proposal": dict(proposal),
        "figures": dict(figures),
        "declared_states": dict(declared_states),
        "cost_bps": cost_bps,
    }


def _same(claimed: str, actual: str) -> bool:
    """Whether two values are the same figure, allowing a claim to carry fewer digits."""
    left, right = claimed.strip(), actual.strip()
    if left.lower() == right.lower():
        return True
    try:
        a, b = Decimal(left), Decimal(right)
    except InvalidOperation:
        return False
    if not a.is_finite() or not b.is_finite():
        # A claim of NaN or Infinity is not a citation of anything.
        return False
    exponent = a.as_tuple().exponent
    if not isinstance(exponent, int):
        return False
    quantum = Decimal(1).scaleb(min(exponent, 0))
    return a == b.quantize(quantum)
