"""The structure lead as a decision core, through the same runtime as any code core."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.strategy import AgentStrategy
from trading_research_agents.cli.main import main
from trading_research_agents.data import Side, Trade
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution import FundingSchedule
from trading_research_agents.llm import FakeCompleter
from trading_research_agents.llm.usage import BudgetLimits, MeteredCompleter, UsageLedger
from trading_research_agents.strategy import Bar, ExitRule, Target
from trading_research_agents.trading import (
    ConfigError,
    Controls,
    KillSwitch,
    Limits,
    RecordedFeed,
    SimulatedVenue,
    TradingConfig,
    load_strategy_config,
    run,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MINUTE = 60 * 1_000_000_000


def bar(i: int, close: str = "100") -> Bar:
    c = Decimal(close)
    return Bar(i * MINUTE, (i + 1) * MINUTE, c, c, c, c, Decimal("1"), 1)


def answer(target: str, confidence: float = 0.9, setup: str = "range_reversion") -> dict[str, Any]:
    return {
        "target": target,
        "setup": "no_setup" if target == "flat" else setup,
        "reason": f"the mechanism says {target}",
        "figures_used": [{"figure": "volume_ratio", "value": "1.0"}],
        "confidence": confidence,
        "horizon_bars": 5,
    }


def test_the_analyst_signals_only_on_change_and_only_above_its_confidence() -> None:
    scripted = FakeCompleter(
        [
            answer("flat"),
            answer("long", 0.3),
            answer("long"),
            answer("long"),
            answer("flat"),
            "not json",
            {
                "target": "short",
                "setup": "range_reversion",
                "reason": "x",
                "figures_used": [],
                "confidence": 0.9,
                "horizon_bars": 0,
            },
        ]
    )
    core = AgentStrategy(scripted, mechanism="funding settlement flow", lookback_bars=5)
    assert core.on_bar(bar(0)) is None and core.on_bar(bar(1)) is None  # fewer than 3 bars
    assert len(scripted.calls) == 0
    assert core.on_bar(bar(2)) is None  # flat while flat
    assert core.on_bar(bar(3)) is None and core.below_confidence == 1
    signal = core.on_bar(bar(4))
    assert signal is not None and signal.target is Target.LONG
    assert "confidence 0.90" in signal.reason
    assert core.on_bar(bar(5)) is None  # already long
    flat = core.on_bar(bar(6))
    assert flat is not None and flat.target is Target.FLAT
    assert core.on_bar(bar(7)) is None and core.failures == 1
    assert core.on_bar(bar(8)) is None and core.failures == 2
    assert core.calls == 7
    sent = scripted.calls[-1].user.splitlines()
    assert sent[0] == "mechanism: funding settlement flow"
    assert sent[1].startswith("position: target=flat")
    assert sent[2].startswith("features over the window") and "zscore=" in sent[2]
    assert len(sent) == 4 + 5  # header lines, then the five bars of the lookback


def test_a_spent_budget_ends_the_session_flat(envelope: Envelope, tmp_path: Path) -> None:
    channel = next(c for c in envelope.channels if c.key == "bybit/taker-taker")
    scripted = FakeCompleter([answer("long")] * 3)
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    metered = MeteredCompleter(
        scripted,
        role="structure_lead",
        provider="fake",
        limits=BudgetLimits(token_budget=300, max_calls_per_hour=100, min_interval_s=0),
        ledger=ledger,
        clock=lambda: 0.0,
    )
    core = AgentStrategy(metered, mechanism="a test mechanism", lookback_bars=4)
    prints = [
        Trade(
            i * MINUTE + 1, "bybit", "BTCUSDT", Decimal("100"), Decimal("0.01"), Side.BUY, f"t{i}"
        )
        for i in range(12)
    ]
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", prints),
        venue=SimulatedVenue(
            channel=channel,
            funding=FundingSchedule(interval_s=28800, rates={}),
            latency_ns=1,
            slippage_bps=Decimal("1"),
        ),
        strategy=core,
        exit_rule=ExitRule(Decimal("0.05"), 100, "the whole life of the mechanism is long"),
        controls=Controls(
            Limits(Decimal("5000"), Decimal("1"), 10, Decimal("100000")), KillSwitch()
        ),
        out_dir=tmp_path / "run",
        bar_interval_ns=MINUTE,
        size=Decimal("0.01"),
        spread_cost_bps=Decimal("1"),
        equity=Decimal("10000"),
    )
    report = run(config)
    assert report.stopped_by is not None and "token budget spent" in report.stopped_by
    assert report.open_position is None and report.round_trips == 1
    rows = UsageLedger.read(tmp_path / "usage.jsonl")
    assert len(rows) == 1 and rows[0]["role"] == "structure_lead"
    assert core.halt is not None


def test_the_analyst_refuses_bars_shorter_than_a_minute(envelope: Envelope, tmp_path: Path) -> None:
    channel = next(c for c in envelope.channels if c.key == "bybit/taker-taker")
    core = AgentStrategy(FakeCompleter([]), mechanism="m")
    with pytest.raises(ValueError, match="needs bars of at least 60s"):
        TradingConfig(
            feed=RecordedFeed("BTCUSDT", []),
            venue=SimulatedVenue(
                channel=channel,
                funding=FundingSchedule(interval_s=28800, rates={}),
                latency_ns=1,
                slippage_bps=Decimal("1"),
            ),
            strategy=core,
            exit_rule=ExitRule(Decimal("0.05"), 10, "the whole life of the mechanism is long"),
            controls=Controls(
                Limits(Decimal("5000"), Decimal("1"), 10, Decimal("100000")), KillSwitch()
            ),
            out_dir=tmp_path,
            bar_interval_ns=30 * 1_000_000_000,
            size=Decimal("0.01"),
            spread_cost_bps=Decimal("1"),
        )
    with pytest.raises(ValueError, match="mechanism"):
        AgentStrategy(FakeCompleter([]), mechanism=" ")
    with pytest.raises(ValueError, match="lookback_bars"):
        AgentStrategy(FakeCompleter([]), mechanism="m", lookback_bars=2)


AGENT_TABLES = """
[agents]
provider = "ollama"
model = "local-model"
mechanism = "positions forced to close into settlement"
token_budget = 200000
max_calls_per_hour = 60
min_interval_s = 60
decide_every_bars = 5
max_output_tokens = 512

[agents.pricing.local-model]
input_per_million = 0
output_per_million = 0
"""


def agent_config(tmp_path: Path, strategy_table: str) -> Path:
    text = (EXAMPLES / "strategy.toml").read_text(encoding="utf-8")
    start = text.index("[strategy]")
    end = text.index("[exit]")
    config = tmp_path / "strategy.toml"
    config.write_text(text[:start] + strategy_table + "\n" + text[end:] + AGENT_TABLES, "utf-8")
    return config


def test_the_config_declares_the_agent_core_and_its_budget(tmp_path: Path) -> None:
    cfg = load_strategy_config(agent_config(tmp_path, '[strategy]\nagent = "structure_lead"\n'))
    assert cfg.agents is not None and cfg.agents.provider == "ollama"
    assert cfg.agents.token_budget == 200000
    assert cfg.agents.decide_every_bars == 5 and cfg.agents.max_output_tokens == 512
    assert cfg.agents.pricing["local-model"] == (Decimal(0), Decimal(0))
    assert cfg.strategy.name == "agent-pending"
    with pytest.raises(ConfigError, match="only structure_lead decides"):
        load_strategy_config(agent_config(tmp_path, '[strategy]\nagent = "auditor"\n'))
    with pytest.raises(ConfigError, match="not both"):
        load_strategy_config(
            agent_config(tmp_path, '[strategy]\nagent = "structure_lead"\nimport = "x:Y"\n')
        )
    bare = agent_config(tmp_path, '[strategy]\nagent = "structure_lead"\n')
    bare.write_text(bare.read_text("utf-8").split("[agents]")[0], "utf-8")
    with pytest.raises(ConfigError, match=r"\[agents\] is required"):
        load_strategy_config(bare)


def test_backtest_with_an_agent_core_refuses_before_any_call_when_the_key_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agents.data.parquet import write_trades

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    rows = [
        Trade(
            i * MINUTE + 1, "bybit", "BTCUSDT", Decimal("100"), Decimal("0.01"), Side.BUY, f"t{i}"
        )
        for i in range(5)
    ]
    parquet = tmp_path / "btc.parquet"
    write_trades(rows, parquet, price_scale=2, qty_scale=3)
    config = agent_config(tmp_path, '[strategy]\nagent = "structure_lead"\n')
    config.write_text(
        config.read_text("utf-8").replace('provider = "ollama"', 'provider = "groq"'), "utf-8"
    )
    code = main(
        [
            "backtest",
            "--config",
            str(config),
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--parquet",
            str(parquet),
            "--out",
            str(tmp_path / "run"),
        ]
    )
    assert code == 1 and "GROQ_API_KEY is not set" in capsys.readouterr().err


def test_the_analyst_is_asked_on_a_cadence_and_the_output_cap_reaches_the_provider() -> None:
    scripted = FakeCompleter([answer("flat")] * 10)
    core = AgentStrategy(
        scripted, mechanism="m", lookback_bars=5, decide_every_bars=3, min_confidence=0.0
    )
    for i in range(12):
        core.on_bar(bar(i))
    # Bars 0 and 1 are below the minimum; from bar 2 on, every third bar asks the model.
    assert core.calls == 4 and len(scripted.calls) == 4
    with pytest.raises(ValueError, match="decide_every_bars"):
        AgentStrategy(scripted, mechanism="m", decide_every_bars=0)

    from trading_research_agents.llm.providers import CallOptions, provider

    p = provider("ollama")  # keyless, so the client builds without the network
    assert p.build_with is not None
    built = p.build_with("m", None, CallOptions(max_output_tokens=256, timeout_s=7, max_retries=1))
    assert getattr(built, "max_tokens", None) == 256
    # The declared timeout and retry count reach the SDK client, per request.
    client = built._client  # type: ignore[attr-defined]
    assert client.timeout == 7 and client.max_retries == 1
    # Undeclared is no cap at all: the vendor's own maximum for the model, and the field is left
    # out of the request rather than guessed at.
    assert getattr(p.build("m", None), "max_tokens", "missing") is None
    with pytest.raises(ValueError, match="call options"):
        CallOptions(timeout_s=0)


def test_a_vendor_error_is_a_failed_call_and_repeated_failures_halt() -> None:
    from trading_research_agents.llm.completer import ProviderError
    from trading_research_agents.llm.providers.openai import OpenAICompleter

    class Broken:
        class chat:  # noqa: N801 - the SDK's attribute shape
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs: Any) -> Any:
                    raise ConnectionResetError("dropped")

    adapter = OpenAICompleter("m", client=Broken())
    with pytest.raises(ProviderError, match="ConnectionResetError"):
        adapter.complete(system="s", user="u", schema={})

    class Flaky:
        def complete(self, **kwargs: Any) -> Any:
            raise ProviderError("APIConnectionError: Connection error.")

    core = AgentStrategy(Flaky(), mechanism="m", lookback_bars=4, max_failures_in_a_row=3)
    for i in range(6):
        core.on_bar(bar(i))
    assert core.failures == 3 and core.halt is not None and "3 failed calls in a row" in core.halt
    with pytest.raises(ValueError, match="max_failures_in_a_row"):
        AgentStrategy(Flaky(), mechanism="m", max_failures_in_a_row=0)
