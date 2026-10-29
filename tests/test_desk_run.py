from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.cli import run_command, shutdown
from trading_research_agents.cli.common import CLEARED, REFUSED
from trading_research_agents.cli.run_command import _Desk, _DeskRun
from trading_research_agents.cli.schedule import Schedule
from trading_research_agents.records import read_lines


@dataclass(frozen=True)
class Base:
    instrument: str = "BTCUSDT"
    spot: bool = False
    asset_class: str = "crypto"
    market: Any = None
    size_step: Decimal = Decimal("0.001")
    size: Decimal = Decimal("0.01")
    size_quote: Decimal | None = None
    max_position: Decimal | None = None
    carried_spend: Decimal = Decimal(0)
    place: Any = None
    account: Any = None


@dataclass
class Result:
    acted: bool
    stopped_by: str | None = None


class Usage:
    cost = Decimal(0)


class Audits:
    def __init__(self) -> None:
        self.seen: list[int] = []

    def after(self, payload: dict[str, Any], ran: int, *, as_json: bool) -> None:
        self.seen.append(ran)


class Keeper:
    def __init__(self) -> None:
        self.kills: list[bool] = []

    def run(self, *, kill: bool) -> list[str]:
        self.kills.append(kill)
        return []


class Cfg:
    def __init__(self, kill_switch: Path | None = None) -> None:
        self.kill_switch = kill_switch
        self.universe = argparse.Namespace(instruments=("BTCUSDT", "ETHUSDT"))
        self.run = argparse.Namespace(instrument="BTCUSDT")
        self.limits = argparse.Namespace(max_position=Decimal(1), max_position_quote=None)


def desk_run(
    tmp_path: Path,
    *,
    cycles: int = 2,
    keeper: Any = None,
    kill_switch: Path | None = None,
) -> _DeskRun:
    args = argparse.Namespace(json=True, every=60, cycles=cycles)
    desk = _Desk(
        args=args,
        cfg=Cfg(kill_switch),
        instrument="BTCUSDT",
        mode="sandbox",
        out=tmp_path,
        beside=tmp_path,
        data_cfg=None,
        chains={},
        declared=argparse.Namespace(classes={}),
        hand=None,
        rate=None,
        keeper=keeper,
        usage=Usage(),
        completer_for=lambda role: None,
    )
    schedule = Schedule(
        every_s=60, cycles=cycles, kill_switch=kill_switch, clock=lambda: 0.0, rest=lambda s: None
    )
    return _DeskRun(desk, Base(), schedule, [], Audits())  # type: ignore[arg-type]


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"asked": [], "results": [], "held_back": ""}

    def one_cycle(cfg: Any, usage: Any, spine: Any, args: Any) -> Result:
        seen["asked"].append(cfg)
        return seen["results"].pop(0) if seen["results"] else Result(acted=False)

    monkeypatch.setattr(run_command, "_refresh_sources", lambda declared: [])
    monkeypatch.setattr(run_command, "_sweep", lambda *a, **k: (("BTCUSDT",), None))
    monkeypatch.setattr(run_command, "_one_cycle", one_cycle)
    monkeypatch.setattr(run_command, "_cycle_report", lambda r, u, m: ({}, []))
    monkeypatch.setattr(run_command, "_market_facts", lambda *a: (False, "crypto", None))
    monkeypatch.setattr(run_command, "_step_for", lambda cfg, i: Decimal("0.001"))
    monkeypatch.setattr(run_command, "held_back_by", lambda cfg, out, now: seen["held_back"])
    return seen


def stops(out: Path) -> list[dict[str, Any]]:
    return [line for line in read_lines(out / shutdown.LOG) if line["event"] == "stopped"]


def test_a_schedule_runs_the_passes_asked_for_and_says_so(
    tmp_path: Path, seams: dict[str, Any]
) -> None:
    run = desk_run(tmp_path, cycles=2)
    assert run.run() == CLEARED
    assert (run.passes, run.ran) == (2, 2)
    (stopped,) = stops(tmp_path)
    assert stopped["stopped_by"] == "the 2 pass(es) asked for" and stopped["passes"] == 2


def test_a_held_back_desk_counts_its_passes_and_asks_nothing(
    tmp_path: Path, seams: dict[str, Any]
) -> None:
    seams["held_back"] = "daily cost budget spent"
    run = desk_run(tmp_path, cycles=3)
    assert run.run() == CLEARED
    assert run.passes == 3 and seams["asked"] == []


def test_an_armed_kill_switch_flattens_first_and_ends_the_run_before_a_cycle(
    tmp_path: Path, seams: dict[str, Any]
) -> None:
    switch = tmp_path / "KILL"
    switch.write_text("stop", encoding="utf-8")
    keeper = Keeper()
    run = desk_run(tmp_path, keeper=keeper, kill_switch=switch)
    assert run.run() == CLEARED
    assert keeper.kills == [True] and seams["asked"] == []
    (stopped,) = stops(tmp_path)
    assert stopped["stopped_by"].endswith("after flattening")


def test_a_refused_cycle_carries_its_spend_and_a_trade_clears_it(
    tmp_path: Path, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = desk_run(tmp_path, cycles=3)
    costs = iter([Decimal("0.02"), Decimal("0.05"), Decimal("0.05")])

    def one_cycle(cfg: Any, usage: Any, spine: Any, args: Any) -> Result:
        seams["asked"].append(cfg)
        usage.cost += next(costs)
        return Result(acted=len(seams["asked"]) == 2, stopped_by=None)

    monkeypatch.setattr(run_command, "_one_cycle", one_cycle)
    assert run.run() == CLEARED
    carried = [cfg.carried_spend for cfg in seams["asked"]]
    assert carried == [Decimal(0), Decimal("0.02"), Decimal(0)]
    assert run.carried["BTCUSDT"] == Decimal("0.05")


def test_a_refused_last_cycle_leaves_the_run_refused(tmp_path: Path, seams: dict[str, Any]) -> None:
    seams["results"] = [Result(acted=False, stopped_by="cost gate")]
    run = desk_run(tmp_path, cycles=1)
    assert run.run() == REFUSED


def test_a_stop_in_the_middle_of_a_cycle_is_refused_and_written_down(
    tmp_path: Path, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(cfg: Any, usage: Any, spine: Any, args: Any) -> Result:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_command, "_one_cycle", interrupted)
    run = desk_run(tmp_path)
    assert run.run() == REFUSED
    (stopped,) = stops(tmp_path)
    assert stopped["stopped_by"] == "a keyboard interrupt, in the middle of a cycle"


def test_another_instrument_is_held_to_the_quote_cap_alone(
    tmp_path: Path, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_command, "_sweep", lambda *a, **k: (("ETHUSDT",), None))
    run = desk_run(tmp_path, cycles=1)
    run.desk.cfg.limits.max_position_quote = Decimal(500)
    run.run()
    (asked,) = seams["asked"]
    assert asked.instrument == "ETHUSDT" and asked.max_position is None


class Stuck(Keeper):
    """A keeper that could not read one position while the kill switch was armed."""

    def run(self, *, kill: bool) -> list[str]:
        self.unresolved = ["ETHUSDT could not be read"] if kill else []
        return super().run(kill=kill)


def test_a_kill_switch_that_left_a_position_unread_says_so_and_exits_refused(
    tmp_path: Path, seams: dict[str, Any]
) -> None:
    switch = tmp_path / "KILL"
    switch.write_text("stop", encoding="utf-8")
    run = desk_run(tmp_path, keeper=Stuck(), kill_switch=switch)
    assert run.run() == REFUSED
    (stopped,) = stops(tmp_path)
    assert "NOT flat yet: ETHUSDT could not be read" in stopped["stopped_by"]
    assert "after flattening" not in stopped["stopped_by"]


def test_a_ladder_link_that_did_not_answer_leaves_a_line(tmp_path: Path) -> None:
    from trading_research_agents.cli.desk_models import LADDER, _ladder_journal
    from trading_research_agents.llm.chain import Attempt

    note = _ladder_journal(tmp_path)
    assert note is not None
    note("structure_lead", Attempt("groq:fast", "failed", "ProviderError: 429 rate limit"))
    note("structure_lead", Attempt("google:steady", "answered", "steady-1"))
    (line,) = read_lines(tmp_path / LADDER)
    assert line["role"] == "structure_lead" and line["link"] == "groq:fast"
    assert line["outcome"] == "failed" and "429" in line["detail"]
