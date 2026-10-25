"""An arena run on disk: `arena.json`, the result and a function of its inputs alone;
`ideas.jsonl`, every idea and its settlement; and `run.json`, when it ran, kept apart so the
result's bytes never depend on the clock.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.evaluation.arena import ArenaResult
from trading_research_agents.records import write_json, write_lines


def arena_id(record: dict[str, Any]) -> str:
    """A run's name: its configuration and the hashes of the archive it read, so the same inputs
    land in the same folder and a changed archive never overwrites an earlier result."""
    inputs = {"config": record["config"], "manifests": record["manifests"]}
    text = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def write_arena(result: ArenaResult, out: Path, *, now: datetime | None = None) -> Path:
    record = result.record()
    folder = out / arena_id(record)
    write_json(folder / "arena.json", record)
    write_lines(folder / "ideas.jsonl", result.ideas)
    created = (now or datetime.now(UTC)).isoformat()
    write_json(folder / "run.json", {"arena": folder.name, "created_at": created})
    return folder
