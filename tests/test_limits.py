"""The lines a client draws for a day: the pause switch, daily model budgets, trades per day and a
losing streak, and the two commands that set the switches. Each is counted from the records on
disk, so a restart cannot reset a day."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from test_config import agent_raw
from trading_research_agents.cli.main import main
from trading_research_agents.cli.run_command import held_back_by, losing_streak_today
from trading_research_agents.trading.config import parse_strategy_config
from trading_research_agents.trading.config_values import ConfigError

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **lines: Any) -> Any:
    monkeypatch.chdir(tmp_path)
    raw = agent_raw()
    for key, value in lines.items():
        table = "agents" if key.startswith("daily_") else "controls"
        raw[table][key] = value
    return parse_strategy_config(raw)


def usage(path: Path, *rows: tuple[datetime, int, str | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps({"at": at.timestamp(), "input_tokens": n, "output_tokens": 0, "cost": c})
            for at, n, c in rows
        )
        + "\n",
        encoding="utf-8",
    )


def test_nothing_declared_holds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert held_back_by(desk(tmp_path, monkeypatch), tmp_path / "runs" / "cycles", NOW) == ""


def test_the_pause_file_holds_entries_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = desk(tmp_path, monkeypatch)
    (tmp_path / "PAUSE").write_text("news blackout\n", encoding="utf-8")
    said = held_back_by(cfg, tmp_path / "runs" / "cycles", NOW)
    assert said.startswith("paused by PAUSE (news blackout)") and "tra pause --off" in said


def test_daily_budgets_count_every_ledger_since_midnight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "runs" / "cycles"
    cfg = desk(tmp_path, monkeypatch, daily_token_budget=1000, daily_cost_budget="0.50")
    yesterday = NOW - timedelta(days=1)
    usage(out / "usage.jsonl", (yesterday, 5000, "9"), (NOW - timedelta(hours=1), 600, "0.10"))
    assert held_back_by(cfg, out, NOW) == ""
    usage(out.parent / "audits" / "usage.jsonl", (NOW - timedelta(minutes=5), 400, "0.10"))
    assert held_back_by(cfg, out, NOW).startswith("daily token budget spent: 1000 of 1000")
    cheap = desk(tmp_path, monkeypatch, daily_cost_budget="0.20")
    assert "daily cost budget spent: 0.2000 of 0.20" in held_back_by(cheap, out, NOW)


def test_trades_per_day_count_every_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "runs" / "cycles"
    cfg = desk(tmp_path, monkeypatch, max_trades_per_day=2)
    for n, instrument in enumerate(("BTCUSDT", "ETHUSDT")):
        folder = out / f"2026-10-18T0{n}-00-00Z-{instrument}"
        folder.mkdir(parents=True)
        (folder / "intents.jsonl").write_text(
            json.dumps({"at": folder.name[:20], "instrument": instrument}) + "\n", encoding="utf-8"
        )
    assert "2 trade(s) today across every instrument" in held_back_by(cfg, out, NOW)


def test_a_losing_streak_counts_today_and_a_winner_resets_it(tmp_path: Path) -> None:
    trips = tmp_path / "round_trips.jsonl"
    rows = [
        {"at": "2026-10-17T20:00:00+00:00", "net": "-1"},
        {"at": "2026-10-18T01:00:00+00:00", "net": "-1"},
        {"at": "2026-10-18T02:00:00+00:00", "net": "2"},
        {"at": "2026-10-18T03:00:00+00:00", "net": "-1"},
        {"at": "2026-10-18T03:30:00+00:00", "note": "no exit fills"},
        {"at": "2026-10-18T04:00:00+00:00", "net": "-0.5"},
    ]
    trips.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    midnight = datetime(2026, 10, 18, tzinfo=UTC)
    assert losing_streak_today(trips, midnight) == 2
    assert losing_streak_today(tmp_path / "none.jsonl", midnight) == 0


def test_a_misspelt_budget_or_a_bad_count_is_refused() -> None:
    raw = agent_raw()
    raw["agents"]["daily_token_bugdet"] = 1000
    with pytest.raises(ConfigError, match="daily_token_bugdet"):
        parse_strategy_config(raw)
    raw = agent_raw()
    raw["controls"]["max_trades_per_day"] = 0
    with pytest.raises(ConfigError, match="max_trades_per_day"):
        parse_strategy_config(raw)


def test_tra_pause_and_tra_kill_set_and_lift_their_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "desk"
    assert main(["init", str(target), "--profile", "autonomous"]) == 0
    monkeypatch.chdir(target)
    capsys.readouterr()
    assert main(["pause", "--reason", "CPI at 12:30"]) == 0
    assert (target / "PAUSE").read_text(encoding="utf-8").startswith("CPI at 12:30 at ")
    assert main(["pause", "--off"]) == 0 and not (target / "PAUSE").exists()
    capsys.readouterr()
    assert main(["kill", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["set"] is True and (target / "KILL").exists()
    assert main(["kill", "--off"]) == 0 and not (target / "KILL").exists()
