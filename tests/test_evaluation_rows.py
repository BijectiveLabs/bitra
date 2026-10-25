from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation import rows
from trading_research_agents.evaluation.baselines import BuyHold, Fade, Momentum, RandomSide
from trading_research_agents.evaluation.policy import DecisionView
from trading_research_agents.evaluation.rows import UnknownRowError, make_policy
from trading_research_agents.evaluation.v1 import recorded_ideas
from trading_research_agents.features.core import Absent
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns

HOUR = 60 * NS_PER_MINUTE
T0 = midnight_ns(date(2025, 7, 8))


def view(z: object, atr: object = 40.0, instrument: str = "BTCUSDT", at: int = T0) -> DecisionView:
    return DecisionView(instrument, at, "1h", (), {"1h": {"mom_z_24": z, "atr_bps": atr}})  # type: ignore[dict-item]


def test_momentum_follows_the_move_with_atr_exits() -> None:
    long = Momentum().decide(view(1.5))
    assert long is not None and long.side is Direction.LONG
    assert long.stop_bps == Decimal("60.0000") and long.take_bps == Decimal("120.0000")
    assert long.max_hold_bars == 24
    short = Momentum().decide(view(-1.2))
    assert short is not None and short.side is Direction.SHORT
    assert Momentum().decide(view(0.5)) is None
    assert Momentum().decide(view(1.5, Absent("no atr"))) is None
    assert Momentum().score(view(1.5)) == 1.5


def test_fade_takes_the_other_side_on_the_same_triggers() -> None:
    faded = Fade().decide(view(1.5))
    assert faded is not None and faded.side is Direction.SHORT and faded.playbook == "fade"
    assert Fade().decide(view(0.2)) is None and Fade().score(view(1.5)) == -1.5


def test_random_fires_when_momentum_does_and_is_seeded() -> None:
    first, second = RandomSide(7), RandomSide(7)
    sides = [first.decide(view(1.5 if k % 2 else 0.1, at=T0 + k * HOUR)) for k in range(40)]
    again = [second.decide(view(1.5 if k % 2 else 0.1, at=T0 + k * HOUR)) for k in range(40)]
    assert [s is None for s in sides] == [k % 2 == 0 for k in range(40)]
    assert sides == again
    assert {s.side for s in sides if s is not None} == {Direction.LONG, Direction.SHORT}


def test_buy_hold_decides_once_and_holds_to_the_end() -> None:
    made = BuyHold(T0 + 48 * HOUR, HOUR)
    first = made.decide(view(0.0))
    assert first is not None and first.max_hold_bars == 48 and first.stop_bps is None
    assert made.decide(view(0.0, at=T0 + HOUR)) is None


def cycle(root: Path, name: str, record: dict[str, object]) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "cycle.json").write_text(json.dumps(record), encoding="utf-8")


def test_the_v1_record_is_what_the_desk_sent(tmp_path: Path) -> None:
    sent: dict[str, object] = {
        "cycle_id": "c1",
        "instrument": "BTCUSDT",
        "finished_at": "2025-07-08T10:00:30+00:00",
        "analyst": {"target": "long"},
        "intent": {"mid": "100", "stop_price": "99", "take_price": "102"},
    }
    cycle(tmp_path, "c1", sent)
    cycle(tmp_path, "c2", {**sent, "cycle_id": "c2", "intent": None, "stopped_by": "cost gate: no"})
    cycle(tmp_path, "c3", {**sent, "cycle_id": "c3", "instrument": "SOLUSDT"})
    found = recorded_ideas(tmp_path, ("BTCUSDT",), T0, T0 + 24 * HOUR, 4 * HOUR)
    assert len(found) == 1
    (only,) = found
    assert only.decided_ns == T0 + 10 * HOUR + NS_PER_MINUTE
    assert only.idea.stop_bps == Decimal(100) and only.idea.take_bps == Decimal(200)
    assert only.idea.max_hold_bars == 240


def test_rows_are_made_by_name_and_unknown_names_are_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert make_policy("momentum", seed=7, end_ns=T0, interval_ns=HOUR).name == "momentum"
    with pytest.raises(UnknownRowError, match="buy_hold"):
        make_policy("momentun", seed=7, end_ns=T0, interval_ns=HOUR)

    class Oracle:
        name = "oracle"
        knowledge_cutoff = date(2026, 1, 1)

        def decide(self, view: DecisionView) -> None:
            return None

    monkeypatch.setattr(rows, "_plugins", lambda: {"oracle": Oracle})
    assert make_policy("oracle", seed=7, end_ns=T0, interval_ns=HOUR).name == "oracle"
