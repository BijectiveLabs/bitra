from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agent.ledger import (
    Axis,
    Ledger,
    LedgerEntry,
    LedgerError,
    ReversalPoint,
    Transferability,
    Verdict,
)
from trading_research_agent.ledger.store import from_record, to_record


def closed_on_cost(entry_id: str, now: datetime) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        subject="informed-flow burst drift, 30s horizon",
        verdict=Verdict.CLOSED,
        recorded_at=now,
        layer=0,
        axis_of_death=(Axis.COST,),
        transferability=Transferability.ENVELOPE_BOUND,
        reversal_point=ReversalPoint("c_rt", Decimal("3.0"), "bps"),
        evidence=("E-0041",),
        envelope=(("hyperliquid/taker-taker/round_trip_bps", "9.0"),),
        note="3.0 bps against a 9.0 bps round trip",
    )


def test_round_trip_through_json_is_exact(now: datetime) -> None:
    e = closed_on_cost("H-010", now)
    again = from_record(json.loads(json.dumps(to_record(e))))
    assert again == e
    assert again.reversal_point is not None
    assert again.reversal_point.value == Decimal("3.0")
    assert str(again.reversal_point.value) == "3.0"


def test_append_then_read_back(tmp_path: Path, now: datetime) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    assert list(ledger.entries()) == []
    ledger.append(closed_on_cost("H-010", now))
    ledger.append(closed_on_cost("H-011", now + timedelta(minutes=1)))
    assert [e.entry_id for e in ledger.entries()] == ["H-010", "H-011"]
    assert ledger.counts()[Verdict.CLOSED] == 2


def test_ids_are_never_reused(tmp_path: Path, now: datetime) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(closed_on_cost("H-010", now))
    with pytest.raises(LedgerError, match="never reused"):
        ledger.append(closed_on_cost("H-010", now))


def test_withdrawal_keeps_both_rows_and_drops_the_target_from_counts(
    tmp_path: Path, now: datetime
) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(closed_on_cost("H-010", now))
    retraction = ledger.withdraw(
        "H-010",
        new_entry_id="W-001",
        reason="cost term was populated from a stale schedule",
        recorded_at=now + timedelta(days=1),
    )
    assert retraction.verdict is Verdict.WITHDRAWN
    assert retraction.retracts == "H-010"
    assert [e.entry_id for e in ledger.entries()] == ["H-010", "W-001"]
    assert ledger.effective() == []
    assert ledger.counts()[Verdict.CLOSED] == 0


def test_a_retraction_cannot_be_retracted_or_repeated(tmp_path: Path, now: datetime) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(closed_on_cost("H-010", now))
    ledger.withdraw("H-010", new_entry_id="W-001", reason="why", recorded_at=now)
    with pytest.raises(LedgerError, match="already retracted"):
        ledger.withdraw("H-010", new_entry_id="W-002", reason="again", recorded_at=now)
    with pytest.raises(LedgerError, match="cannot itself be retracted"):
        ledger.withdraw("W-001", new_entry_id="W-003", reason="no", recorded_at=now)
    with pytest.raises(LedgerError, match="no such entry"):
        ledger.withdraw("H-999", new_entry_id="W-004", reason="no", recorded_at=now)


def test_an_unreadable_row_stops_the_reader_with_its_line_number(
    tmp_path: Path, now: datetime
) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append(closed_on_cost("H-010", now))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    with pytest.raises(LedgerError, match=r":2: unreadable"):
        list(ledger.entries())


def test_a_row_that_breaks_a_rule_on_disk_is_refused_on_read(tmp_path: Path, now: datetime) -> None:
    # The file could have been edited by hand. The rules hold on the way in as well as out.
    path = tmp_path / "ledger.jsonl"
    record = to_record(closed_on_cost("H-010", now))
    record["axis_of_death"] = ["power"]
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="POWER"):
        list(Ledger(path).entries())


def test_unknown_schema_is_refused(now: datetime) -> None:
    record = to_record(closed_on_cost("H-010", now))
    record["schema"] = 99
    with pytest.raises(LedgerError, match="schema"):
        from_record(record)


def test_blank_lines_are_not_rows(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append(closed_on_cost("H-010", now))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")
    assert len(list(ledger.entries())) == 1
