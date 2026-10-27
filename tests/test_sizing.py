"""Kelly from the desk's own record and the Monte Carlo drawdown check, all checkable by hand."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_config import agent_raw
from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution.kelly import (
    KellyError,
    SizingRule,
    by_record,
    kelly,
    kelly_cap,
    measure,
    multiples,
    wilson_low,
)
from trading_research_agents.execution.montecarlo import (
    SimulationError,
    largest_within,
    nearest_rank,
    simulate,
)
from trading_research_agents.trading.config import parse_strategy_config
from trading_research_agents.trading.config_values import ConfigError

D = Decimal


def rs(*pairs: tuple[str, int]) -> list[Decimal]:
    out: list[Decimal] = []
    for value, count in pairs:
        out.extend([D(value)] * count)
    return out


def trips(results: list[Decimal]) -> list[dict[str, Any]]:
    return [{"net": "1", "r": str(r)} for r in results]


def test_the_kelly_fraction_is_the_closed_form_on_the_record() -> None:
    # Half the trades win two planned losses, half lose one: f* = (0.5 * 2 - 0.5 * 1) / 2 = 0.25.
    even = measure(rs(("2", 20), ("-1", 20)))
    assert (even.wins, even.losses, even.win_rate) == (20, 20, D("0.5"))
    assert kelly(even) == D("0.25")
    # A flat trade is neither a win nor a loss, and does not move the fraction.
    flat = measure(rs(("2", 10), ("-1", 10), ("0", 5)))
    assert flat.trades == 25 and kelly(flat) == D("0.25")
    # Every trade a win or a loss: the familiar p / L - q / W.
    lopsided = measure(rs(("1.5", 12), ("-0.8", 18)))
    p, q = D(12) / 30, D(18) / 30
    assert kelly(lopsided) == p / D("0.8") - q / D("1.5")


def test_the_win_rate_the_record_supports_is_the_lower_wilson_bound() -> None:
    assert wilson_low(50, 100).quantize(D("0.0001")) == D("0.4188")
    assert wilson_low(0, 30) == 0
    assert wilson_low(30, 30) < 1
    with pytest.raises(KellyError):
        wilson_low(5, 0)


def test_the_cap_waits_for_a_record_and_refuses_one_with_no_edge() -> None:
    short = kelly_cap(rs(("2", 5), ("-1", 5)), fraction=D("0.25"), min_trades=30)
    assert short.cap_bps is None and not short.refuses and "below the 30" in short.reason

    cap = kelly_cap(rs(("2", 20), ("-1", 20)), fraction=D("0.25"), min_trades=30)
    # A quarter of 0.25 of equity at the stop is 625 bps.
    assert cap.cap_bps == D("625.00") and not cap.refuses

    losing = kelly_cap(rs(("1", 10), ("-1", 25)), fraction=D("0.5"), min_trades=30)
    assert losing.refuses and losing.cap_bps is None and "no edge" in losing.reason
    no_win = kelly_cap(rs(("-1", 30)), fraction=D("0.5"), min_trades=30)
    assert no_win.refuses and "none of the last 30" in no_win.reason
    no_loss = kelly_cap(rs(("1", 30)), fraction=D("0.5"), min_trades=30)
    assert not no_loss.refuses and no_loss.cap_bps is None and "no loss" in no_loss.reason
    with pytest.raises(KellyError):
        kelly_cap([], fraction=D("1.5"), min_trades=30)


def test_only_finished_trades_that_know_their_planned_loss_are_counted() -> None:
    rows: list[dict[str, Any]] = [
        {"net": "1.2", "r": "0.8"},
        {"note": "the venue returned no exit fills"},
        {"net": "-2", "r": None},
        {"net": "-2", "r": "not a number"},
        {"net": "-1.1", "r": "-1.05"},
    ]
    assert multiples(rows) == [D("0.8"), D("-1.05")]


def test_a_simulation_is_the_same_on_every_run_and_checkable_on_a_plain_path() -> None:
    record = rs(("2", 20), ("-1", 20))
    first = simulate([float(r) for r in record], 100.0, paths=500, trades=50, seed=11)
    again = simulate([float(r) for r in record], 100.0, paths=500, trades=50, seed=11)
    assert first.record() == again.record()
    other = simulate([float(r) for r in record], 100.0, paths=500, trades=50, seed=12)
    assert other.record() != first.record()

    # Only losses of one planned loss at 100 bps: ten trades leave 0.99 ** 10 of the account.
    ruin = simulate([-1.0], 100.0, paths=100, trades=10, seed=1, room_bps=900.0)
    assert ruin.end_median == pytest.approx(0.99**10)
    assert ruin.drawdown_p95_bps == pytest.approx((1 - 0.99**10) * 10_000)
    assert ruin.reach_share == 1.0
    assert simulate([-1.0], 0.0, paths=100, trades=10, seed=1).end_median == 1.0
    with pytest.raises(SimulationError):
        simulate([], 10.0, paths=10, trades=10, seed=1)


def test_the_percentile_is_nearest_rank_in_integer_arithmetic() -> None:
    values = [float(i) for i in range(1, 2001)]
    assert nearest_rank(values, 95) == 1900.0 and nearest_rank(values, 50) == 1000.0
    assert nearest_rank([3.0], 5) == 3.0


def test_the_risk_is_cut_until_the_drawdown_limit_is_out_of_reach() -> None:
    record = rs(("2", 20), ("-1", 20))
    roomy, seen = largest_within(
        record, D("100"), room_bps=D("5000"), tolerance=D("0.05"), paths=500, trades=50, seed=3
    )
    assert roomy == D("100") and seen.reach_share is not None and seen.reach_share <= 0.05

    tight, near = largest_within(
        record, D("625"), room_bps=D("1000"), tolerance=D("0.05"), paths=500, trades=50, seed=3
    )
    assert D(0) < tight < D("625")
    assert near.risk_bps == float(tight) and near.reach_share is not None
    assert near.reach_share <= 0.05
    # A hundredth more, and the chance is over the tolerance.
    over = simulate(
        [float(r) for r in record],
        float(tight) + 0.02,
        paths=500,
        trades=50,
        seed=3,
        room_bps=1000.0,
    )
    assert over.reach_share is not None and over.reach_share > 0.05

    spent, _ = largest_within(
        record, D("625"), room_bps=D("0"), tolerance=D("0.05"), paths=500, trades=50, seed=3
    )
    assert spent == 0


def test_the_record_lowers_the_declared_risk_and_never_raises_it() -> None:
    rule = SizingRule(kelly_fraction=D("0.25"), simulate_paths=500, simulate_trades=50)
    sized = by_record(rule, trips(rs(("2", 20), ("-1", 20))), room_bps=D("1000"))
    assert not sized.refuses and sized.cap_bps is not None and sized.cap_bps < D("625")
    assert "cut to" in sized.reason and sized.simulation is not None
    record = sized.record()
    assert record["kelly"]["kelly"] == "0.2500" and record["rule"]["seed"] == rule.seed

    # Without a drawdown limit the simulation is reported and the Kelly ceiling stands.
    free = by_record(rule, trips(rs(("2", 20), ("-1", 20))))
    assert free.cap_bps == D("625.00") and free.simulation is not None

    # Only the newest lookback_round_trips count: an old losing run is forgotten.
    recent = SizingRule(kelly_fraction=D("0.25"), lookback_round_trips=40, simulate_paths=500)
    history = trips(rs(("-1", 60)) + rs(("2", 20), ("-1", 20)))
    assert by_record(recent, history).kelly.full == D("0.25")


def test_the_sizing_table_is_checked_like_every_other() -> None:
    def parsed(table: dict[str, Any]) -> SizingRule:
        return parse_strategy_config(agent_raw(sizing=table)).sizing

    rule = parsed({"kelly_fraction": "0.25", "min_round_trips": 40, "lookback_round_trips": 80})
    assert rule.kelly_fraction == D("0.25") and rule.min_round_trips == 40 and not rule.idle
    with pytest.raises(ConfigError, match="unknown"):
        parsed({"kelly_fraction": "0.25", "full_kelly": True})
    with pytest.raises(ConfigError, match="without kelly_fraction"):
        parsed({"min_round_trips": 40})
    with pytest.raises(ConfigError, match="at most 1"):
        parsed({"kelly_fraction": "2"})
    with pytest.raises(ConfigError, match="at least 10"):
        parsed({"kelly_fraction": "0.25", "min_round_trips": 3})
    assert SizingRule().idle
    # A misspelt table used to be ignored, which left sizing off without a word.
    with pytest.raises(ConfigError, match=r"\[sizng\] \(did you mean \[sizing\]\?\)"):
        parse_strategy_config({**agent_raw(), "sizng": {"kelly_fraction": "0.25"}})


def write_trips(root: Path, results: list[Decimal]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row) for row in trips(results)]
    (root / "round_trips.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_cycle_refuses_when_the_desks_own_record_shows_no_edge(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=D("10000"),
        stop_frac=D("0.01"),
        take_frac=D("0.02"),
        size_step=D("0.001"),
        max_risk_per_trade_bps=D("50"),
        sizing=SizingRule(kelly_fraction=D("0.25"), simulate_paths=200, simulate_trades=20),
    )
    write_trips(cfg.root, rs(("1", 10), ("-1", 25)))
    result = run_cycle(cfg)
    assert not result.acted
    assert any(r.startswith("sizing: ") and "no edge" in r for r in result.refusals)
    assert result.briefs["sizing"]["refuses"] is True


def test_a_cycle_risks_no_more_than_the_record_supports(tmp_path: Path, envelope: Envelope) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=D("10000"),
        stop_frac=D("0.01"),
        take_frac=D("0.02"),
        size_step=D("0.001"),
        max_risk_per_trade_bps=D("50"),
        # Kelly 0.25 at a hundredth of it is 25 bps, below the 50 declared.
        sizing=SizingRule(kelly_fraction=D("0.01"), simulate_paths=200, simulate_trades=20),
    )
    write_trips(cfg.root, rs(("2", 20), ("-1", 20)))
    result = run_cycle(cfg)
    assert result.risk is not None and result.risk.allowed_bps == D("25.00")
    # The same figures are in the record a person reads later, not only in memory.
    assert result.record()["sizing"]["cap_bps"] == "25.00"
    assert result.risk.risk_bps is not None and result.risk.risk_bps <= D("25")
    assert result.intent is not None and result.intent["loss_at_stop_bps"] is not None


def test_tra_sizing_reports_what_a_cycle_would_compute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.cli.main import main

    target = tmp_path / "desk"
    assert main(["init", str(target), "--profile", "autonomous", "--venue", "bybit"]) == 0
    runs = target / "runs" / "cycles"
    rows = trips(rs(("2", 20), ("-1.05", 22)))
    for row in rows:
        loss = row["r"].startswith("-")
        row["excursion"] = {
            "mfe_bps": "15" if loss else "210",
            "mae_bps": "-101" if loss else "-30",
        }
    runs.mkdir(parents=True)
    (runs / "round_trips.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    capsys.readouterr()
    args = ["sizing", "--config", str(target / "strategy.toml"), "--runs", str(runs), "--json"]
    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["declared"] is False and payload["round_trips"] == 42
    assert payload["sizing"]["kelly"]["kelly"] == "0.1916"
    assert payload["excursions"]["measured"] == 42
    # The same answer as the cycle's own function on the same rows and room.
    rule = SizingRule(kelly_fraction=D("0.25"))
    room = D(payload["room_bps"])
    assert payload["sizing"] == by_record(rule, rows, room_bps=room).record()


def test_a_rung_above_one_is_priced_at_its_own_size_before_the_risk_check(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.conviction import Conviction, Tier
    from trading_research_agents.execution.risk import risk_bps_of

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=D("10000"),
        stop_frac=D("0.01"),
        take_frac=D("0.02"),
        size_step=D("0.001"),
        max_risk_per_trade_bps=D("500"),
        max_position=D("20"),
        conviction=Conviction(ladder=(Tier(at_least=D(0), size_multiple=D(2)),)),
    )
    result = run_cycle(cfg)
    assert result.stake is not None and result.risk is not None and result.verdict is not None
    assert result.risk.size > cfg.size
    # Twice the base walks deeper into the book; the loss at the stop is priced on that walk.
    deeper = result.verdict.cost.round_trip_bps
    stop = D(result.exits["stop_price"])
    mid = result.verdict.cost.mid
    expected = risk_bps_of(
        size=result.risk.size, entry=mid, stop=stop, equity=D("10000"), cost_bps=deeper
    )
    assert result.risk.risk_bps == expected


def test_the_cap_is_decided_on_the_exact_figure_not_the_printed_one() -> None:
    from trading_research_agents.execution.risk import within_risk

    # 5.0004 units one price point from the stop on 10000 of equity: 5.0004 bps, printed 5.00.
    over = within_risk(
        size=D("5.0004"),
        entry=D("100"),
        stop=D("99"),
        equity=D("10000"),
        allowed_bps=D("5"),
        step=D("0.0001"),
    )
    assert over.size == D("5.0000") and over.reasons


def test_sizing_figures_that_are_not_numbers_are_refused_as_configuration() -> None:
    with pytest.raises(ConfigError, match="finite"):
        parse_strategy_config(agent_raw(sizing={"kelly_fraction": "nan"}))
    with pytest.raises(ConfigError, match="between 100 and 5000"):
        parse_strategy_config(agent_raw(sizing={"kelly_fraction": "0.25", "simulate_paths": 20000}))


def test_tra_sizing_refuses_a_fraction_that_is_not_one(tmp_path: Path) -> None:
    from trading_research_agents.cli.main import main

    with pytest.raises(SystemExit):
        main(["sizing", "--fraction", "abc"])
    with pytest.raises(SystemExit):
        main(["sizing", "--fraction", "1.5"])


def test_fills_are_set_against_what_they_were_priced_at() -> None:
    from trading_research_agents.execution.reconcile import reconcile

    rows = [
        {"net": "1", "entry_slippage_bps": "0.40", "fees_bps": "11.20", "priced_fee_bps": "11.00"},
        {
            "net": "-1",
            "entry_slippage_bps": "-0.10",
            "fees_bps": "11.00",
            "priced_fee_bps": "11.00",
        },
        {"net": "2", "entry_slippage_bps": "1.30", "fees_bps": "12.00", "priced_fee_bps": "11.00"},
        {"note": "the venue returned no exit fills for this position"},
    ]
    found = reconcile(rows)
    assert found["round_trips"] == 3
    assert found["entry_slippage"] == {"measured": 3, "median_bps": "0.40", "worse_than_priced": 2}
    assert found["fees"] == {"measured": 3, "median_bps": "11.20", "priced_median_bps": "11.00"}
    assert reconcile([])["entry_slippage"]["median_bps"] is None
