from __future__ import annotations

from trading_research_agents.ledger import (
    ENVELOPE_BOUND_AXES,
    UNIVERSAL_AXES,
    Axis,
    Transferability,
    Verdict,
    implied_transferability,
)


def test_every_axis_is_classified_or_deliberately_unmapped() -> None:
    unmapped = {a for a in Axis if implied_transferability(a) is None}
    assert unmapped == {Axis.DATA, Axis.POWER}
    assert UNIVERSAL_AXES.isdisjoint(ENVELOPE_BOUND_AXES)
    assert UNIVERSAL_AXES | ENVELOPE_BOUND_AXES | unmapped == set(Axis)


def test_mapping_matches_the_taxonomy() -> None:
    assert implied_transferability(Axis.IDEA) is Transferability.UNIVERSAL
    assert implied_transferability(Axis.MECHANISM_INVERTED) is Transferability.UNIVERSAL
    assert implied_transferability(Axis.COST) is Transferability.ENVELOPE_BOUND
    assert implied_transferability(Axis.CADENCE) is Transferability.ENVELOPE_BOUND
    assert implied_transferability(Axis.ADVERSE_SELECTION) is Transferability.ENVELOPE_BOUND


def test_verdict_families_partition_the_vocabulary() -> None:
    deaths = {v for v in Verdict if v.is_death}
    live = {v for v in Verdict if v.is_live}
    facts = {v for v in Verdict if v.is_characterisation}
    assert deaths == {Verdict.FALSIFIED, Verdict.CLOSED}
    assert live == {Verdict.OPEN, Verdict.NOT_PROVEN}
    assert facts == {Verdict.CONFIRMED, Verdict.VALIDATED}
    assert deaths | live | facts | {Verdict.WITHDRAWN} == set(Verdict)


def test_verdict_values_are_the_published_spelling() -> None:
    # The ledger is read by people. "NOT PROVEN" has a space in the record and keeps it here.
    assert Verdict.NOT_PROVEN.value == "NOT PROVEN"
    assert Transferability.ENVELOPE_BOUND.value == "ENVELOPE-BOUND"
