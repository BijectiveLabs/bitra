"""A cycle's raw inputs, hashes and index line; reading history from the index; verify; prune."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.agents.verify import verify_cycle, verify_runs
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.desk import filled_from_records
from trading_research_agents.execution.targets import intents_today
from trading_research_agents.execution.trigger import last_seen
from trading_research_agents.records import (
    INDEX_DIR,
    append_index,
    build_index,
    read_gzip_json,
    recent,
    sha256_of,
)
from trading_research_agents.store.prune import apply, plan
from trading_research_agents.store.snapshot import INPUTS, read_inputs
from trading_research_agents.trading.positions import filled_entries


def test_a_cycle_keeps_its_inputs_a_hash_of_every_file_and_an_index_line(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    result = run_cycle(cfg)
    folder = Path(str(result.path))
    summary = json.loads((folder / "cycle.json").read_text(encoding="utf-8"))

    assert INPUTS in summary["files"] and "briefs/technical.json" in summary["files"]
    assert all(sha256_of(folder / name) == digest for name, digest in summary["files"].items())
    [row] = list(recent(cfg.root))
    assert row["cycle_id"] == result.cycle and row["intent"] == result.intent
    assert row["summary_sha256"] == sha256_of(folder / "cycle.json")

    snapshot, _, settings = read_inputs(read_gzip_json(folder / INPUTS))
    assert len(snapshot.get("ohlcv").value) == 5 and settings["lookback_bars"] == 5
    assert snapshot.get("orderbook").value.bids[0].size > 0

    checked = verify_cycle(folder, row)
    assert checked.ok and checked.hashed and checked.recomputed, checked.problems


def test_verify_names_a_file_changed_after_the_cycle_ended(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    folder = Path(str(run_cycle(cfg).path))
    brief = folder / "briefs" / "technical.json"
    edited = json.loads(brief.read_text(encoding="utf-8"))
    edited["zscore"] = 3.0
    brief.write_text(json.dumps(edited), encoding="utf-8")
    (folder / "note.txt").write_text("added later", encoding="utf-8")

    found = verify_runs(cfg.root)
    [failed] = found["failed"]
    said = " ".join(failed["problems"])
    assert "briefs/technical.json differs from the hash" in said
    assert "note.txt was added after the cycle ended" in said
    assert "technical brief zscore is 3.0; the inputs give" in said


def legacy_desk(root: Path, now: datetime) -> None:
    """Cycle directories the way a desk wrote them before the index existed."""
    for minutes, instrument, sent in (
        (30, "BTCUSDT", False),
        (20, "ETHUSDT", True),
        (10, "BTCUSDT", True),
    ):
        at = now - timedelta(minutes=minutes)
        cycle = f"{at:%Y-%m-%dT%H-%M-%SZ}-{instrument}"
        folder = root / cycle
        folder.mkdir(parents=True)
        intent = {
            "instrument": instrument,
            "target": "long",
            "size": "1",
            "mid": "100",
            "risk_bps": "10",
            "at": cycle,
        }
        summary = {
            "cycle_id": cycle,
            "instrument": instrument,
            "seen": {"mid": "100"},
            "held": False,
            "intent": intent if sent else None,
            "placed": {"sent": True, "filled": "1", "order_id": cycle} if sent else None,
        }
        (folder / "cycle.json").write_text(json.dumps(summary), encoding="utf-8")
        if sent:
            (folder / "intents.jsonl").write_text(json.dumps(intent) + "\n", encoding="utf-8")
            (folder / "placed.json").write_text(json.dumps(summary["placed"]), encoding="utf-8")


def test_the_index_reads_the_same_history_the_directories_hold(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    root = tmp_path / "cycles"
    legacy_desk(root, now)
    before = (
        last_seen(root, instrument="BTCUSDT"),
        intents_today(root, at=now, instrument=None),
        filled_from_records(root),
        filled_entries(root),
    )
    assert build_index(root) == 3 and (root / INDEX_DIR).is_dir()
    after = (
        last_seen(root, instrument="BTCUSDT"),
        intents_today(root, at=now, instrument=None),
        filled_from_records(root),
        filled_entries(root),
    )
    assert before == after
    assert after[1] == 2 and set(after[2]) == {"BTCUSDT", "ETHUSDT"}


def test_a_cycle_that_never_finished_is_still_read(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    root = tmp_path / "cycles"
    legacy_desk(root, now)
    build_index(root)
    # Sent to the venue, then the process died before its summary and index line were written.
    cycle = f"{now:%Y-%m-%dT%H-%M-%SZ}-SOLUSDT"
    folder = root / cycle
    folder.mkdir()
    intent = {"instrument": "SOLUSDT", "target": "short", "size": "2", "mid": "50", "at": cycle}
    (folder / "intents.jsonl").write_text(json.dumps(intent) + "\n", encoding="utf-8")
    (folder / "placed.json").write_text(json.dumps({"sent": True, "filled": "2"}), "utf-8")
    assert "SOLUSDT" in filled_from_records(root)
    assert "SOLUSDT" in filled_entries(root)
    assert intents_today(root, at=now, instrument="SOLUSDT") == 1


def test_the_last_line_written_for_a_cycle_wins(tmp_path: Path) -> None:
    root = tmp_path / "cycles"
    cycle = f"{datetime.now(UTC):%Y-%m-%dT%H-%M-%SZ}-BTCUSDT"
    append_index(root, {"cycle_id": cycle, "instrument": "BTCUSDT", "held": True})
    append_index(root, {"cycle_id": cycle, "instrument": "BTCUSDT", "held": False})
    assert [row["held"] for row in recent(root)] == [False]


def test_prune_says_first_keeps_the_index_and_archives_on_request(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    root = tmp_path / "cycles"
    legacy_desk(root, now - timedelta(days=40))
    legacy_desk(root, now)
    with pytest.raises(ValueError, match="keep at least 2 days"):
        plan(root, timedelta(days=1))
    chosen = plan(root, timedelta(days=30))
    assert len(chosen.folders) == 3
    kept = apply(root, chosen, archive=True)
    assert kept is not None and any(kept.glob("*.zip"))
    with zipfile.ZipFile(next(kept.glob("*.zip"))) as box:
        assert any(name.endswith("cycle.json") for name in box.namelist())
    assert len([p for p in root.iterdir() if p.name != INDEX_DIR]) == 3
    # The pruned cycles stay in the index, so what the desk reads about its past is unchanged.
    assert len(list(recent(root))) == 6
    assert verify_runs(root)["pruned"] == 3
