from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.ledger import (
    Axis,
    LedgerEntry,
    LedgerError,
    ReversalPoint,
    Transferability,
    Verdict,
)


def falsified(now: datetime, **overrides: Any) -> LedgerEntry:
    base: dict[str, Any] = {
        "entry_id": "H-001",
        "subject": "positioning drift after accumulation",
        "verdict": Verdict.FALSIFIED,
        "recorded_at": now,
        "layer": 5,
        "axis_of_death": (Axis.IDEA,),
        "transferability": Transferability.UNIVERSAL,
    }
    base.update(overrides)
    return LedgerEntry(**base)


def test_a_universal_falsification_is_accepted(now: datetime) -> None:
    e = falsified(now)
    assert e.final_axis is Axis.IDEA
    assert e.reversal_point is None


def test_death_needs_an_axis(now: datetime) -> None:
    with pytest.raises(LedgerError, match="axis of death"):
        falsified(now, axis_of_death=())


def test_power_is_never_a_death(now: datetime) -> None:
    with pytest.raises(LedgerError, match="POWER cannot be an axis of death"):
        falsified(now, axis_of_death=(Axis.POWER,), transferability=Transferability.UNIVERSAL)


def test_transferability_follows_the_final_axis(now: datetime) -> None:
    # "Data, then Idea": the defect was fixed and the hypothesis died on its merits.
    e = falsified(now, axis_of_death=(Axis.DATA, Axis.IDEA))
    assert e.final_axis is Axis.IDEA
    with pytest.raises(LedgerError, match="UNIVERSAL"):
        falsified(
            now,
            axis_of_death=(Axis.DATA, Axis.IDEA),
            transferability=Transferability.ENVELOPE_BOUND,
        )


def test_data_as_final_axis_is_resolved_case_by_case(now: datetime) -> None:
    falsified(now, axis_of_death=(Axis.DATA,), transferability=Transferability.UNIVERSAL)
    falsified(
        now,
        axis_of_death=(Axis.DATA,),
        transferability=Transferability.ENVELOPE_BOUND,
        reversal_point=ReversalPoint("resolution", Decimal("1"), "ms"),
    )


def test_universal_verdicts_carry_no_reversal_point(now: datetime) -> None:
    with pytest.raises(LedgerError, match="no reversal point"):
        falsified(now, reversal_point=ReversalPoint("c_rt", Decimal("3.0"), "bps"))


def test_closed_requires_envelope_bound_and_a_reversal_point(now: datetime) -> None:
    ok = falsified(
        now,
        verdict=Verdict.CLOSED,
        axis_of_death=(Axis.COST,),
        transferability=Transferability.ENVELOPE_BOUND,
        reversal_point=ReversalPoint("c_rt", Decimal("3.0"), "bps"),
    )
    assert ok.verdict is Verdict.CLOSED

    with pytest.raises(LedgerError, match="reversal point"):
        falsified(
            now,
            verdict=Verdict.CLOSED,
            axis_of_death=(Axis.COST,),
            transferability=Transferability.ENVELOPE_BOUND,
        )
    with pytest.raises(LedgerError, match="ENVELOPE-BOUND"):
        falsified(now, verdict=Verdict.CLOSED, axis_of_death=(Axis.COST,))


def test_a_repeated_axis_is_refused(now: datetime) -> None:
    with pytest.raises(LedgerError, match="repeats"):
        falsified(now, axis_of_death=(Axis.IDEA, Axis.IDEA))


def test_not_proven_carries_a_blocking_axis_and_nothing_else(now: datetime) -> None:
    e = LedgerEntry(
        "H-002", "whale state change drift", Verdict.NOT_PROVEN, now, blocking_axis=Axis.POWER
    )
    assert e.blocking_axis is Axis.POWER
    with pytest.raises(LedgerError, match="blocking axis"):
        LedgerEntry("H-003", "x", Verdict.OPEN, now)
    with pytest.raises(LedgerError, match="no axis of death"):
        LedgerEntry(
            "H-004", "x", Verdict.OPEN, now, blocking_axis=Axis.DATA, axis_of_death=(Axis.IDEA,)
        )
    with pytest.raises(LedgerError, match="not classified"):
        LedgerEntry(
            "H-005",
            "x",
            Verdict.NOT_PROVEN,
            now,
            blocking_axis=Axis.POWER,
            transferability=Transferability.UNIVERSAL,
        )


def test_characterisations_carry_no_axis(now: datetime) -> None:
    LedgerEntry("C-001", "median quoted spread across 676 symbols", Verdict.CONFIRMED, now)
    with pytest.raises(LedgerError, match="not a hypothesis"):
        LedgerEntry("C-002", "x", Verdict.CONFIRMED, now, axis_of_death=(Axis.IDEA,))
    universal = Transferability.UNIVERSAL
    with pytest.raises(LedgerError, match="not classified"):
        LedgerEntry("V-001", "x", Verdict.VALIDATED, now, transferability=universal)


def test_withdrawal_names_its_target_and_its_reason(now: datetime) -> None:
    LedgerEntry("W-001", "x", Verdict.WITHDRAWN, now, retracts="H-001", note="cost term was zero")
    with pytest.raises(LedgerError, match="name the entry"):
        LedgerEntry("W-002", "x", Verdict.WITHDRAWN, now, note="why")
    with pytest.raises(LedgerError, match="states why"):
        LedgerEntry("W-003", "x", Verdict.WITHDRAWN, now, retracts="H-001")
    with pytest.raises(LedgerError, match="retract itself"):
        LedgerEntry("W-004", "x", Verdict.WITHDRAWN, now, retracts="W-004", note="why")
    with pytest.raises(LedgerError, match="only a WITHDRAWN"):
        falsified(now, retracts="H-000")


def test_clock_must_be_utc(now: datetime) -> None:
    with pytest.raises(LedgerError, match="timezone-aware"):
        falsified(now, recorded_at=now.replace(tzinfo=None))
    jakarta = timezone(timedelta(hours=7))
    with pytest.raises(LedgerError, match="UTC"):
        falsified(now, recorded_at=now.astimezone(jakarta))


def test_layer_is_bounded(now: datetime) -> None:
    falsified(now, layer=0)
    falsified(now, layer=8)
    with pytest.raises(LedgerError, match="layer"):
        falsified(now, layer=9)


def test_identity_fields_are_required(now: datetime) -> None:
    with pytest.raises(LedgerError, match="entry_id"):
        falsified(now, entry_id="  ")
    with pytest.raises(LedgerError, match="subject"):
        falsified(now, subject="")


def test_reversal_point_refuses_floats() -> None:
    with pytest.raises(LedgerError, match="Decimal"):
        ReversalPoint("c_rt", 3.0, "bps")  # type: ignore[arg-type]
    with pytest.raises(LedgerError, match="comparison"):
        ReversalPoint("c_rt", Decimal("3.0"), "bps", comparison="==")
