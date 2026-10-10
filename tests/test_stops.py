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
