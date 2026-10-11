"""What the cycle is doing while it does it, so a terminal is not a blank screen for a minute.

A cycle spends most of its wall clock waiting on sockets: one data pull, then four analysts, then
the validators. Printing the outcome afterwards is correct and useless - the operator cannot tell a
slow model from a hung process, and the first thing anyone does with silence is press Ctrl-C.

So each step says when it starts and how it ended, and a caller that cares draws it. The events are
the only thing this module knows about; nothing here renders, and nothing downstream reads them.
That is deliberate: progress must never be able to change a decision, so it travels on its own wire.

The callback is invoked from the worker threads of the fan-out, so a sink that touches a terminal
holds its own lock.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter
from typing import Any

# The same words the session runtime already uses for its own steps, plus the two a cycle needs:
# a step that refused on a gate, and a cycle the trigger held before anyone was paid.
RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
REFUSED = "refused"
FAILED = "failed"
HELD = "held"
STATES = (RUNNING, DONE, SKIPPED, REFUSED, FAILED, HELD)


@dataclass(frozen=True)
class Step:
    """One thing the cycle is doing, and where it got to."""

    name: str
    state: str
    detail: str = ""
    seconds: float | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"a step's state is one of {list(STATES)}")

    def record(self) -> dict[str, Any]:
        return {
            "step": self.name,
            "state": self.state,
            "detail": self.detail,
            "seconds": None if self.seconds is None else round(self.seconds, 3),
        }


Watch = Callable[[Step], None]


def silent(step: Step) -> None:
    """The default: nobody is watching, and a cycle does not care."""


def announce(
    watch: Watch, name: str, state: str, detail: str = "", seconds: float | None = None
) -> None:
    """Tell the watcher, and never let a watcher's own failure reach the cycle."""
    # A broken progress bar is not a reason to lose a decision.
    with suppress(Exception):
        watch(Step(name=name, state=state, detail=detail, seconds=seconds))


def timed(watch: Watch, name: str, work: Callable[[], Any], detail: str = "") -> Callable[[], Any]:
    """Wrap a unit of work so the watcher sees it begin, and sees how it ended."""

    def run() -> Any:
        announce(watch, name, RUNNING, detail)
        started = perf_counter()
        try:
            answer = work()
        except Exception as exc:
            announce(watch, name, FAILED, exc.__class__.__name__, perf_counter() - started)
            raise
        announce(watch, name, DONE, "", perf_counter() - started)
        return answer

    return run
