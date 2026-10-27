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
            "stop_frac": Decimal("0.01"),
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


def test_a_declared_matrix_carries_the_readings_and_the_policy() -> None:
    cfg = parse_strategy_config(
        agent_raw(
            confluence={
                "required": ["macro", "onchain"],
                "long_states": ["bullish"],
                "on_missing": "ignore",
                "min_support": 2,
                "max_opposition": 0,
            }
        )
    )
    matrix = cfg.confluence
    assert matrix.required == ("macro", "onchain")
    assert matrix.on_missing == "ignore" and matrix.min_support == 2
    assert matrix.max_opposition == 0 and not matrix.idle


def test_a_matrix_that_names_something_that_reads_nothing_is_refused() -> None:
    with pytest.raises(ConfigError, match="is not one of"):
        parse_strategy_config(agent_raw(confluence={"required": ["technical_analyst"]}))
    # A desk written before 0.11 runs unchanged: the old name is read as the reading it became.
    old = parse_strategy_config(agent_raw(confluence={"required": ["macro_analyst"]}))
    assert old.confluence.required == ("macro",)
    with pytest.raises(ConfigError, match="names macro twice"):
        parse_strategy_config(agent_raw(confluence={"required": ["macro", "macro_analyst"]}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(confluence={"requires": ["macro"]}))
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
    with pytest.raises(ConfigError, match=r"needs \[exit\].take_profit_frac"):
        parse_strategy_config(ratio)

    ratio["exit"]["take_profit_frac"] = Decimal("0.02")
    assert parse_strategy_config(ratio).targets.min_reward_risk == Decimal("1.5")

    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(targets={"min_net": Decimal("5")}))


def test_the_edge_multiple_and_the_research_cost_are_read_like_any_other_target() -> None:
    cfg = parse_strategy_config(
        agent_raw(
            targets={
                "min_net_bps": Decimal("5"),
                "min_edge_multiple": Decimal("2.0"),
                "max_research_bps": Decimal("25"),
            }
        )
    )
    assert cfg.targets.min_edge_multiple == Decimal("2.0")
    assert cfg.targets.max_research_bps == Decimal("25")
    with pytest.raises(ConfigError, match="at least 1"):
        parse_strategy_config(agent_raw(targets={"min_edge_multiple": Decimal("0.5")}))


def test_the_trigger_is_read_and_widening_needs_something_to_widen() -> None:
    cfg = parse_strategy_config(agent_raw())
    assert cfg.trigger.idle

    declared = parse_strategy_config(
        agent_raw(
            trigger={
                "min_move_bps": Decimal("15"),
                "max_spread_bps": Decimal("8"),
                "cooldown_s": 900,
                "widen_after": 6,
            }
        )
    )
    assert declared.trigger.min_move_bps == Decimal("15")
    assert declared.trigger.cooldown_s == 900 and declared.trigger.widen_after == 6
    with pytest.raises(ConfigError, match="min_move_bps comes with it"):
        parse_strategy_config(agent_raw(trigger={"widen_after": 3}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(trigger={"min_move": Decimal("15")}))


def test_effort_is_declared_once_and_overridden_per_role() -> None:
    cfg = parse_strategy_config(
        agent_raw(
            agents={
                **AGENTS,
                "effort": "high",
                "roles": {"validator": {"effort": "low"}},
            }
        )
    )
    assert cfg.agents is not None
    assert cfg.agents.for_role("technical_analyst").effort == "high"
    assert cfg.agents.for_role("validator").effort == "low"
    plain = parse_strategy_config(agent_raw()).agents
    assert plain is not None and plain.for_role("technical_analyst").effort is None
    with pytest.raises(ConfigError, match="effort is one of"):
        parse_strategy_config(agent_raw(agents={**AGENTS, "effort": "ludicrous"}))
    with pytest.raises(ConfigError, match="effort is one of"):
        parse_strategy_config(
            agent_raw(agents={**AGENTS, "roles": {"validator": {"effort": "ludicrous"}}})
        )


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
                "weights": {"technical_analyst": Decimal(2), "macro": Decimal(1)},
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
    with pytest.raises(ConfigError, match="is not one of"):
        parse_strategy_config(agent_raw(conviction={"weights": {"portfolio_manager": Decimal(1)}}))
    old = parse_strategy_config(agent_raw(conviction={"weights": {"onchain_analyst": Decimal(1)}}))
    assert old.conviction.weights == {"onchain": Decimal(1)}
    with pytest.raises(ConfigError, match="names onchain twice"):
        parse_strategy_config(
            agent_raw(
                conviction={"weights": {"onchain": Decimal(1), "onchain_analyst": Decimal(2)}}
            )
        )
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


def test_a_flag_about_a_key_is_not_a_key() -> None:
    """[live].key_ip_unbound_ok is a documented setting; the secret guard used to refuse it."""
    raw = agent_raw(live={"venue": "bybit", "environment": "demo", "key_ip_unbound_ok": True})
    cfg = parse_strategy_config(raw)
    assert cfg.live is not None and cfg.live.key_ip_unbound_ok and cfg.live.environment == "demo"

    with pytest.raises(ConfigError, match="never a file"):
        parse_strategy_config(agent_raw(live={"venue": "bybit", "api_secret": "x"}))
    with pytest.raises(ConfigError, match="never a file"):
        parse_strategy_config(agent_raw(execution={"mode": "venue", "api_key": "x"}))
    with pytest.raises(ConfigError, match=r"\[live\]: unknown"):
        parse_strategy_config(agent_raw(live={"venue": "bybit", "environmnet": "demo"}))


def _quiet(tmp_path: Path, **run: Any) -> dict[str, Any]:
    (tmp_path / "quiet.py").write_text(CORE, encoding="utf-8")
    config = raw({"file": "quiet.py", "class": "Quiet"})
    config["run"].update(run)
    return config


def test_the_base_size_is_declared_once_in_one_unit(tmp_path: Path) -> None:
    quoted = _quiet(tmp_path, size_quote=Decimal("100"))
    del quoted["run"]["size"]
    cfg = parse_strategy_config(quoted, base=tmp_path)
    assert cfg.run.size_quote == Decimal("100") and cfg.run.size == Decimal(0)

    with pytest.raises(ConfigError, match="declares the base size once"):
        parse_strategy_config(_quiet(tmp_path, size_quote=Decimal("100")), base=tmp_path)
    neither = _quiet(tmp_path)
    del neither["run"]["size"]
    with pytest.raises(ConfigError, match="neither"):
        parse_strategy_config(neither, base=tmp_path)
    with pytest.raises(ConfigError, match="positive"):
        parse_strategy_config(_quiet(tmp_path, size=Decimal("-1")), base=tmp_path)


def test_a_replay_refuses_a_quote_size_rather_than_picking_a_price(tmp_path: Path) -> None:
    quoted = _quiet(tmp_path, size_quote=Decimal("100"))
    del quoted["run"]["size"]
    cfg = parse_strategy_config(quoted, base=tmp_path)
    with pytest.raises(ConfigError, match="only `tra run` has"):
        cfg.run.fixed_size()
    plain = parse_strategy_config(_quiet(tmp_path), base=tmp_path)
    assert plain.run.fixed_size() == Decimal("0.01")


def test_each_instrument_may_carry_its_own_step(tmp_path: Path) -> None:
    steps = {"BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.01")}
    config = _quiet(tmp_path, size_step=steps)
    config["universe"] = {"instruments": ["BTCUSDT", "ETHUSDT"]}
    cfg = parse_strategy_config(config, base=tmp_path)
    assert cfg.run.step_for("ETHUSDT") == Decimal("0.01")
    assert cfg.run.step_for("BTCUSDT") == Decimal("0.001")

    # A desk that can trade an instrument it has no step for would round it to a guess.
    config["universe"] = {"instruments": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]}
    with pytest.raises(ConfigError, match="SOLUSDT"):
        parse_strategy_config(config, base=tmp_path)
    with pytest.raises(ConfigError, match="positive"):
        parse_strategy_config(_quiet(tmp_path, size_step={"BTCUSDT": 0}), base=tmp_path)
    one = parse_strategy_config(_quiet(tmp_path, size_step=Decimal("0.001")), base=tmp_path)
    assert one.run.step_for("anything") == Decimal("0.001")


def test_the_exit_fractions_read_under_their_names_and_the_old_ones(tmp_path: Path) -> None:
    config = _quiet(tmp_path)
    rest = {k: v for k, v in config["exit"].items() if "frac" not in k}
    config["exit"] = {**rest, "stop_frac": Decimal("0.003"), "take_profit_frac": Decimal("0.006")}
    cfg = parse_strategy_config(config, base=tmp_path)
    assert cfg.exit_rule.stop_frac == Decimal("0.003")
    assert cfg.exit_rule.take_profit_frac == Decimal("0.006")

    # A desk written before the rename keeps working.
    config["exit"] = {
        **rest,
        "stop_sigma_frac": Decimal("0.01"),
        "take_profit_sigma_frac": Decimal("0.02"),
    }
    old = parse_strategy_config(config, base=tmp_path)
    assert old.exit_rule.stop_frac == Decimal("0.01")
    assert old.exit_rule.take_profit_frac == Decimal("0.02")


def test_a_setting_that_no_longer_does_anything_is_read_and_named_by_check() -> None:
    from trading_research_agents.trading.config_agents import renamed_in

    desk = agent_raw(
        agents={
            **AGENTS,
            "analysts": ["technical_analyst", "sentiment_analyst"],
            "reuse_reading_for": "1h",
            "roles": {"macro_analyst": {"model": "m"}, "validator": {"model": "v"}},
        },
        confluence={"required": ["onchain_analyst"]},
    )
    cfg = parse_strategy_config(desk)
    assert cfg.agents is not None and cfg.agents.readings == ("sentiment",)
    assert set(cfg.agents.roles) == {"validator"}
    notes = renamed_in(desk)
    assert any("[agents].analysts is [agents].readings" in n for n in notes)
    assert any("[agents.roles.macro_analyst]" in n and "asks no model" in n for n in notes)
    assert any("reuse_reading_for" in n for n in notes)
    assert any("onchain_analyst is onchain" in n for n in notes)
    assert renamed_in(agent_raw()) == ()

    # A label weight changed the size; with no label left, reading on would size differently.
    with pytest.raises(ConfigError, match="no longer gives a label"):
        parse_strategy_config(agent_raw(conviction={"reader_labels": Decimal("0.5")}))
    both = agent_raw(agents={**AGENTS, "analysts": ["technical_analyst"], "readings": []})
    with pytest.raises(ConfigError, match="one of the two"):
        parse_strategy_config(both)


def test_the_technical_team_is_declared_and_its_members_take_their_own_models() -> None:
    cfg = parse_strategy_config(
        agent_raw(
            agents={
                **AGENTS,
                "team": ["technical_peer", "flow_analyst"],
                "roles": {"technical_peer": {"provider": "google", "model": "other-family"}},
            },
            conviction={"weights": {"technical_analyst": Decimal(1), "flow_analyst": Decimal(1)}},
            confluence={"required": ["flow_analyst"]},
        )
    )
    assert cfg.agents is not None and cfg.agents.team == ("technical_peer", "flow_analyst")
    assert cfg.agents.for_role("technical_peer").model == "other-family"
    assert cfg.conviction.weights["flow_analyst"] == Decimal(1)
    assert cfg.confluence.required == ("flow_analyst",)
    assert parse_strategy_config(agent_raw()).agents.team == ()  # type: ignore[union-attr]
    for team, message in ((["astrologer"], "is not one of"), (["flow_analyst"] * 2, "twice")):
        with pytest.raises(ConfigError, match=message):
            parse_strategy_config(agent_raw(agents={**AGENTS, "team": team}))
