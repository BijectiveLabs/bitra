"""Stops measured from the market: the range, the clamp, the config, and the cycle using them."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_config import agent_raw
from test_cycle import MINUTE_NS, analyst_answer, chains, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.data.schema import Candle
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.stops import StopError, VolatilityExit, average_true_range
from trading_research_agents.trading.config import ConfigError, parse_strategy_config


def bar(i: int, high: str, low: str, close: str) -> Candle:
    return Candle(
        start_ns=i * MINUTE_NS,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(1),
    )


BARS = [
    bar(0, "101", "99", "100"),
    bar(1, "102", "100", "101"),  # range 2
    bar(2, "101", "97", "98"),  # range 4
    bar(3, "104", "99", "103"),  # high against the last close: 6
    bar(4, "104", "102", "103"),  # range 2
]


def test_the_average_true_range_is_wilders_and_counts_a_gap() -> None:
    # True ranges 2, 4, 6, 2. Seed over three: 4. Then (4 * 2 + 2) / 3.
    assert average_true_range(BARS, 3) == Decimal(10) / 3
    with pytest.raises(StopError, match="needs 5"):
        average_true_range(BARS[:4], 4)
    flat = [bar(i, "100", "100", "100") for i in range(4)]
    with pytest.raises(StopError, match="did not move"):
        average_true_range(flat, 2)


def test_the_stop_grows_with_the_horizon_and_is_held_inside_its_bounds() -> None:
    rule = VolatilityExit(
        stop_atr=Decimal(1),
        reward_to_risk=Decimal(2),
        min_stop_frac=Decimal("0.001"),
        max_stop_frac=Decimal("0.5"),
        atr_bars=3,
    )
    one = rule.place(BARS, horizon_bars=1)
    four = rule.place(BARS, horizon_bars=4)
    atr_frac = Decimal(10) / 3 / Decimal(103)
    assert one.stop_frac == atr_frac and one.clamped_to is None
    assert four.stop_frac == atr_frac * 2
    assert four.take_frac == four.stop_frac * 2

    tight = VolatilityExit(min_stop_frac=Decimal("0.2"), max_stop_frac=Decimal("0.3"), atr_bars=3)
    floor = tight.place(BARS, horizon_bars=1)
    assert floor.stop_frac == Decimal("0.2") and floor.clamped_to == "min_stop_frac"
    assert floor.record()["measured_stop_bps"] != floor.record()["stop_bps"]
    wide = VolatilityExit(max_stop_frac=Decimal("0.01"), atr_bars=3).place(BARS, horizon_bars=9)
    assert wide.stop_frac == Decimal("0.01") and wide.clamped_to == "max_stop_frac"


def test_a_rule_that_cannot_place_a_stop_is_refused_when_written() -> None:
    with pytest.raises(StopError, match="stop_atr is a positive"):
        VolatilityExit(stop_atr=Decimal(0))
    with pytest.raises(StopError, match="is above"):
        VolatilityExit(min_stop_frac=Decimal("0.05"), max_stop_frac=Decimal("0.01"))
    with pytest.raises(StopError, match="below 1"):
        VolatilityExit(max_stop_frac=Decimal("1.5"))
    with pytest.raises(StopError, match="at least 2"):
        VolatilityExit(atr_bars=1)


def exit_table(**keys: Any) -> dict[str, Any]:
    return {
        "timeout_bars": 240,
        "mechanism_bound": "The dislocation resolves within a few hours and is gone after that.",
        **keys,
    }


def test_the_config_reads_volatility_mode_and_refuses_a_mixed_one() -> None:
    cfg = parse_strategy_config(agent_raw(exit=exit_table(mode="volatility", stop_atr=2)))
    rule = cfg.exit_rule.volatility
    assert rule is not None and rule.stop_atr == 2 and rule.atr_bars == 14
    # The widest stop stands in wherever one number is needed up front.
    assert cfg.exit_rule.stop_frac == rule.max_stop_frac
    assert cfg.exit_rule.take_profit_frac == rule.max_stop_frac * rule.reward_to_risk

    fixed = parse_strategy_config(agent_raw(exit=exit_table(stop_frac=Decimal("0.01"))))
    assert fixed.exit_rule.volatility is None

    with pytest.raises(ConfigError, match="remove them"):
        parse_strategy_config(
            agent_raw(exit=exit_table(mode="volatility", stop_frac=Decimal("0.01")))
        )
    with pytest.raises(ConfigError, match='apply with mode = "volatility"'):
        parse_strategy_config(agent_raw(exit=exit_table(stop_frac=Decimal("0.01"), stop_atr=2)))
    with pytest.raises(ConfigError, match="mode is one of"):
        parse_strategy_config(agent_raw(exit=exit_table(mode="atr")))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(exit=exit_table(stop_frac=Decimal("0.01"), stopfrac=1)))
    code_core = agent_raw(exit=exit_table(mode="volatility"))
    code_core["strategy"] = {"file": "strategies/trailing_zscore.py", "class": "TrailingZScore"}
    with pytest.raises(ConfigError, match="placed by the agent cycle"):
        parse_strategy_config(code_core)
    with pytest.raises(ConfigError, match="is above"):
        parse_strategy_config(
            agent_raw(exit=exit_table(mode="volatility", min_stop_frac=Decimal("0.5")))
        )


SWINGING = ["100", "103", "98", "102", "99", "101", "100", "104"]


def test_a_cycle_in_volatility_mode_places_and_records_a_measured_stop(
    tmp_path: Path, envelope: Envelope
) -> None:
    rule = VolatilityExit(
        stop_atr=Decimal(1),
        min_stop_frac=Decimal("0.001"),
        max_stop_frac=Decimal("0.5"),
        atr_bars=3,
    )
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer(horizon=4)],
        chains=chains(closes=SWINGING),
        lookback_bars=8,
        volatility_exit=rule,
    )
    result = run_cycle(cfg)

    assert result.intent is not None, result.refusals
    measured = result.exits["measured"]
    assert measured["mode"] == "volatility" and measured["horizon_bars"] == 4
    mid = Decimal(result.intent["mid"])
    stop = Decimal(measured["stop_bps"]) / 10_000
    # The record rounds to a hundredth of a bp and the price to the book's tick.
    tick = Decimal("0.01")
    assert abs(Decimal(result.intent["stop_price"]) - mid * (1 - stop)) <= tick
    assert abs(Decimal(result.intent["take_price"]) - mid * (1 + 2 * stop)) <= tick
    assert measured["clamped_to"] is None
    from trading_research_agents.cli.main import _cycle_report

    _, lines = _cycle_report(result, cfg.usage, "sandbox")
    assert any(line.startswith("exits measured: ATR ") for line in lines)


def test_a_stop_that_cannot_be_measured_refuses_the_trade(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path, envelope, [analyst_answer()], volatility_exit=VolatilityExit(atr_bars=14)
    )
    result = run_cycle(cfg)

    assert result.intent is None
    assert any(r.startswith("exit:") and "needs 15" in r for r in result.refusals)


def test_a_take_profit_inside_the_round_trip_refuses_the_trade(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        stop_frac=Decimal("0.01"),
        take_frac=Decimal("0.0001"),
    )
    result = run_cycle(cfg)

    assert result.intent is None
    assert any("a winner would lose money" in r for r in result.refusals)


def test_the_intent_says_what_share_of_trades_must_win_to_break_even(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        stop_frac=Decimal("0.003"),
        take_frac=Decimal("0.006"),
    )
    result = run_cycle(cfg)
    assert result.intent is not None
    assert result.verdict is not None
    cost = result.verdict.cost.round_trip_bps
    take = Decimal(result.exits["take_pnl_bps"])
    stop = -Decimal(result.exits["stop_pnl_bps"])
    expected = ((stop + cost) / (take + stop)).quantize(Decimal("0.001"))
    assert Decimal(result.intent["break_even_win_rate"]) == expected
    # A 2:1 bracket needs a third of trades to win before costs, and more once they are paid.
    assert expected > Decimal("0.334")


def analyst_rule(**keys: Any) -> VolatilityExit:
    base: dict[str, Any] = {
        "stop_atr": Decimal(1),
        "min_stop_frac": Decimal("0.002"),
        "max_stop_frac": Decimal("0.05"),
        "atr_bars": 3,
        "analyst_levels": True,
        # These bars are wide on purpose; the noise floor has its own test below.
        "min_stop_atr": Decimal(0),
    }
    return VolatilityExit(**{**base, **keys})


def test_a_named_stop_inside_one_bars_range_is_widened_to_it() -> None:
    rule = analyst_rule(min_stop_atr=Decimal(1))
    mid = Decimal(100)
    atr = average_true_range(BARS, 3)
    placed = rule.place(
        BARS, horizon_bars=1, mid=mid, side="long", stop_price=mid - atr / 4, target_price=None
    )
    assert placed.clamped_to == "min_stop_atr" and placed.source == "analyst"
    assert abs(placed.stop_frac - atr / BARS[-1].close) < Decimal("0.000001")
    assert "ATR" in placed.notes[0]
    with pytest.raises(StopError, match="min_stop_atr"):
        analyst_rule(min_stop_atr=Decimal(-1))


def test_the_analysts_levels_are_used_inside_the_operators_bounds() -> None:
    rule = analyst_rule()
    mid = Decimal(100)
    named = rule.place(
        BARS,
        horizon_bars=1,
        mid=mid,
        side="long",
        stop_price=Decimal(98),
        target_price=Decimal(105),
    )
    assert named.source == "analyst" and named.stop_frac == Decimal("0.02")
    assert named.take_frac == Decimal("0.05") and named.clamped_to is None
    assert named.record()["proposed_stop_bps"] == "200.00"

    short = rule.place(
        BARS,
        horizon_bars=1,
        mid=mid,
        side="short",
        stop_price=Decimal(101),
        target_price=Decimal(97),
    )
    assert short.stop_frac == Decimal("0.01") and short.take_frac == Decimal("0.03")

    tight = rule.place(
        BARS,
        horizon_bars=1,
        mid=mid,
        side="long",
        stop_price=Decimal("99.95"),
        target_price=Decimal("100.5"),
    )
    # 5 bps is inside the floor: the stop is widened to it, so the position will be smaller.
    assert tight.stop_frac == Decimal("0.002") and tight.clamped_to == "min_stop_frac"

    far = rule.place(
        BARS,
        horizon_bars=1,
        mid=mid,
        side="long",
        stop_price=Decimal(99),
        target_price=Decimal(120),
    )
    assert far.take_frac == Decimal("0.04") and "max_reward_to_risk" in far.notes[0]

    open_ended = rule.place(
        BARS, horizon_bars=1, mid=mid, side="long", stop_price=Decimal(99), target_price=None
    )
    assert open_ended.take_frac == Decimal("0.02")


def test_levels_on_the_wrong_side_or_too_close_are_not_taken_as_given() -> None:
    rule = analyst_rule()
    mid = Decimal(100)
    wrong = rule.place(
        BARS,
        horizon_bars=1,
        mid=mid,
        side="long",
        stop_price=Decimal(101),
        target_price=Decimal(105),
    )
    # A long's stop above the price is no stop: the market's measured one stands.
    assert wrong.source == "measured" and "no stop named" in wrong.notes[0]
    with pytest.raises(StopError, match="min_reward_to_risk"):
        rule.place(
            BARS,
            horizon_bars=1,
            mid=mid,
            side="long",
            stop_price=Decimal(99),
            target_price=Decimal(101),
        )
    # Volatility mode ignores whatever the analyst named.
    measured = analyst_rule(analyst_levels=False).place(
        BARS, horizon_bars=1, mid=mid, side="long", stop_price=Decimal(98)
    )
    assert measured.source == "measured" and measured.mode == "volatility"


def test_a_cycle_uses_the_analysts_levels_and_refuses_a_trade_its_costs_cannot_carry(
    tmp_path: Path, envelope: Envelope
) -> None:
    rule = analyst_rule(max_stop_frac=Decimal("0.5"))
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer(horizon=4, stop_price=100.0, target_price=108.0)],
        chains=chains(closes=SWINGING),
        lookback_bars=8,
        volatility_exit=rule,
    )
    result = run_cycle(cfg)
    assert result.intent is not None, result.refusals
    assert result.exits["measured"]["source"] == "analyst"
    assert Decimal(result.intent["stop_price"]) == Decimal("100.00")
    assert Decimal(result.intent["take_price"]) == Decimal("108.00")
    # The analyst's own target is the move the cost gate weighs, not the declared constant.
    assert Decimal(result.intent["edge_bps"]) == Decimal(result.exits["measured"]["take_bps"])
    assert result.intent["edge_source"].startswith("the analyst's own target")

    small, _, _ = cycle_config(
        tmp_path / "small",
        envelope,
        [analyst_answer(horizon=4, stop_price=100.0, target_price=108.0)],
        chains=chains(closes=SWINGING),
        lookback_bars=8,
        volatility_exit=rule,
        magnitude_bps=Decimal("1"),
    )
    # A declared magnitude too small for this timeframe no longer refuses a target that clears.
    assert run_cycle(small).intent is not None

    unnamed, _, _ = cycle_config(
        tmp_path / "unnamed",
        envelope,
        [analyst_answer(horizon=4, stop_price=100.0, target_price=None)],
        chains=chains(closes=SWINGING),
        lookback_bars=8,
        volatility_exit=rule,
        magnitude_bps=Decimal("1"),
    )
    # A take profit the system set as a multiple of the stop claims nothing; the constant stands.
    flat = run_cycle(unnamed)
    assert flat.intent is None and any("cost gate" in r for r in flat.refusals), flat.refusals

    thin, _, _ = cycle_config(
        tmp_path / "thin",
        envelope,
        [analyst_answer(horizon=4, stop_price=101.8, target_price=102.4)],
        chains=chains(closes=SWINGING),
        lookback_bars=8,
        volatility_exit=rule,
    )
    refused = run_cycle(thin)
    assert refused.intent is None
    assert any("max_break_even_win_rate" in r for r in refused.refusals), refused.refusals


def test_the_analyst_mode_is_read_and_is_the_agent_desk_default() -> None:
    cfg = parse_strategy_config(agent_raw(exit=exit_table(mode="analyst", min_reward_to_risk=2)))
    rule = cfg.exit_rule.volatility
    assert rule is not None and rule.analyst_levels and rule.min_reward_to_risk == 2
    assert cfg.exit_rule.take_profit_frac == rule.max_stop_frac * 2
    raw = agent_raw()
    raw["exit"] = {}
    default = parse_strategy_config(raw).exit_rule.volatility
    assert default is not None and default.analyst_levels
    with pytest.raises(ConfigError, match='apply with mode = "analyst"'):
        parse_strategy_config(agent_raw(exit=exit_table(mode="volatility", max_reward_to_risk=3)))
    with pytest.raises(ConfigError, match="max_break_even_win_rate"):
        parse_strategy_config(agent_raw(targets={"max_break_even_win_rate": 0}))


def test_the_analyst_answer_carries_levels_or_none() -> None:
    import json

    from trading_research_agents.agents.technical_analyst.role import parse
    from trading_research_agents.llm.form import FormError

    base = json.loads(analyst_answer())
    assert parse(json.dumps(base)).stop_price is None
    del base["stop_price"], base["target_price"]
    # An answer that leaves the levels out is read as naming none.
    assert parse(json.dumps(base)).target_price is None
    with pytest.raises(FormError, match="above zero"):
        parse(json.dumps({**base, "stop_price": -1, "target_price": None}))
    levels = parse(json.dumps({**base, "stop_price": 99.5, "target_price": 103}))
    assert levels.stop_price == Decimal("99.5") and levels.target_price == Decimal("103")
