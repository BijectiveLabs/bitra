"""A stop from outside the process takes the same way out as Ctrl-C, and is written down."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from trading_research_agents.cli import shutdown


def test_sigterm_inside_the_block_is_an_interrupt_named_after_it() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt) as caught, shutdown.as_interrupt():
        signal.raise_signal(signal.SIGTERM)
    assert shutdown.stopped_by(caught.value) == "SIGTERM from outside the process"
    # Outside the block the process has the handler it had before.
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_stop_is_called_what_it_was() -> None:
    assert shutdown.stopped_by(KeyboardInterrupt()) == "a keyboard interrupt"
    venue = KeyboardInterrupt("interrupted at the venue: cancelled")
    assert shutdown.stopped_by(venue) == "interrupted at the venue: cancelled"


def test_the_journal_keeps_one_line_per_start_and_stop(tmp_path: Path) -> None:
    shutdown.journal(tmp_path, "started", every_s=300.0, instruments=["BTCUSDT"])
    shutdown.journal(tmp_path, "stopped", stopped_by="a keyboard interrupt", cycles=4)
    rows = [json.loads(line) for line in (tmp_path / shutdown.LOG).read_text("utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["started", "stopped"]
    assert rows[1]["cycles"] == 4 and "pid" in rows[0] and "at" in rows[0]


def test_a_journal_that_cannot_be_written_never_stops_the_desk(tmp_path: Path) -> None:
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    shutdown.journal(blocked, "started")


def test_a_second_desk_in_the_same_project_is_refused(tmp_path: Path) -> None:
    import os

    # Someone else's live process holds the lock: the parent of this test process will do.
    (tmp_path / shutdown.LOCK).write_text(
        json.dumps({"pid": os.getppid(), "at": "2026-10-05T05:07:45+00:00"}), encoding="utf-8"
    )
    with pytest.raises(shutdown.DeskBusyError, match="another desk is already running"):
        shutdown.claim(tmp_path)


def test_a_lock_left_by_a_dead_process_is_taken_over_and_released(tmp_path: Path) -> None:
    import os

    (tmp_path / shutdown.LOCK).write_text(json.dumps({"pid": 999_999_999}), encoding="utf-8")
    assert not shutdown.alive(999_999_999)
    held = shutdown.claim(tmp_path)
    assert json.loads(held.read_text("utf-8"))["pid"] == os.getpid()
    shutdown.release(held)
    assert not held.exists()
    assert shutdown.alive(os.getpid())
