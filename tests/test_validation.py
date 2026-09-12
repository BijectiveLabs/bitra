from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trading_research_agent.chain import (
    CostModel,
    CostTerm,
    Era,
    EraLeakageError,
    KillCriterion,
    NullMode,
    Power,
    Preregistration,
    Seal,
    SealedEra,
    SealError,
    TradeRecord,
    Universe,
    ValidationError,
    check_requirements,
    research_only,
    validate,
)
from trading_research_agent.chain.requirements import to_entry as requirements_entry
from trading_research_agent.chain.seal import day_end_ns, day_start_ns, era_of
from trading_research_agent.chain.validation import (
    NetTrade,
    anchor_robustness,
    benjamini_hochberg,
    block_bootstrap_ci,
    collapse,
    permutation_null,
    subperiod_means,
    to_entry,
    to_record,
)
from trading_research_agent.data import Direction, Manifest, Source
from trading_research_agent.ledger import Axis, Transferability, Verdict

S = 1_000_000_000
DAY = 86_400 * S
ERA = Era(date(2022, 1, 1), date(2025, 6, 30), (SealedEra("held-out B", date(2025, 7, 1), None),))
RESEARCH_START = day_start_ns(date(2022, 1, 1))


def registration(**overrides: Any) -> Preregistration:
    base: dict[str, Any] = {
        "name": "funding settlement squeeze",
        "mechanism": "Longs paying funding are forced by margin to reduce into settlement.",
        "claim": "Net return over 4h after settlement is positive when funding exceeds 15 bps.",
        "metric": "net_return_bps",
        "horizon": timedelta(hours=4),
        "universe": Universe(("BTCUSDT", "ETHUSDT"), "top 15 by volume"),
        "era": ERA,
        "cost_model": CostModel(
            (
                CostTerm("fee", "s"),
                CostTerm("spread_cost", "s"),
                CostTerm("slippage", "s"),
                CostTerm("funding_accrual", "s"),
            )
        ),
        "power": Power(30, Decimal("8.0"), "declared"),
        "kill_criteria": (
            KillCriterion("net_return_ci95_lower_bps", "<=", Decimal("0"), Verdict.NOT_PROVEN),
            KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.FALSIFIED),
            KillCriterion("permutation_p_value", ">=", Decimal("0.05"), Verdict.FALSIFIED),
            KillCriterion("subperiod_min_net_bps", "<=", Decimal("0"), Verdict.FALSIFIED),
        ),
        "registered_at": datetime(2026, 9, 13, tzinfo=UTC),
    }
    base.update(overrides)
    return Preregistration(**base)


def trades(
    n: int, *, gross: float, noise: float = 0.0, seed: int = 1, instruments: int = 2
) -> list[TradeRecord]:
    rng = np.random.default_rng(seed)
    out: list[TradeRecord] = []
    names = ("BTCUSDT", "ETHUSDT")[:instruments]
    for i in range(n):
        entry = RESEARCH_START + i * DAY
        for j, name in enumerate(names):
            value = gross + rng.normal(0.0, noise) if noise else gross
            out.append(
                TradeRecord(
                    instrument=name,
                    entry_ts_ns=entry,
                    exit_ts_ns=entry + 4 * 3600 * S,
                    direction=Direction.LONG if j == 0 else Direction.SHORT,
                    weight=Decimal("0.5"),
                    gross_return_bps=Decimal(f"{value:.4f}"),
                )
            )
    return out


def flat_cost(cost: str) -> Any:
    return lambda t: Decimal(cost)


# The seal


def test_era_boundaries_are_whole_days_in_utc() -> None:
    assert era_of(day_start_ns(date(2022, 1, 1)), ERA) == "research"
    assert era_of(day_end_ns(date(2025, 6, 30)) - 1, ERA) == "research"
    assert era_of(day_end_ns(date(2025, 6, 30)), ERA) == "held-out B"
    assert era_of(day_start_ns(date(2021, 12, 31)), ERA) == "outside"
    research_only([], ERA, who="empty is fine")
    with pytest.raises(EraLeakageError, match="1 row\\(s\\) in held-out B"):
        research_only([RESEARCH_START, day_start_ns(date(2025, 7, 1))], ERA, who="test")


def test_a_seal_opens_once_against_its_registration_and_never_again(tmp_path: Path) -> None:
    spec = registration().seal()
    seal = Seal(
        "held-out B", specification_hash=spec, gates=("net > 0",), path=tmp_path / "seal.json"
    )
    assert not seal.is_open()
    receipt = seal.open(spec, opened_at=datetime(2026, 9, 13, tzinfo=UTC))
    assert receipt.gates == ("net > 0",) and seal.is_open()
    with pytest.raises(SealError, match="already opened"):
        seal.open(spec, opened_at=datetime(2026, 9, 14, tzinfo=UTC))
    with pytest.raises(SealError, match="already opened"):
        seal.open("0" * 64, opened_at=datetime(2026, 9, 14, tzinfo=UTC))

    fresh = Seal("held-out B", specification_hash=spec, gates=("g",), path=tmp_path / "other.json")
    with pytest.raises(SealError, match="not the pre-registered specification"):
        fresh.open("1" * 64, opened_at=datetime(2026, 9, 14, tzinfo=UTC))
    with pytest.raises(SealError, match="without acceptance gates"):
        Seal("held-out B", specification_hash=spec, gates=(), path=tmp_path / "x.json")
    with pytest.raises(SealError, match="UTC"):
        fresh.open(spec, opened_at=datetime(2026, 9, 14))


# Layer 4


def manifest(first: date, last: date, rows: int, instrument: str = "BTCUSDT") -> Manifest:
    return Manifest(
        venue="bybit",
        instrument=instrument,
        first_ts=datetime(first.year, first.month, first.day, tzinfo=UTC),
        last_ts=datetime(last.year, last.month, last.day, 23, 59, 59, tzinfo=UTC),
        rows_accepted=rows,
        rows_rejected=0,
        sources=(Source("x", "ab" * 32, 1),),
        normaliser_version="t",
        audit_passed=True,
        findings=(),
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


def test_requirements_pass_when_the_data_can_see_the_claim() -> None:
    req = check_requirements(
        registration(), manifest(date(2021, 12, 1), date(2025, 8, 1), 5_000_000)
    )
    assert req.passed and req.blocking_axis is None


def test_a_coverage_gap_is_blocked_on_data_not_killed() -> None:
    req = check_requirements(
        registration(), manifest(date(2023, 1, 1), date(2025, 8, 1), 5_000_000)
    )
    assert not req.passed and req.blocking_axis is Axis.DATA
    entry = requirements_entry(
        req, registration(), entry_id="R-4", recorded_at=datetime(2026, 9, 13, tzinfo=UTC)
    )
    assert entry.verdict is Verdict.NOT_PROVEN and entry.blocking_axis is Axis.DATA
    assert entry.layer == 4 and "coverage-limited" in entry.note
    with pytest.raises(ValueError, match="nothing to record"):
        requirements_entry(
            check_requirements(
                registration(), manifest(date(2021, 12, 1), date(2025, 8, 1), 5_000_000)
            ),
            registration(),
            entry_id="R-5",
            recorded_at=datetime(2026, 9, 13, tzinfo=UTC),
        )


def test_too_coarse_data_is_blocked_on_cadence_and_a_short_era_on_power() -> None:
    coarse = check_requirements(
        registration(), manifest(date(2021, 12, 1), date(2025, 8, 1), 1_000)
    )
    assert not coarse.passed and coarse.blocking_axis is Axis.CADENCE
    short = check_requirements(
        registration(power=Power(100_000, Decimal("8"), "d")),
        manifest(date(2021, 12, 1), date(2025, 8, 1), 5_000_000),
    )
    assert short.blocking_axis is Axis.POWER
    foreign = check_requirements(
        registration(),
        manifest(date(2021, 12, 1), date(2025, 8, 1), 5_000_000, instrument="SOLUSDT"),
    )
    assert any(f.check == "universe" and not f.ok for f in foreign.findings)


# Layer 5 primitives


def test_collapse_makes_one_observation_per_timestamp() -> None:
    nets = [NetTrade(t, Decimal("1")) for t in trades(3, gross=10.0)]
    stamps, series = collapse(nets)
    assert stamps.size == 3 and series.size == 3
    assert series[0] == pytest.approx(0.5 * 9 + 0.5 * 9)


def test_block_bootstrap_follows_the_specification_not_the_reference() -> None:
    rng = np.random.default_rng(0)
    series = rng.normal(5.0, 2.0, size=200)
    ci = block_bootstrap_ci(series, block_length=5, n_resamples=1000, seed=0)
    assert ci.lower < ci.point < ci.upper and ci.n == 200
    with pytest.raises(ValidationError, match="too short"):
        block_bootstrap_ci(series[:9], block_length=5, n_resamples=1000)
    with pytest.raises(ValidationError, match="missing or non-finite"):
        block_bootstrap_ci(np.array([1.0, np.nan, 2.0, 3.0]), block_length=1, n_resamples=1000)
    with pytest.raises(ValidationError, match="at least 1000"):
        block_bootstrap_ci(series, block_length=5, n_resamples=10)


def test_the_null_is_costed_and_its_p_value_never_reaches_zero() -> None:
    # Strong edge: gross +30 bps every day, cost 10 bps, sign-flipped control pays the same cost.
    nets = [NetTrade(t, Decimal("10")) for t in trades(60, gross=30.0, noise=3.0)]
    report = permutation_null(nets, mode=NullMode.SIGN, n_perm=1000, seed=0)
    assert report.observed == pytest.approx(20.0, abs=1.0)
    assert report.null_mean == pytest.approx(-10.0, abs=1.5)  # centred near minus the round trip
    assert report.p_perm == pytest.approx(1 / 1001)
    assert report.sigma_above > 3
    with pytest.raises(ValidationError, match="at least 1000"):
        permutation_null(nets, mode=NullMode.SIGN, n_perm=5)


def test_the_cross_sectional_shuffle_keeps_turnover_and_moves_only_the_mapping() -> None:
    nets = [NetTrade(t, Decimal("10")) for t in trades(40, gross=30.0, noise=3.0)]
    report = permutation_null(nets, mode=NullMode.CROSS_SECTIONAL, n_perm=1000, seed=1)
    # Equal weights per timestamp: shuffling the mapping changes nothing, so the null is flat.
    assert report.mode is NullMode.CROSS_SECTIONAL
    assert report.null_sd > 0


def test_subperiods_and_benjamini_hochberg() -> None:
    assert subperiod_means(np.array([1.0, 1.0, 2.0, 2.0, 3.0, 3.0]), 3) == (1.0, 2.0, 3.0)
    with pytest.raises(ValidationError, match="cannot be cut"):
        subperiod_means(np.array([1.0, 2.0]), 3)
    assert benjamini_hochberg([0.001, 0.02, 0.04, 0.3], q=0.05) == (True, True, False, False)
    assert benjamini_hochberg([]) == ()
    with pytest.raises(ValidationError, match="lie in"):
        benjamini_hochberg([1.5])


def test_anchor_robustness_is_two_sided_and_refuses_a_single_phase() -> None:
    nets = [-6.0, 1.0, 2.0, 7.94, 3.0, -5.89]
    t = [-3.1, 0.5, 0.8, 2.49, 1.0, -2.2]
    report = anchor_robustness(nets, t, reported_index=3)
    assert report.n_phases == 6 and report.n_positive == 4
    assert report.n_significant_positive == 1 and report.n_significant_negative == 2
    assert not report.sign_consistent
    assert report.reported_rank == 1 and report.reported_label == "OPTIMISTIC"
    assert anchor_robustness([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], 0).reported_label == "PESSIMISTIC"
    with pytest.raises(ValidationError, match="one phase is the defect"):
        anchor_robustness([1.0], [1.0], 0)
    with pytest.raises(ValidationError, match="must be explicit"):
        anchor_robustness([1.0, float("nan")], [1.0, 1.0], 0)


# The verdict


def test_a_real_edge_survives_layer_five_and_stays_open() -> None:
    result = validate(
        registration(),
        trades(60, gross=30.0, noise=3.0),
        flat_cost("10"),
        block_length=5,
        n_resamples=1000,
        n_perm=1000,
    )
    assert result.decision.verdict is Verdict.OPEN and result.decision.axis is Axis.DATA
    assert result.power_achieved and result.n_timestamps == 60
    assert result.metrics["gross_return_bps"] > result.metrics["net_return_bps"]
    entry = to_entry(
        result, registration(), entry_id="V-1", recorded_at=datetime(2026, 9, 13, tzinfo=UTC)
    )
    assert entry.verdict is Verdict.OPEN and entry.blocking_axis is Axis.DATA and entry.layer == 5
    assert entry.evidence[0].startswith("preregistration:") and entry.evidence[1].startswith(
        "trades:"
    )
    record = to_record(result)
    assert record["decision"]["verdict"] == "OPEN" and record["null"]["p_perm"] < 0.01


def test_a_signal_smaller_than_its_cost_is_falsified_on_the_idea() -> None:
    result = validate(
        registration(),
        trades(60, gross=3.0, noise=1.0),
        flat_cost("9"),
        block_length=5,
        n_resamples=1000,
        n_perm=1000,
    )
    assert result.decision.verdict is Verdict.FALSIFIED and result.decision.axis is Axis.IDEA
    entry = to_entry(
        result, registration(), entry_id="V-2", recorded_at=datetime(2026, 9, 13, tzinfo=UTC)
    )
    assert entry.axis_of_death == (Axis.IDEA,)
    assert entry.transferability is Transferability.UNIVERSAL


def test_a_negative_result_without_power_is_not_proven_not_falsified() -> None:
    result = validate(
        registration(power=Power(500, Decimal("8"), "d")),
        trades(60, gross=3.0, noise=1.0),
        flat_cost("9"),
        block_length=5,
        n_resamples=1000,
        n_perm=1000,
    )
    assert result.decision.verdict is Verdict.NOT_PROVEN and result.decision.axis is Axis.POWER
    assert not result.power_achieved
    assert all(f.effective is Verdict.NOT_PROVEN for f in result.decision.fired)


def test_an_edge_that_lives_in_one_subperiod_dies_on_condition() -> None:
    rows = trades(60, gross=0.0, noise=0.5)
    late = [
        TradeRecord(t.instrument, t.entry_ts_ns, t.exit_ts_ns, t.direction, t.weight, Decimal("60"))
        if i >= 80
        else t
        for i, t in enumerate(rows)
    ]
    result = validate(
        registration(
            kill_criteria=(
                KillCriterion("subperiod_min_net_bps", "<=", Decimal("0"), Verdict.FALSIFIED),
            )
        ),
        late,
        flat_cost("0.5"),
        block_length=5,
        n_resamples=1000,
        n_perm=1000,
    )
    assert result.decision.verdict is Verdict.FALSIFIED and result.decision.axis is Axis.CONDITION
    assert min(result.subperiods) < 0 < max(result.subperiods)


def test_validation_refuses_leaks_foreign_instruments_and_unknown_metrics() -> None:
    leaked = trades(40, gross=10.0)
    leaked[0] = TradeRecord(
        "BTCUSDT",
        day_start_ns(date(2025, 7, 2)),
        day_start_ns(date(2025, 7, 2)) + S,
        Direction.LONG,
        Decimal("1"),
        Decimal("1"),
    )
    with pytest.raises(EraLeakageError):
        validate(registration(), leaked, flat_cost("1"), block_length=5, n_resamples=1000)
    foreign = trades(40, gross=10.0)
    foreign[0] = TradeRecord(
        "SOLUSDT", RESEARCH_START, RESEARCH_START + S, Direction.LONG, Decimal("1"), Decimal("1")
    )
    with pytest.raises(ValidationError, match="outside the declared universe"):
        validate(registration(), foreign, flat_cost("1"), block_length=5, n_resamples=1000)
    with pytest.raises(ValidationError, match="does not compute"):
        validate(
            registration(
                kill_criteria=(KillCriterion("sharpe", "<", Decimal("1"), Verdict.FALSIFIED),)
            ),
            trades(40, gross=10.0),
            flat_cost("1"),
            block_length=5,
            n_resamples=1000,
        )
    with pytest.raises(ValidationError, match="no trades"):
        validate(registration(), [], flat_cost("1"), block_length=5)
