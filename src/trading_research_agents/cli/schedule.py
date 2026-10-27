"""When the next pass runs, what ends a run, and what holds a pass back from asking at all."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import sleep
from time import time as wall_clock
from typing import Any

from trading_research_agents.agents.cycle import budget_spent
from trading_research_agents.records import read_lines


def held_back_by(cfg: Any, out: Path, now: datetime) -> str:
    """Why this pass takes no new entry and asks no model, or empty. Each is the client's own line,
    counted per UTC day from the records on disk, so a restart cannot reset it."""
    from trading_research_agents.execution.targets import intents_today
    from trading_research_agents.llm.usage import spent_since

    if cfg.pause_switch is not None and cfg.pause_switch.exists():
        why = cfg.pause_switch.read_text(encoding="utf-8", errors="replace").strip()
        return (
            f"paused by {cfg.pause_switch}"
            + (f" ({why[:120]})" if why else "")
            + ("; `tra pause --off` resumes")
        )
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    a = cfg.agents
    if a is not None and (a.daily_token_budget or a.daily_cost_budget):
        ledgers = (out / "usage.jsonl", out.parent / "audits" / "usage.jsonl")
        tokens, cost = spent_since(ledgers, midnight.timestamp())
        if a.daily_token_budget and tokens >= a.daily_token_budget:
            return f"daily token budget spent: {tokens} of {a.daily_token_budget} since 00:00 UTC"
        if a.daily_cost_budget and cost >= a.daily_cost_budget:
            return f"daily cost budget spent: {cost:.4f} of {a.daily_cost_budget} since 00:00 UTC"
    cap = cfg.limits.max_trades_per_day
    if cap is not None:
        sent = intents_today(out, at=now, instrument=None)
        if sent >= cap:
            return (
                f"{sent} trade(s) today across every instrument; [controls].max_trades_per_day "
                f"is {cap}"
            )
    streak_cap = cfg.limits.max_losing_streak
    if streak_cap is not None:
        streak = losing_streak_today(out / "round_trips.jsonl", midnight)
        if streak >= streak_cap:
            return (
                f"{streak} losing round trips in a row today; [controls].max_losing_streak is "
                f"{streak_cap}"
            )
    return ""


def losing_streak_today(path: Path, midnight: datetime) -> int:
    """Losing round trips in a row, newest first, closed since `midnight`. A trip with no result is
    passed over; a winner ends the count."""

    streak = 0
    for trip in reversed(read_lines(path) if path.is_file() else []):
        try:
            closed = datetime.fromisoformat(str(trip.get("at")))
        except ValueError:
            continue
        if closed < midnight:
            break
        if trip.get("net") is None:
            continue
        if Decimal(str(trip["net"])) >= 0:
            break
        streak += 1
    return streak


@dataclass
class Schedule:
    """When the next cycle runs, and what ends the run.

    A desk does not want a cycle every few seconds; it wants one when a bar has closed and the
    figures have changed. So the wait lands on the interval's own boundary, and everything that
    should end a run - the budget, the kill switch, the count asked for, a keyboard - ends it by
    name rather than by the process stopping quietly.
    """

    every_s: float | None = None
    cycles: int | None = None
    kill_switch: Path | None = None
    clock: Callable[[], float] = wall_clock
    rest: Callable[[float], None] = sleep

    @property
    def looping(self) -> bool:
        return self.every_s is not None

    @property
    def every_label(self) -> str:
        every = int(self.every_s or 0)
        for size, unit in ((3600, "h"), (60, "m")):
            if every >= size and every % size == 0:
                return f"{every // size}{unit}"
        return f"{every}s"

    def stop_reason(self, result: Any, ran: int) -> str:
        if not self.looping:
            return "one cycle"
        # A pass that chose nothing has no result to read, and that is not a reason to stop.
        stopped = getattr(result, "stopped_by", None)
        if stopped and budget_spent(str(stopped)):
            return f"the budget: {stopped}"
        if self.kill_switch is not None and self.kill_switch.exists():
            return f"the kill switch at {self.kill_switch}"
        if self.cycles is not None and ran >= self.cycles:
            return f"the {self.cycles} pass(es) asked for"
        return ""

    def left(self) -> float:
        """Seconds to the next boundary, so a cycle reads a bar that has finished happening."""
        every = self.every_s or 0.0
        if every <= 0:
            return 0.0
        return round(every - (self.clock() % every), 3)

    def announce(self, left: float) -> str:
        """The wait as an operator reads it: when it ends on the clock, and why it is short."""
        ends = datetime.fromtimestamp(self.clock() + left, tz=UTC).strftime("%H:%M:%S")
        minutes, seconds = divmod(int(left + 0.999), 60)
        hours, minutes = divmod(minutes, 60)
        shown = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"
        return (
            f"scheduler: next pass at {ends} UTC, in {shown}. "
            f"Passes land on the clock, every {self.every_label}, "
            "not a full interval after the last one"
        )

    def wait(self) -> float:
        """Sleep to the next boundary and say how long that was."""
        left = self.left()
        if left > 0:
            self.rest(left)
        return left
