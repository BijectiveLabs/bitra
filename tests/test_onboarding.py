"""From nothing to a backtest in four commands, each saying what comes next."""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from trading_research_agent.cli.main import main
from trading_research_agent.data import Rejection, Trade
from trading_research_agent.data.venues import register
from trading_research_agent.data.venues.bybit import Bybit

HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional"
)


def row(i: int) -> str:
    ts = 1735689600 + i * 30
    return f"{ts}.0001,BTCUSDT,Buy,0.01,{93500 + (i % 7)}.00,PlusTick,id-{i},0,0,0"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class LocalArchive:
    """A daily venue served from a local directory, with the bundled layout."""

    name = "local-archive"
    cadence = "daily"

    def __init__(self, base: str) -> None:
        self.base = base
        self._bybit = Bybit()

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str:
        if day is None:
            raise ValueError("daily")
        return f"{self.base}/{instrument}{day.isoformat()}.csv.gz"

    def checksum_url(self, archive_url: str) -> str | None:
        return None

    def normalise(self, lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
        for row_ in self._bybit.normalise(lines, instrument=instrument):
            yield replace(row_, venue=self.name) if isinstance(row_, Trade) else row_


@pytest.fixture
def served(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "www"
    root.mkdir()
    with gzip.open(root / "BTCUSDT2025-01-01.csv.gz", "wb") as fh:
        fh.write(("\n".join([HEADER, *(row(i) for i in range(400))]) + "\n").encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    register(LocalArchive(url))
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_four_commands_from_nothing_to_a_backtest(
    tmp_path: Path, served: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    project = tmp_path / "my-run"
    assert main(["init", str(project)]) == 0
    monkeypatch.chdir(project)
    config = project / "strategy.toml"
    config.write_text(
        config.read_text("utf-8").replace(
            'venue_key = "bybit/taker-taker"', 'venue_key = "local-archive/taker-taker"'
        ),
        "utf-8",
    )
    envelope = project / "envelope.toml"
    envelope.write_text(
        envelope.read_text("utf-8").replace(
            'venue = "bybit"\nname = "taker-taker"',
            'venue = "local-archive"\nname = "taker-taker"',
            1,
        ),
        "utf-8",
    )
    capsys.readouterr()

    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "ok    strategy.toml" in out and "todo  no dataset for BTCUSDT" in out

    assert main(["data", "get", "BTCUSDT", "2025-01-01"]) == 0
    out = capsys.readouterr().out
    assert "PASSED" in out and "ready: " in out and "now `tra backtest`" in out
    assert (project / "data" / "BTCUSDT-2025-01-01.parquet").exists()
    assert (project / "data" / "BTCUSDT-2025-01-01.manifest.json").exists()
    assert not (project / "data" / "BTCUSDT-funding.json").exists()  # this venue has no funding

    assert main(["check"]) == 1  # funding is the one thing still flagged
    out = capsys.readouterr().out
    assert "ok    dataset:" in out and "todo  funding" in out

    assert main(["backtest", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["label"] == "replay" and payload["prints"] == 400
    assert payload["out"].startswith("runs") and Path(payload["out"]).is_dir()


def test_backtest_names_what_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "p"
    assert main(["init", str(project)]) == 0
    monkeypatch.chdir(project)
    capsys.readouterr()
    assert main(["backtest"]) == 1
    err = capsys.readouterr().err
    assert "no dataset for BTCUSDT" in err and "tra data get BTCUSDT" in err
    monkeypatch.chdir(tmp_path)
    assert main(["data", "get", "BTCUSDT", "2025-01-01"]) == 1
    assert "pass --venue" in capsys.readouterr().err


def test_init_with_an_agent_makes_the_analyst_the_core(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "agent-run"
    assert main(["init", str(project), "--agent", "deepseek", "--model", "deepseek-flash"]) == 0
    out = capsys.readouterr().out
    assert "put DEEPSEEK_API_KEY in .env" in out
    text = (project / "strategy.toml").read_text("utf-8")
    assert 'agent = "technical_analyst"' in text and 'provider = "deepseek"' in text
    assert "[agents.pricing.deepseek-flash]" in text and "magnitude_bps = 20" in text
    from trading_research_agent.trading import load_strategy_config

    cfg = load_strategy_config(project / "strategy.toml")
    assert cfg.agents is not None and cfg.agents.magnitude_bps == 20
    monkeypatch.chdir(project)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "DEEPSEEK_API_KEY is not set" in out
    assert main(["init", str(project / "x"), "--agent", "deepseek"]) == 1
    assert "--agent needs --model" in capsys.readouterr().err
    assert main(["init", str(project / "y"), "--agent", "nope", "--model", "m"]) == 1
    assert "no provider named" in capsys.readouterr().err
