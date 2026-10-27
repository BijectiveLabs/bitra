"""The drawdown limit, and the controls a desk gets without writing them down."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from test_config import agent_raw
from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.alarms import DRAWDOWN, alarms_of
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.trading.capital import EQUITY_PEAK, Account, drawdown_bps
from trading_research_agents.trading.config import parse_strategy_config
from trading_research_agents.trading.config_values import ConfigError

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def test_the_peak_only_rises_and_clearing_it_starts_again(tmp_path: Path) -> None:
    assert drawdown_bps(tmp_path, NOW, Decimal(10000)) == (Decimal(0), Decimal(10000))
    assert drawdown_bps(tmp_path, NOW, Decimal(10500)) == (Decimal(0), Decimal(10500))
    fallen, peak = drawdown_bps(tmp_path, NOW, Decimal(9450))
    assert peak == Decimal(10500) and fallen == Decimal("1000.00")
    (tmp_path / EQUITY_PEAK).unlink()
    assert drawdown_bps(tmp_path, NOW, Decimal(9450)) == (Decimal(0), Decimal(9450))


def account_of(*equities: int) -> Callable[[], Account]:
    readings = iter(Decimal(e) for e in equities)

    def account() -> Account:
        return Account("USDT", next(readings), Decimal(5000), Decimal(0))

    return account


def test_a_drawdown_past_the_limit_stops_new_entries_before_anyone_is_asked(
    tmp_path: Path, envelope: Envelope
) -> None:
    account = account_of(10000, 10500, 9400)
    for _ in range(2):
        cfg, _, _ = cycle_config(
            tmp_path,
            envelope,
            [analyst_answer()],
            account=account,
            max_drawdown_bps=Decimal(1000),
        )
        run_cycle(cfg)
    cfg, _, usage = cycle_config(
        tmp_path, envelope, [analyst_answer()], account=account, max_drawdown_bps=Decimal(1000)
    )
    stopped = run_cycle(cfg)
    assert (
        stopped.stopped_by is not None
        and "1047.62 bps from the peak of 10500" in stopped.stopped_by
    )
    assert not stopped.acted and usage.tokens == 0
    assert stopped.briefs["equity"]["drawdown_bps"] == "1047.62"
    assert [a.kind for a in alarms_of({"stopped_by": stopped.stopped_by})] == [DRAWDOWN]


def test_flatten_arms_the_kill_switch(tmp_path: Path, envelope: Envelope) -> None:
    kill = tmp_path / "KILL"
    account = account_of(10000, 8000)
    for _ in range(2):
        cfg, _, _ = cycle_config(
            tmp_path,
            envelope,
            [analyst_answer()],
            account=account,
            max_drawdown_bps=Decimal(1000),
            drawdown_kill=kill,
        )
        result = run_cycle(cfg)
    assert kill.is_file() and "armed to flatten and stop" in str(result.stopped_by)


def test_a_desk_with_no_controls_table_starts_protected() -> None:
    raw = agent_raw()
    raw.pop("controls", None)
    raw["run"] = {"instrument": "BTCUSDT", "venue_key": "bybit/taker-taker", "size": 1}
    raw["exit"] = {}
    cfg = parse_strategy_config(raw)
    limits = cfg.limits
    assert limits.max_risk_per_trade_bps == 50 and limits.daily_loss_limit_bps == 300
    assert limits.max_orders_per_minute == 6 and limits.max_position is None
    assert limits.max_drawdown_bps is None
    # An agent desk with nothing in [exit] gets stops measured from the market.
    assert cfg.exit_rule.volatility is not None and cfg.exit_rule.timeout_bars == 240
    assert cfg.run.bar_interval_s == 60 and cfg.run.settlement_interval_s == 28_800


def test_the_drawdown_settings_are_checked_when_written() -> None:
    def controls(**keys: object) -> dict[str, object]:
        raw = agent_raw()
        raw["controls"] = {**raw.get("controls", {}), **keys}
        return raw

    cfg = parse_strategy_config(controls(max_drawdown_bps=1500))
    assert cfg.limits.max_drawdown_bps == 1500 and cfg.limits.on_drawdown == "stop"
    with pytest.raises(ConfigError, match="through the kill switch"):
        parse_strategy_config(controls(max_drawdown_bps=1500, on_drawdown="flatten"))
    with pytest.raises(ConfigError, match="on_drawdown is one of"):
        parse_strategy_config(controls(on_drawdown="panic"))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(controls(max_draw_down_bps=1500))
