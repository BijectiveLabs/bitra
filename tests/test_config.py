"""`strategy.toml` names the decision core; the package ships none of its own."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.trading import ConfigError, load_strategy_config, parse_strategy_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

CORE = """
from trading_research_agents.strategy import Bar, Signal, Target

class Quiet:
    name = "quiet"

    def __init__(self, *, threshold: int = 1) -> None:
        self.threshold = threshold

    def on_bar(self, bar: Bar) -> Signal | None:
        return None
"""


def raw(strategy: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "exit": {
            "stop_sigma_frac": Decimal("0.01"),
            "timeout_bars": 10,
            "mechanism_bound": "the dislocation resolves within a few bars or is gone",
        },
        "run": {
            "instrument": "BTCUSDT",
            "venue_key": "bybit/taker-taker",
            "bar_interval_s": 60,
            "size": Decimal("0.01"),
            "spread_cost_bps": Decimal("3"),
            "latency_ms": Decimal("6"),
            "slippage_bps": Decimal("1"),
            "settlement_interval_s": 28800,
        },
        "controls": {
            "max_risk_per_trade_bps": Decimal("50"),
            "max_position": Decimal("1"),
            "max_orders_per_minute": 6,
            "daily_loss_limit_bps": Decimal("300"),
        },
    }


def test_the_example_config_points_at_an_example_core_by_file() -> None:
    cfg = load_strategy_config(EXAMPLES / "strategy.toml")
    assert cfg.strategy.name == "example-trailing-zscore"
    assert cfg.run.venue_key == "bybit/taker-taker"


def test_a_core_file_is_found_next_to_the_config_first(tmp_path: Path) -> None:
    (tmp_path / "quiet.py").write_text(CORE, encoding="utf-8")
    config = tmp_path / "strategy.toml"
    text = (EXAMPLES / "strategy.toml").read_text(encoding="utf-8")
    text = text.replace('file = "examples/strategies/trailing_zscore.py"', 'file = "quiet.py"')
    text = text.replace('class = "TrailingZScore"', 'class = "Quiet"')
    text = text.replace("[strategy.params]\nlookback = 60\nentry_z = 2.0\nexit_z = 0.5\n", "")
    config.write_text(text, encoding="utf-8")
    cfg = load_strategy_config(config)
    assert cfg.strategy.name == "quiet"


def test_a_core_can_be_imported_from_an_installed_package() -> None:
    strategy = {"import": "trading_research_agents.trading.controls:KillSwitch"}
    with pytest.raises(ConfigError, match="not a Strategy"):
        parse_strategy_config(raw(strategy))
    with pytest.raises(ConfigError, match="cannot import"):
        parse_strategy_config(raw({"import": "no_such_package.here:Core"}))
    with pytest.raises(ConfigError, match=r"package\.module:Class"):
        parse_strategy_config(raw({"import": "not-a-target"}))


def test_the_old_builtin_key_names_nothing() -> None:
    with pytest.raises(ConfigError, match="names the core"):
        parse_strategy_config(raw({"builtin": "trailing-zscore"}))
    with pytest.raises(ConfigError, match="names the core"):
        parse_strategy_config(raw({}))


def test_file_loading_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"needs \[strategy\]\.class"):
        parse_strategy_config(raw({"file": "x.py"}))
    with pytest.raises(ConfigError, match="does not exist"):
        parse_strategy_config(raw({"file": str(tmp_path / "missing.py"), "class": "X"}))
    broken = tmp_path / "broken.py"
    broken.write_text("def (\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="failed to load"):
        parse_strategy_config(raw({"file": str(broken), "class": "X"}))
    good = tmp_path / "quiet.py"
    good.write_text(CORE, encoding="utf-8")
    with pytest.raises(ConfigError, match="has no class"):
        parse_strategy_config(raw({"file": str(good), "class": "Loud"}))
    with pytest.raises(ConfigError, match="unexpected keyword"):
        parse_strategy_config(raw({"file": str(good), "class": "Quiet", "params": {"nope": 1}}))
    cfg = parse_strategy_config(
        raw({"file": str(good), "class": "Quiet", "params": {"threshold": 3}})
    )
    assert cfg.strategy.name == "quiet"


AGENTS = {
    "provider": "groq",
    "model": "a-model",
    "mechanism": "short-horizon reversion after a burst of prints",
    "token_budget": 60000,
    "max_calls_per_hour": 60,
    "min_interval_s": 0,
    "magnitude_bps": Decimal("25"),
}


def agent_raw(**tables: Any) -> dict[str, Any]:
    out = raw({"agent": "technical_analyst"})
    out["agents"] = dict(AGENTS)
    out.update(tables)
    return out


def test_the_two_gates_are_absent_until_they_are_declared() -> None:
    cfg = parse_strategy_config(agent_raw())
    assert cfg.confluence.idle and cfg.targets.idle
    assert cfg.agents is not None and cfg.agents.validators == ()


def test_a_declared_matrix_carries_the_roles_and_the_policy() -> None:
    cfg = parse_strategy_config(
        agent_raw(
            confluence={
                "required": ["macro_analyst", "onchain_analyst"],
                "long_states": ["bullish"],
                "on_missing": "ignore",
                "min_support": 2,
                "max_opposition": 0,
            }
        )
    )
    matrix = cfg.confluence
    assert matrix.required == ("macro_analyst", "onchain_analyst")
    assert matrix.on_missing == "ignore" and matrix.min_support == 2
    assert matrix.max_opposition == 0 and not matrix.idle


def test_a_matrix_that_names_something_that_reads_nothing_is_refused() -> None:
    with pytest.raises(ConfigError, match="does not read a context"):
        parse_strategy_config(agent_raw(confluence={"required": ["technical_analyst"]}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(confluence={"requires": ["macro_analyst"]}))
    with pytest.raises(ConfigError, match="on_missing"):
        parse_strategy_config(agent_raw(confluence={"on_missing": "maybe"}))
    with pytest.raises(ConfigError, match="min_support is a non-negative integer"):
        parse_strategy_config(agent_raw(confluence={"min_support": -1}))


def test_targets_are_read_and_a_ratio_needs_a_take_profit() -> None:
    declared = agent_raw(
        targets={
            "min_net_bps": Decimal("5"),
            "max_round_trips_per_day": 6,
        }
    )
    cfg = parse_strategy_config(declared)
    assert cfg.targets.min_net_bps == Decimal("5")
    assert cfg.targets.max_round_trips_per_day == 6 and not cfg.targets.idle

    ratio = agent_raw(targets={"min_reward_risk": Decimal("1.5")})
    with pytest.raises(ConfigError, match=r"needs \[exit\].take_profit_sigma_frac"):
        parse_strategy_config(ratio)

    ratio["exit"]["take_profit_sigma_frac"] = Decimal("0.02")
    assert parse_strategy_config(ratio).targets.min_reward_risk == Decimal("1.5")

    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(targets={"min_net": Decimal("5")}))


def test_the_stances_are_named_and_asked_once() -> None:
    cfg = parse_strategy_config(agent_raw(agents={**AGENTS, "validators": ["pro", "contra"]}))
    assert cfg.agents is not None and cfg.agents.validators == ("pro", "contra")

    with pytest.raises(ConfigError, match="is not a stance"):
        parse_strategy_config(agent_raw(agents={**AGENTS, "validators": ["bullish"]}))
    with pytest.raises(ConfigError, match="asks each stance once"):
        parse_strategy_config(agent_raw(agents={**AGENTS, "validators": ["pro", "pro"]}))


def test_conviction_is_absent_until_declared_and_reads_its_ladder_from_the_top() -> None:
    assert parse_strategy_config(agent_raw()).conviction.idle

    cfg = parse_strategy_config(
        agent_raw(
            conviction={
                "weights": {"technical_analyst": Decimal(2), "macro_analyst": Decimal(1)},
                "validator_weight": Decimal(1),
                "min_score": Decimal("0.4"),
                "max_leverage": Decimal(3),
                "ladder": [
                    {"at_least": Decimal("0.4"), "size_multiple": Decimal("0.25")},
                    {"at_least": Decimal("0.8"), "size_multiple": Decimal(1)},
                ],
            }
        )
    )
    rule = cfg.conviction
    assert not rule.idle and rule.max_leverage == Decimal(3)
    assert [str(t.at_least) for t in rule.ladder] == ["0.8", "0.4"]
    assert rule.weights["technical_analyst"] == Decimal(2)


def test_a_conviction_rule_that_cannot_be_applied_is_refused() -> None:
    with pytest.raises(ConfigError, match="is not a role that is asked"):
        parse_strategy_config(agent_raw(conviction={"weights": {"portfolio_manager": Decimal(1)}}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(conviction={"floor": Decimal("0.4")}))
    with pytest.raises(ConfigError, match="at_least and size_multiple"):
        parse_strategy_config(agent_raw(conviction={"ladder": [{"at_least": Decimal("0.5")}]}))
    with pytest.raises(ConfigError, match="min_score"):
        parse_strategy_config(agent_raw(conviction={"min_score": Decimal(2)}))
    with pytest.raises(ConfigError, match="max_leverage"):
        parse_strategy_config(agent_raw(conviction={"max_leverage": Decimal(0)}))


def test_the_venue_size_step_is_declared_beside_the_size() -> None:
    assert parse_strategy_config(agent_raw()).run.size_step == Decimal("0.000001")
    declared = agent_raw()
    declared["run"]["size_step"] = Decimal("0.001")
    assert parse_strategy_config(declared).run.size_step == Decimal("0.001")
