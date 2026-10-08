"""What the desk is trying to do, in numbers, checked before there is an intent.

Trading, not investing: the operator states what a trade has to be worth and how often the desk is
willing to do it, and code holds the line. Five declarations, all in basis points or counts, all
the client's to set:

  min_net_bps              what must be left of the edge after every cost the gate priced
  min_edge_multiple        the claimed edge as a multiple of the round trip it has to pay for
  min_reward_risk          the take profit against the stop, both from [exit]
  max_research_bps         what the models may cost to reach a decision, against its own notional
  max_round_trips_per_day  how many intents the desk will write in a day for this instrument

The second one is the difference between a desk and a toll collector. A cost of 18 bps against an
edge of 20 is not a two basis point trade, it is a trade that needs the next hour to behave exactly
as described: the fees are certain, the edge is a probability, and slippage moves only one of them.
Requiring the edge to be a multiple of the cost prices that asymmetry before a model is believed
rather than after the fills are in.

The research cost is the other half of the same argument. Tokens are a real cost of the decision, so
a decision that cost more than the trade can plausibly return is refused on its own numbers.

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

from trading_research_agents.records import has_index, recent


class TargetError(ValueError):
    """The targets as written cannot be applied."""


@dataclass(frozen=True)
class Targets:
    """[targets]: the shape of a trade this desk is willing to take."""

    min_net_bps: Decimal | None = None
    min_edge_multiple: Decimal | None = None
    min_reward_risk: Decimal | None = None
    max_research_bps: Decimal | None = None
    max_round_trips_per_day: int | None = None

    def __post_init__(self) -> None:
        if self.min_net_bps is not None and self.min_net_bps < 0:
            raise TargetError("[targets].min_net_bps is not negative")
        if self.min_edge_multiple is not None and self.min_edge_multiple < 1:
            raise TargetError(
                "[targets].min_edge_multiple is at least 1; below that the desk is trading to pay "
                "the venue"
            )
        if self.min_reward_risk is not None and self.min_reward_risk <= 0:
            raise TargetError("[targets].min_reward_risk is positive")
        if self.max_research_bps is not None and self.max_research_bps <= 0:
            raise TargetError("[targets].max_research_bps is positive")
        if self.max_round_trips_per_day is not None and self.max_round_trips_per_day < 0:
            raise TargetError("[targets].max_round_trips_per_day is not negative")

    @property
    def idle(self) -> bool:
        return (
            self.min_net_bps is None
            and self.min_edge_multiple is None
            and self.min_reward_risk is None
            and self.max_research_bps is None
            and self.max_round_trips_per_day is None
        )

    def record(self) -> dict[str, Any]:
        return {
            "min_net_bps": None if self.min_net_bps is None else str(self.min_net_bps),
            "min_edge_multiple": (
                None if self.min_edge_multiple is None else str(self.min_edge_multiple)
            ),
            "min_reward_risk": None if self.min_reward_risk is None else str(self.min_reward_risk),
            "max_research_bps": (
                None if self.max_research_bps is None else str(self.max_research_bps)
            ),
            "max_round_trips_per_day": self.max_round_trips_per_day,
        }


@dataclass(frozen=True)
class TargetVerdict:
    """Whether the trade in front of the gate is the trade the desk said it wanted."""

    admitted: bool
    net_bps: Decimal
    reward_risk: Decimal | None
    round_trips_today: int
    edge_multiple: Decimal | None = None
    research_bps: Decimal | None = None
    reasons: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "net_bps": str(self.net_bps.quantize(Decimal("0.0001"))),
            "reward_risk": None if self.reward_risk is None else str(self.reward_risk),
            "round_trips_today": self.round_trips_today,
            "edge_multiple": None if self.edge_multiple is None else str(self.edge_multiple),
            "research_bps": None if self.research_bps is None else str(self.research_bps),
            "reasons": list(self.reasons),
        }


def edge_multiple(*, edge_bps: Decimal, cost_bps: Decimal) -> Decimal | None:
    """The claimed edge as a multiple of the round trip. A free round trip has no multiple."""
    if cost_bps <= 0:
        return None
    return (edge_bps / cost_bps).quantize(Decimal("0.01"))


def research_bps(*, spend: Decimal, notional: Decimal) -> Decimal | None:
    """What the models cost to reach this decision, against the notional it wants to trade.

    The spend a caller hands in is every call since the desk last wrote an intent, not the calls of
    one cycle: a day of refusals is paid for by whatever trade the day eventually produces, and
    pretending otherwise is how research cost disappears from a track record.
    """
    if notional <= 0 or spend < 0:
        return None
    return (spend / notional * Decimal(10_000)).quantize(Decimal("0.01"))


def reward_risk(*, stop_frac: Decimal, take_frac: Decimal | None) -> Decimal | None:
    """The reward-to-risk of the exit rule as declared. No take profit means no ratio to check."""
    if take_frac is None:
        return None
    if stop_frac <= 0:
        raise TargetError("[exit].stop_frac is positive")
    return (take_frac / stop_frac).quantize(Decimal("0.001"))


def intents_today(root: Path, *, at: datetime, instrument: str | None) -> int:
    """How many intents this desk has already written today, for one instrument or for all of them.

    Counted from the cycle directories on disk, because that is the only record that survives a
    restart. A row that cannot be read is not counted and not fatal: the count is a floor, and the
    gate is stricter the more it finds, never looser.
    """
    day = at.astimezone(tz=at.tzinfo).date().isoformat()
    return sum(1 for row in _rows(root, day) if _is_today(row, day=day, instrument=instrument))


def _rows(root: Path, day: str) -> Iterator[dict[str, Any]]:
    if not root.is_dir():
        return
    if has_index(root):
        for row in recent(root, day=day):
            if isinstance(row.get("intent"), dict):
                yield row["intent"]
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


def _is_today(row: dict[str, Any], *, day: str, instrument: str | None) -> bool:
    if instrument is not None and str(row.get("instrument", "")) != instrument:
        return False
    # Cycle ids are 2026-09-28T01-57-45Z, so the date is the first ten characters either way.
    return str(row.get("at", ""))[:10] == day


def within_targets(
    targets: Targets,
    *,
    net_bps: Decimal,
    reward_to_risk: Decimal | None,
    round_trips_today: int,
    over_cost: Decimal | None = None,
    research: Decimal | None = None,
) -> TargetVerdict:
    """Compare the trade with what the operator declared. Every refusal names its figure."""
    reasons: list[str] = []
    if targets.min_net_bps is not None and net_bps < targets.min_net_bps:
        reasons.append(f"net {net_bps} bps is below the declared minimum {targets.min_net_bps} bps")
    if targets.min_edge_multiple is not None:
        if over_cost is None:
            reasons.append(
                "the round trip priced at zero, so the edge cannot be compared against it and the "
                "multiple the desk declared cannot be shown to be met"
            )
        elif over_cost < targets.min_edge_multiple:
            reasons.append(
                f"the claimed edge is {over_cost}x the round trip and the desk declared "
                f"{targets.min_edge_multiple}x; the cost is certain and the edge is not"
            )
    if (
        targets.max_research_bps is not None
        and research is not None
        and research > targets.max_research_bps
    ):
        reasons.append(
            f"the models cost {research} bps of this trade's own notional to reach a decision, "
            f"above the declared {targets.max_research_bps} bps"
        )
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
        edge_multiple=over_cost,
        research_bps=research,
        reasons=tuple(reasons),
    )
