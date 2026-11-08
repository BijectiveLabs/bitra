"""The typed judge as the rule desk's event guard: the judge reads headlines, the system decides."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from test_cycle import cycle_config, envelope  # noqa: F401  (envelope is a fixture)
from test_cycle_rule import Always
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution.event_guard import EventGuard, event_guard
from trading_research_agents.features.text import KINDS
from trading_research_agents.llm.judge import Checked, FakeJudge, Picked
from trading_research_agents.market_data.text import read_text_range

pytest.importorskip("pyarrow")


def judge(kind: str = "hack", about: float = 0.9) -> FakeJudge:
    probabilities = dict.fromkeys(KINDS, 0.0) | {kind: 0.9, "none": 0.1}
    return FakeJudge(
        {
            "kind": Picked(kind, 0.8, probabilities),
            "kind_reversed": Picked(kind, 0.8, probabilities),
            "about_BTC": Checked(about),
            "about_market": Checked(0.1),
            "instructions": Checked(0.01),
        },
        model="jev-1.14.0",
    )


def news(tmp_path: Path) -> Path:
    path = tmp_path / "events.csv"
    path.write_text(
        "when,what,source\n2026-09-27T09:30:00+00:00,exchange hot wallet drained,CoinDesk\n",
        encoding="utf-8",
    )
    return path


def guarded(tmp_path: Path, envelope: Envelope, **changes: object):  # type: ignore[no-untyped-def]  # noqa: F811
    settings: dict[str, object] = {
        "rule": Always(),
        "judge": judge(),
        "event_guard": EventGuard(mode="paper", hold_at=0.5),
        "news": (news(tmp_path),),
        "text_root": tmp_path / "data",
    }
    settings.update(changes)
    cfg, _, _ = cycle_config(tmp_path, envelope, [], **settings)
    return run_cycle(cfg)


def test_on_paper_the_guard_records_what_it_would_hold_and_the_rule_trades(
    tmp_path: Path,
    envelope: Envelope,  # noqa: F811
) -> None:
    result = guarded(tmp_path, envelope)
    assert result.intent is not None, result.refusals
    said = result.intent["event_guard"]
    assert said["would_hold"] is True and said["mode"] == "paper"
    assert said["kind"] == "hack" and said["model"] == "jev-1.14.0"
    assert said["min_confidence"] == 0.5 and said["hold_at"] == 0.5
    assert said["headline"] == "exchange hot wallet drained"
    # The headline is archived as first seen, so the guard's reading can be replayed.
    kept = read_text_range(tmp_path / "data", "desk", result_day(), result_day())
    assert [i.text for i in kept] == ["exchange hot wallet drained"]


def result_day() -> date:
    return date(2026, 9, 27)


def test_in_hold_mode_an_adverse_event_holds_the_entry(
    tmp_path: Path,
    envelope: Envelope,  # noqa: F811
) -> None:
    result = guarded(tmp_path, envelope, event_guard=EventGuard(mode="hold", hold_at=0.5))
    assert result.intent is None
    assert any(r.startswith("events: hack") for r in result.refusals), result.refusals


def test_an_event_that_does_not_concern_the_asset_holds_nothing(
    tmp_path: Path,
    envelope: Envelope,  # noqa: F811
) -> None:
    held = EventGuard(mode="hold", hold_at=0.5)
    result = guarded(tmp_path, envelope, judge=judge(about=0.05), event_guard=held)
    assert result.intent is not None and result.intent["event_guard"]["would_hold"] is False


def test_an_approval_is_not_an_adverse_event(
    tmp_path: Path,
    envelope: Envelope,  # noqa: F811
) -> None:
    held = EventGuard(mode="hold", hold_at=0.5)
    result = guarded(tmp_path, envelope, judge=judge(kind="approval"), event_guard=held)
    assert result.intent is not None


def test_in_hold_mode_a_guard_without_a_judge_fails_closed(
    tmp_path: Path,
    envelope: Envelope,  # noqa: F811
) -> None:
    held = EventGuard(mode="hold", hold_at=0.5)
    result = guarded(tmp_path, envelope, judge=None, event_guard=held)
    assert result.intent is None
    assert any("no judge" in r for r in result.refusals), result.refusals


def test_the_guard_is_declared_with_its_threshold() -> None:
    made = event_guard({"mode": "paper", "hold_at": 0.7})
    assert made is not None and made.kinds == ("hack", "outage", "delisting", "restriction")
    assert event_guard(None) is None
    with pytest.raises(ValueError, match="hold_at"):
        event_guard({"mode": "hold"})
    with pytest.raises(ValueError, match="mode"):
        event_guard({"mode": "loud", "hold_at": 0.7})
    with pytest.raises(ValueError, match="between 0 and 1"):
        event_guard({"hold_at": Decimal(2)})
