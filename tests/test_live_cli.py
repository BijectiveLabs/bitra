"""The common spellings, the project scaffold, the live gate, and funding from a plugin."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from trading_research_agents.cli.main import main, read_env_file
from trading_research_agents.data import Side, Trade
from trading_research_agents.data.venues import archive_venue, register
from trading_research_agents.data.venues.bybit import Bybit
from trading_research_agents.trading import Ladder, LadderError, Tier
from trading_research_agents.trading.config import load_funding, load_strategy_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MINUTE = 60 * 1_000_000_000

LIVE_TABLES = """
[live]
venue = "bybit"
environment = "demo"

[canary]
max_notional = 200
max_round_trips = 2
session_loss_limit_bps = 100
max_session_minutes = 30
"""


def test_init_writes_a_project_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "proj"
    assert main(["init", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["files"]) == {
        "strategy.toml",
        "envelope.toml",
        "mapping.toml",
        "data.toml",
        "observation.toml",
        "sources/macro.toml",
        "sources/onchain.toml",
        "sources/derivatives.toml",
        "sources/sentiment.toml",
        "sources/news.toml",
        "strategies/trailing_zscore.py",
        ".env.example",
        ".gitignore",
        "README.md",
    }
    cfg = load_strategy_config(target / "strategy.toml")
    assert cfg.strategy.name == "example-trailing-zscore" and cfg.live is None
    assert "BYBIT_API_KEY=" in (target / ".env.example").read_text(encoding="utf-8")
    assert main(["init", str(target)]) == 1
    assert "never overwrites" in capsys.readouterr().err


def test_the_shelf_starts_empty_and_check_says_what_filling_it_in_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "proj"
    assert main(["init", str(target)]) == 0
    monkeypatch.chdir(target)
    capsys.readouterr()

    main(["check"])
    out = capsys.readouterr().out
    assert "nothing declared yet" in out
    assert "ok    roster: technical_analyst; `tra run` asks these" in out

    (target / "sources" / "macro.toml").write_text(
        '[[series]]\nname = "us10y"\nfile = "macro/us10y.csv"\nts = "date"\n'
        'ts_unit = "iso8601"\nvalue = "value"\n',
        encoding="utf-8",
    )
    main(["check"])
    # The export is declared but not on disk yet, so it is a todo rather than a silent skip.
    assert "todo  sources/macro.toml: declared but not on disk: macro/us10y.csv" in (
        capsys.readouterr().out
    )

    (target / "sources" / "macro").mkdir()
    (target / "sources" / "macro" / "us10y.csv").write_text(
        "date,value\n2026-09-20,4.10\n", encoding="utf-8"
    )
    main(["check"])
    out = capsys.readouterr().out
    assert "ok    sources/macro.toml: 1 declared (us10y)" in out
    assert "ok    roster: technical_analyst, macro_analyst; `tra run` asks these" in out


def test_backtest_is_trade_replay_by_another_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agents.data.parquet import write_trades

    rows = [
        Trade(
            i * MINUTE + 1, "bybit", "BTCUSDT", Decimal("100"), Decimal("0.01"), Side.BUY, f"t{i}"
        )
        for i in range(70)
    ]
    parquet = tmp_path / "btc.parquet"
    write_trades(rows, parquet, price_scale=2, qty_scale=3)
    target = tmp_path / "proj"
    assert main(["init", str(target)]) == 0
    capsys.readouterr()
    code = main(
        [
            "backtest",
            "--config",
            str(target / "strategy.toml"),
            "--envelope",
            str(target / "envelope.toml"),
            "--parquet",
            str(parquet),
            "--out",
            str(tmp_path / "run"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["label"] == "replay" and payload["prints"] == 70


def test_the_env_file_is_read_explicitly_and_the_environment_wins(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# keys\nexport BYBIT_API_KEY='abc'\nBYBIT_API_SECRET=\"s e c\"\nPLAIN=1\n\n",
        encoding="utf-8",
    )
    assert read_env_file(env) == {"BYBIT_API_KEY": "abc", "BYBIT_API_SECRET": "s e c", "PLAIN": "1"}
    env.write_text("not a pair\n", encoding="utf-8")
    with pytest.raises(ValueError, match="KEY=VALUE"):
        read_env_file(env)


def live_config(tmp_path: Path) -> Path:
    text = (EXAMPLES / "strategy.toml").read_text(encoding="utf-8")
    text = text.replace(
        'file = "examples/strategies/trailing_zscore.py"',
        f'file = "{EXAMPLES.as_posix()}/strategies/trailing_zscore.py"',
    )
    config = tmp_path / "strategy.toml"
    config.write_text(text + LIVE_TABLES, encoding="utf-8")
    return config


def test_live_refuses_without_the_tables_the_ladder_or_the_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = [
        "--envelope",
        str(EXAMPLES / "envelope.toml"),
        "--out",
        str(tmp_path / "run"),
        "--ladder",
        str(tmp_path / "ladder.json"),
    ]
    code = main(["live", "--config", str(EXAMPLES / "strategy.toml"), *base])
    assert code == 1 and "[live] and [canary]" in capsys.readouterr().err

    config = live_config(tmp_path)
    code = main(["live", "--config", str(config), *base])
    assert code == 1 and "is at RESEARCH" in capsys.readouterr().err

    ladder = Ladder(tmp_path / "ladder.json", live_available=True)
    for evidence in ("replay OPEN", "paper OPEN", "shadow OPEN"):
        ladder.promote(evidence=evidence, signed_by="op")
    assert Ladder(tmp_path / "ladder.json").tier == Tier.CANARY
    with pytest.raises(LadderError, match="PRODUCTION is not reachable"):
        ladder.promote(evidence="canary OPEN", signed_by="op")

    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    code = main(["live", "--config", str(config), *base])
    assert code == 1 and "missing BYBIT_API_KEY" in capsys.readouterr().err


def test_a_key_in_the_config_file_is_refused_by_name(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'environment = "demo"', 'environment = "demo"\napi_key = "x"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="keys come from the environment"):
        load_strategy_config(config)


def test_funding_file_loads_into_the_schedule(tmp_path: Path) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        json.dumps(
            {
                "interval_s": 28800,
                "rates": {"1735689600000000000": "0.0001", "1735718400000000000": "-0.00005"},
            }
        ),
        encoding="utf-8",
    )
    schedule = load_funding(path, 28800)
    assert schedule.rates[1735689600000000000] == Decimal("0.0001")
    with pytest.raises(ValueError, match="differs"):
        load_funding(path, 3600)
    path.write_text(json.dumps({"rates": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no rates"):
        load_funding(path, 28800)
    config = live_config(tmp_path)
    path.write_text(json.dumps({"rates": {"1735689600000000000": "0.0001"}}), encoding="utf-8")
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "settlement_interval_s = 28800",
            f'settlement_interval_s = 28800\nfunding = "{path.name}"',
        ),
        encoding="utf-8",
    )
    assert load_strategy_config(config).funding is not None


# Funding from the venue plugin, against a local double of the public endpoint


class FundingHandler(BaseHTTPRequestHandler):
    settlements: list[tuple[int, str]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert parts.path == "/v5/market/funding/history"
        start, end, limit = int(query["startTime"]), int(query["endTime"]), int(query["limit"])
        rows = [
            {"symbol": query["symbol"], "fundingRate": rate, "fundingRateTimestamp": str(ts)}
            for ts, rate in sorted(self.settlements, reverse=True)
            if start <= ts <= end
        ][:limit]
        body = json.dumps({"retCode": 0, "retMsg": "OK", "result": {"list": rows}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def funding_server() -> Iterator[str]:
    hour_ms = 3_600_000
    FundingHandler.settlements = [
        (1_735_689_600_000 + i * 8 * hour_ms, f"{0.0001 * (1 if i % 2 == 0 else -1):.6f}")
        for i in range(450)
    ]
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FundingHandler))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    register(Bybit(api_base=url))
    try:
        yield url
    finally:
        register(Bybit())
        server.shutdown()
        server.server_close()


def test_funding_pages_backwards_and_writes_the_schedule(
    funding_server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    venue = archive_venue("bybit")
    assert isinstance(venue, Bybit)
    rates = venue.funding_rates(
        "BTCUSDT", start_ms=1_735_689_600_000, end_ms=1_735_689_600_000 + 449 * 8 * 3_600_000
    )
    assert len(rates) == 450 and rates[0].ts_ns < rates[-1].ts_ns
    out = tmp_path / "funding.json"
    code = main(
        [
            "data",
            "funding",
            "--venue",
            "bybit",
            "--instrument",
            "BTCUSDT",
            "--from",
            "2025-01-01",
            "--to",
            "2025-01-31",
            "--out",
            str(out),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["interval_s"] == 28800 and payload["count"] == 93
    schedule = load_funding(out, 28800)
    assert len(schedule.rates) == 93


def test_the_autonomous_profile_lays_the_cycle_down_wired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research_agents.trading.config import load_strategy_config

    target = tmp_path / "desk"
    assert main(["init", str(target), "--profile", "autonomous"]) == 0
    said = capsys.readouterr().out
    assert "the cycle is wired" in said and "name the provider" in said

    cfg = load_strategy_config(target / "strategy.toml")
    assert cfg.agents is not None and cfg.agents.role == "technical_analyst"
    # The gates are live rather than commented, and they are the operator's to change.
    assert not cfg.conviction.idle and cfg.conviction.min_score == Decimal("0.4")
    assert [str(t.size_multiple) for t in cfg.conviction.ladder] == ["1.0", "0.5", "0.25"]
    assert cfg.targets.min_net_bps == Decimal("5")
    assert cfg.execution.mode == "sandbox"
    # Nothing is required of the matrix yet: no source is declared, and a role nobody asked has
    # agreed to nothing.
    assert cfg.confluence.required == ()
    assert cfg.conviction.weights == {"technical_analyst": Decimal("2.0")}

    # The data section survived the rewrite that put [agents] in, and so did the shelf.
    assert "[data]" in (target / "strategy.toml").read_text("utf-8")
    assert (target / "sources" / "macro.toml").is_file()
    assert "Your first hour" in (target / "README.md").read_text("utf-8")

    monkeypatch.chdir(target)
    capsys.readouterr()
    main(["check"])
    out = capsys.readouterr().out
    assert "ok    conviction: floor 0.4" in out
    assert "ok    execution: sandbox; nothing leaves the machine" in out
    assert "todo  [agents].provider is still the placeholder" in out


def test_the_default_profile_is_unchanged_and_agent_init_keeps_the_gates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.trading.config import load_strategy_config

    plain = tmp_path / "plain"
    assert main(["init", str(plain)]) == 0
    capsys.readouterr()
    cfg = load_strategy_config(plain / "strategy.toml")
    assert cfg.agents is None and cfg.conviction.idle and cfg.targets.idle

    with_agent = tmp_path / "agent"
    assert main(["init", str(with_agent), "--agent", "groq", "--model", "a-model"]) == 0
    capsys.readouterr()
    text = (with_agent / "strategy.toml").read_text("utf-8")
    # The live [agents] table replaced the commented one without taking the rest of the page.
    assert '[agents]\nprovider = "groq"' in text
    assert "# [agents]" not in text
    assert "[data]" in text and "# [conviction]" in text and "# [execution]" in text


def test_check_says_what_the_three_files_add_up_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "desk"
    assert main(["init", str(target), "--profile", "autonomous"]) == 0
    monkeypatch.chdir(target)
    capsys.readouterr()

    main(["check"])
    out = capsys.readouterr().out
    assert "ok    symbols: BTCUSDT is BTC/USDT:USDT to the sources" in out
    # The shipped chains fail over to a second venue, and a desk should know it prices only one.
    assert "venues: fees are priced on bybit, data also comes from binance" in out

    # An order that would go to one venue at another's fee is a refusal, not a note.
    config = target / "strategy.toml"
    config.write_text(
        config.read_text("utf-8").replace(
            'mode = "sandbox"', 'mode = "venue"\nvenue = "binance"', 1
        ),
        "utf-8",
    )
    main(["check"])
    said = capsys.readouterr().out
    assert "at the other's fee" in said


def test_init_for_a_venue_wires_data_orders_and_fees_for_that_venue(tmp_path: Path) -> None:
    from trading_research_agents.data.sources.config import load_data_config
    from trading_research_agents.trading.config import load_strategy_config

    target = tmp_path / "okx-desk"
    assert main(["init", str(target), "--profile", "autonomous", "--venue", "okx"]) == 0

    data = (target / "data.toml").read_text(encoding="utf-8")
    assert "bybit" not in data and "ccxt:okx" in data and "intervalTime" not in data
    # The book comes from the practice account the orders go to.
    assert 'chain = ["ccxt:okx@testnet"]' in data
    load_data_config(target / "data.toml")

    strategy = (target / "strategy.toml").read_text(encoding="utf-8")
    assert 'venue_key = "okx/taker-taker"' in strategy and "[live]" not in strategy.replace(
        "# [live]", ""
    )
    config = load_strategy_config(target / "strategy.toml")
    assert config.execution.venue == "okx" and config.execution.environment == "testnet"
    assert config.execution.mode == "sandbox"
    assert config.run.size_step == Decimal("0.000001")

    envelope = (target / "envelope.toml").read_text(encoding="utf-8")
    assert "bybit" not in envelope and 'venue = "okx"' in envelope
    example = (target / ".env.example").read_text(encoding="utf-8")
    assert "OKX_API_PASSPHRASE=" in example and "BYBIT" not in example


def test_models_lists_what_the_key_can_use_and_init_takes_a_default_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.llm.providers import registry

    monkeypatch.setenv("GROQ_API_KEY", "k" * 40)
    monkeypatch.setattr(registry, "model_ids", lambda name, base_url=None: ["a-20b", "b-120b"])
    assert main(["models", "groq", "--filter", "120", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["models"] == ["b-120b"]

    # A provider with a default model needs no --model; one without says where to look.
    assert main(["init", str(tmp_path / "a"), "--agent", "anthropic"]) == 0
    assert 'provider = "anthropic"' in (tmp_path / "a" / "strategy.toml").read_text(
        encoding="utf-8"
    )
    # A provider whose model has run live here has a default; the others still ask for one.
    assert main(["init", str(tmp_path / "b"), "--agent", "groq"]) == 0
    assert 'model = "openai/gpt-oss-120b"' in (tmp_path / "b" / "strategy.toml").read_text("utf-8")
    capsys.readouterr()
    assert main(["init", str(tmp_path / "c"), "--agent", "deepseek"]) != 0
    assert "tra models deepseek" in capsys.readouterr().err


def test_blank_fees_are_read_from_the_account_and_written_ones_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from trading_research_agents.cli import main as package_main  # noqa: F401
    from trading_research_agents.envelope.load import load_envelope

    cli = __import__("importlib").import_module("trading_research_agents.cli.main")
    envelope = tmp_path / "envelope.toml"
    envelope.write_text(
        "\n".join(
            [
                "[[channel]]",
                'venue = "okx"',
                'name = "taker-taker"',
                "entry_fee_bps =                  # your taker fee",
                "exit_fee_bps =                   # your taker fee",
                "",
                "[[channel]]",
                'venue = "okx"',
                'name = "maker-taker"',
                "entry_fee_bps = 1",
                "exit_fee_bps =                   # your taker fee",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = SimpleNamespace(
        execution=SimpleNamespace(venue="okx", environment="testnet"),
        run=SimpleNamespace(venue_key="okx/taker-taker", instrument="BTCUSDT"),
        live=None,
    )
    monkeypatch.setattr(
        cli,
        "_read_fees",
        lambda *a: ((Decimal("2"), Decimal("5")), "read from your testnet account"),
    )
    good, bad = cli._fill_account_fees(envelope, cfg, {}, datetime(2026, 10, 8, tzinfo=UTC))
    channels = load_envelope(envelope).channels
    assert [(c.entry_fee_bps, c.exit_fee_bps) for c in channels] == [
        (Decimal("5"), Decimal("5")),
        (Decimal("1"), Decimal("5")),
    ]
    assert "written into envelope.toml" in good[0]
    # The maker fee written by hand is kept, and it is below what the account pays: said so.
    assert "prices okx/maker-taker maker at 1 bps and your account pays 2" in bad[0]

    monkeypatch.setattr(cli, "_read_fees", lambda *a: (None, "OKX_API_PASSPHRASE not set"))
    blank = tmp_path / "blank.toml"
    blank.write_text(
        '[[channel]]\nvenue = "okx"\nname = "taker-taker"\nentry_fee_bps =\nexit_fee_bps =\n',
        encoding="utf-8",
    )
    _, bad = cli._fill_account_fees(blank, cfg, {}, datetime(2026, 10, 8, tzinfo=UTC))
    assert "OKX_API_PASSPHRASE not set" in bad[0]


def test_a_standard_tier_fee_is_replaced_by_the_accounts_own_once_it_reports_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from trading_research_agents.envelope.load import load_envelope

    cli = __import__("importlib").import_module("trading_research_agents.cli.main")
    envelope = tmp_path / "envelope.toml"
    envelope.write_text(
        '[[channel]]\nvenue = "okx"\nname = "taker-taker"\nentry_fee_bps =\nexit_fee_bps =\n',
        encoding="utf-8",
    )
    cfg = SimpleNamespace(
        execution=SimpleNamespace(venue="okx", environment="testnet"),
        run=SimpleNamespace(venue_key="okx/taker-taker", instrument="BTCUSDT"),
        live=None,
    )
    when = datetime(2026, 10, 8, tzinfo=UTC)
    monkeypatch.setattr(
        cli, "_read_fees", lambda *a: ((Decimal("2"), Decimal("5")), "okx's standard tier ...")
    )
    cli._fill_account_fees(envelope, cfg, {}, when)
    assert "standard tier" in envelope.read_text(encoding="utf-8")
    assert load_envelope(envelope).channels[0].entry_fee_bps == Decimal("5")

    monkeypatch.setattr(
        cli, "_read_fees", lambda *a: ((Decimal("1.5"), Decimal("4")), "read from your account")
    )
    good, _ = cli._fill_account_fees(envelope, cfg, {}, when)
    assert load_envelope(envelope).channels[0].entry_fee_bps == Decimal("4")
    assert "read from your okx account" in envelope.read_text(encoding="utf-8")
    assert "written into envelope.toml" in good[0]


def test_a_written_fee_under_the_venues_standard_tier_is_said_to_price_too_cheap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    cli = __import__("importlib").import_module("trading_research_agents.cli.main")
    envelope = tmp_path / "envelope.toml"
    # A perpetual's fee, written for a desk that now trades the spot pair, which costs more.
    envelope.write_text(
        '[[channel]]\nvenue = "bybit"\nname = "taker-taker"\nentry_fee_bps = 5.5\n'
        "exit_fee_bps = 5.5\n",
        encoding="utf-8",
    )
    cfg = SimpleNamespace(
        execution=SimpleNamespace(venue="bybit", environment="demo", markets=("linear perpetual",)),
        run=SimpleNamespace(venue_key="bybit/taker-taker", instrument="SOLUSDT"),
        live=None,
    )
    monkeypatch.setattr(
        cli,
        "_read_fees",
        lambda *a: ((Decimal("10"), Decimal("10")), "bybit's standard tier from its market list"),
    )
    _, bad = cli._fill_account_fees(
        envelope,
        cfg,
        {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"},
        datetime(2026, 10, 17, tzinfo=UTC),
    )
    assert any("taker at 5.5 bps and bybit's standard tier is 10" in line for line in bad)
