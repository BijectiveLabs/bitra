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
