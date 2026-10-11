"""Records survive a crash mid-write, whole files are never half written, and hashes are stable."""

from __future__ import annotations

import json
from pathlib import Path

from trading_research_agents.records import (
    append_line,
    read_gzip_json,
    read_lines,
    sha256_of,
    write_gzip_json,
    write_json,
)


def test_a_torn_last_line_costs_only_itself(tmp_path: Path) -> None:
    path = tmp_path / "positions.jsonl"
    append_line(path, {"event": "opened", "n": 1}, durable=True)
    # What a crash in the middle of a write leaves behind: half a line, no newline.
    with path.open("ab") as fh:
        fh.write(b'{"event": "clo')
    append_line(path, {"event": "closed", "n": 2}, durable=True)
    rows = read_lines(path)
    assert [row["n"] for row in rows] == [1, 2]
    assert path.read_bytes().count(b"\n") == 3


def test_reading_skips_what_is_not_a_record(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    path.write_bytes(b'{"a": 1}\n\nnot json\n[1, 2]\n{"b": 2}')
    assert read_lines(path) == [{"a": 1}, {"b": 2}]
    assert read_lines(tmp_path / "missing.jsonl") == []


def test_a_whole_file_is_replaced_in_one_step(tmp_path: Path) -> None:
    path = tmp_path / "cycle.json"
    write_json(path, {"v": 1})
    write_json(path, {"v": 2}, durable=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cycle.json"]


def test_compressed_inputs_hash_the_same_for_the_same_content(tmp_path: Path) -> None:
    one, two = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    write_gzip_json(one, {"candles": [[1, "100.5"]]})
    write_gzip_json(two, {"candles": [[1, "100.5"]]})
    assert sha256_of(one) == sha256_of(two)
    assert read_gzip_json(one) == {"candles": [[1, "100.5"]]}
