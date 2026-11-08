"""A desk whose one decision is the operator's rule: no model asked, the rest of the cycle kept."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from test_cycle import cycle_config, envelope  # noqa: F401  (envelope is a fixture)
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope import Envelope
from trading_research_agents.strategy.core import Bar, Idea, Target


class Always:
    """Long on every decision with enough bars, at fixed levels; a rule the test can predict."""

    name = "always-long"

    def __init__(self, needs: int = 3) -> None:
        self.needs = needs
        self.seen: list[int] = []

    def decide_on_bars(self, bars: Sequence[Bar]) -> Idea | None:
        self.seen.append(len(bars))
        if len(bars) < self.needs:
            return None
        return Idea(Target.LONG, Decimal(30), Decimal(90), 6, "always long, for the test")


def test_a_rule_decides_and_no_model_is_asked(tmp_path: Path, envelope: Envelope) -> None:  # noqa: F811
    rule = Always()
    cfg, fake, _ = cycle_config(tmp_path, envelope, [], rule=rule)
    result = run_cycle(cfg)
    assert fake.calls == [] and rule.seen == [5]
    assert result.intent is not None, result.refusals
    intent = result.intent
    assert intent["decided_by"] == "rule:always-long" and intent["target"] == "long"
    mid = Decimal(intent["mid"])
    assert Decimal(intent["stop_price"]) == (mid * Decimal("0.997")).quantize(mid)
    assert Decimal(intent["take_price"]) == (mid * Decimal("1.009")).quantize(mid)
    assert intent["edge_bps"] == "40" and "measured" in intent["edge_source"]


def test_a_rule_without_an_idea_ends_the_cycle_flat(tmp_path: Path, envelope: Envelope) -> None:  # noqa: F811
    cfg, fake, _ = cycle_config(tmp_path, envelope, [], rule=Always(needs=50))
    result = run_cycle(cfg)
    assert fake.calls == [] and result.intent is None
    assert any(r.startswith("rule: always-long has no idea") for r in result.refusals)


RULE_FILE = """
from decimal import Decimal

from trading_research_agents.strategy.core import Idea, Target


class Always:
    name = "always-long"

    def decide_on_bars(self, bars):
        if len(bars) < 3:
            return None
        return Idea(Target.LONG, Decimal(30), Decimal(90), 6, "always long, for the test")
"""


def test_the_cli_runs_a_rule_desk_with_no_agents_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    from test_cycle import MINUTE_NS
    from trading_research_agents.cli.main import main
    from trading_research_agents.data import Side, Trade
    from trading_research_agents.data.parquet import write_trades

    project = tmp_path / "desk"
    assert main(["init", str(project)]) == 0
    monkeypatch.chdir(project)
    (project / "strategies" / "always.py").write_text(RULE_FILE, "utf-8")
    config = project / "strategy.toml"
    text = config.read_text("utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    rule = '[strategy]\nfile = "strategies/always.py"\nclass = "Always"\nedge_bps = 100\n\n'
    config.write_text(text[:start] + rule + text[end:], "utf-8")
    ohlcv_only = '[data.ohlcv]\nchain = ["file:data"]\nttl = "3650d"\ninterval_s = 60\nlimit = 10\n'
    (project / "data.toml").write_text(ohlcv_only, "utf-8")
    rows = [
        Trade(
            1_700_000_000_000_000_000 + i * MINUTE_NS,
            "bybit",
            "BTCUSDT",
            Decimal("93000") + Decimal(i),
            Decimal("0.01"),
            Side.BUY,
            str(i),
        )
        for i in range(10)
    ]
    write_trades(rows, project / "data" / "BTCUSDT-2023-11-14.parquet", price_scale=2, qty_scale=3)
    capsys.readouterr()
    code = main(["run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    # The offline source serves candles and no book, so the cycle decides and then stops at the
    # cost, as the agent desk's own test does; the point is that it decided without a model.
    assert code == 1 and payload["analyst"]["target"] == "long"
    assert payload["analyst"]["setup"] == "rule:always-long" and payload["tokens"] == 0
    assert "no book" in str(payload["stopped_by"])


def test_a_rule_stop_wider_than_the_desk_allows_is_refused(
    tmp_path: Path,
    envelope: Envelope,  # noqa: F811
) -> None:
    cfg, _, _ = cycle_config(tmp_path, envelope, [], rule=Always(), stop_frac=Decimal("0.002"))
    result = run_cycle(cfg)
    assert result.intent is None
    assert any("past the desk's widest stop" in r for r in result.refusals), result.refusals


def test_init_lays_down_the_daily_rule_desk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.cli.main import main
    from trading_research_agents.trading import ConfigError, load_strategy_config

    project = tmp_path / "desk"
    assert main(["init", str(project), "--profile", "rule"]) == 0
    assert "edge_bps" in capsys.readouterr().out
    config = project / "strategy.toml"
    # Nothing guesses the edge: the desk loads only once the operator wrote theirs in.
    with pytest.raises(ConfigError, match="edge_bps"):
        load_strategy_config(config)
    config.write_text(
        config.read_text("utf-8").replace("# edge_bps = ", "edge_bps = 105 #", 1), "utf-8"
    )
    cfg = load_strategy_config(config)
    assert cfg.rule is not None and cfg.rule.name == "example-momentum-long"
    assert cfg.run.bar_interval_s == 86400 and cfg.exit_rule.timeout_bars == 6
    assert cfg.agents is None and cfg.edge_bps == Decimal(105)
    assert cfg.exit_rule.stop_frac == Decimal("0.12")


def test_check_describes_a_rule_desk_as_a_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research_agents.cli.main import main

    project = tmp_path / "desk"
    assert main(["init", str(project), "--profile", "rule"]) == 0
    config = project / "strategy.toml"
    config.write_text(
        config.read_text("utf-8").replace("# edge_bps = ", "edge_bps = 105 #", 1), "utf-8"
    )
    monkeypatch.chdir(project)
    capsys.readouterr()
    main(["check"])
    said = capsys.readouterr().out
    assert "decision: the rule example-momentum-long" in said and "no model" in said
    assert "asks a model" not in said and "roster:" not in said
    assert "exits: the rule names its stop and take" in said
    assert "auditors: none on a rule desk" in said
    assert "paths: recorded on every cycle as a risk reading" in said
    assert "dataset: none; `tra run` decides on the live chains" in said
    assert "funding: live" in said
    assert "ready to run `tra data get" not in said


def test_a_candle_without_a_trade_count_is_still_a_traded_bar() -> None:
    from trading_research_agents.agents.cycle.rule import as_bars
    from trading_research_agents.data.schema import Candle

    price = Decimal(100)
    candle = Candle(0, price, price, price, price, Decimal(3))
    bars = as_bars([candle], 60)
    assert len(bars) == 1 and not bars[0].empty and bars[0].end_ns == 60 * 1_000_000_000


def test_check_names_the_event_guard_and_its_unknown_kinds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research_agents.cli.main import main

    project = tmp_path / "desk"
    assert main(["init", str(project), "--profile", "rule"]) == 0
    config = project / "strategy.toml"
    text = config.read_text("utf-8").replace("# edge_bps = ", "edge_bps = 105 #", 1)
    guard = '\n[event_guard]\nmode = "paper"\nhold_at = 0.7\nkinds = ["hack", "earthquake"]\n'
    config.write_text(text + guard, "utf-8")
    monkeypatch.chdir(project)
    capsys.readouterr()
    main(["check"])
    out = capsys.readouterr()
    said = out.out + out.err
    assert "event guard: paper" in said and "earthquake" in said
    assert "no judge declared" in said
