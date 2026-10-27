"""Agents that do not wait for each other, and a cycle that survives one of them failing.

A model call is seconds of waiting on a socket. Two advocating reviewers in sequence is twice that
wait for no reason, so calls that do not depend on each other go out together on a small pool of
threads. What makes this safe is not the concurrency but what happens around it: each answer is
written the moment it arrives, a failure is recorded and does not touch the others, and the results
come back in a fixed order no matter who finished first, so two runs of the same cycle read the
same way.

This is the whole of our fan-out. It is deliberately not a framework: there is no graph to
declare, no scheduler to configure, and the checkpoint is a file on disk.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from trading_research_agents.records import write_json

MAX_WORKERS = 4


@dataclass(frozen=True)
class Task:
    """One agent's turn: a name, the work, and where its answer is kept."""

    name: str
    work: Callable[[], Any]
    path: Path | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a task names its agent")


@dataclass
class Outcome:
    name: str
    answer: Any = None
    error: str = ""
    seconds: float = 0.0
    reused: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    def record(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "ok": self.ok,
            "error": self.error,
            "seconds": round(self.seconds, 3),
            "reused": self.reused,
        }


@dataclass
class FanOut:
    """The results of one round, in a fixed order, with what failed kept beside what worked."""

    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def answers(self) -> dict[str, Any]:
        return {o.name: o.answer for o in self.outcomes if o.ok}

    @property
    def failed(self) -> dict[str, str]:
        return {o.name: o.error for o in self.outcomes if not o.ok}

    @property
    def seconds(self) -> float:
        """Wall clock of the round: the slowest agent, not the sum of all of them."""
        return round(max((o.seconds for o in self.outcomes), default=0.0), 3)

    def record(self) -> dict[str, Any]:
        return {
            "agents": [o.record() for o in self.outcomes],
            "failed": self.failed,
            "seconds": self.seconds,
        }


def _reuse(path: Path | None) -> Any | None:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write(path: Path | None, payload: Any) -> None:
    if path is None:
        return
    write_json(path, payload)


def run_tasks(
    tasks: Sequence[Task],
    *,
    max_workers: int = MAX_WORKERS,
    resume: bool = False,
    to_record: Callable[[Any], Any] | None = None,
) -> FanOut:
    """Run every task at once, keep every answer, and let no single failure end the round.

    With `resume`, a task whose answer file already exists is not run again: that is how a cycle
    interrupted by a rate limit picks up where it stopped without paying for the same call twice.
    """
    if max_workers < 1:
        raise ValueError("max_workers is at least 1")
    if not tasks:
        return FanOut()
    names = [t.name for t in tasks]
    if len(set(names)) != len(names):
        raise ValueError("two tasks carry the same agent name")

    pending: list[Task] = []
    outcomes: dict[str, Outcome] = {}
    for task in tasks:
        if resume:
            saved = _reuse(task.path)
            if saved is not None:
                outcomes[task.name] = Outcome(task.name, answer=saved, reused=True)
                continue
        pending.append(task)

    def one(task: Task) -> Outcome:
        started = perf_counter()
        try:
            answer = task.work()
        except Exception as exc:
            # A failed agent is a line in the record. The cycle decides what to do about it.
            return Outcome(
                task.name,
                error=f"{exc.__class__.__name__}: {exc}",
                seconds=perf_counter() - started,
            )
        payload = to_record(answer) if to_record is not None else answer
        _write(task.path, payload)
        return Outcome(task.name, answer=answer, seconds=perf_counter() - started)

    if pending:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as pool:
            for outcome in pool.map(one, pending):
                outcomes[outcome.name] = outcome
    # Fixed order, so a report of the same cycle reads the same way every time.
    return FanOut(outcomes=[outcomes[name] for name in sorted(outcomes)])
