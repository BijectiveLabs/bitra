"""The risk manager inside `tra run`: what it may decide, and that it only makes things smaller."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.risk_manager import run as risk
from trading_research_agents.llm.completer import FakeCompleter
from trading_research_agents.llm.form import FormError
from trading_research_agents.trading.controls import Limits

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def answer(decision: str = "continue", **fields: Any) -> str:
    body: dict[str, Any] = {
        "decision": decision,
        "size_multiple": None,
        "pause_instruments": [],
        "hold_hours": 6,
        "reason": "three losses in a row on one coin",
        "triggers": ["losing_streak 3"],
    }
    body.update(fields)
    return json.dumps(body)


def test_an_answer_that_would_add_size_or_pause_nothing_is_refused() -> None:
    assert risk.parse(answer()).decision == "continue"
    cut = risk.parse(answer("reduce", size_multiple=0.5))
    assert cut.size_multiple == Decimal("0.5")
    for wrong in (1.5, 1, 0, None):
        with pytest.raises(FormError):
            risk.parse(answer("reduce", size_multiple=wrong))
    with pytest.raises(FormError, match="names the instruments"):
        risk.parse(answer("pause"))
    assert risk.parse(answer("pause", pause_instruments=["filusdt"])).pause_instruments == (
        "FILUSDT",
    )
    assert risk.parse(answer(hold_hours=500)).hold_hours == risk.MAX_HOLD_HOURS


def test_what_is_in_force_only_tightens_survives_a_restart_and_lapses(tmp_path: Path) -> None:
    held = risk.Overrides.load(tmp_path, NOW)
    assert held.multiple(NOW) == 1 and held.pause_of("BTCUSDT", NOW) is None
    held.apply(risk.parse(answer("reduce", size_multiple=0.5)), NOW)
    # A milder reduction later does not undo the deeper cut.
    held.apply(risk.parse(answer("reduce", size_multiple=0.8)), NOW)
    held.apply(risk.parse(answer("pause", pause_instruments=["FILUSDT"])), NOW)

    again = risk.Overrides.load(tmp_path, NOW + timedelta(hours=1))
    assert again.multiple(NOW + timedelta(hours=1)) == Decimal("0.5")
    assert again.pause_of("filusdt", NOW + timedelta(hours=1)) is not None
    assert again.pause_of("BTCUSDT", NOW + timedelta(hours=1)) is None

    later = NOW + timedelta(hours=7)
    lapsed = risk.Overrides.load(tmp_path, later)
    assert lapsed.multiple(later) == 1 and lapsed.pause_of("FILUSDT", later) is None


def test_the_brief_is_what_the_run_recorded(tmp_path: Path) -> None:
    trips = [("FILUSDT", "-0.4"), ("SUIUSDT", "0.9"), ("FILUSDT", "-0.2"), ("NEARUSDT", "-0.3")]
    (tmp_path / "round_trips.jsonl").write_text(
        "".join(json.dumps({"instrument": i, "net": n}) + "\n" for i, n in trips),
        encoding="utf-8",
    )
    got = risk.brief(
        tmp_path, limits=Limits(), overrides=risk.Overrides.load(tmp_path, NOW), alarms=[], now=NOW
    )
    rows = got["round_trips"]
    assert rows["closed"] == 4 and rows["last_20_wins"] == 1 and rows["losing_streak"] == 2
    assert rows["per_instrument_last_20"]["FILUSDT"] == {"trips": 2, "wins": 0, "net": "-0.6"}
    assert got["limits"]["daily_loss_limit_bps"] == "300"


def asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str, kill: Path | None) -> Any:
    import importlib

    from trading_research_agents.trading.config import AuditPolicy

    cli = importlib.import_module("trading_research_agents.cli.main")

    fake = FakeCompleter([reply])
    monkeypatch.setattr(cli, "_metered_roles", lambda *a, **k: lambda role: fake)
    audits = cli.Auditing(
        policy=AuditPolicy(),
        settings=None,
        config=tmp_path / "strategy.toml",
        runs=tmp_path / "runs" / "cycles",
        clock=lambda: NOW.timestamp(),
        limits=Limits(),
        kill_switch=kill,
    )
    (tmp_path / "runs" / "cycles").mkdir(parents=True)
    return audits._risk(tmp_path / "runs" / "audits", ())


def test_a_reduction_is_put_in_force_and_written_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = asked(tmp_path, monkeypatch, answer("reduce", size_multiple=0.5), None)
    assert any("0.5x size" in line for line in said)
    runs = tmp_path / "runs" / "cycles"
    assert risk.Overrides.load(runs, NOW).multiple(NOW) == Decimal("0.5")
    assert "reduce" in (runs / "risk_decisions.jsonl").read_text("utf-8")


def test_a_halt_arms_the_kill_switch_or_pauses_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kill = tmp_path / "KILL"
    said = asked(tmp_path / "a", monkeypatch, answer("halt"), kill)
    assert kill.is_file() and any("flatten and stop" in line for line in said)

    asked(tmp_path / "b", monkeypatch, answer("halt"), None)
    held = risk.Overrides.load(tmp_path / "b" / "runs" / "cycles", NOW)
    assert held.pause_of("ANYUSDT", NOW) is not None


def test_a_risk_manager_that_cannot_answer_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = asked(tmp_path, monkeypatch, "not json", None)
    assert "limits unchanged" in said[0]
    assert not (tmp_path / "runs" / "cycles" / risk.STATE).exists()


def test_todays_loss_is_given_in_bps_of_equity_beside_the_money(tmp_path: Path) -> None:
    trips = [
        ("2026-10-09T10:00:00+00:00", "-9"),
        ("2026-10-10T09:00:00+00:00", "-3"),
        ("2026-10-10T11:00:00+00:00", "-2"),
    ]
    (tmp_path / "round_trips.jsonl").write_text(
        "".join(json.dumps({"at": a, "instrument": "SOLUSDT", "net": n}) + "\n" for a, n in trips),
        encoding="utf-8",
    )
    (tmp_path / "equity.jsonl").write_text(
        json.dumps({"day": "2026-10-10", "equity": "5000"}) + "\n", encoding="utf-8"
    )
    got = risk.brief(
        tmp_path, limits=Limits(), overrides=risk.Overrides.load(tmp_path, NOW), alarms=[], now=NOW
    )
    assert got["today"]["round_trips"] == 2 and got["today"]["net"] == "-5"
    assert got["today"]["net_bps_of_equity"] == "-10.00"
    assert "quote currency" in got["units"]["net"]
