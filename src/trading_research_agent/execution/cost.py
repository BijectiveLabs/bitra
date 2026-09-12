"""The round-trip cost identity, with one owner and every term kept separate.

    c_rt = 2 * fee + spread_cost + slippage + funding_accrual

For most of the research programme this package descends from, the measurement path contained
none of these terms: returns were computed mid to mid, and the corrected expectation moved from
marginally positive to -5.6 bps. Four terms living in four places is how an absent term stays
invisible. Here the identity has one function, every term is an explicit argument, and the
terms come back separately so a reader can see which one is doing the work.

Envelope parameters are arguments, never constants (R-27). A fee buried as a literal makes a
verdict unfalsifiable by anyone with a different schedule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trading_research_agent.envelope.model import Channel

BPS = Decimal(10_000)
NS_PER_S = 1_000_000_000


class CostError(ValueError):
    """A cost term could not be computed honestly. Never a silent zero."""


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


@dataclass(frozen=True)
class FundingSchedule:
    """The venue's settlement clock and the realised rates at each settlement.

    The clock is structural (Bybit perpetuals settle every eight hours from the Unix epoch), so
    the interval and anchor are not economic assumptions. The rates are: they are what the venue
    actually charged, keyed by the settlement timestamp in epoch nanoseconds, as fractions
    (0.0001 is one basis point).
    """

    interval_s: int
    rates: Mapping[int, Decimal]
    anchor_ts_ns: int = 0

    def __post_init__(self) -> None:
        if self.interval_s <= 0:
            raise CostError("settlement interval must be positive")
        for ts, rate in self.rates.items():
            if not isinstance(rate, Decimal) or not rate.is_finite():
                raise CostError(f"funding rate at {ts} is not a finite Decimal: {rate!r}")

    def settlements_spanned(self, entry_ts_ns: int, exit_ts_ns: int) -> tuple[int, ...]:
        """Settlements strictly after entry and at or before exit.

        A position opened at the exact instant of a settlement was not open for it. The walk
        starts from the venue's anchor, not from the position, because a position opened between
        two settlements does not move where those settlements fall.
        """
        if exit_ts_ns < entry_ts_ns:
            raise CostError("exit is before entry")
        step = self.interval_s * NS_PER_S
        k = (entry_ts_ns - self.anchor_ts_ns) // step + 1
        spanned: list[int] = []
        while True:
            t = self.anchor_ts_ns + k * step
            if t > exit_ts_ns:
                break
            if t > entry_ts_ns:
                spanned.append(t)
            k += 1
        return tuple(spanned)


def funding_accrual_bps(
    direction: Direction, schedule: FundingSchedule, entry_ts_ns: int, exit_ts_ns: int
) -> Decimal:
    """Signed funding over the holding period: direction * sum(rate_k) * 1e4.

    A long through a positive-funding settlement pays; a short receives. Zero is a legitimate
    answer for a position that spans no settlement. A settlement the position did span, with no
    rate on record, is not zero and is refused: a parser once defaulted a missing funding field
    to zero and the effect was invisible for weeks (R-17).
    """
    spanned = schedule.settlements_spanned(entry_ts_ns, exit_ts_ns)
    total = Decimal(0)
    for t in spanned:
        if t not in schedule.rates:
            raise CostError(
                f"no funding rate for settlement at {t}; the holding period spans it and "
                "a missing rate is not zero"
            )
        total += schedule.rates[t]
    return Decimal(direction.sign) * total * BPS


def pair_leg_fee_bps(fee_leg_bps: Decimal, hedge_beta: Decimal) -> Decimal:
    """Fee for one touch of a hedged pair: fee * (1 + beta).

    At a hedge ratio of 1.4 a pair transacts 2.4 legs, not 2. The omission understates cost most
    on exactly the pairs a selector prefers, which are the ones with the largest hedge ratio.
    """
    if not isinstance(hedge_beta, Decimal) or not hedge_beta.is_finite() or hedge_beta < 0:
        raise CostError(f"hedge_beta must be a finite non-negative Decimal, got {hedge_beta!r}")
    return fee_leg_bps * (1 + hedge_beta)


@dataclass(frozen=True)
class CostBreakdown:
    """Every term of the identity. `round_trip_bps` is their sum and nothing else."""

    fee_round_trip_bps: Decimal
    spread_cost_bps: Decimal
    slippage_bps: Decimal
    funding_accrual_bps: Decimal

    @property
    def round_trip_bps(self) -> Decimal:
        return (
            self.fee_round_trip_bps
            + self.spread_cost_bps
            + self.slippage_bps
            + self.funding_accrual_bps
        )


def realised_cost(
    *,
    channel: Channel,
    direction: Direction,
    entry_ts_ns: int,
    exit_ts_ns: int,
    spread_cost_bps: Decimal,
    slippage_bps: Decimal,
    funding: FundingSchedule,
    hedge_beta: Decimal = Decimal(0),
) -> CostBreakdown:
    """Assemble the identity. Spread and slippage are measured by the caller, not assumed here.

    `spread_cost_bps` is the half-spread read from the recorded book at the decision timestamp.
    `slippage_bps` is what walking the recorded depth at the intended size cost (see `book`).
    Both are required arguments so that omitting one is a missing argument, not a silent zero.
    """
    for label, value in (("spread_cost_bps", spread_cost_bps), ("slippage_bps", slippage_bps)):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise CostError(f"{label} must be a finite Decimal, got {value!r}")
        if value < 0:
            raise CostError(f"{label} cannot be negative; a credit here is the adversity unpaid")
    entry_fee = channel.entry_fee_bps
    exit_fee = channel.exit_fee_bps
    if hedge_beta:
        entry_fee = pair_leg_fee_bps(entry_fee, hedge_beta)
        exit_fee = pair_leg_fee_bps(exit_fee, hedge_beta)
    return CostBreakdown(
        fee_round_trip_bps=entry_fee + exit_fee,
        spread_cost_bps=spread_cost_bps,
        slippage_bps=slippage_bps,
        funding_accrual_bps=funding_accrual_bps(direction, funding, entry_ts_ns, exit_ts_ns),
    )
