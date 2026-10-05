"""A view that reads a desk and cannot reach it.

The console runs commands. This does not: it opens the directory a desk writes into and renders what
is there. These tests are about the contract - which directory, what one cycle reads as, and what
happens to a record that is still being written - because everything else about it is rendering.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.cli.watch import Desk, decision_of, look

NOW = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)


def cycle(root: Path, at: str, instrument: str, **fields: Any) -> Path:
    where = root / f"{at}-{instrument}"
    where.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "cycle_id": f"{at}-{instrument}",
        "instrument": instrument,
        "refusals": [],
        "analyst": {"target": "long", "setup": "range_reversion"},
        "conviction": {"score": {"score": "0.800"}},
        "risk": {"risk_bps": "0.27"},
        "targets": {"net_bps": "14.4630"},
        "intent": None,
        "placed": None,
        "held": False,
        "tokens": 1000,
        "cost_declared": "0.01",
    }
    payload.update(fields)
    (where / "cycle.json").write_text(json.dumps(payload), encoding="utf-8")
    return where


def test_the_watcher_reads_the_directory_it_was_given(tmp_path: Path) -> None:
    """`--out` names the cycle directory, the way every other command names it."""
    cycles = tmp_path / "runs" / "cycles"
    cycle(cycles, "2026-09-29T15-00-00Z", "BTCUSDT")
    cycle(cycles, "2026-09-29T15-05-00Z", "ETHUSDT")

    desk = look(cycles, now=NOW)
    assert [d.instrument for d in desk.decisions] == ["ETHUSDT", "BTCUSDT"]
    assert desk.spend["cycles_read"] == 2 and desk.spend["tokens_in_view"] == 2000
    # A directory with nothing in it is not an error; it is a desk that has not run.
    assert look(tmp_path / "nothing", now=NOW).decisions == []


def test_one_cycle_reads_as_what_became_of_it(tmp_path: Path) -> None:
    placed = decision_of(
        {
            "cycle_id": "2026-09-29T15-21-59Z-ETHUSDT",
            "instrument": "ETHUSDT",
            "analyst": {"target": "long", "setup": "range_reversion"},
            "conviction": {"score": {"score": "0.800"}},
            "risk": {"risk_bps": "0.27"},
            "targets": {"net_bps": "14.4630"},
            "intent": {"target": "long", "size": "0.010"},
            "placed": {"sent": True, "side": "buy", "size": "0.010", "venue": "bybit"},
        }
    )
    assert placed.outcome == "placed" and placed.detail == "buy 0.010 on bybit"
    assert placed.setup == "range_reversion" and placed.net_bps == "14.463"

    cancelled = decision_of(
        {
            "instrument": "ETHUSDT",
            "placed": {
                "sent": True,
                "side": "buy",
                "size": "0.010",
                "venue": "bybit",
                "cancelled": True,
            },
        }
    )
    assert "cancelled unfilled" in cancelled.detail

    refused = decision_of(
        {"instrument": "BTCUSDT", "placed": {"sent": False, "refused": "notional is above the cap"}}
    )
    assert refused.outcome == "not placed" and "above the cap" in refused.detail

    held = decision_of(
        {"instrument": "BTCUSDT", "held": True, "trigger": {"reasons": ["nothing new to read"]}}
    )
    assert held.outcome == "held" and held.detail == "nothing new to read"

    gate = decision_of({"instrument": "BTCUSDT", "refusals": ["conviction: below the floor"]})
    assert gate.outcome == "refused" and gate.detail == "conviction: below the floor"

    stopped = decision_of({"instrument": "BTCUSDT", "stopped_by": "technical: ProviderError"})
    assert stopped.outcome == "stopped"


def test_a_record_still_being_written_is_skipped_rather_than_shown_wrong(tmp_path: Path) -> None:
    cycles = tmp_path / "runs" / "cycles"
    cycle(cycles, "2026-09-29T15-00-00Z", "BTCUSDT")
    half = cycles / "2026-09-29T15-10-00Z-ETHUSDT"
    half.mkdir(parents=True)
    (half / "cycle.json").write_text('{"cycle_id": "half', encoding="utf-8")

    desk = look(cycles, now=NOW)
    assert [d.instrument for d in desk.decisions] == ["BTCUSDT"]


def test_what_broke_is_pulled_out_of_the_refusals(tmp_path: Path) -> None:
    cycles = tmp_path / "runs" / "cycles"
    cycle(
        cycles,
        "2026-09-29T15-00-00Z",
        "BTCUSDT",
        refusals=[
            "conviction: below the floor",
            "data funding: every source refused",
            "venue: OrderRejected",
        ],
        stopped_by="technical: ProviderError: 429",
    )
    desk = look(cycles, now=NOW)
    # A gate saying no is not an alarm; a chain running out and a venue refusing are.
    assert not any("conviction" in line for line in desk.alarms)
    assert any("data funding" in line for line in desk.alarms)
    assert any("venue: OrderRejected" in line for line in desk.alarms)
    assert any("ProviderError" in line for line in desk.alarms)


def test_the_screen_and_the_book_come_from_where_they_are_written(tmp_path: Path) -> None:
    cycles = tmp_path / "runs" / "cycles"
    cycle(
        cycles,
        "2026-09-29T15-00-00Z",
        "BTCUSDT",
        desk={"held": [{"instrument": "ETHUSDT", "side": "buy", "risk_bps": "0.27"}]},
    )
    screens = tmp_path / "runs" / "screens"
    screens.mkdir(parents=True)
    (screens / "2026-09-29T15-00-00Z.json").write_text(
        json.dumps({"at": "2026-09-29T15-00-00Z", "chosen": ["BTCUSDT"], "candidates": []}),
        encoding="utf-8",
    )
    audits = tmp_path / "runs" / "audits"
    (audits / "2026-09-29T14-00-00Z").mkdir(parents=True)

    desk = look(cycles, now=NOW)
    assert desk.screen["chosen"] == ["BTCUSDT"]
    assert desk.book[0]["instrument"] == "ETHUSDT"
    assert desk.audits == ["2026-09-29T14-00-00Z"]


def test_the_record_says_what_it_read_and_when(tmp_path: Path) -> None:
    desk = look(tmp_path, now=NOW)
    row = desk.record()
    assert row["at"] == "2026-09-29T16:00:00+00:00" and row["root"] == str(tmp_path)
    assert isinstance(desk, Desk)
