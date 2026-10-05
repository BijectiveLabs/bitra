"""Seven roles over recorded data: fixed order, every answer recorded, actions only tighten."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.session import SessionConfig, SessionError, run_session
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
    text = text[:start] + '[strategy]\nagent = "technical_analyst"\n\n' + text[end:] + AGENT_TABLES
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


def test_a_session_runs_the_roles_in_order_and_records_everything(
    tmp_path: Path, envelope: Envelope
) -> None:
    screener = {
        "kept": [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "flat prints"}],
        "excluded": [{"instrument": "BBBUSDT", "reason": "too few prints"}],
    }
    # AAAUSDT has 8 bars; the analyst is asked from the third bar on: 6 calls.
    analyst_calls = [
        analyst("flat"),
        analyst("long"),
        analyst("long"),
        analyst("flat"),
        analyst("flat"),
        analyst("flat"),
    ]
    risk = {
        "decision": "tighten",
        "limits": {"max_position": "0.02", "max_orders_per_minute": 3},
        "reason": "one round trip; hold the size down",
        "triggers": ["net_bps_total"],
    }
    allocation = {
        "size": "0.01",
        "reason": "declared",
        "allocations": [
            {"instrument": "AAAUSDT", "fraction": 0.5, "reason": "one instrument kept"}
        ],
    }
    audit = {
        "findings": [
            {"area": "tokens", "severity": "info", "detail": "tokens per round trip recorded"}
        ],
        "verdict": "sound",
        "summary": "One kept instrument, one round trip, budget respected.",
    }
    result, usage, fake = scripted_session(
        tmp_path, envelope, [screener, *analyst_calls, risk, allocation, final(audit)]
    )
    roles = [c.system[:40] for c in fake.calls]
    assert len(fake.calls) == 10
    assert [k["instrument"] for k in result.kept] == ["AAAUSDT"]
    assert [e["instrument"] for e in result.excluded] == ["BBBUSDT"]
    assert set(result.reports) == {"AAAUSDT"} and result.reports["AAAUSDT"].round_trips == 1
    assert result.risk is not None and result.risk["decision"] == "tighten"
    assert result.limits_after.max_position == Decimal("0.02")
    assert result.limits_after.max_orders_per_minute == 3
    assert result.limits_after.max_risk_per_trade_bps == Decimal("50")
    assert result.allocation == allocation["allocations"]
    assert result.audit is not None and result.audit["verdict"] == "sound"
    assert result.stopped_by is None and result.refusals == []
    out = tmp_path / "out"
    session = json.loads((out / "session.json").read_text("utf-8"))
    assert session["audit_verdict"] == "sound" and session["limits_after"]["max_position"] == "0.02"
    assert (out / "figures.json").exists() and (out / "audit.json").exists()
    assert (out / "AAAUSDT" / "trades.jsonl").exists()
    by_role = usage.by_role()
    assert by_role["technical_analyst"]["calls"] == 6 and by_role["auditor"]["calls"] == 1
    assert set(by_role) == {
        "market_screener",
        "technical_analyst",
        "risk_manager",
        "executor",
        "auditor",
    }
    recorded = sorted(p.name for p in out.glob("*-*.json"))
    assert recorded[0] == "01-market_screener.json" and recorded[-1].endswith("auditor.json")
    assert "tokens_per_round_trip" in fake.calls[-1].user and roles


def test_a_resumed_session_reuses_recorded_answers_and_finished_runs(
    tmp_path: Path, envelope: Envelope
) -> None:
    screener = {
        "kept": [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "flat prints"}],
        "excluded": [{"instrument": "BBBUSDT", "reason": "too few prints"}],
    }
    risk = {"decision": "continue", "limits": {}, "reason": "nothing fired", "triggers": []}
    allocation = {
        "size": "0.01",
        "reason": "declared",
        "allocations": [{"instrument": "AAAUSDT", "fraction": 1.0, "reason": "kept"}],
    }
    audit = {"findings": [], "verdict": "sound", "summary": "Flat session."}
    first, _, fake = scripted_session(
        tmp_path, envelope, [screener, *[analyst("flat")] * 6, risk, allocation, final(audit)]
    )
    assert len(fake.calls) == 10 and first.refusals == []

    # The second pass has nothing scripted: every answer must come from disk.
    second, usage, again = scripted_session(tmp_path, envelope, [], resume=True)
    assert again.calls == [] and second.stopped_by is None
    assert [k["instrument"] for k in second.kept] == ["AAAUSDT"]
    assert second.reports["AAAUSDT"].bars == first.reports["AAAUSDT"].bars
    assert second.reports["AAAUSDT"].net_bps_total == first.reports["AAAUSDT"].net_bps_total
    assert second.risk == first.risk and second.allocation == first.allocation
    assert second.audit is not None and second.audit["verdict"] == "sound"
    assert [r.split(":")[0] for r in second.refusals] == [
        "market_screener",
        "AAAUSDT",
        "risk_manager",
        "executor",
        "auditor",
    ]
    assert all("reused the recorded" in r for r in second.refusals)
    assert usage.by_role() == {}

    # A recorded run that stopped early is not a checkpoint: it runs again.
    run_json = tmp_path / "out" / "AAAUSDT" / "run.json"
    summary = json.loads(run_json.read_text("utf-8"))
    summary["stopped_by"] = "halt: 5 failed calls in a row"
    run_json.write_text(json.dumps(summary), "utf-8")
    third, _, again = scripted_session(tmp_path, envelope, [analyst("flat")] * 6, resume=True)
    assert len(again.calls) == 6 and third.stopped_by is None
    assert any("stopped by halt: 5 failed calls in a row; run again" in r for r in third.refusals)
    assert json.loads(run_json.read_text("utf-8"))["stopped_by"] is None


def test_the_screener_sees_layer_one_when_an_observation_is_declared(
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
    screener = {"kept": [], "excluded": [{"instrument": "AAAUSDT", "reason": "does not recur"}]}
    result, _, fake = scripted_session(
        tmp_path,
        envelope,
        [screener, {"decision": "continue", "limits": {}, "reason": "r", "triggers": []}],
        observation=tmp_path / "observation.toml",
    )
    candidates = {c["instrument"]: c for c in json.loads(fake.calls[0].user)["candidates"]}
    observed = candidates["AAAUSDT"]["observation"]
    assert observed["subject"] == "any print at all" and observed["recurrence"] == "recurs"
    assert observed["events"] == 7 and observed["events_per_subperiod"] == [3, 4]
    assert (
        observed["response_bps"]["n"] == 6 and observed["horizon_bars"] == 1
    )  # the last has no bar after
    assert "observation" in candidates["BBBUSDT"] and result.kept == []
    figures = json.loads((tmp_path / "out" / "figures.json").read_text("utf-8"))
    assert all("observation" in f for f in figures)


def test_the_screener_cannot_invent_and_unmentioned_candidates_are_excluded(
    tmp_path: Path, envelope: Envelope
) -> None:
    screener = {
        "kept": [{"instrument": "ZZZUSDT", "regime": "trending", "reason": "made up"}],
        "excluded": [],
    }
    audit = {"findings": [], "verdict": "questioned", "summary": "nothing was kept"}
    result, _, fake = scripted_session(tmp_path, envelope, [screener, final(audit)])
    assert result.kept == [] and {e["instrument"] for e in result.excluded} == {
        "AAAUSDT",
        "BBBUSDT",
    }
    assert any("was not given" in r for r in result.refusals)
    assert result.reports == {} and result.risk is None and result.allocation == []
    assert len(fake.calls) == 2  # screener, auditor; no analyst, no risk, no executor


def test_a_loosening_risk_decision_is_refused_and_a_demotion_goes_down(
    tmp_path: Path, envelope: Envelope
) -> None:
    Ladder(tmp_path / "ladder.json", live_available=True).promote(evidence="e", signed_by="o")
    screener = {
        "kept": [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "r"}],
        "excluded": [{"instrument": "BBBUSDT", "reason": "r"}],
    }
    analyst_calls = [analyst("flat")] * 6
    risk = {
        "decision": "tighten",
        "limits": {"max_position": "5"},
        "reason": "wants more",
        "triggers": [],
    }
    allocation = {"size": "0.01", "reason": "r", "allocations": []}
    audit = {"findings": [], "verdict": "questioned", "summary": "the risk manager asked to loosen"}
    result, _, _ = scripted_session(
        tmp_path, envelope, [screener, *analyst_calls, risk, allocation, final(audit)]
    )
    assert result.limits_after.max_position == Decimal("0.05")
    assert any("only tighter is allowed" in r for r in result.refusals)

    demoting = dict(risk, decision="demote", limits={}, reason="latency far above the declared")
    result, _, _ = scripted_session(
        tmp_path,
        envelope,
        [screener, *analyst_calls, demoting, allocation, final(audit)],
        out_dir=tmp_path / "out2",
    )
    assert Ladder(tmp_path / "ladder.json").tier == Tier.RESEARCH
    assert result.risk is not None and result.risk["decision"] == "demote"


def test_a_halt_arms_the_kill_switch_file_and_a_spent_budget_ends_the_session(
    tmp_path: Path, envelope: Envelope
) -> None:
    screener = {
        "kept": [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "r"}],
        "excluded": [{"instrument": "BBBUSDT", "reason": "r"}],
    }
    analyst_calls = [analyst("flat")] * 6
    halt = {
        "decision": "halt",
        "limits": {},
        "reason": "venue disagrees with the model",
        "triggers": ["x"],
    }
    result, _, fake = scripted_session(tmp_path, envelope, [screener, *analyst_calls, halt])
    assert (tmp_path / "KILL").read_text("utf-8").strip() == "venue disagrees with the model"
    assert result.stopped_by is not None and "halt" in result.stopped_by
    assert result.allocation == [] and result.audit is None
    assert len(fake.calls) == 8  # nothing asked after the halt

    fake2 = FakeCompleter([screener, analyst("flat")])
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
            datasets={"AAAUSDT": dataset(tmp_path, "AAAUSDT", ["100"] * 8)},
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

    screener = {
        "kept": [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "r"}],
        "excluded": [],
    }
    responses = [
        screener,
        *([analyst("flat")] * 6),
        {"decision": "continue", "limits": {}, "reason": "fine", "triggers": []},
        {
            "size": "0.01",
            "reason": "r",
            "allocations": [{"instrument": "AAAUSDT", "fraction": 1.0, "reason": "all"}],
        },
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
            f"AAAUSDT={dataset(tmp_path, 'AAAUSDT', ['100'] * 8)}",
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
        code == 0 and payload["audit"]["verdict"] == "sound" and payload["runs"] == {"AAAUSDT": 0}
    )
    assert payload["tokens"] > 0 and (tmp_path / "cli-out" / "usage.jsonl").exists()
    load_envelope(EXAMPLES / "envelope.toml")

    # Without an audit verdict the session is not cleared, whatever the runs earned.
    responses[:] = [screener, *([analyst("flat")] * 6)]
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
            f"AAAUSDT={tmp_path / 'AAAUSDT.parquet'}",
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
    assert code == 1 and payload["audit"] is None and payload["runs"] == {"AAAUSDT": 0}
