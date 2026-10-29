"""Evals: the same bars per model, figures a trader compares. Traces: the record, in order."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_session import EXAMPLES, analyst, config, dataset, final, scripted_session
from trading_research_agents.agents.evals import (
    EvalConfig,
    ModelSpec,
    evaluate,
    percentile,
    table,
)
from trading_research_agents.agents.trace import TraceError, lines, trace
from trading_research_agents.envelope import Envelope, load_envelope
from trading_research_agents.llm import FakeCompleter
from trading_research_agents.trading import load_strategy_config


def test_model_specs_parse_and_slug() -> None:
    spec = ModelSpec.parse("groq:openai/gpt-oss-120b")
    assert spec.provider == "groq" and spec.model == "openai/gpt-oss-120b"
    assert spec.slug == "groq-openai-gpt-oss-120b" and str(spec) == "groq:openai/gpt-oss-120b"
    assert ModelSpec.parse("ollama:qwen3:8b").model == "qwen3:8b"
    for bad in ("groq", ":m", "p:", "  :  "):
        with pytest.raises(ValueError, match="PROVIDER:MODEL"):
            ModelSpec.parse(bad)


def test_percentiles_are_nearest_rank() -> None:
    assert percentile([], 0.5) == 0
    assert percentile([7], 0.9) == 7
    assert percentile([5, 1, 3, 2, 4], 0.5) == 3
    assert percentile([5, 1, 3, 2, 4], 0.9) == 5
    assert percentile(list(range(1, 11)), 0.9) == 9


def test_an_eval_runs_each_model_over_the_same_bars_and_writes_the_rows(
    tmp_path: Path, envelope: Envelope
) -> None:
    prices = ["100", "100", "100", "101", "102", "103", "104", "105", "106", "107"]
    path = dataset(tmp_path, "AAAUSDT", prices)
    strategy = load_strategy_config(config(tmp_path))
    scripts: dict[str, list[Any]] = {
        # asked from the third bar on: eight calls each; one goes long and holds, one stays flat
        "a:trend": [analyst("flat")] + [analyst("long")] * 7,
        "b:flat": [analyst("flat")] * 8,
    }
    fakes: dict[str, FakeCompleter] = {}

    def completer_for(spec: ModelSpec) -> FakeCompleter:
        fakes[str(spec)] = FakeCompleter(scripts[str(spec)])
        return fakes[str(spec)]

    rows = evaluate(
        EvalConfig(
            strategy=strategy,
            envelope=envelope,
            instrument="AAAUSDT",
            dataset=path,
            models=(ModelSpec("a", "trend"), ModelSpec("b", "flat")),
            completer_for=completer_for,
            out_dir=tmp_path / "eval",
            kill_file=tmp_path / "KILL",
        )
    )
    assert [r.model for r in rows] == ["trend", "flat"]
    trend, flat = rows
    assert trend.bars == flat.bars and trend.calls == flat.calls == 8
    assert trend.failed_calls == 0 and trend.input_tokens > 0
    assert trend.intents >= 1 and flat.intents == 0 and flat.round_trips == 0
    assert flat.net_bps_total == Decimal(0) and flat.tokens_per_bps is None
    assert (tmp_path / "eval" / "a-trend" / "usage.jsonl").exists()
    assert (tmp_path / "eval" / "b-flat" / "run.json").exists()
    written = json.loads((tmp_path / "eval" / "eval.json").read_text("utf-8"))
    assert [r["model"] for r in written["rows"]] == ["trend", "flat"]
    assert written["rows"][1]["tokens_per_bps"] is None
    text = table(rows)
    assert text[0].startswith("model") and "a:trend" in text[1] and "b:flat" in text[2]
    assert len(fakes["a:trend"].calls) == 8


def test_an_eval_refuses_what_it_cannot_compare(tmp_path: Path, envelope: Envelope) -> None:
    strategy = load_strategy_config(config(tmp_path))
    path = dataset(tmp_path, "AAAUSDT", ["100"] * 4)
    base: dict[str, Any] = {
        "strategy": strategy,
        "envelope": envelope,
        "instrument": "AAAUSDT",
        "dataset": path,
        "completer_for": lambda spec: FakeCompleter([]),
        "out_dir": tmp_path / "eval",
    }
    with pytest.raises(ValueError, match="at least one model"):
        EvalConfig(models=(), **base)
    with pytest.raises(ValueError, match="named twice"):
        EvalConfig(models=(ModelSpec("a", "m"), ModelSpec("a", "m")), **base)
    with pytest.raises(FileNotFoundError):
        EvalConfig(models=(ModelSpec("a", "m"),), **{**base, "dataset": tmp_path / "no.parquet"})
    plain = load_strategy_config(EXAMPLES / "strategy.toml")
    with pytest.raises(Exception, match=r"\[agents\]"):
        EvalConfig(models=(ModelSpec("a", "m"),), **{**base, "strategy": plain})


def test_the_cli_evaluates_registered_providers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.cli.main import main
    from trading_research_agents.llm.providers import Provider, register

    register(
        Provider(
            "scripted-eval",
            None,
            None,
            True,
            lambda model, base_url: FakeCompleter([analyst("flat")] * 8),
        )
    )
    code = main(
        [
            "agent",
            "eval",
            "--model",
            "scripted-eval:one",
            "--model",
            "scripted-eval:two",
            "--config",
            str(config(tmp_path)),
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--dataset",
            f"AAAUSDT={dataset(tmp_path, 'AAAUSDT', ['100'] * 6)}",
            "--out",
            str(tmp_path / "eval-out"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and [r["model"] for r in payload["rows"]] == ["one", "two"]
    assert all(r["calls"] == 4 and r["failed_calls"] == 0 for r in payload["rows"])
    assert (tmp_path / "eval-out" / "eval.json").exists()
    load_envelope(EXAMPLES / "envelope.toml")


def test_a_trace_reads_a_session_and_a_run_back_in_order(
    tmp_path: Path, envelope: Envelope, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.cli.main import main

    calls = [analyst("flat"), analyst("long"), analyst("long"), analyst("flat")]
    audit_steps = [
        {
            "action": "call",
            "tool": "search_ledger",
            "arguments": {"ledger_path": str(tmp_path / "none.jsonl"), "query": "funding"},
            "answer": {},
            "thought": "look",
        },
        final({"findings": [], "verdict": "sound", "summary": "One round trip."}),
    ]
    result, _, _ = scripted_session(tmp_path, envelope, [*calls, *audit_steps])
    assert result.audit is not None
    session = trace(tmp_path / "out")
    assert session.kind == "session" and session.header["audit_verdict"] == "sound"
    # Only the auditor answers inside a session's own record; the lead's calls are its runs'.
    assert [e.who for e in session.roles] == ["auditor", "auditor", "auditor"]
    assert session.roles[0].detail == "sound: One round trip."
    assert session.roles[1].kind == "step" and session.roles[1].detail.startswith("search_ledger(")
    assert "->" in session.roles[1].detail and session.roles[2].detail == "answered"
    assert len(session.calls) == 6 and session.calls[0].who == "structure_lead"
    assert "total" in session.calls[-1].detail
    kinds = [e.kind for e in session.market]
    assert kinds[:2] == ["intent", "fill"]
    assert all(e.who == "BBBUSDT" for e in session.market)
    assert session.market[0].at.startswith("1970-01-01 00:0")

    run = trace(tmp_path / "out" / "BBBUSDT")
    assert run.kind == "run" and run.roles == [] and run.calls == []
    assert [e.kind for e in run.market] == kinds
    text = lines(session)
    assert text[0].startswith("session ") and "roles:" in text and "market (bar time, UTC):" in text
    with pytest.raises(TraceError, match="neither"):
        trace(tmp_path)

    code = main(["agent", "trace", str(tmp_path / "out"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["kind"] == "session" and len(payload["roles"]) == 3
    assert main(["agent", "trace", str(tmp_path / "nowhere")]) == 1
