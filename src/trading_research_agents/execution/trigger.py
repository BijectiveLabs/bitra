"""Whether anything happened that is worth paying a model to look at.

A scheduler on a five minute bar will ask the roster two hundred and eighty times a day. Most of
those cycles refuse, and refusing is the right answer - but the tokens were still spent, and a desk
that spends more on research than it can win on the trade is not a desk. The fix is not to abandon
the schedule. It is to put a check in front of it that costs nothing.

Everything here is computed from figures the cycle already has for free: the snapshot's own book,
the cycle summaries on disk, and the clock. No model is called, so a cycle that holds costs the
price of one data pull. What it looks at:

  min_move_bps      the market has to have moved since the last look, or nothing is new to read
  max_spread_bps    a book this wide cannot be traded through whatever anyone concludes
  cooldown_s        one decision per instrument in this window; a second one is a second opinion
  widen_after       after a run of refusals with nothing changing, ask for a larger move each time
  look_valid_s      a last look older than this is not a reference; the instrument is a first look

The last one is the honest answer to the day that produces no trade. The desk does not go blind: it
raises its own bar for being disturbed, and one real move brings it straight back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any

from trading_research_agents.records import has_index, recent

BPS = Decimal(10_000)
# The cycle store owns this name; execution reads the file without importing upward for it.
SUMMARY = "cycle.json"
# A streak of refusals should not be able to raise the bar until the desk can never be woken.
MAX_WIDENING = 8


class TriggerError(ValueError):
    """The trigger as declared cannot be applied."""


@dataclass(frozen=True)
class Trigger:
    """[trigger]: what has to be true before the roster is paid to have an opinion."""

    min_move_bps: Decimal | None = None
    max_spread_bps: Decimal | None = None
    cooldown_s: int | None = None
    widen_after: int | None = None
    # A move measured against a mid from yesterday says little about now, so a reference older than
    # this is dropped and the instrument is looked at as if for the first time.
    look_valid_s: int | None = 3600

    def __post_init__(self) -> None:
        if self.look_valid_s is not None and self.look_valid_s < 1:
            raise TriggerError("[trigger].look_valid_for is at least one second")
        if self.min_move_bps is not None and self.min_move_bps < 0:
            raise TriggerError("[trigger].min_move_bps is not negative")
        if self.max_spread_bps is not None and self.max_spread_bps <= 0:
            raise TriggerError("[trigger].max_spread_bps is positive")
        if self.cooldown_s is not None and self.cooldown_s < 0:
            raise TriggerError("[trigger].cooldown_s is not negative")
        if self.widen_after is not None:
            if self.widen_after < 1:
                raise TriggerError("[trigger].widen_after counts refusals, so it is at least 1")
            if self.min_move_bps is None:
                raise TriggerError(
                    "[trigger].widen_after widens the move required, so min_move_bps comes with it"
                )

    @property
    def idle(self) -> bool:
        return (
            self.min_move_bps is None
            and self.max_spread_bps is None
            and self.cooldown_s is None
            and self.widen_after is None
        )

    def record(self) -> dict[str, Any]:
        return {
            "min_move_bps": None if self.min_move_bps is None else str(self.min_move_bps),
            "max_spread_bps": None if self.max_spread_bps is None else str(self.max_spread_bps),
            "cooldown_s": self.cooldown_s,
            "widen_after": self.widen_after,
            "look_valid_s": self.look_valid_s,
        }


@dataclass(frozen=True)
class Seen:
    """What the cycles already on disk say about this instrument."""

    mid: Decimal | None = None
    at: datetime | None = None
    refusals: int = 0
    last_intent_at: datetime | None = None

    def record(self) -> dict[str, Any]:
        return {
            "mid": None if self.mid is None else str(self.mid),
            "at": None if self.at is None else self.at.isoformat(),
            "refusals": self.refusals,
            "last_intent_at": (
                None if self.last_intent_at is None else self.last_intent_at.isoformat()
            ),
        }


@dataclass(frozen=True)
class Wake:
    """Whether to ask, and the arithmetic behind it."""

    ask: bool
    moved_bps: Decimal | None = None
    required_bps: Decimal | None = None
    spread_bps: Decimal | None = None
    reasons: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "ask": self.ask,
            "moved_bps": None if self.moved_bps is None else str(self.moved_bps),
            "required_bps": None if self.required_bps is None else str(self.required_bps),
            "spread_bps": None if self.spread_bps is None else str(self.spread_bps),
            "reasons": list(self.reasons),
        }


def moved_bps(*, mid: Decimal, before: Decimal) -> Decimal:
    """How far the mid has travelled since the last look, in basis points, unsigned."""
    if before <= 0:
        raise TriggerError("the previous mid is positive")
    return (abs(mid - before) / before * BPS).quantize(Decimal("0.01"))


def required_move(trigger: Trigger, *, refusals: int) -> Decimal | None:
    """The move this cycle has to see, after a run of refusals has raised the desk's own bar."""
    if trigger.min_move_bps is None:
        return None
    if trigger.widen_after is None or refusals < trigger.widen_after:
        return trigger.min_move_bps
    over = min(refusals - trigger.widen_after + 2, MAX_WIDENING)
    return trigger.min_move_bps * Decimal(over)


def worth_asking(
    trigger: Trigger,
    *,
    mid: Decimal | None,
    spread_bps: Decimal | None,
    seen: Seen,
    now: datetime,
    round_trips_today: int = 0,
    max_round_trips_per_day: int | None = None,
) -> Wake:
    """Decide, from free figures only, whether this cycle should pay anyone for an opinion."""
    holds: list[str] = []
    needed = required_move(trigger, refusals=seen.refusals)
    travelled: Decimal | None = None

    if max_round_trips_per_day is not None and round_trips_today >= max_round_trips_per_day:
        holds.append(
            f"{round_trips_today} intent(s) written today and the declared limit is "
            f"{max_round_trips_per_day}; nothing this cycle concludes could be acted on"
        )

    if trigger.cooldown_s is not None and seen.last_intent_at is not None:
        since = (now - seen.last_intent_at).total_seconds()
        if since < trigger.cooldown_s:
            holds.append(
                f"the last intent was {int(since)}s ago and the declared cooldown is "
                f"{trigger.cooldown_s}s; a second decision now is a second opinion on one position"
            )

    wide = trigger.max_spread_bps
    if wide is not None and spread_bps is not None and spread_bps > wide:
        holds.append(
            f"the book is {spread_bps} bps wide against the declared maximum {wide} bps; the toll "
            "alone would take whatever was found"
        )

    # Measured whenever there is something to measure against, declared minimum or not: the move is
    # what a pass over several instruments is ordered by, and a figure computed only when a
    # threshold happens to be set is a figure that disappears exactly when the threshold is waived.
    before = seen.mid
    if (
        before is not None
        and seen.at is not None
        and trigger.look_valid_s is not None
        and (now - seen.at).total_seconds() > trigger.look_valid_s
    ):
        before = None
    if mid is not None and before is not None and before > 0:
        travelled = moved_bps(mid=mid, before=before)
    if needed is not None and travelled is not None and travelled < needed:
        because = (
            ""
            if needed == trigger.min_move_bps
            else f", widened by {seen.refusals} refusals with nothing changing"
        )
        holds.append(
            f"the mid has moved {travelled} bps since the last cycle and the declared minimum "
            f"is {needed} bps{because}; there is nothing new to read"
        )

    return Wake(
        ask=not holds,
        moved_bps=travelled,
        required_bps=needed,
        spread_bps=spread_bps,
        reasons=tuple(holds),
    )


def spread_of(bid: Decimal | None, ask: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """The mid and the spread in bps of one touch, or nothing when there is no two-sided book."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    mid = (bid + ask) / Decimal(2)
    return mid, ((ask - bid) / mid * BPS).quantize(Decimal("0.01"))


def last_seen(root: Path, *, instrument: str, look_back: int = 40) -> Seen:
    """Read the recent cycles for this instrument: the last mid, and how long it has been refusing.

    From disk rather than from memory, because a process that restarts must not forget that it has
    already asked the same question forty times. A summary that cannot be read is skipped, which
    makes the streak a floor: the trigger holds less often than it could, never more.

    A cycle this rule already held is not counted. It is the desk deciding not to look, not the desk
    looking and finding nothing, and counting it would let the trigger widen its own bar every few
    minutes of quiet until nothing could wake it.
    """
    mid: Decimal | None = None
    at: datetime | None = None
    refusals = 0
    last_intent: datetime | None = None
    counting = True

    for row, when in _summaries(root, instrument=instrument, look_back=look_back):
        if mid is None:
            found = _mid_of(row)
            if found is not None:
                mid, at = found, when
        if row.get("held"):
            continue
        acted = row.get("intent") is not None
        if acted:
            counting = False
            if last_intent is None:
                last_intent = _intent_at(row) or when
        elif counting:
            refusals += 1
        if mid is not None and last_intent is not None:
            break
    return Seen(mid=mid, at=at, refusals=refusals, last_intent_at=last_intent)


def previous_looks(screens: Path, *, files: int = 6) -> dict[str, tuple[Decimal, datetime]]:
    """The latest mid each instrument had in the passes already written, with when it was taken.

    A pass looks at every instrument but runs a cycle on a few, so the cycles on disk are a poor
    answer to "since the last look": most names have not had one for hours. The screens are the
    record of every look.
    """
    if not screens.is_dir():
        return {}
    out: dict[str, tuple[Decimal, datetime]] = {}
    for path in sorted(screens.glob("*.json"), reverse=True)[: max(files, 1)]:
        when = at_of(path.stem)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = record.get("candidates") if isinstance(record, dict) else None
        if when is None or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("instrument") in out:
                continue
            found = _mid_of({"seen": {"mid": row.get("mid")}})
            if found is not None:
                out[str(row["instrument"])] = (found, when)
    return out


def _summaries(
    root: Path, *, instrument: str, look_back: int
) -> list[tuple[dict[str, Any], datetime | None]]:
    """The newest cycle summaries for one instrument, newest first."""
    if not root.is_dir():
        return []
    if has_index(root):
        rows = islice(recent(root, instrument=instrument), max(look_back, 1))
        return [(row, at_of(str(row.get("cycle_id", "")))) for row in rows]
    out: list[tuple[dict[str, Any], datetime | None]] = []
    for path in sorted(root.glob(f"*/{SUMMARY}"), reverse=True)[: max(look_back, 1)]:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict) or str(row.get("instrument", "")) != instrument:
            continue
        out.append((row, at_of(path.parent.name)))
    return out


def _mid_of(row: dict[str, Any]) -> Decimal | None:
    seen = row.get("seen")
    raw = seen.get("mid") if isinstance(seen, dict) else None
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None
    return value if value > 0 else None


def _intent_at(row: dict[str, Any]) -> datetime | None:
    intent = row.get("intent")
    if not isinstance(intent, dict):
        return None
    return at_of(str(intent.get("at", "")))


def at_of(cycle: str) -> datetime | None:
    """The clock time a cycle id stands for.

    Ids are minted as 2026-09-28T01-57-45Z, and a desk watching several instruments mints
    2026-09-28T01-57-45Z-BTCUSDT. The moment is the first twenty characters either way.
    """
    try:
        return datetime.strptime(cycle[:20], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
