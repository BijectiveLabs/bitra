"""Closed vocabularies for verdicts, axes and transferability.

These sets are closed on purpose. Free-form verdict language is what makes a ledger unreadable
after thirty entries, and it is how "not proven" quietly turns into "falsified" in a summary.
"""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    """How a ledger row ended, or that it has not."""

    FALSIFIED = "FALSIFIED"
    CLOSED = "CLOSED"
    NOT_PROVEN = "NOT PROVEN"
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    VALIDATED = "VALIDATED"
    WITHDRAWN = "WITHDRAWN"

    @property
    def is_death(self) -> bool:
        return self in (Verdict.FALSIFIED, Verdict.CLOSED)

    @property
    def is_live(self) -> bool:
        return self in (Verdict.OPEN, Verdict.NOT_PROVEN)

    @property
    def is_characterisation(self) -> bool:
        # A fact about a venue or a component, never a judgement about a strategy.
        return self in (Verdict.CONFIRMED, Verdict.VALIDATED)


class Axis(StrEnum):
    """What ended a hypothesis, or what is standing in its way."""

    IDEA = "idea"
    COST = "cost"
    DATA = "data"
    CADENCE = "cadence"
    POWER = "power"
    CONDITION = "condition"
    METHOD = "method"
    CAPITAL = "capital"
    VENUE = "venue"
    ADVERSE_SELECTION = "adverse selection"
    MECHANISM_INVERTED = "mechanism inverted"


class Transferability(StrEnum):
    """Whether a verdict carries to an operator on different infrastructure."""

    UNIVERSAL = "UNIVERSAL"
    ENVELOPE_BOUND = "ENVELOPE-BOUND"


UNIVERSAL_AXES: frozenset[Axis] = frozenset(
    {Axis.IDEA, Axis.CONDITION, Axis.METHOD, Axis.MECHANISM_INVERTED}
)

ENVELOPE_BOUND_AXES: frozenset[Axis] = frozenset(
    {Axis.COST, Axis.CADENCE, Axis.CAPITAL, Axis.VENUE, Axis.ADVERSE_SELECTION}
)


def implied_transferability(axis: Axis) -> Transferability | None:
    """Mechanical mapping from axis to transferability.

    Returns None for the two axes that do not map: DATA is decided case by case (a better
    instrument may or may not exist), and POWER is not a death at all.
    """
    if axis in UNIVERSAL_AXES:
        return Transferability.UNIVERSAL
    if axis in ENVELOPE_BOUND_AXES:
        return Transferability.ENVELOPE_BOUND
    return None
