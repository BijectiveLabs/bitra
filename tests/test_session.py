"""Seven roles over recorded data: fixed order, every answer recorded, actions only tighten."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.session import SessionConfig, SessionError
from trading_research_agents.agents.session_run import run_session
from trading_research_agents.data import Side, Trade
from trading_research_agents.envelope import Envelope, load_envelope
from trading_research_agents.llm import FakeCompleter
from trading_research_agents.llm.usage import BudgetLimits, MeteredCompleter, UsageLedger
from trading_research_agents.surface import ActionRefusedError, demote, tighten_limits
from trading_research_agents.trading import Ladder, Tier, load_strategy_config
from trading_research_agents.trading.controls import KillSwitch, Limits

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MINUTE = 60 * 1_000_000_000

AGENT_TABLES = """
[agents]
provider = "local"
base_url = "http://localhost:1/v1"
model = "scripted"
mechanism = "positions forced to close into settlement"
token_budget = 500000
max_calls_per_hour = 1000
min_interval_s = 0
lookback_bars = 4
"""


MOVING = ["50", "51", "50", "52", "50", "53"]


def dataset(tmp_path: Path, name: str, prices: list[str]) -> Path:
    pytest.importorskip("pyarrow")
    from trading_research_agents.data.parquet import write_trades

    rows = [
        Trade(i * MINUTE + 1, "bybit", name, Decimal(p), Decimal("0.01"), Side.BUY, f"{name}-{i}")
        for i, p in enumerate(prices)
    ]
    path = tmp_path / f"{name}.parquet"
    write_trades(rows, path, price_scale=2, qty_scale=3)
    return path


def config(tmp_path: Path) -> Path:
    text = (EXAMPLES / "strategy.toml").read_text(encoding="utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    text = text[:start] + '[strategy]\nagent = "structure_lead"\n\n' + text[end:] + AGENT_TABLES
    text = text.replace('kill_switch = "KILL"', f'kill_switch = "{(tmp_path / "KILL").as_posix()}"')
    path = tmp_path / "strategy.toml"
    path.write_text(text, encoding="utf-8")
    return path


def analyst(target: str, confidence: float = 0.9) -> dict[str, Any]:
    return {
        "target": target,
        "setup": "no_setup" if target == "flat" else "range_reversion",
        "reason": f"says {target}",
        "figures_used": [{"figure": "volume_ratio", "value": "1.0"}],
        "confidence": confidence,
        "horizon_bars": 3,
    }


def final(answer: dict[str, Any]) -> dict[str, Any]:
    """The auditor answers through the tool loop: a step that carries the answer."""
    return {"action": "answer", "tool": "", "arguments": {}, "answer": answer, "thought": "done"}


def scripted_session(
    tmp_path: Path, envelope: Envelope, responses: list[Any], **overrides: Any
) -> tuple[Any, UsageLedger, FakeCompleter]:
    fake = FakeCompleter(responses)
    usage = UsageLedger(tmp_path / "out" / "usage.jsonl")
    limits = BudgetLimits(token_budget=500000, max_calls_per_hour=1000, min_interval_s=0)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            metered[role] = MeteredCompleter(
                fake, role=role, provider="fake", limits=limits, ledger=usage, clock=lambda: 0.0
            )
        return metered[role]

    settings: dict[str, Any] = {
        "strategy": load_strategy_config(config(tmp_path)),
        "envelope": envelope,
        "datasets": {
            "AAAUSDT": dataset(tmp_path, "AAAUSDT", ["100"] * 8),
            "BBBUSDT": dataset(tmp_path, "BBBUSDT", ["50", "51", "50", "52", "50", "53"]),
        },
        "out_dir": tmp_path / "out",
        "completer_for": completer_for,
        "usage": usage,
        "magnitude_bps": Decimal("20"),
        "ladder_path": tmp_path / "ladder.json",
        "kill_file": tmp_path / "KILL",
    }
    settings.update(overrides)
    return run_session(SessionConfig(**settings)), usage, fake


def test_a_session_screens_by_rule_runs_the_lead_and_records_everything(
    tmp_path: Path, envelope: Envelope
) -> None:
    audit = {
        "findings": [
            {"area": "tokens", "severity": "info", "detail": "tokens per round trip recorded"}
        ],
        "verdict": "sound",
        "summary": "One kept instrument, budget respected.",
    }
    # BBBUSDT has 6 bars; the lead is asked from the third bar on: 4 calls.
    result, usage, fake = scripted_session(
        tmp_path, envelope, [*[analyst("flat")] * 4, final(audit)]
    )
    assert len(fake.calls) == 5
    # The screen is a written rule: a dataset that never moved is excluded by name.
    assert [k["instrument"] for k in result.kept] == ["BBBUSDT"]
    assert result.excluded == [
        {"instrument": "AAAUSDT", "reason": "no volatility: the figures are degenerate"}
    ]
    assert result.risk is not None and result.risk["decision"] == "continue"
    assert result.risk["decided_by"] == "rule"
    assert result.allocation == [
        {"instrument": "BBBUSDT", "fraction": 1.0, "reason": "an equal share"}
    ]
    assert result.audit is not None and result.audit["verdict"] == "sound"
    assert result.stopped_by is None and result.refusals == []
    out = tmp_path / "out"
    session = json.loads((out / "session.json").read_text("utf-8"))
    assert session["audit_verdict"] == "sound" and session["risk"]["decision"] == "continue"
    assert (out / "figures.json").exists() and (out / "audit.json").exists()
    # Only the lead and the auditor are models; everything else asked no one.
    assert set(usage.by_role()) == {"structure_lead", "auditor"}
    assert "tokens_per_round_trip" in fake.calls[-1].user


def test_a_resumed_session_reuses_recorded_answers_and_finished_runs(
    tmp_path: Path, envelope: Envelope
) -> None:
    audit = {"findings": [], "verdict": "sound", "summary": "Flat session."}
    first, _, fake = scripted_session(tmp_path, envelope, [*[analyst("flat")] * 4, final(audit)])
    assert len(fake.calls) == 5 and first.refusals == []

    # The second pass has nothing scripted: every answer must come from disk.
    second, usage, again = scripted_session(tmp_path, envelope, [], resume=True)
    assert again.calls == [] and second.stopped_by is None
    assert second.kept == first.kept and second.allocation == first.allocation
    assert second.reports["BBBUSDT"].bars == first.reports["BBBUSDT"].bars
    assert second.audit is not None and second.audit["verdict"] == "sound"
    assert [r.split(":")[0] for r in second.refusals] == ["BBBUSDT", "auditor"]
    assert all("reused the recorded" in r for r in second.refusals)
    assert usage.by_role() == {}

    # A recorded run that stopped early is not a checkpoint: it runs again.
    run_json = tmp_path / "out" / "BBBUSDT" / "run.json"
    summary = json.loads(run_json.read_text("utf-8"))
    summary["stopped_by"] = "halt: 5 failed calls in a row"
    run_json.write_text(json.dumps(summary), "utf-8")
    third, _, again = scripted_session(tmp_path, envelope, [analyst("flat")] * 4, resume=True)
    assert len(again.calls) == 4 and third.stopped_by is None
    assert any("stopped by halt: 5 failed calls in a row; run again" in r for r in third.refusals)
    assert json.loads(run_json.read_text("utf-8"))["stopped_by"] is None


def test_the_screen_reads_layer_one_when_an_observation_is_declared(
    tmp_path: Path, envelope: Envelope
) -> None:
    (tmp_path / "observation.toml").write_text(
        """
[observation]
subject = "any print at all"
interval_s = 60
horizon_bars = 1
min_events = 3
subperiods = 2
min_subperiods_with_events = 2

[event]
feature = "abs_return_bps"
window_bars = 1
comparison = ">="
threshold = 0
""",
        "utf-8",
    )
    scripted_session(
        tmp_path,
        envelope,
        [*[analyst("flat")] * 4, final({"findings": [], "verdict": "sound", "summary": "s"})],
        observation=tmp_path / "observation.toml",
    )
    figures = json.loads((tmp_path / "out" / "figures.json").read_text("utf-8"))
    observed = {f["instrument"]: f["observation"] for f in figures}["AAAUSDT"]
    assert observed["subject"] == "any print at all" and observed["recurrence"] == "recurs"
    assert observed["events"] == 7 and observed["events_per_subperiod"] == [3, 4]
    # The last print has no bar after it to measure a response on.
    assert observed["response_bps"]["n"] == 6 and observed["horizon_bars"] == 1
    assert all("observation" in f for f in figures)


def test_a_spent_budget_ends_the_session(tmp_path: Path, envelope: Envelope) -> None:
    fake2 = FakeCompleter([analyst("flat"), analyst("flat")])
    usage = UsageLedger(tmp_path / "out3" / "usage.jsonl")
    tight = BudgetLimits(token_budget=100, max_calls_per_hour=100, min_interval_s=0)

    def completer_for(role: str) -> MeteredCompleter:
        return MeteredCompleter(
            fake2, role=role, provider="fake", limits=tight, ledger=usage, clock=lambda: 0.0
        )

    result = run_session(
        SessionConfig(
            strategy=load_strategy_config(config(tmp_path)),
            envelope=envelope,
            datasets={
                "BBBUSDT": dataset(tmp_path, "BBBUSDT", ["50", "51", "50", "52", "50", "53"])
            },
            out_dir=tmp_path / "out3",
            completer_for=completer_for,
            usage=usage,
            magnitude_bps=Decimal("20"),
            ladder_path=tmp_path / "ladder3.json",
        )
    )
    assert result.stopped_by is not None and "token budget" in result.stopped_by


def test_session_config_refuses_what_it_cannot_run(tmp_path: Path, envelope: Envelope) -> None:
    plain = load_strategy_config(EXAMPLES / "strategy.toml")
    with pytest.raises(SessionError, match=r"\[agents\]"):
        SessionConfig(
            strategy=plain,
            envelope=envelope,
            datasets={"X": tmp_path / "x.parquet"},
            out_dir=tmp_path,
            completer_for=lambda role: FakeCompleter([]),
            usage=UsageLedger(tmp_path / "u.jsonl"),
            magnitude_bps=Decimal("1"),
            ladder_path=tmp_path / "l.json",
        )
    with pytest.raises(SessionError, match="at least one dataset"):
        SessionConfig(
            strategy=load_strategy_config(config(tmp_path)),
            envelope=envelope,
            datasets={},
            out_dir=tmp_path,
            completer_for=lambda role: FakeCompleter([]),
            usage=UsageLedger(tmp_path / "u.jsonl"),
            magnitude_bps=Decimal("1"),
            ladder_path=tmp_path / "l.json",
        )


def test_actions_only_tighten_and_only_go_down(tmp_path: Path) -> None:
    current = Limits(Decimal("50"), Decimal("0.05"), 6, Decimal("300"))
    tightened = tighten_limits(current, {"max_position": "0.01", "daily_loss_limit_bps": 300})
    assert tightened.after.max_position == Decimal("0.01") and tightened.changed == (
        "max_position",
    )
    with pytest.raises(ActionRefusedError, match="only tighter"):
        tighten_limits(current, {"max_risk_per_trade_bps": "60"})
    with pytest.raises(ActionRefusedError, match="not a limit"):
        tighten_limits(current, {"max_leverage": 3})
    with pytest.raises(ActionRefusedError, match="stay positive"):
        tighten_limits(current, {"max_position": "0"})
    with pytest.raises(ActionRefusedError, match="integer"):
        tighten_limits(current, {"max_orders_per_minute": "3"})
    ladder = Ladder(tmp_path / "l.json", live_available=True)
    ladder.promote(evidence="e", signed_by="o")
    assert demote(ladder, reason="why").to_tier == Tier.RESEARCH
    with pytest.raises(ActionRefusedError, match="states its reason"):
        demote(ladder, reason=" ")
    switch = KillSwitch()
    from trading_research_agents.surface import arm_kill_switch, refuse

    assert arm_kill_switch(switch, reason="stop", touch=tmp_path / "K") == "stop" and switch.armed
    with pytest.raises(ActionRefusedError, match="needs an operator"):
        refuse("promote")


def test_the_cli_runs_a_session_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research_agents.cli.main import main
    from trading_research_agents.llm.providers import Provider, register

    responses = [
        *([analyst("flat")] * 4),
        final({"findings": [], "verdict": "sound", "summary": "ok"}),
    ]
    register(
        Provider(
            "scripted-test", None, None, True, lambda model, base_url: FakeCompleter(responses)
        )
    )
    cfg = config(tmp_path)
    cfg.write_text(
        cfg.read_text("utf-8").replace('provider = "local"', 'provider = "scripted-test"'), "utf-8"
    )
    code = main(
        [
            "agent",
            "session",
            "--config",
            str(cfg),
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--dataset",
            f"BBBUSDT={dataset(tmp_path, 'BBBUSDT', ['50', '51', '50', '52', '50', '53'])}",
            "--magnitude",
            "20",
            "--out",
            str(tmp_path / "cli-out"),
            "--ladder",
            str(tmp_path / "ladder.json"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert (
        code == 0 and payload["audit"]["verdict"] == "sound" and payload["runs"] == {"BBBUSDT": 0}
    )
    assert payload["tokens"] > 0 and (tmp_path / "cli-out" / "usage.jsonl").exists()
    load_envelope(EXAMPLES / "envelope.toml")

    # Without an audit verdict the session is not cleared, whatever the runs earned.
    responses[:] = [*([analyst("flat")] * 4)]
    register(
        Provider(
            "scripted-short", None, None, True, lambda model, base_url: FakeCompleter(responses)
        )
    )
    cfg.write_text(
        cfg.read_text("utf-8").replace('provider = "scripted-test"', 'provider = "scripted-short"'),
        "utf-8",
    )
    code = main(
        [
            "agent",
            "session",
            "--config",
            str(cfg),
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--dataset",
            f"BBBUSDT={tmp_path / 'BBBUSDT.parquet'}",
            "--magnitude",
            "20",
            "--out",
            str(tmp_path / "cli-short"),
            "--ladder",
            str(tmp_path / "ladder.json"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["audit"] is None and payload["runs"] == {"BBBUSDT": 0}


def test_a_bar_that_closed_before_the_entry_filled_never_closes_the_position(
    tmp_path: Path, envelope: Envelope
) -> None:
    prices = ["50", "51", "50", "52", "50", "53", "49", "54", "48", "55"]
    calls = [analyst("flat"), *[analyst("long")] * 3, *[analyst("flat")] * 4]
    audit = {"findings": [], "verdict": "sound", "summary": "s"}
    scripted_session(
        tmp_path,
        envelope,
        [*calls, final(audit)],
        datasets={"CCCUSDT": dataset(tmp_path, "CCCUSDT", prices)},
    )
    trades = (tmp_path / "out" / "CCCUSDT" / "trades.jsonl").read_text("utf-8").splitlines()
    rows = [json.loads(line) for line in trades]
    assert rows and all(row["exit_ts_ns"] >= row["entry_ts_ns"] for row in rows)
