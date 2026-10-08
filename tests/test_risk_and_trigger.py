"""Two steps a risk manager owns, and the check that comes before anyone is paid to think.

Risk per trade is arithmetic over the operator's declarations: the loss at the stop against equity,
and the size that fits it. The trigger is the same kind of thing at the other end of the cycle - it
decides from figures that cost nothing whether the roster should be asked at all.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution.risk import (
    RiskError,
    risk_bps_of,
    size_for_risk,
    within_risk,
)
from trading_research_agents.execution.targets import (
    Targets,
    edge_multiple,
    research_bps,
    within_targets,
)
from trading_research_agents.execution.trigger import (
    Seen,
    Trigger,
    TriggerError,
    last_seen,
    required_move,
    spread_of,
    worth_asking,
)

STEP = Decimal("0.000001")


def test_the_loss_at_the_stop_is_what_risk_per_trade_measures() -> None:
    # 1.02 away from 102.00, five units, against ten thousand: 5.1 bps of the account.
    assert risk_bps_of(
        size=Decimal("5"), entry=Decimal("102"), stop=Decimal("100.98"), equity=Decimal("10000")
    ) == Decimal("5.10")
    assert size_for_risk(
        allowed_bps=Decimal("3"),
        entry=Decimal("102"),
        stop=Decimal("100.98"),
        equity=Decimal("10000"),
        step=Decimal("0.001"),
    ) == Decimal("2.941")
    with pytest.raises(RiskError, match="no risk to size from"):
        size_for_risk(
            allowed_bps=Decimal("3"),
            entry=Decimal("102"),
            stop=Decimal("102"),
            equity=Decimal("10000"),
            step=STEP,
        )


def test_a_size_above_the_risk_allowance_is_cut_to_fit_and_says_so() -> None:
    cut = within_risk(
        size=Decimal("5"),
        entry=Decimal("102"),
        stop=Decimal("100.98"),
        equity=Decimal("10000"),
        allowed_bps=Decimal("3"),
        step=Decimal("0.001"),
    )
    assert cut.admitted and cut.size == Decimal("2.941")
    assert cut.risk_bps == Decimal("3.00") and "cut to 2.941" in cut.reasons[0]

    inside = within_risk(
        size=Decimal("5"),
        entry=Decimal("102"),
        stop=Decimal("100.98"),
        equity=Decimal("10000"),
        allowed_bps=Decimal("6"),
        step=Decimal("0.001"),
    )
    assert inside.admitted and inside.size == Decimal("5") and inside.reasons == ()


def test_a_risk_allowance_smaller_than_one_step_refuses_rather_than_rounding_up() -> None:
    refused = within_risk(
        size=Decimal("5"),
        entry=Decimal("102"),
        stop=Decimal("100.98"),
        equity=Decimal("10000"),
        allowed_bps=Decimal("1"),
        step=Decimal("1"),
    )
    assert not refused.admitted and refused.size == Decimal(0)
    assert "below one step of the venue" in refused.reasons[0]


def test_nothing_declared_passes_the_size_through_untouched() -> None:
    for missing in ({"allowed_bps": None}, {"equity": None}, {"stop": None}):
        kwargs: dict[str, Any] = {
            "size": Decimal("5"),
            "entry": Decimal("102"),
            "stop": Decimal("100.98"),
            "equity": Decimal("10000"),
            "allowed_bps": Decimal("3"),
            **missing,
        }
        verdict = within_risk(**kwargs)
        assert verdict.admitted and verdict.size == Decimal("5")


def test_the_edge_has_to_be_a_multiple_of_the_cost_not_a_little_over_it() -> None:
    assert edge_multiple(edge_bps=Decimal("20"), cost_bps=Decimal("18")) == Decimal("1.11")
    assert edge_multiple(edge_bps=Decimal("20"), cost_bps=Decimal("0")) is None
    targets = Targets(min_edge_multiple=Decimal("2"))
    thin = within_targets(
        targets,
        net_bps=Decimal("2"),
        reward_to_risk=None,
        round_trips_today=0,
        over_cost=Decimal("1.11"),
    )
    assert not thin.admitted and "1.11x the round trip" in thin.reasons[0]
    fat = within_targets(
        targets,
        net_bps=Decimal("40"),
        reward_to_risk=None,
        round_trips_today=0,
        over_cost=Decimal("3.2"),
    )
    assert fat.admitted and fat.edge_multiple == Decimal("3.2")
    unpriced = within_targets(
        targets, net_bps=Decimal("40"), reward_to_risk=None, round_trips_today=0, over_cost=None
    )
    assert not unpriced.admitted and "priced at zero" in unpriced.reasons[0]


def test_the_models_own_cost_is_measured_against_the_notional_they_produced() -> None:
    # Thirty cents of calls against a two hundred dollar trade is 15 bps of the trade itself.
    assert research_bps(spend=Decimal("0.30"), notional=Decimal("200")) == Decimal("15.00")
    assert research_bps(spend=Decimal("0.30"), notional=Decimal(0)) is None
    verdict = within_targets(
        Targets(max_research_bps=Decimal("10")),
        net_bps=Decimal("40"),
        reward_to_risk=None,
        round_trips_today=0,
        research=Decimal("15.00"),
    )
    assert not verdict.admitted and "15.00 bps of this trade's own notional" in verdict.reasons[0]


def test_a_quiet_market_is_not_worth_asking_about() -> None:
    trigger = Trigger(min_move_bps=Decimal("20"))
    seen = Seen(mid=Decimal("102"), at=datetime(2026, 9, 27, 9, 55, tzinfo=UTC))
    quiet = worth_asking(
        trigger,
        mid=Decimal("102.05"),
        spread_bps=Decimal("9.8"),
        seen=seen,
        now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
    )
    assert not quiet.ask and quiet.moved_bps == Decimal("4.90")
    assert "nothing new to read" in quiet.reasons[0]

    moved = worth_asking(
        trigger,
        mid=Decimal("103"),
        spread_bps=Decimal("9.8"),
        seen=seen,
        now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
    )
    assert moved.ask and moved.reasons == () and moved.moved_bps == Decimal("98.04")


def test_a_last_look_from_long_ago_is_a_first_look_not_a_move() -> None:
    trigger = Trigger(min_move_bps=Decimal("20"))
    seen = Seen(mid=Decimal("100"), at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC))
    wake = worth_asking(
        trigger,
        mid=Decimal("103"),
        spread_bps=Decimal("1"),
        seen=seen,
        now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
    )
    assert wake.ask and wake.moved_bps is None

    kept = worth_asking(
        Trigger(min_move_bps=Decimal("20"), look_valid_s=None),
        mid=Decimal("103"),
        spread_bps=Decimal("1"),
        seen=seen,
        now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
    )
    assert kept.moved_bps == Decimal("300.00")


def test_the_passes_already_written_are_the_record_of_every_look(tmp_path: Path) -> None:
    from trading_research_agents.execution.trigger import previous_looks

    screens = tmp_path / "screens"
    screens.mkdir()
    for name, mid in (("2026-10-07T14-00-00Z", "10"), ("2026-10-07T14-30-00Z", "11")):
        rows = [{"instrument": "NEARUSDT", "mid": mid}, {"instrument": "ADAUSDT", "mid": None}]
        (screens / f"{name}.json").write_text(json.dumps({"candidates": rows}), encoding="utf-8")
    looks = previous_looks(screens)
    assert looks["NEARUSDT"] == (Decimal("11"), datetime(2026, 10, 7, 14, 30, tzinfo=UTC))
    assert "ADAUSDT" not in looks and previous_looks(tmp_path / "none") == {}


def test_nothing_to_compare_against_asks_rather_than_holding_forever() -> None:
    wake = worth_asking(
        Trigger(min_move_bps=Decimal("20")),
        mid=None,
        spread_bps=None,
        seen=Seen(),
        now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
    )
    assert wake.ask and wake.moved_bps is None


def test_a_run_of_refusals_raises_the_desks_own_bar_and_one_move_lowers_it() -> None:
    trigger = Trigger(min_move_bps=Decimal("10"), widen_after=3)
    assert required_move(trigger, refusals=0) == Decimal("10")
    assert required_move(trigger, refusals=3) == Decimal("20")
    assert required_move(trigger, refusals=6) == Decimal("50")
    assert required_move(trigger, refusals=99) == Decimal("80")  # capped, never blind
    held = worth_asking(
        trigger,
        mid=Decimal("102.15"),
        spread_bps=Decimal("1"),
        seen=Seen(mid=Decimal("102"), refusals=4),
        now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
    )
    assert not held.ask and held.required_bps == Decimal("30")
    assert "widened by 4 refusals" in held.reasons[0]
    with pytest.raises(TriggerError, match="min_move_bps comes with it"):
        Trigger(widen_after=2)


def test_a_book_too_wide_a_cooldown_and_a_spent_day_all_hold_before_a_call() -> None:
    now = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    wide = worth_asking(
        Trigger(max_spread_bps=Decimal("5")),
        mid=Decimal("102"),
        spread_bps=Decimal("9.8"),
        seen=Seen(),
        now=now,
    )
    assert not wide.ask and "9.8 bps wide" in wide.reasons[0]

    cooling = worth_asking(
        Trigger(cooldown_s=900),
        mid=Decimal("102"),
        spread_bps=Decimal("1"),
        seen=Seen(last_intent_at=datetime(2026, 9, 27, 9, 55, tzinfo=UTC)),
        now=now,
    )
    assert not cooling.ask and "300s ago" in cooling.reasons[0]

    spent = worth_asking(
        Trigger(cooldown_s=60),
        mid=Decimal("102"),
        spread_bps=Decimal("1"),
        seen=Seen(),
        now=now,
        round_trips_today=6,
        max_round_trips_per_day=6,
    )
    assert not spent.ask and "could be acted on" in spent.reasons[0]


def test_the_spread_of_a_one_sided_or_crossed_book_is_nothing() -> None:
    mid, spread = spread_of(Decimal("101.95"), Decimal("102.05"))
    assert mid == Decimal("102") and spread == Decimal("9.80")
    assert spread_of(None, Decimal("102")) == (None, None)
    assert spread_of(Decimal("103"), Decimal("102")) == (None, None)


def test_what_the_cycles_on_disk_say_survives_a_restart(tmp_path: Path) -> None:
    root = tmp_path / "cycles"
    for name, payload in (
        (
            "2026-09-27T09-00-00Z",
            {
                "instrument": "BTCUSDT",
                "seen": {"mid": "100"},
                "intent": {"at": "2026-09-27T09-00-00Z"},
            },
        ),
        ("2026-09-27T09-30-00Z", {"instrument": "BTCUSDT", "seen": {"mid": "101"}, "intent": None}),
        ("2026-09-27T09-45-00Z", {"instrument": "ETHUSDT", "seen": {"mid": "3"}, "intent": None}),
        ("2026-09-27T09-55-00Z", {"instrument": "BTCUSDT", "seen": {"mid": "102"}, "intent": None}),
    ):
        where = root / name
        where.mkdir(parents=True)
        (where / "cycle.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "broken").mkdir()
    (root / "broken" / "cycle.json").write_text("{not json", encoding="utf-8")

    seen = last_seen(root, instrument="BTCUSDT")
    assert seen.mid == Decimal("102")  # the newest cycle for this instrument, not for ETHUSDT
    assert seen.refusals == 2  # the two since the last intent
    assert seen.last_intent_at == datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
    assert last_seen(root, instrument="SOLUSDT") == Seen()


def test_a_cycle_cuts_the_size_to_the_declared_risk_and_the_intent_says_so(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=Decimal("10000"),
        stop_frac=Decimal("0.01"),
        take_frac=Decimal("0.02"),
        size_step=Decimal("0.001"),
        max_risk_per_trade_bps=Decimal("3"),
    )
    result = run_cycle(cfg)

    assert result.risk is not None and result.risk.admitted
    assert result.risk.size == Decimal("2.941") and result.risk.risk_bps == Decimal("3.00")
    assert result.acted and result.intent is not None
    assert result.intent["size"] == "2.941" and result.intent["risk_bps"] == "3.00"
    assert any("would risk 5.10 bps" in r for r in result.refusals)


def test_a_declared_risk_limit_with_no_stop_refuses_rather_than_sending_an_unchecked_size(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=Decimal("10000"),
        max_risk_per_trade_bps=Decimal("3"),
    )
    result = run_cycle(cfg)

    assert not result.acted
    assert any("declare [exit].stop_frac" in r for r in result.refusals)


def test_a_held_cycle_pays_for_one_data_pull_and_nothing_else(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, fake, usage = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        trigger=Trigger(min_move_bps=Decimal("50")),
    )
    first = run_cycle(cfg)
    assert first.acted and len(fake.calls) == 1

    # Same book, five minutes later: the mid has not moved, so nobody is asked a second time. The
    # scripted model has no second answer, so a call would have shown up as a failure.
    later = run_cycle(replace(cfg, clock=lambda: datetime(2026, 9, 27, 10, 5, tzinfo=UTC)))
    assert later.held and not later.acted and len(fake.calls) == 1
    assert later.wake is not None and later.wake.moved_bps == Decimal("0.00")
    assert any("trigger: the mid has moved 0.00 bps" in r for r in later.refusals)
    assert later.record()["seen"]["mid"] == "102.00"
    assert usage.tokens > 0  # the first cycle's tokens, not a second cycle's


def test_a_cycle_the_trigger_held_is_not_counted_as_the_desk_finding_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cycles"
    for name, payload in (
        ("2026-09-27T09-00-00Z", {"instrument": "BTCUSDT", "seen": {"mid": "100"}, "intent": None}),
        (
            "2026-09-27T09-05-00Z",
            {"instrument": "BTCUSDT", "seen": {"mid": "100"}, "intent": None, "held": True},
        ),
        (
            "2026-09-27T09-10-00Z",
            {"instrument": "BTCUSDT", "seen": {"mid": "100"}, "intent": None, "held": True},
        ),
    ):
        where = root / name
        where.mkdir(parents=True)
        (where / "cycle.json").write_text(json.dumps(payload), encoding="utf-8")

    # One cycle looked and found nothing; two were held before anyone looked. Counting those would
    # let the trigger widen its own bar every few minutes of quiet until nothing could wake it.
    assert last_seen(root, instrument="BTCUSDT").refusals == 1


def test_the_operator_can_ask_anyway_and_the_cycle_says_so(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        trigger=Trigger(min_move_bps=Decimal("50")),
        forced=True,
    )
    first = run_cycle(cfg)
    assert first.acted

    forced, _, _ = cycle_config(
        tmp_path / "again",
        envelope,
        {"technical_analyst": analyst_answer()},
        trigger=Trigger(min_move_bps=Decimal("50")),
        forced=True,
    )
    result = run_cycle(forced)
    assert result.acted and not result.held
    assert result.wake is not None and "asked with --now" in result.wake.reasons[0]


def test_the_deciding_analyst_has_to_name_a_setup_and_point_at_a_figure(
    tmp_path: Path, envelope: Envelope
) -> None:
    uncited, _, _ = cycle_config(
        tmp_path / "uncited",
        envelope,
        {"technical_analyst": analyst_answer(cited=[{"figure": "zscore", "value": "-9999"}])},
    )
    result = run_cycle(uncited)
    assert not result.acted
    assert any("discarded a citation" in r for r in result.refusals)
    assert any("rests on nothing this cycle measured" in r for r in result.refusals)

    shapeless, _, _ = cycle_config(
        tmp_path / "shapeless",
        envelope,
        {"technical_analyst": analyst_answer(setup="no_setup")},
    )
    result = run_cycle(shapeless)
    assert not result.acted
    assert any("with no setup named" in r for r in result.refusals)


def test_an_intent_carries_the_setup_and_the_figures_the_direction_came_from(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path, envelope, {"technical_analyst": analyst_answer(setup="range_breakout")}
    )
    result = run_cycle(cfg)

    assert result.intent is not None
    assert result.intent["setup"] == "range_breakout"
    assert result.intent["cited"] == [{"figure": "interval_s", "value": "60"}]
    assert result.record()["analyst"]["setup"] == "range_breakout"


def test_a_watcher_sees_every_step_as_it_happens_and_can_change_none_of_them(
    tmp_path: Path, envelope: Envelope
) -> None:
    steps: list[tuple[str, str]] = []

    def watch(step: Any) -> None:
        steps.append((step.name, step.state))
        raise RuntimeError("a broken progress bar is not a reason to lose a decision")

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        watch=watch,
        trigger=Trigger(min_move_bps=Decimal("5")),
    )
    result = run_cycle(cfg)

    assert result.acted
    seen = dict(steps)
    assert seen["data"] == "done" and seen["trigger"] == "done"
    assert ("technical", "running") in steps and seen["technical"] == "done"
    assert seen["cost"] == "done" and seen["cost gate"] == "done" and seen["intent"] == "done"
