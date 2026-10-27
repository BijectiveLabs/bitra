"""What wakes an auditor, and what is none of its business.

A gate refusing is the software working, so it is not an alarm. A cycle that stopped halfway, a
model call that never came back, a data chain that ran out and a venue that would not take the order
are all things an operator on a server would otherwise find out about from a log, or not at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.alarms import DATA, PROVIDER, STOPPED, VENUE, Watch, alarms_of
from trading_research_agents.trading.config_values import ConfigError


def record(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cycle_id": "2026-09-29T10-00-00Z",
        "refusals": [],
        "agents": {"failed": {}},
        "validators": None,
        "stopped_by": None,
        "intent": None,
        "placed": None,
    }
    base.update(fields)
    return base


def test_a_gate_saying_no_is_not_an_alarm() -> None:
    quiet = record(
        refusals=[
            "structure_lead: flat, so there is nothing to price",
            "targets: the claimed edge is 1.94x the round trip and the desk declared 2.0x",
            "trigger: the mid has moved 2.00 bps since the last cycle",
        ]
    )
    assert alarms_of(quiet) == ()


def test_the_run_stopping_halfway_is_the_alarm_an_operator_would_never_see() -> None:
    stopped = record(
        stopped_by="structure_lead: BudgetSpentError: token budget spent: 200100 of 200000",
        agents={"failed": {"structure_lead": "ProviderError: 429 rate limit"}},
    )
    fired = alarms_of(stopped)
    kinds = [a.kind for a in fired]
    assert kinds == [STOPPED, PROVIDER]
    assert fired[0].at == "2026-09-29T10-00-00Z"
    assert "429" in fired[1].detail


def test_data_and_venue_trouble_each_fire_once() -> None:
    fired = alarms_of(
        record(
            refusals=[
                "data funding: every source refused",
                "data ohlcv: used while stale, because the config allows it",
                "venue: InsufficientFunds: not enough margin",
            ],
            placed={"sent": False, "error": "InsufficientFunds"},
        )
    )
    assert [a.kind for a in fired] == [DATA, VENUE]
    assert fired[0].detail.startswith("data funding")


def test_one_cooldown_per_kind_so_one_broken_thing_is_not_reported_every_five_minutes() -> None:
    watch = Watch(cooldown_s=1800)
    first = alarms_of(record(stopped_by="structure_lead: ProviderError"))
    assert [a.kind for a in watch.due(first, now=1000.0)] == [STOPPED]
    # The same thing, eight minutes later: already reported.
    assert watch.due(first, now=1480.0) == ()
    # A different kind is a different thing, and does not wait behind the first.
    venue = alarms_of(record(refusals=["venue: OrderRejected"]))
    assert [a.kind for a in watch.due(venue, now=1481.0)] == [VENUE]
    # Past the cooldown, the first one is worth saying again.
    assert [a.kind for a in watch.due(first, now=3000.0)] == [STOPPED]
    with pytest.raises(ValueError, match="not negative"):
        Watch(cooldown_s=-1)


def test_the_policy_is_read_from_the_project(tmp_path: Path) -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import parse_strategy_config

    default = parse_strategy_config(agent_raw())
    assert default.audit.on_alarm and default.audit.quant_every_cycles is None
    assert default.audit.alarm_cooldown_s == 1800 and not default.audit.idle

    declared = parse_strategy_config(
        agent_raw(audit={"on_alarm": False, "quant_every_cycles": 20, "alarm_cooldown_s": 600})
    )
    assert not declared.audit.on_alarm and declared.audit.quant_every_cycles == 20
    with pytest.raises(ConfigError, match="at least 1"):
        parse_strategy_config(agent_raw(audit={"quant_every_cycles": 0}))
    with pytest.raises(ConfigError, match="true or false"):
        parse_strategy_config(agent_raw(audit={"on_alarm": "yes"}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(audit={"on_alert": True}))


def test_the_scheduler_asks_the_right_auditor_for_the_right_reason(tmp_path: Path) -> None:
    from trading_research_agents.cli.auditing import Auditing
    from trading_research_agents.trading.config_gates import AuditPolicy

    clock = [1000.0]
    audits = Auditing(
        policy=AuditPolicy(
            on_alarm=True, alarm_cooldown_s=900, quant_every_cycles=3, risk_manager=False
        ),
        settings=None,
        config=tmp_path / "strategy.toml",
        runs=tmp_path / "runs" / "cycles",
        clock=lambda: clock[0],
    )
    # A clean cycle, and not yet the third: nobody is asked.
    assert audits.due(record(), 1) == ((), ())

    who, fired = audits.due(record(stopped_by="structure_lead: ProviderError: 429"), 2)
    assert who == ("tech_auditor",) and [a.kind for a in fired] == [STOPPED]

    # The same failure on the next cycle is inside the cooldown, but the count is up.
    clock[0] = 1100.0
    who, fired = audits.due(record(stopped_by="structure_lead: ProviderError: 429"), 3)
    assert who == ("quant_auditor",) and fired == ()


def test_an_auditor_that_cannot_run_is_a_line_not_the_end_of_the_run(tmp_path: Path) -> None:
    from trading_research_agents.cli.auditing import Auditing
    from trading_research_agents.trading.config_gates import AuditPolicy

    audits = Auditing(
        policy=AuditPolicy(on_alarm=True),
        settings=None,  # nothing to build a completer from, so the audit will fail
        config=tmp_path / "strategy.toml",
        runs=tmp_path / "runs" / "cycles",
    )
    said = audits.after(record(stopped_by="structure_lead: ProviderError"), 1, as_json=True)
    assert any("the audit could not run" in line for line in said)


def auditing(tmp_path: Path, clock: list[float], **policy: Any) -> Any:
    from trading_research_agents.cli.auditing import Auditing
    from trading_research_agents.trading.config_gates import AuditPolicy

    return Auditing(
        policy=AuditPolicy(**{"risk_manager": False, **policy}),
        settings=None,
        config=tmp_path / "strategy.toml",
        runs=tmp_path / "runs" / "cycles",
        clock=lambda: clock[0],
    )


DAY = 1_791_590_400.0  # 2026-10-10T00:00:00Z


def test_the_daily_loss_limit_wakes_the_quant_auditor_once_that_day(tmp_path: Path) -> None:
    from trading_research_agents.agents.alarms import DAILY_LOSS, DAILY_LOSS_MARK

    clock = [DAY + 3600]
    audits = auditing(tmp_path, clock, alarm_cooldown_s=60)
    limit = record(stopped_by=f"{DAILY_LOSS_MARK} 210 bps since the day began, at or past 200")
    who, fired = audits.due(limit, 1)
    # The limit working is not a breakage, so the technical auditor is left alone.
    assert who == ("quant_auditor",) and [a.kind for a in fired] == [DAILY_LOSS]
    # Every later cycle that day says the same thing, past the cooldown; it was already looked at.
    clock[0] += 7200
    assert audits.due(limit, 2) == ((), ())
    # A restart keeps the record, and the next day is a new day.
    clock[0] = DAY + 86_400 + 60
    assert auditing(tmp_path, clock, alarm_cooldown_s=60).due(limit, 1)[0] == ("quant_auditor",)


def write_trips(tmp_path: Path, nets: list[str]) -> None:
    import json

    path = tmp_path / "runs" / "cycles" / "round_trips.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"at": f"2026-10-10T0{i}:00:00+00:00", "order_id": str(i), "net": n}
        for i, n in enumerate(nets)
    ]
    rows.insert(1, {"at": "x", "order_id": "noted", "note": "the contract size is unknown"})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_a_run_of_losing_round_trips_wakes_the_quant_auditor_once_per_new_loss(
    tmp_path: Path,
) -> None:
    from trading_research_agents.agents.alarms import LOSING_STREAK, losing_streak

    clock = [DAY]
    audits = auditing(tmp_path, clock, alarm_cooldown_s=0)
    write_trips(tmp_path, ["-0.4", "0.9", "-0.2", "-0.3"])
    assert losing_streak(tmp_path / "runs" / "cycles" / "round_trips.jsonl")[0] == 2
    assert audits.due(record(), 1) == ((), ())

    write_trips(tmp_path, ["-0.4", "0.9", "-0.2", "-0.3", "-0.1"])
    who, fired = audits.due(record(), 2)
    assert who == ("quant_auditor",) and [a.kind for a in fired] == [LOSING_STREAK]
    assert "3 losing round trips in a row" in fired[0].detail
    # Nothing new closed: the same streak is not reported again.
    assert audits.due(record(), 3) == ((), ())
    # A winner ends it.
    write_trips(tmp_path, ["-0.4", "0.9", "-0.2", "-0.3", "-0.1", "0.5"])
    assert audits.due(record(), 4) == ((), ())
    off = auditing(tmp_path, clock, losing_streak=0)
    write_trips(tmp_path, ["-1", "-1", "-1", "-1", "-1"])
    assert off.due(record(), 1) == ((), ())


def test_the_quant_auditor_can_run_on_a_clock_that_survives_a_restart(tmp_path: Path) -> None:
    clock = [DAY]
    audits = auditing(tmp_path, clock, quant_every_s=86_400)
    # The first cycle starts the clock.
    assert audits.due(record(), 1) == ((), ())
    clock[0] += 3600
    assert audits.due(record(), 2) == ((), ())
    clock[0] = DAY + 86_400
    restarted = auditing(tmp_path, clock, quant_every_s=86_400)
    assert restarted.due(record(), 1)[0] == ("quant_auditor",)
    assert restarted.due(record(), 2) == ((), ())


def test_the_new_audit_settings_are_read_and_checked() -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import parse_strategy_config

    cfg = parse_strategy_config(agent_raw(audit={"quant_every": "24h", "losing_streak": 4}))
    assert cfg.audit.quant_every_s == 86_400 and cfg.audit.losing_streak == 4
    assert parse_strategy_config(agent_raw()).audit.losing_streak == 3
    with pytest.raises(ConfigError, match="at least 5m"):
        parse_strategy_config(agent_raw(audit={"quant_every": "1m"}))
    with pytest.raises(ConfigError, match="quant_every"):
        parse_strategy_config(agent_raw(audit={"quant_every": "daily"}))
    with pytest.raises(ConfigError, match="non-negative"):
        parse_strategy_config(agent_raw(audit={"losing_streak": -1}))


def test_the_risk_manager_is_asked_whenever_the_quant_auditor_is(tmp_path: Path) -> None:
    from trading_research_agents.agents.alarms import DAILY_LOSS_MARK

    clock = [DAY + 3600]
    audits = auditing(tmp_path, clock, risk_manager=True)
    limit = record(stopped_by=f"{DAILY_LOSS_MARK} 310 bps since the day began")
    assert audits.due(limit, 1)[0] == ("risk_manager", "quant_auditor")
    # A breakage is the technical auditor's alone; nobody is asked to act on it.
    assert audits.due(record(stopped_by="structure_lead: ProviderError"), 2)[0] == ("tech_auditor",)
