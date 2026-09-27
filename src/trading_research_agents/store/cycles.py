"""Where a cycle keeps its papers.

A cycle is the unit an auditor replays, so its artefacts live together under one directory named
by the cycle itself. The layout is fixed on purpose: a reader who has seen one cycle directory
can read any of them, and a tool that walks them does not need a manifest to know what is where.

    runs/<project>/cycles/<cycle id>/
      snapshot.json      what the engine saw, with provenance and age
      briefs/<agent>.json  the figures code handed each agent
      answers/<agent>.json what the agent answered, with tokens and latency
      decision.json      the gate the confluence matrix opened
      intents.jsonl      every intent with the control's verdict
      fills.jsonl        every fill, simulated or real
      cycle.json         the summary: cost, outcome, who refused what
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNAPSHOT = "snapshot.json"
SUMMARY = "cycle.json"
DECISION = "decision.json"
BRIEFS = "briefs"
ANSWERS = "answers"


@dataclass(frozen=True)
class CycleDir:
    """One cycle's directory, created on demand, with a name for every artefact."""

    root: Path
    cycle: str

    @property
    def path(self) -> Path:
        return self.root / self.cycle

    def make(self) -> CycleDir:
        (self.path / BRIEFS).mkdir(parents=True, exist_ok=True)
        (self.path / ANSWERS).mkdir(parents=True, exist_ok=True)
        return self

    @property
    def snapshot(self) -> Path:
        return self.path / SNAPSHOT

    @property
    def summary(self) -> Path:
        return self.path / SUMMARY

    @property
    def decision(self) -> Path:
        return self.path / DECISION

    def brief(self, agent: str) -> Path:
        return self.path / BRIEFS / f"{agent}.json"

    def answer(self, agent: str) -> Path:
        return self.path / ANSWERS / f"{agent}.json"

    def write_summary(self, payload: dict[str, Any]) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        self.summary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        return self.summary


def cycles_in(root: Path) -> list[CycleDir]:
    """Every cycle under a directory, oldest first, by the name the cycle gave itself."""
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (p / SNAPSHOT).is_file()]
    return [CycleDir(root=root, cycle=p.name) for p in sorted(found, key=lambda p: p.name)]


def latest_cycle(root: Path) -> CycleDir | None:
    found = cycles_in(root)
    return found[-1] if found else None
