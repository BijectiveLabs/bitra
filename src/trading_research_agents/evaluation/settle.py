"""Settlement of one idea on the minutes after it: take, stop or time, net of the desk's costs.

The idea enters at the open of the first traded minute at or after its decision, never at the price
it was decided on. A minute that reaches both levels counts as the stop, because the order inside a
minute is unknown and the worse reading is the honest one; a level is filled at the level. A minute
nobody saw inside the hold leaves the idea unsettled, since whether a level was touched in it is
unknown, and so does a funding settlement the archive holds no rate for.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any

from trading_research_agents.data.schema import Direction
from trading_research_agents.envelope.model import Channel
from trading_research_agents.evaluation.history import MinuteTape
from trading_research_agents.evaluation.policy import TradeIdea
from trading_research_agents.execution.cost import (
    CostBreakdown,
    CostError,
    FundingSchedule,
    funding_accrual_bps,
)
from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S
from trading_research_agents.market_data.deriv import DerivPoint, Series

BPS = Decimal(10_000)
ENTRY_WAIT_MINUTES = 5
PLACES = Decimal("0.0001")

# A schedule measured from the archive, the reason the archive gives none, or None for a market
# that pays no funding at all.
Funding = FundingSchedule | str | None


@dataclass(frozen=True)
class Costs:
    """The channel's fees per side, and what the operator declares per round trip: the full quoted
    spread, paid half at entry and half at exit, and the slippage of both legs."""

    channel: Channel
    spread_bps: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        for name, value in (("spread_bps", self.spread_bps), ("slippage_bps", self.slippage_bps)):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise CostError(f"{name} is a declared, non-negative number of basis points")

    def record(self) -> dict[str, str]:
        return {
            "channel": self.channel.key,
            "entry_fee_bps": str(self.channel.entry_fee_bps),
            "exit_fee_bps": str(self.channel.exit_fee_bps),
            "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps),
        }


def _q(value: Decimal) -> str:
    return str(value.quantize(PLACES))


@dataclass(frozen=True)
class Settlement:
    row: str
    instrument: str
    decided_ns: int
    entry_ns: int
    exit_ns: int
    side: Direction
    playbook: str
    entry_price: Decimal
    exit_price: Decimal
    ended: str
    stop_bps: Decimal | None
    take_bps: Decimal | None
    gross_bps: Decimal
    cost: CostBreakdown
    funding_basis: str

    @property
    def net_bps(self) -> Decimal:
        return self.gross_bps - self.cost.round_trip_bps

    @property
    def r_multiple(self) -> Decimal | None:
        """Net over the planned loss: the stop distance plus the round-trip cost."""
        if self.stop_bps is None:
            return None
        return self.net_bps / (self.stop_bps + self.cost.round_trip_bps)

    def record(self) -> dict[str, Any]:
        r = self.r_multiple
        cost = self.cost
        return {
            "row": self.row,
            "instrument": self.instrument,
            "decided_ns": self.decided_ns,
            "entry_ns": self.entry_ns,
            "exit_ns": self.exit_ns,
            "side": self.side.value,
            "playbook": self.playbook,
            "entry_price": str(self.entry_price),
            "exit_price": _q(self.exit_price),
            "ended": self.ended,
            "stop_bps": None if self.stop_bps is None else _q(self.stop_bps),
            "take_bps": None if self.take_bps is None else _q(self.take_bps),
            "gross_bps": _q(self.gross_bps),
            "net_bps": _q(self.net_bps),
            "r_multiple": None if r is None else _q(r),
            "cost": {
                "fee_round_trip_bps": _q(cost.fee_round_trip_bps),
                "spread_cost_bps": _q(cost.spread_cost_bps),
                "slippage_bps": _q(cost.slippage_bps),
                "funding_accrual_bps": _q(cost.funding_accrual_bps),
                "certainty": {
                    "fees": "declared",
                    "spread": "declared",
                    "slippage": "declared",
                    "funding": self.funding_basis,
                },
            },
        }


@dataclass(frozen=True)
class Unsettled:
    row: str
    instrument: str
    decided_ns: int
    reason: str

    def record(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "instrument": self.instrument,
            "decided_ns": self.decided_ns,
            "unsettled": self.reason,
        }


def settle(
    *,
    row: str,
    instrument: str,
    idea: TradeIdea,
    decided_ns: int,
    hold_ns: int,
    tape: MinuteTape,
    costs: Costs,
    funding: Funding,
) -> Settlement | Unsettled:
    """The idea's outcome on the minutes from its decision, or why the archive cannot say."""
    if decided_ns % NS_PER_MINUTE or hold_ns <= 0 or hold_ns % NS_PER_MINUTE:
        raise MarketDataError("a decision and its hold fall on minute boundaries")
    entry = _entry(tape, decided_ns)
    if isinstance(entry, str):
        return Unsettled(row, instrument, decided_ns, entry)
    path = tape.span(entry.start_ns, entry.start_ns + hold_ns)
    if path is None:
        return Unsettled(row, instrument, decided_ns, "the archive ends before the hold does")
    if any(m.gap is GapState.MISSING for m in path):
        return Unsettled(row, instrument, decided_ns, "a minute inside the hold was not seen")
    if isinstance(funding, str):
        return Unsettled(row, instrument, decided_ns, f"funding: {funding}")
    price = entry.ohlc()[0]
    ended, exit_ns, exit_price = _walk(idea, price, path)
    try:
        cost = _cost(costs, idea.side, entry.start_ns, exit_ns, funding)
    except CostError as exc:
        return Unsettled(row, instrument, decided_ns, f"funding: {exc}")
    return Settlement(
        row=row,
        instrument=instrument,
        decided_ns=decided_ns,
        entry_ns=entry.start_ns,
        exit_ns=exit_ns,
        side=idea.side,
        playbook=idea.playbook,
        entry_price=price,
        exit_price=exit_price,
        ended=ended,
        stop_bps=idea.stop_bps,
        take_bps=idea.take_bps,
        gross_bps=_gross(idea, price, ended, exit_price),
        cost=cost,
        funding_basis="none declared" if funding is None else "measured",
    )


def _entry(tape: MinuteTape, decided_ns: int) -> FlowBar | str:
    for k in range(ENTRY_WAIT_MINUTES):
        span = tape.span(decided_ns + k * NS_PER_MINUTE, decided_ns + (k + 1) * NS_PER_MINUTE)
        if span is None:
            return "the archive ends before the entry"
        bar = span[0]
        if bar.gap is GapState.MISSING:
            return "the minute to enter in was not seen"
        if not bar.empty:
            return bar
    return f"no trade within {ENTRY_WAIT_MINUTES} minutes of the decision to enter at"


def _walk(idea: TradeIdea, entry: Decimal, path: Sequence[FlowBar]) -> tuple[str, int, Decimal]:
    sign = idea.side.sign
    stop = None if idea.stop_bps is None else entry * (1 - sign * idea.stop_bps / BPS)
    take = None if idea.take_bps is None else entry * (1 + sign * idea.take_bps / BPS)
    last = entry
    for bar in path:
        if bar.empty:
            continue
        _, high, low, close = bar.ohlc()
        worst, best = (low, high) if sign > 0 else (high, low)
        if stop is not None and (worst - stop) * sign <= 0:
            return "stop", bar.end_ns, stop
        if take is not None and (best - take) * sign >= 0:
            return "take", bar.end_ns, take
        last = close
    return "time", path[-1].end_ns, last


def _gross(idea: TradeIdea, entry: Decimal, ended: str, exit_price: Decimal) -> Decimal:
    if ended == "stop" and idea.stop_bps is not None:
        return -idea.stop_bps
    if ended == "take" and idea.take_bps is not None:
        return idea.take_bps
    return (exit_price - entry) / entry * BPS * idea.side.sign


def _cost(
    costs: Costs,
    side: Direction,
    entry_ns: int,
    exit_ns: int,
    funding: FundingSchedule | None,
) -> CostBreakdown:
    paid = Decimal(0) if funding is None else funding_accrual_bps(side, funding, entry_ns, exit_ns)
    return CostBreakdown(
        fee_round_trip_bps=costs.channel.entry_fee_bps + costs.channel.exit_fee_bps,
        spread_cost_bps=costs.spread_bps,
        slippage_bps=costs.slippage_bps,
        funding_accrual_bps=paid,
    )


def funding_schedule(points: Sequence[DerivPoint]) -> FundingSchedule | str:
    """The settlement clock and the rates the archive holds, or why it holds none.

    The clock is the greatest common step between settlements, so a venue that moved a market from
    eight-hourly to four-hourly settlement keeps every settlement on it; one the archive has no
    rate for then refuses the ideas that span it instead of counting it as zero.
    """
    rates = {p.as_of_ns: p.value for p in points if p.series is Series.FUNDING}
    times = sorted(rates)
    if len(times) < 2:
        return "fewer than two funding settlements in the archive for this market"
    step = 0
    for a, b in pairwise(times):
        step = math.gcd(step, b - a)
    if step % NS_PER_S:
        return "funding settlements are not a whole number of seconds apart"
    return FundingSchedule(interval_s=step // NS_PER_S, rates=rates, anchor_ts_ns=times[0] % step)
