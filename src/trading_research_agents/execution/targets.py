"""What the desk is trying to do, in numbers, checked before there is an intent.

Trading, not investing: the operator states what a trade has to be worth and how often the desk is
willing to do it, and code holds the line. Three declarations, all in basis points or counts, all
the client's to set:

  min_net_bps              what must be left of the edge after every cost the gate priced
  min_reward_risk          the take profit against the stop, both from [exit]
  max_round_trips_per_day  how many intents the desk will write in a day for this instrument

The frequency count is read from the intents the cycles actually wrote today, not from a counter in
memory. A process that restarts must not forget how much it has already traded.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


class TargetError(ValueError):
    """The targets as written cannot be applied."""


@dataclass(frozen=True)
class Targets:
    """[targets]: the shape of a trade this desk is willing to take."""

    min_net_bps: Decimal | None = None
    min_reward_risk: Decimal | None = None
    max_round_trips_per_day: int | None = None

    def __post_init__(self) -> None:
        if self.min_net_bps is not None and self.min_net_bps < 0:
            raise TargetError("[targets].min_net_bps is not negative")
        if self.min_reward_risk is not None and self.min_reward_risk <= 0:
            raise TargetError("[targets].min_reward_risk is positive")
        if self.max_round_trips_per_day is not None and self.max_round_trips_per_day < 0:
            raise TargetError("[targets].max_round_trips_per_day is not negative")

    @property
    def idle(self) -> bool:
        return (
            self.min_net_bps is None
            and self.min_reward_risk is None
            and self.max_round_trips_per_day is None
        )

    def record(self) -> dict[str, Any]:
        return {
            "min_net_bps": None if self.min_net_bps is None else str(self.min_net_bps),
            "min_reward_risk": None if self.min_reward_risk is None else str(self.min_reward_risk),
            "max_round_trips_per_day": self.max_round_trips_per_day,
        }


@dataclass(frozen=True)
class TargetVerdict:
    """Whether the trade in front of the gate is the trade the desk said it wanted."""

    admitted: bool
    net_bps: Decimal
    reward_risk: Decimal | None
    round_trips_today: int
    reasons: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "net_bps": str(self.net_bps),
            "reward_risk": None if self.reward_risk is None else str(self.reward_risk),
            "round_trips_today": self.round_trips_today,
            "reasons": list(self.reasons),
        }


def reward_risk(*, stop_frac: Decimal, take_frac: Decimal | None) -> Decimal | None:
    """The reward-to-risk of the exit rule as declared. No take profit means no ratio to check."""
    if take_frac is None:
        return None
    if stop_frac <= 0:
        raise TargetError("[exit].stop_sigma_frac is positive")
    return (take_frac / stop_frac).quantize(Decimal("0.001"))


def intents_today(root: Path, *, at: datetime, instrument: str) -> int:
    """How many intents this desk has already written today for this instrument.

    Counted from the cycle directories on disk, because that is the only record that survives a
    restart. A row that cannot be read is not counted and not fatal: the count is a floor, and the
    gate is stricter the more it finds, never looser.
    """
    day = at.astimezone(tz=at.tzinfo).date().isoformat()
    return sum(1 for row in _rows(root) if _is_today(row, day=day, instrument=instrument))


def _rows(root: Path) -> Iterator[dict[str, Any]]:
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/intents.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _is_today(row: dict[str, Any], *, day: str, instrument: str) -> bool:
    if str(row.get("instrument", "")) != instrument:
        return False
    # Cycle ids are 2026-09-28T01-57-45Z, so the date is the first ten characters either way.
    return str(row.get("at", ""))[:10] == day


def within_targets(
    targets: Targets,
    *,
    net_bps: Decimal,
    reward_to_risk: Decimal | None,
    round_trips_today: int,
) -> TargetVerdict:
    """Compare the trade with what the operator declared. Every refusal names its figure."""
    reasons: list[str] = []
    if targets.min_net_bps is not None and net_bps < targets.min_net_bps:
        reasons.append(f"net {net_bps} bps is below the declared minimum {targets.min_net_bps} bps")
    if targets.min_reward_risk is not None:
        if reward_to_risk is None:
            reasons.append(
                "no take profit is declared in [exit], so the reward-to-risk target cannot be met"
            )
        elif reward_to_risk < targets.min_reward_risk:
            reasons.append(
                f"reward-to-risk {reward_to_risk} is below the declared {targets.min_reward_risk}"
            )
    if (
        targets.max_round_trips_per_day is not None
        and round_trips_today >= targets.max_round_trips_per_day
    ):
        reasons.append(
            f"{round_trips_today} intent(s) already written today; the declared limit is "
            f"{targets.max_round_trips_per_day}"
        )
    return TargetVerdict(
        admitted=not reasons,
        net_bps=net_bps,
        reward_risk=reward_to_risk,
        round_trips_today=round_trips_today,
        reasons=tuple(reasons),
    )
