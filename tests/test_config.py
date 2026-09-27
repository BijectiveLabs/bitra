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
