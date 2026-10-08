"""Layer 6: exits bound to the mechanism, simulated with deliberately adverse fill assumptions.

Only OHLC is known per bar. The order of events inside a bar cannot be recovered, so a stop
simulator has to choose an assumption to fill that gap, and a favourable choice manufactures an
edge that does not exist. Every choice here points against the strategy: a touch is a fire,
a gap through the stop fills at the worse open, a bar touching both take-profit and stop is a
stop, a take-profit fills exactly at its limit and never better. If a hypothesis survives these,
it survives because the edge is real, not because the simulator was generous.

The research this descends from measured what the conservatism costs when it is wrong: at a
one-sigma stop, 38.3 percent of trades were stopped, 82.1 percent of those would have lost
anyway, a correct stop saved 149 bps and a wrong one cost 862 on average. That asymmetry is a
property of the market, not of the instrument, whose only job is to keep the decision causal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trading_research_agents.data.schema import Direction
from trading_research_agents.strategy.core import Bar

BPS = Decimal(10_000)


class ExitError(ValueError):
    """The exit cannot be simulated honestly as specified."""


def stop_price(entry_px: Decimal, direction: Direction, frac: Decimal) -> Decimal:
    """The stop at `frac` of price from entry, on the adverse side."""
    if not isinstance(entry_px, Decimal) or entry_px <= 0:
        raise ExitError(f"entry_px must be a positive Decimal, got {entry_px!r}")
    if not isinstance(frac, Decimal) or frac <= 0:
        raise ExitError(f"frac must be a positive Decimal, got {frac!r}")
    if direction is Direction.LONG:
        return entry_px * (1 - frac)
    return entry_px * (1 + frac)


def _slip(fill: Decimal, direction: Direction, slip_bps: Decimal) -> Decimal:
    factor = slip_bps / BPS
    return fill * (1 - factor) if direction is Direction.LONG else fill * (1 + factor)


def _check_slip(slip_bps: Decimal) -> None:
    if not isinstance(slip_bps, Decimal) or slip_bps < 0:
        # A negative slip would credit the strategy with the adversity it is meant to pay.
        raise ExitError(f"slip_bps must be a non-negative Decimal, got {slip_bps!r}")


@dataclass(frozen=True)
class StopOutcome:
    exit_px: Decimal
    bars_held: int
    stopped: bool


def simulate_with_stop(
    direction: Direction,
    stop_px: Decimal,
    bars: Sequence[Bar],
    exit_px: Decimal,
    slip_bps: Decimal,
) -> StopOutcome:
    """Walk the bars in order; exit the instant the stop is touched, else at `exit_px`.

    A touch exactly at the level counts. A gap through the stop fills at the worse open, then
    slippage is applied on the adverse side. Slippage is not charged on the scheduled exit; the
    round-trip cost at the caller's layer already carries it. An empty bar is skipped, not
    scored as a touch: missing data is not evidence that the stop fired.
    """
    _check_slip(slip_bps)
    if not isinstance(stop_px, Decimal) or stop_px <= 0:
        raise ExitError("stop_px must be a positive Decimal")
    for i, bar in enumerate(bars):
        if bar.empty:
            continue
        o, h, lo = bar.ohl()
        touched = lo <= stop_px if direction is Direction.LONG else h >= stop_px
        if not touched:
            continue
        fill = min(stop_px, o) if direction is Direction.LONG else max(stop_px, o)
        return StopOutcome(exit_px=_slip(fill, direction, slip_bps), bars_held=i + 1, stopped=True)
    return StopOutcome(exit_px=exit_px, bars_held=len(bars), stopped=False)


class BracketOutcome(StrEnum):
    TAKE_PROFIT = "tp"
    STOP_LOSS = "sl"
    SCHEDULED = "exit"


@dataclass(frozen=True)
class BracketResult:
    exit_px: Decimal
    bars_held: int
    cause: BracketOutcome


def simulate_with_bracket(
    entry_px: Decimal,
    direction: Direction,
    tp_px: Decimal,
    sl_px: Decimal,
    bars: Sequence[Bar],
    exit_px: Decimal,
    slip_bps: Decimal,
) -> BracketResult:
    """Walk the bars with both a take-profit and a stop. A two-sided bar is a stop.

    Which came first inside a bar cannot be proven, so the adverse reading is taken; on a
    reversion strategy a two-sided bar is the common case, not the edge case. A take-profit is a
    resting limit and fills exactly at its level, never better, and pays no slippage.
    """
    _check_slip(slip_bps)
    if direction is Direction.LONG and not tp_px > entry_px > sl_px:
        raise ExitError(
            f"LONG requires sl < entry < tp; got sl={sl_px} entry={entry_px} tp={tp_px}"
        )
    if direction is Direction.SHORT and not tp_px < entry_px < sl_px:
        raise ExitError(
            f"SHORT requires tp < entry < sl; got tp={tp_px} entry={entry_px} sl={sl_px}"
        )
    for i, bar in enumerate(bars):
        if bar.empty:
            continue
        o, h, lo = bar.ohl()
        if direction is Direction.LONG:
            sl_hit, tp_hit = lo <= sl_px, h >= tp_px
        else:
            sl_hit, tp_hit = h >= sl_px, lo <= tp_px
        if sl_hit:
            fill = min(sl_px, o) if direction is Direction.LONG else max(sl_px, o)
            return BracketResult(_slip(fill, direction, slip_bps), i + 1, BracketOutcome.STOP_LOSS)
        if tp_hit:
            return BracketResult(tp_px, i + 1, BracketOutcome.TAKE_PROFIT)
    return BracketResult(exit_px, len(bars), BracketOutcome.SCHEDULED)


@dataclass(frozen=True)
class ExitRule:
    """How a position ends, declared before it opens, and tied to the mechanism it trades.

    `mechanism_bound` is the one sentence saying why this exit is where the mechanism is done.
    A stop chosen because it looked good on a chart is not bound to anything; the rule refuses
    to exist without the sentence.
    """

    stop_frac: Decimal
    timeout_bars: int
    mechanism_bound: str
    take_profit_frac: Decimal | None = None
    slip_bps: Decimal = Decimal("5.0")

    def __post_init__(self) -> None:
        if not isinstance(self.stop_frac, Decimal) or self.stop_frac <= 0:
            raise ExitError("stop_frac must be a positive Decimal")
        if self.timeout_bars <= 0:
            raise ExitError("timeout_bars must be positive")
        if len(self.mechanism_bound.split()) < 6:
            raise ExitError(
                "an exit rule states, in a sentence, why it is where the mechanism ends"
            )
        if self.take_profit_frac is not None and self.take_profit_frac <= 0:
            raise ExitError("take_profit_frac must be positive when given")
        _check_slip(self.slip_bps)

    def stop_for(self, entry_px: Decimal, direction: Direction) -> Decimal:
        return stop_price(entry_px, direction, self.stop_frac)

    def take_profit_for(self, entry_px: Decimal, direction: Direction) -> Decimal | None:
        if self.take_profit_frac is None:
            return None
        if direction is Direction.LONG:
            return entry_px * (1 + self.take_profit_frac)
        return entry_px * (1 - self.take_profit_frac)
