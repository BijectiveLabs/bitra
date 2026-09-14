"""The bounded tool loop: closed catalog, checked arguments, capped steps, every step recorded."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trading_research_agent.agent import FakeCompleter
from trading_research_agent.agent.react import ToolLoopError, catalog_text, run_tool_loop
from trading_research_agent.agent.usage import BudgetLimits, MeteredCompleter, UsageLedger
from trading_research_agent.surface import ledger_search, runs_index

ANSWER_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def step(
    action: str,
    tool: str = "",
    arguments: dict[str, Any] | None = None,
    answer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "tool": tool,
        "arguments": arguments or {},
        "answer": answer or {},
        "thought": "t",
    }


def add(a: int, b: int = 1) -> dict[str, int]:
    """Add two numbers."""
    return {"sum": a + b}


def test_the_loop_calls_a_tool_then_answers_and_records_each_step() -> None:
    scripted = FakeCompleter(
        [step("call", "add", {"a": 2, "b": 3}), step("answer", answer={"ok": True})]
    )
    result = run_tool_loop(
        completer=scripted,
        system="S",
        question={"q": 1},
        tools={"add": add},
        answer_schema=ANSWER_SCHEMA,
        max_steps=4,
    )
    assert result.answer == {"ok": True} and result.calls == 2
    assert [s.tool for s in result.steps] == ["add", None]
    assert result.steps[0].observation == '{"sum": 5}' and result.steps[0].error is None
    second = json.loads(scripted.calls[1].user)
    assert second["steps_so_far"][0]["observation"] == '{"sum": 5}' and second["steps_left"] == 3
    assert "add(a: int, b: int = 1): Add two numbers." in catalog_text({"add": add})
    assert "Answer schema" in scripted.calls[0].system


def test_unknown_tools_bad_arguments_and_malformed_steps_are_errors_the_model_reads() -> None:
    scripted = FakeCompleter(
        [
            step("call", "delete_everything", {}),
            step("call", "add", {"a": 1, "c": 2}),
            step("call", "add", {}),
            "not json",
            step("answer", answer={"ok": False}),
        ]
    )
    result = run_tool_loop(
        completer=scripted,
        system="S",
        question={},
        tools={"add": add},
        answer_schema=ANSWER_SCHEMA,
        max_steps=6,
    )
    errors = [s.error for s in result.steps[:4]]
    assert errors[0] is not None and "no tool named 'delete_everything'" in errors[0]
    assert errors[1] is not None and "unknown arguments ['c']" in errors[1]
    assert errors[2] is not None and "missing arguments ['a']" in errors[2]
    assert errors[3] is not None and "malformed step" in errors[3]
    assert result.answer == {"ok": False} and result.calls == 5


def test_the_cap_and_the_budget_end_the_loop_without_an_answer(tmp_path: Path) -> None:
    forever = FakeCompleter([step("call", "add", {"a": 1})] * 3)
    with pytest.raises(ToolLoopError, match="no answer after 3 steps"):
        run_tool_loop(
            completer=forever,
            system="S",
            question={},
            tools={"add": add},
            answer_schema=ANSWER_SCHEMA,
            max_steps=3,
        )
    metered = MeteredCompleter(
        FakeCompleter([step("call", "add", {"a": 1})] * 5),
        role="auditor",
        provider="fake",
        limits=BudgetLimits(token_budget=10, max_calls_per_hour=100, min_interval_s=0),
        ledger=UsageLedger(tmp_path / "usage.jsonl"),
        clock=lambda: 0.0,
    )
    with pytest.raises(ToolLoopError, match="budget spent at step 2"):
        run_tool_loop(
            completer=metered,
            system="S",
            question={},
            tools={"add": add},
            answer_schema=ANSWER_SCHEMA,
            max_steps=5,
        )
    with pytest.raises(ValueError, match="max_steps"):
        run_tool_loop(
            completer=forever, system="S", question={}, tools={}, answer_schema={}, max_steps=0
        )


def test_long_observations_are_cut() -> None:
    def big() -> dict[str, str]:
        """A large answer."""
        return {"text": "x" * 100}

    scripted = FakeCompleter([step("call", "big"), step("answer", answer={"ok": True})])
    result = run_tool_loop(
        completer=scripted,
        system="S",
        question={},
        tools={"big": big},
        answer_schema=ANSWER_SCHEMA,
        max_steps=3,
        observation_limit=40,
    )
    assert (
        result.steps[0].observation is not None
        and "cut at 40 characters" in result.steps[0].observation
    )


def test_recall_tools_search_the_ledger_and_index_runs(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from trading_research_agent.ledger import (
        Axis,
        Ledger,
        LedgerEntry,
        ReversalPoint,
        Transferability,
        Verdict,
    )

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(
        LedgerEntry(
            entry_id="H-1",
            subject="funding settlement flow on perpetuals",
            verdict=Verdict.CLOSED,
            recorded_at=datetime(2026, 9, 1, tzinfo=UTC),
            layer=0,
            axis_of_death=(Axis.COST,),
            transferability=Transferability.ENVELOPE_BOUND,
            reversal_point=ReversalPoint("c_rt", Decimal("3.0"), "bps"),
            note="3 bps against 9 bps round trip",
        )
    )
    ledger.append(
        LedgerEntry(
            entry_id="H-2",
            subject="basis carry across venues",
            verdict=Verdict.OPEN,
            recorded_at=datetime(2026, 9, 2, tzinfo=UTC),
            layer=3,
            blocking_axis=Axis.DATA,
        )
    )
    hit = ledger_search(str(tmp_path / "ledger.jsonl"), "funding settlement")
    assert hit["matched"] == 1 and hit["rows"][0]["entry_id"] == "H-1"
    assert ledger_search(str(tmp_path / "ledger.jsonl"), "nothing here")["matched"] == 0
    with pytest.raises(ValueError, match="at least one word"):
        ledger_search(str(tmp_path / "ledger.jsonl"), "  ")

    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {"label": "replay", "instrument": "BTCUSDT", "round_trips": 2, "net_bps_total": "-1"}
        ),
        "utf-8",
    )
    index = runs_index(str(tmp_path / "runs"))
    assert index["total"] == 1 and index["runs"][0]["instrument"] == "BTCUSDT"
    assert runs_index(str(tmp_path / "runs"), instrument="ETHUSDT")["total"] == 0
    with pytest.raises(FileNotFoundError):
        runs_index(str(tmp_path / "nowhere"))
