from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.cli.main import HANDLERS
from trading_research_agents.cli.parser import build_parser
from trading_research_agents.cli.record import record_report, record_status
from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.archive import SourceFile, write_flow_part
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import NS_PER_MINUTE

pytest.importorskip("pyarrow")

DATA_TOML = """
[data.ohlcv]
chain = ["ccxt"]

[record]
venue = "bybit"
instruments = ["BTCUSDT"]
streams = ["trades"]
"""


class FakePro:
    def __init__(self) -> None:
        self.markets = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "swap": True,
                "linear": True,
                "contract": True,
                "contractSize": 1,
                "precision": {"price": 0.1, "amount": 0.001},
            }
        }
        self.markets_by_id = {"BTCUSDT": [self.markets["BTC/USDT:USDT"]]}
        self.has: dict[str, Any] = {}
        self.closed = False

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    async def watch_trades(self, symbol: str) -> list[dict[str, Any]]:
        await asyncio.sleep(0.2)
        now = time.time_ns() // 1_000_000
        return [{"timestamp": now, "price": 93530.1, "amount": 0.01, "side": "buy", "id": str(now)}]

    async def close(self) -> None:
        self.closed = True


class FakeRest:
    has: dict[str, Any] = {}
    rateLimit = 20  # noqa: N815


def args_for(tmp_path: Path, *extra: str) -> Any:
    config = tmp_path / "data.toml"
    config.write_text(DATA_TOML, encoding="utf-8")
    return build_parser().parse_args(
        ["record", "--data-config", str(config), "--out", str(tmp_path / "data"), *extra]
    )


def test_the_commands_are_wired() -> None:
    assert ("record", "run") in HANDLERS and ("record", "status") in HANDLERS
    assert build_parser().parse_args(["record", "status"]).command == "status"
    # `--once` is the recorder's own; the console's one-shot `-c` must not read it as a command.
    parsed = build_parser().parse_args(["record", "--once", "2s"])
    assert parsed.once is None and parsed.record_for == "2s"
    assert parsed.command == "run" and build_parser().parse_args(["record"]).command == "run"


def test_record_once_runs_for_the_bounded_time_on_a_fake_venue(tmp_path: Path) -> None:
    pro = FakePro()
    payload, lines, code = record_report(
        args_for(tmp_path, "--once", "2s"), connect=lambda venue: (pro, FakeRest())
    )
    assert code == 0 and pro.closed and payload["markets"] == ["BTCUSDT"]
    events = (tmp_path / "data" / "market" / "events.jsonl").read_text(encoding="utf-8")
    said = [json.loads(line)["event"] for line in events.splitlines()]
    assert said[0] == "started" and said[-1] == "stopped"
    assert any("recorded" in line for line in lines)


def test_status_reads_coverage_and_gaps_from_the_records(tmp_path: Path) -> None:
    root = tmp_path / "data"
    now = time.time_ns()
    hour = now - now % (60 * NS_PER_MINUTE) - 60 * NS_PER_MINUTE
    made = FlowBarBuilder(venue="bybit", instrument="BTCUSDT", source="stream:bybit", start_ns=hour)
    bars = []
    for i in range(60):
        at = hour + i * NS_PER_MINUTE + 1
        trade = Trade(
            at, "bybit", "BTCUSDT", Decimal("93530.1"), Decimal("0.010"), Side.BUY, f"t{i}"
        )
        bars.extend(made.push(trade))
    bars.extend(made.finish(hour + 60 * NS_PER_MINUTE))
    write_flow_part(bars, root, price_scale=1, qty_scale=3, sources=(SourceFile("s", None),))
    events = root / "market" / "events.jsonl"
    rows = [
        {
            "at": datetime.now(UTC).isoformat(),
            "at_ns": now,
            "event": "gap_closed",
            "instrument": "BTCUSDT",
            "stream": "trades",
            "seconds": 12.5,
        },
        {
            "at": datetime.now(UTC).isoformat(),
            "at_ns": now,
            "event": "latency",
            "instrument": "BTCUSDT",
            "latency_p50_ms": 40,
            "latency_max_ms": 900,
        },
    ]
    events.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    payload, lines, code = record_status(args_for(tmp_path, "status"))
    market = payload["markets"]["BTCUSDT"]
    assert code == 0 and market["flow_minutes_24h"] == 60
    assert market["gap_seconds_24h"] == 12.5 and market["latency_max_ms"] == 900
    assert any("BTCUSDT" in line for line in lines)
