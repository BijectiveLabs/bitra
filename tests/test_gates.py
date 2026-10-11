"""The two declared gates: does the context agree with the direction, and is this the trade wanted.

Both are code reading the operator's own declarations. The confluence gate reads the state code
derived from their thresholds, never a model's label, and it fails closed: a role that produced no
state has not agreed to anything. The targets gate counts the intents already on disk, so a process
that restarts cannot forget what it has already done today.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.execution.confluence import (
    ConfluenceError,
    Matrix,
    confluent,
    declared_of,
)
from trading_research_agents.execution.targets import (
    TargetError,
    Targets,
    intents_today,
    reward_risk,
    within_targets,
)

BULLISH = {"declared": "bullish", "as_role_says_it": "risk-on", "series_declared": 1}
BEARISH = {"declared": "bearish", "as_role_says_it": "risk-off", "series_declared": 1}
SILENT = {"declared": "undeclared", "series_declared": 0}
MIXED = {"declared": "mixed", "states": ["bearish", "bullish"], "series_declared": 2}


def test_a_long_needs_the_declared_states_the_operator_asked_for() -> None:
    matrix = Matrix(required=("macro_analyst", "onchain_analyst"))
    agreed = confluent(
        matrix, target="long", states={"macro_analyst": BULLISH, "onchain_analyst": BULLISH}
    )

    assert agreed.admitted and agreed.reasons == ()
    assert agreed.states == {"macro_analyst": "bullish", "onchain_analyst": "bullish"}


def test_one_role_pointing_the_other_way_refuses_and_names_it() -> None:
    matrix = Matrix(required=("macro_analyst", "onchain_analyst"))
    agreed = confluent(
        matrix, target="long", states={"macro_analyst": BULLISH, "onchain_analyst": BEARISH}
    )

    assert not agreed.admitted
    assert any("onchain_analyst declares bearish" in r for r in agreed.reasons)


def test_a_state_nobody_produced_is_not_a_neutral_state() -> None:
    matrix = Matrix(required=("macro_analyst", "sentiment_analyst"))
    agreed = confluent(
        matrix, target="long", states={"macro_analyst": BULLISH, "sentiment_analyst": SILENT}
    )

    assert not agreed.admitted
    assert any("sentiment_analyst declares undeclared" in r for r in agreed.reasons)

    absent = confluent(matrix, target="long", states={"macro_analyst": BULLISH})
    assert not absent.admitted
    assert any("was not asked" in r for r in absent.reasons)
    assert absent.states["sentiment_analyst"] == "not asked"


def test_mixed_declarations_do_not_pass_for_agreement() -> None:
    matrix = Matrix(required=("onchain_analyst",))
    agreed = confluent(matrix, target="short", states={"onchain_analyst": MIXED})

    assert not agreed.admitted and any("declares mixed" in r for r in agreed.reasons)


def test_an_operator_may_allow_a_silent_leg_and_the_record_says_they_did() -> None:
    matrix = Matrix(required=("macro_analyst", "sentiment_analyst"), on_missing="ignore")
    agreed = confluent(
        matrix, target="long", states={"macro_analyst": BULLISH, "sentiment_analyst": SILENT}
    )

    assert agreed.admitted
    assert any("allowed by on_missing = ignore" in r for r in agreed.reasons)


def test_votes_are_counted_against_the_declared_minimums() -> None:
    matrix = Matrix(min_support=2, max_opposition=1)
    thin = confluent(matrix, target="long", states={}, votes=("support", "oppose", "abstain"))

    assert not thin.admitted
    assert thin.support == 1 and thin.opposition == 1 and thin.abstained == 1
    assert any("1 validator(s) support it; 2 required" in r for r in thin.reasons)

    enough = confluent(matrix, target="long", states={}, votes=("support", "support", "abstain"))
    assert enough.admitted

    loud = confluent(
        matrix, target="long", states={}, votes=("support", "support", "oppose", "oppose")
    )
    assert not loud.admitted and any("oppose it" in r for r in loud.reasons)


def test_a_matrix_that_asks_for_nothing_says_so() -> None:
    assert Matrix().idle
    assert not Matrix(required=("macro_analyst",)).idle
    assert not Matrix(min_support=1).idle
    # A declared zero is the strictest setting there is, not the absence of a setting.
    assert not Matrix(max_opposition=0).idle
    refused = confluent(Matrix(max_opposition=0), target="long", states={}, votes=("oppose",))
    assert not refused.admitted


def test_a_matrix_that_cannot_be_applied_is_refused_when_it_is_written() -> None:
    with pytest.raises(ConfluenceError, match="on_missing"):
        Matrix(on_missing="maybe")
    with pytest.raises(ConfluenceError, match="is not a state"):
        Matrix(long_states=("optimistic",))
    with pytest.raises(ConfluenceError, match="at least one state"):
        Matrix(short_states=())
    with pytest.raises(ConfluenceError, match="long or short"):
        confluent(Matrix(), target="flat", states={})


def test_a_declaration_that_is_missing_or_unknown_reads_as_undeclared() -> None:
    assert declared_of(None) == "undeclared"
    assert declared_of({}) == "undeclared"
    assert declared_of({"declared": "optimistic"}) == "undeclared"
    assert declared_of(BEARISH) == "bearish"


def test_the_reward_to_risk_comes_from_the_exit_rule_as_declared() -> None:
    assert reward_risk(stop_frac=Decimal("0.01"), take_frac=Decimal("0.02")) == Decimal("2.000")
    assert reward_risk(stop_frac=Decimal("0.01"), take_frac=None) is None
    with pytest.raises(TargetError, match="stop_frac is positive"):
        reward_risk(stop_frac=Decimal("0"), take_frac=Decimal("0.02"))


def test_every_target_refusal_names_its_figure() -> None:
    targets = Targets(
        min_net_bps=Decimal("10"),
        min_reward_risk=Decimal("1.5"),
        max_round_trips_per_day=2,
    )
    thin = within_targets(
        targets, net_bps=Decimal("4"), reward_to_risk=Decimal("1.2"), round_trips_today=2
    )

    assert not thin.admitted and len(thin.reasons) == 3
    assert any("net 4 bps is below" in r for r in thin.reasons)
    assert any("reward-to-risk 1.2 is below" in r for r in thin.reasons)
    assert any("2 intent(s) already written today" in r for r in thin.reasons)

    good = within_targets(
        targets, net_bps=Decimal("11"), reward_to_risk=Decimal("2"), round_trips_today=1
    )
    assert good.admitted and good.reasons == ()
    assert Decimal(good.record()["net_bps"]) == Decimal("11")


def test_a_ratio_target_without_a_take_profit_cannot_be_met() -> None:
    met = within_targets(
        Targets(min_reward_risk=Decimal("1.5")),
        net_bps=Decimal("20"),
        reward_to_risk=None,
        round_trips_today=0,
    )
    assert not met.admitted and any("no take profit" in r for r in met.reasons)


def test_targets_that_cannot_be_applied_are_refused_when_they_are_written() -> None:
    with pytest.raises(TargetError, match="min_net_bps"):
        Targets(min_net_bps=Decimal("-1"))
    with pytest.raises(TargetError, match="min_reward_risk"):
        Targets(min_reward_risk=Decimal("0"))
    with pytest.raises(TargetError, match="max_round_trips_per_day"):
        Targets(max_round_trips_per_day=-1)
    assert Targets().idle


def test_the_round_trip_count_is_read_from_the_intents_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "cycles"
    rows = [
        {"instrument": "BTCUSDT", "at": "2026-09-28T01-50-35Z", "target": "long"},
        {"instrument": "BTCUSDT", "at": "2026-09-28T02-10-00Z", "target": "short"},
        {"instrument": "ETHUSDT", "at": "2026-09-28T02-11-00Z", "target": "long"},
        {"instrument": "BTCUSDT", "at": "2026-09-27T23-59-00Z", "target": "long"},
    ]
    for number, row in enumerate(rows):
        where = root / f"cycle-{number}"
        where.mkdir(parents=True)
        (where / "intents.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    broken = root / "cycle-broken"
    broken.mkdir()
    (broken / "intents.jsonl").write_text("{not json}\n\n", encoding="utf-8")

    at = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
    assert intents_today(root, at=at, instrument="BTCUSDT") == 2
    assert intents_today(root, at=at, instrument="ETHUSDT") == 1
    assert intents_today(tmp_path / "nothing", at=at, instrument="BTCUSDT") == 0
