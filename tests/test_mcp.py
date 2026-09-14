"""The MCP surface: seven read tools, refusals as messages, nothing that trades."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from trading_research_agent.trading import Ladder

mcp_sdk = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402 - after the skip

from trading_research_agent.mcp import build_server  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def call(name: str, arguments: dict[str, Any]) -> Any:
    async with Client(build_server(), raise_exceptions=True) as client:
        return await client.call_tool(name, arguments)


def test_the_server_lists_exactly_the_read_tools() -> None:
    async def names() -> tuple[list[str], dict[str, Any]]:
        async with Client(build_server(), raise_exceptions=True) as client:
            tools = await client.list_tools()
            return sorted(t.name for t in tools.tools), {t.name: t.annotations for t in tools.tools}

    listed, annotations = run(names())
    assert listed == [
        "audit_dataset",
        "check_registration",
        "cost_floor_check",
        "ladder_show",
        "ledger_search",
        "ledger_show",
        "run_report",
        "runs_index",
        "validate_trades",
    ]
    for name, note in annotations.items():
        assert note is not None and note.read_only_hint is True, name
    forbidden = {"place_order", "promote", "record", "demote", "run_live", "submit"}
    assert forbidden.isdisjoint(listed)


def test_cost_floor_returns_structured_content() -> None:
    result = run(
        call(
            "cost_floor_check",
            {"envelope_path": str(EXAMPLES / "envelope.toml"), "magnitude_bps": "3.0"},
        )
    )
    assert result.is_error is False
    body = result.structured_content
    assert body["outcome"] == "cleared" and body["channel"] == "hyperliquid/maker-maker"
    assert body["round_trip_bps"] == "3.0" and body["stopped"] is False
    stopped = run(
        call(
            "cost_floor_check",
            {"envelope_path": str(EXAMPLES / "envelope.toml"), "magnitude_bps": "1.0"},
        )
    )
    assert stopped.structured_content["stopped"] is True


def test_a_gate_refusal_is_a_message_the_model_reads(tmp_path: Path) -> None:
    result = run(call("cost_floor_check", {"envelope_path": "nope.toml", "magnitude_bps": "3"}))
    assert result.is_error is True
    assert "does not exist" in result.content[0].text
    bad = run(
        call(
            "cost_floor_check",
            {"envelope_path": str(EXAMPLES / "envelope.toml"), "magnitude_bps": "many"},
        )
    )
    assert bad.is_error is True and "must be a number" in bad.content[0].text
    both = run(
        call(
            "audit_dataset",
            {
                "archive_path": str(EXAMPLES / "mapping.toml"),
                "instrument": "X",
                "venue": "bybit",
                "mapping_path": str(EXAMPLES / "mapping.toml"),
            },
        )
    )
    assert both.is_error is True and "exactly one" in both.content[0].text


def test_audit_ladder_and_run_report_read_the_files(tmp_path: Path) -> None:
    csv = tmp_path / "vendor.csv"
    csv.write_text(
        "timestamp,price,amount,side,id\n1735689600097,93530.00,0.003,sell,a-1\n"
        "1735689600144,93530.00,0.991,SELL,a-2\n",
        encoding="utf-8",
    )
    audit = run(
        call(
            "audit_dataset",
            {
                "archive_path": str(csv),
                "instrument": "BTCUSDT",
                "mapping_path": str(EXAMPLES / "mapping.toml"),
            },
        )
    )
    assert audit.structured_content["passed"] is True
    assert audit.structured_content["rows_accepted"] == 2

    ladder = Ladder(tmp_path / "ladder.json")
    ladder.promote(evidence="layer 5 OPEN", signed_by="op")
    shown = run(call("ladder_show", {"ladder_path": str(tmp_path / "ladder.json")}))
    assert shown.structured_content["tier"] == "PAPER"

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"label": "replay", "fills": 2}), "utf-8")
    (run_dir / "trades.jsonl").write_text('{"a":1}\n{"a":2}\n', "utf-8")
    report = run(call("run_report", {"run_dir": str(run_dir)}))
    assert report.structured_content["closed_round_trips"] == 2
    assert report.structured_content["label"] == "replay"
