from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.chain import (
    CostModel,
    CostTerm,
    Era,
    KillCriterion,
    Power,
    Preregistration,
    PreregistrationError,
    SealedEra,
    Universe,
    amend,
)
from trading_research_agents.chain.preregistration import from_record, to_record
from trading_research_agents.ledger import Verdict

# The kill table and the era partition are the ones the pre-registration standard itself uses.
CRITERIA = (
    KillCriterion("net_return_ci95_lower_bps", "<=", Decimal("0"), Verdict.NOT_PROVEN),
    KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.FALSIFIED),
    KillCriterion("permutation_p_value", ">=", Decimal("0.05"), Verdict.FALSIFIED),
)

ERA = Era(
    research_start=date(2022, 1, 1),
    research_end=date(2025, 6, 30),
    sealed=(
        SealedEra("held-out A", date(2017, 1, 1), date(2021, 12, 31)),
        SealedEra("held-out B", date(2025, 7, 1), None),
    ),
)


def cost_model() -> CostModel:
    return CostModel(
        (
            CostTerm("fee", "venue schedule, declared tier"),
            CostTerm("spread_cost", "half-spread at decision time from recorded book"),
            CostTerm("slippage", "walk the recorded book for intended size"),
            CostTerm("funding_accrual", "realised funding over the holding period"),
        )
    )


def registration(now: datetime, **overrides: Any) -> Preregistration:
    base: dict[str, Any] = {
        "name": "funding settlement flow",
        "mechanism": "Longs paying funding are forced to reduce into settlement by margin.",
        "claim": "Net return over 4h after settlement is positive when funding exceeds 15 bps.",
        "metric": "net_return_bps",
        "horizon": timedelta(hours=4),
        "universe": Universe(("BTCUSDT", "ETHUSDT"), "top 15 perpetuals by 30d volume"),
        "era": ERA,
        "cost_model": cost_model(),
        "power": Power(required_sample=1200, effect_bps=Decimal("8.0"), basis="two-sided, 80 pct"),
        "kill_criteria": CRITERIA,
        "registered_at": now,
    }
    base.update(overrides)
    return Preregistration(**base)


def test_a_complete_registration_seals_deterministically(now: datetime) -> None:
    a = registration(now)
    b = registration(now)
    assert a.seal() == b.seal()
    assert len(a.seal()) == 64
    assert registration(now, metric="gross_return_bps").seal() != a.seal()
    assert registration(now, horizon=timedelta(hours=8)).seal() != a.seal()


def test_every_cost_term_is_declared_with_a_source() -> None:
    three = tuple(t for t in cost_model().terms if t.name != "funding_accrual")
    with pytest.raises(PreregistrationError, match="missing funding_accrual"):
        CostModel(three)
    with pytest.raises(PreregistrationError, match="repeats"):
        CostModel((*cost_model().terms, CostTerm("fee", "again")))
    with pytest.raises(PreregistrationError, match="unknown cost term"):
        CostTerm("commission", "x")
    with pytest.raises(PreregistrationError, match="needs a source"):
        CostTerm("fee", " ")


def test_kill_criteria_are_numeric_and_fire_a_kill_verdict() -> None:
    with pytest.raises(PreregistrationError, match="FALSIFIED or NOT PROVEN"):
        KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.CLOSED)
    with pytest.raises(PreregistrationError, match="Decimal"):
        KillCriterion("net_return_bps", "<", 0.0, Verdict.FALSIFIED)  # type: ignore[arg-type]
    with pytest.raises(PreregistrationError, match="comparison"):
        KillCriterion("net_return_bps", "==", Decimal("0"), Verdict.FALSIFIED)


def test_the_era_is_partitioned_before_research(now: datetime) -> None:
    with pytest.raises(PreregistrationError, match="at least one held-out era"):
        Era(date(2022, 1, 1), date(2025, 6, 30), ())
    with pytest.raises(PreregistrationError, match="overlaps"):
        Era(date(2022, 1, 1), date(2025, 6, 30), (SealedEra("B", date(2025, 6, 30), None),))
    with pytest.raises(PreregistrationError, match="ends before it starts"):
        Era(date(2025, 1, 1), date(2022, 1, 1), ERA.sealed)
    with pytest.raises(PreregistrationError, match="ends before it starts"):
        SealedEra("A", date(2021, 1, 1), date(2020, 1, 1))


def test_the_mechanism_is_one_sentence_that_names_a_forced_loser(now: datetime) -> None:
    with pytest.raises(PreregistrationError, match="one sentence"):
        registration(now, mechanism="Longs pay funding. They must reduce into settlement.")
    with pytest.raises(PreregistrationError, match="one sentence, on one line"):
        registration(now, mechanism="Longs pay funding\nand must reduce.")
    with pytest.raises(PreregistrationError, match="who is forced to lose"):
        registration(now, mechanism="Momentum works.")


def test_structural_requirements(now: datetime) -> None:
    with pytest.raises(PreregistrationError, match="horizon"):
        registration(now, horizon=timedelta(0))
    with pytest.raises(PreregistrationError, match="at least one numeric kill criterion"):
        registration(now, kill_criteria=())
    with pytest.raises(PreregistrationError, match="UTC"):
        registration(now, registered_at=now.replace(tzinfo=None))
    with pytest.raises(PreregistrationError, match="claim is required"):
        registration(now, claim="  ")
    with pytest.raises(PreregistrationError, match="at least one instrument"):
        Universe((), "rule")
    with pytest.raises(PreregistrationError, match="positive count"):
        Power(0, Decimal("1"), "basis")
    with pytest.raises(PreregistrationError, match="only an amendment"):
        registration(now, amendment_reason="why")


def test_tightening_is_a_larger_kill_region() -> None:
    lower = KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.FALSIFIED)
    assert KillCriterion(
        "net_return_bps", "<", Decimal("2"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(lower)
    assert KillCriterion(
        "net_return_bps", "<=", Decimal("0"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(lower)
    assert not KillCriterion(
        "net_return_bps", "<", Decimal("-1"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(lower)
    assert not lower.is_at_least_as_strict_as(
        KillCriterion("net_return_bps", "<=", Decimal("0"), Verdict.FALSIFIED)
    )

    upper = KillCriterion("permutation_p_value", ">=", Decimal("0.05"), Verdict.FALSIFIED)
    assert KillCriterion(
        "permutation_p_value", ">=", Decimal("0.01"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(upper)
    assert not KillCriterion(
        "permutation_p_value", ">", Decimal("0.05"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(upper)
    assert not KillCriterion(
        "other", ">=", Decimal("0.01"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(upper)
    assert not KillCriterion(
        "permutation_p_value", "<", Decimal("0.01"), Verdict.FALSIFIED
    ).is_at_least_as_strict_as(upper)


def test_an_amendment_may_tighten_and_add_but_never_loosen_or_drop(now: datetime) -> None:
    original = registration(now)
    later = now + timedelta(days=3)

    tightened = amend(
        original,
        amended_at=later,
        reason="p-value threshold tightened after audit found a family of 6 tests",
        kill_criteria=(
            CRITERIA[0],
            CRITERIA[1],
            KillCriterion("permutation_p_value", ">=", Decimal("0.01"), Verdict.FALSIFIED),
            KillCriterion("subperiod_min_net_bps", "<=", Decimal("0"), Verdict.FALSIFIED),
        ),
    )
    assert tightened.amends == original.seal()
    assert tightened.registered_at == later
    assert tightened.horizon == original.horizon
    assert original.amends is None
    assert len(tightened.kill_criteria) == 4

    with pytest.raises(PreregistrationError, match="loosens or drops"):
        amend(
            original,
            amended_at=later,
            reason="x",
            kill_criteria=(
                CRITERIA[0],
                CRITERIA[1],
                KillCriterion("permutation_p_value", ">=", Decimal("0.10"), Verdict.FALSIFIED),
            ),
        )
    with pytest.raises(PreregistrationError, match="loosens or drops"):
        amend(original, amended_at=later, reason="x", kill_criteria=CRITERIA[:2])
    with pytest.raises(PreregistrationError, match="states why"):
        amend(original, amended_at=later, reason="  ")


def test_an_amendment_may_correct_the_cost_source_and_the_universe(now: datetime) -> None:
    original = registration(now)
    corrected = CostModel(
        tuple(
            CostTerm(t.name, "funding from the settlement history endpoint, verified live")
            if t.name == "funding_accrual"
            else t
            for t in original.cost_model.terms
        )
    )
    amended = amend(
        original,
        amended_at=now + timedelta(days=1),
        reason="funding field on the stream is sent only on change; R-22 check failed",
        cost_model=corrected,
        universe=Universe(("BTCUSDT",), "top 15 by volume, denylist applied"),
    )
    assert amended.cost_model == corrected
    assert amended.universe.instruments == ("BTCUSDT",)
    assert amended.kill_criteria == original.kill_criteria


def test_json_round_trip_and_tamper_detection(now: datetime) -> None:
    original = registration(now)
    record = to_record(original)
    again = from_record(json.loads(json.dumps(record)))
    assert again == original
    assert again.seal() == record["seal"]

    tampered = json.loads(json.dumps(record))
    tampered["kill_criteria"][2]["threshold"] = "0.50"
    with pytest.raises(PreregistrationError, match="altered"):
        from_record(tampered)

    tampered = json.loads(json.dumps(record))
    tampered["schema"] = 2
    with pytest.raises(PreregistrationError, match="schema"):
        from_record(tampered)


def test_amendment_round_trips_with_its_pointer(now: datetime) -> None:
    original = registration(now)
    amended = amend(
        original, amended_at=now + timedelta(days=1), reason="tightened", kill_criteria=CRITERIA
    )
    again = from_record(to_record(amended))
    assert again.amends == original.seal()
    assert again.amendment_reason == "tightened"
    assert again.registered_at.tzinfo is UTC or again.registered_at.utcoffset() == timedelta(0)
