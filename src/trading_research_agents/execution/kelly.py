"""The Kelly fraction a desk's own record supports, used as a ceiling on what one trade may risk.

Every trade here has a stop and a target, so a finished trade is one number: what it made, as a
multiple of the loss the cycle planned at its stop (`r` on a round trip, -1 being a stop hit as
priced). From those multiples the record gives three figures - how often a trade won, the average
win and the average loss - and the fraction of equity that maximises long-run growth follows from
them in closed form:

    f* = (p * W - q * L) / ((p + q) * W * L)

with p and q the shares of winning and losing trades, W and L the average win and loss in planned
losses. A trade that closed flat counts in neither. With every trade a win or a loss, this is the
familiar p / L - q / W.

Two choices make it a ceiling rather than a lever. Only a fraction of f* is applied, because the
three figures are estimates and full Kelly on an overstated edge is the fastest way to lose an
account. And the result only ever lowers the risk the operator declared, never raises it. A record
too short to say anything leaves the declared risk alone; a record long enough that shows no edge
refuses new entries, because that is what the formula says to bet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.execution.montecarlo import largest_within, simulate

BPS = Decimal(10_000)
# One-sided 95%: the win rate the record supports with that confidence, reported beside f*.
Z_95 = Decimal("1.645")
QUANTUM = Decimal("0.0001")


class KellyError(ValueError):
    """The inputs are not a record this can measure."""


@dataclass(frozen=True)
class Measured:
    """What the desk's own finished trades say, in planned losses."""

    trades: int
    wins: int
    losses: int
    win_rate: Decimal
    win_rate_low: Decimal
    avg_win_r: Decimal | None
    avg_loss_r: Decimal | None

    def record(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": str(self.win_rate.quantize(QUANTUM)),
            "win_rate_low_95": str(self.win_rate_low.quantize(QUANTUM)),
            "avg_win_r": None if self.avg_win_r is None else str(self.avg_win_r.quantize(QUANTUM)),
            "avg_loss_r": (
                None if self.avg_loss_r is None else str(self.avg_loss_r.quantize(QUANTUM))
            ),
        }


@dataclass(frozen=True)
class KellyCap:
    """The ceiling on risk per trade, or why there is none, and the arithmetic behind it."""

    measured: Measured | None
    full: Decimal | None
    fraction: Decimal
    cap_bps: Decimal | None
    refuses: bool
    reason: str

    def record(self) -> dict[str, Any]:
        return {
            "measured": None if self.measured is None else self.measured.record(),
            "kelly": None if self.full is None else str(self.full.quantize(QUANTUM)),
            "fraction": str(self.fraction),
            "cap_bps": None
            if self.cap_bps is None
            else str(self.cap_bps.quantize(Decimal("0.01"))),
            "refuses": self.refuses,
            "reason": self.reason,
        }


def multiples(rows: Sequence[Mapping[str, Any]]) -> list[Decimal]:
    """The result of every finished round trip that knows its planned loss, oldest first."""
    found: list[Decimal] = []
    for row in rows:
        if "net" not in row or row.get("r") in (None, ""):
            continue
        try:
            value = Decimal(str(row["r"]))
        except InvalidOperation:
            continue
        if value.is_finite():
            found.append(value)
    return found


def wilson_low(wins: int, trades: int, z: Decimal = Z_95) -> Decimal:
    """The lower Wilson bound on a win rate: what the record supports, not what it happened to
    show."""
    if trades <= 0 or not 0 <= wins <= trades:
        raise KellyError("wins are counted out of a positive number of trades")
    n = Decimal(trades)
    p = Decimal(wins) / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * ((p * (1 - p) / n) + z2 / (4 * n * n)).sqrt()
    return max((centre - spread) / (1 + z2 / n), Decimal(0))


def measure(results: Sequence[Decimal]) -> Measured:
    if not results:
        raise KellyError("there is no finished trade to measure")
    won = [r for r in results if r > 0]
    lost = [-r for r in results if r < 0]
    n = len(results)
    return Measured(
        trades=n,
        wins=len(won),
        losses=len(lost),
        win_rate=Decimal(len(won)) / Decimal(n),
        win_rate_low=wilson_low(len(won), n),
        avg_win_r=sum(won, Decimal(0)) / len(won) if won else None,
        avg_loss_r=sum(lost, Decimal(0)) / len(lost) if lost else None,
    )


def kelly(measured: Measured) -> Decimal | None:
    """f* for these figures, or None when the record has no loss or no win to size against."""
    if measured.avg_win_r is None or measured.avg_loss_r is None:
        return None
    n = Decimal(measured.trades)
    p, q = Decimal(measured.wins) / n, Decimal(measured.losses) / n
    w, loss = measured.avg_win_r, measured.avg_loss_r
    return (p * w - q * loss) / ((p + q) * w * loss)


def kelly_cap(results: Sequence[Decimal], *, fraction: Decimal, min_trades: int) -> KellyCap:
    """The ceiling the record supports on risk per trade, in bps of equity."""
    if not Decimal(0) < fraction <= Decimal(1):
        raise KellyError("the Kelly fraction is above 0 and at most 1")
    if min_trades <= 0:
        raise KellyError("the minimum number of finished trades is positive")
    if len(results) < min_trades:
        return KellyCap(
            None,
            None,
            fraction,
            None,
            False,
            f"{len(results)} finished trade(s) with a planned loss on record, below the "
            f"{min_trades} the sizing waits for; the declared risk stands",
        )
    measured = measure(results)
    if measured.avg_win_r is None and measured.avg_loss_r is not None:
        return KellyCap(
            measured,
            None,
            fraction,
            None,
            True,
            f"none of the last {measured.trades} finished trades won; no new entry until a "
            "person reviews the desk",
        )
    full = kelly(measured)
    if full is None:
        return KellyCap(
            measured,
            None,
            fraction,
            None,
            False,
            f"no losing trade among the last {measured.trades}, so there is no loss to size "
            "against; the declared risk stands",
        )
    if full <= 0:
        return KellyCap(
            measured,
            full,
            fraction,
            None,
            True,
            f"the last {measured.trades} finished trades give a Kelly fraction of "
            f"{full.quantize(QUANTUM)}: no edge to bet on; no new entry until a person reviews "
            "the desk",
        )
    cap = full * fraction * BPS
    return KellyCap(
        measured,
        full,
        fraction,
        cap,
        False,
        f"Kelly {full.quantize(QUANTUM)} from the last {measured.trades} finished trades, "
        f"{fraction} of it: at most {cap.quantize(Decimal('0.01'))} bps of equity at the stop",
    )


@dataclass(frozen=True)
class SizingRule:
    """[sizing]: how far the desk's own record may lower the risk the operator declared.

    Off until `kelly_fraction` is declared. Every other figure has a standard value.
    """

    kelly_fraction: Decimal | None = None
    min_round_trips: int = 30
    lookback_round_trips: int = 100
    simulate_trades: int = 100
    simulate_paths: int = 2000
    seed: int = 20_261_021
    max_drawdown_probability: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if self.kelly_fraction is not None and not Decimal(0) < self.kelly_fraction <= 1:
            raise KellyError("[sizing].kelly_fraction is above 0 and at most 1")
        if self.min_round_trips < 10:
            raise KellyError("[sizing].min_round_trips is at least 10; fewer measure nothing")
        if self.lookback_round_trips < self.min_round_trips:
            raise KellyError("[sizing].lookback_round_trips is at least min_round_trips")
        if not 10 <= self.simulate_trades <= 1000:
            raise KellyError("[sizing].simulate_trades is between 10 and 1000")
        if not 100 <= self.simulate_paths <= 20_000:
            raise KellyError("[sizing].simulate_paths is between 100 and 20000")
        if self.seed < 0:
            raise KellyError("[sizing].seed is a non-negative integer")
        if not Decimal(0) < self.max_drawdown_probability < Decimal("0.5"):
            raise KellyError("[sizing].max_drawdown_probability is above 0 and below 0.5")

    @property
    def idle(self) -> bool:
        return self.kelly_fraction is None

    def record(self) -> dict[str, Any]:
        return {
            "kelly_fraction": None if self.kelly_fraction is None else str(self.kelly_fraction),
            "min_round_trips": self.min_round_trips,
            "lookback_round_trips": self.lookback_round_trips,
            "simulate_trades": self.simulate_trades,
            "simulate_paths": self.simulate_paths,
            "seed": self.seed,
            "max_drawdown_probability": str(self.max_drawdown_probability),
        }


@dataclass(frozen=True)
class Sized:
    """What the record allows one trade to risk, and everything it was worked out from."""

    rule: SizingRule
    kelly: KellyCap
    cap_bps: Decimal | None
    refuses: bool
    reason: str
    simulation: dict[str, Any] | None = None

    def record(self) -> dict[str, Any]:
        return {
            "rule": self.rule.record(),
            "kelly": self.kelly.record(),
            "cap_bps": None if self.cap_bps is None else str(self.cap_bps),
            "refuses": self.refuses,
            "reason": self.reason,
            "simulation": self.simulation,
        }


def by_record(
    rule: SizingRule,
    rows: Sequence[Mapping[str, Any]],
    *,
    room_bps: Decimal | None = None,
) -> Sized:
    """The ceiling on risk per trade the desk's round trips support, Kelly first, then the
    drawdown the next `simulate_trades` could take it through."""
    if rule.kelly_fraction is None:
        raise KellyError("[sizing] is not declared")
    results = multiples(rows)[-rule.lookback_round_trips :]
    cap = kelly_cap(results, fraction=rule.kelly_fraction, min_trades=rule.min_round_trips)
    if cap.refuses or cap.cap_bps is None:
        return Sized(rule, cap, None, cap.refuses, cap.reason)
    if room_bps is None:
        seen = simulate(
            [float(r) for r in results],
            float(cap.cap_bps),
            paths=rule.simulate_paths,
            trades=rule.simulate_trades,
            seed=rule.seed,
        )
        return Sized(rule, cap, cap.cap_bps, False, cap.reason, seen.record())
    applied, seen = largest_within(
        results,
        cap.cap_bps,
        room_bps=room_bps,
        tolerance=rule.max_drawdown_probability,
        paths=rule.simulate_paths,
        trades=rule.simulate_trades,
        seed=rule.seed,
    )
    if applied <= 0:
        return Sized(
            rule,
            cap,
            None,
            True,
            f"{room_bps} bps of room is left under [controls].max_drawdown_bps, and no risk per "
            "trade keeps the chance of using it up within "
            f"[sizing].max_drawdown_probability {rule.max_drawdown_probability}",
            seen.record(),
        )
    if applied < cap.cap_bps:
        reason = (
            f"{cap.reason}; cut to {applied} bps so that the chance of using up the {room_bps} "
            f"bps left under the drawdown limit within {rule.simulate_trades} trades is at most "
            f"{rule.max_drawdown_probability}"
        )
    else:
        reason = (
            f"{cap.reason}; the chance of using up the {room_bps} bps left under the drawdown "
            f"limit within {rule.simulate_trades} trades is within "
            f"{rule.max_drawdown_probability}"
        )
    return Sized(rule, cap, applied, False, reason, seen.record())
