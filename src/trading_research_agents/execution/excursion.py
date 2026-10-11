"""How far a position went the right way, and how far the wrong way, before it closed.

A round trip reports one number: what it earned. That number hides the two facts an operator
actually needs. A trade that closed at plus two basis points after being twenty in front was an
exit problem, not an entry problem. A trade that closed at plus two after being fourteen under
water was a stop that nearly fired, and it will fire on the next one like it.

So every closed round trip also carries its maximum favourable excursion and its maximum adverse
excursion, in basis points against the entry, with the bar each happened on. Both are signed the
way a trader reads them: MFE is positive when the position was in front, MAE is negative when it
was under water, whichever way the position was facing.

These are path figures, not cost terms. They never enter the cost identity, and they never change
what a trade earned; they go into the record so the risk manager and the auditors can see the
shape of what happened rather than only the outcome.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.data.schema import Direction
from trading_research_agents.execution.cost import BPS, CostError

QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class Excursion:
    """The best and worst the position ever stood at, and when."""

    mfe_bps: Decimal
    mae_bps: Decimal
    bars_to_mfe: int
    bars_to_mae: int
    bars: int

    def record(self) -> dict[str, Any]:
        return {
            "mfe_bps": str(self.mfe_bps.quantize(QUANTUM)),
            "mae_bps": str(self.mae_bps.quantize(QUANTUM)),
            "bars_to_mfe": self.bars_to_mfe,
            "bars_to_mae": self.bars_to_mae,
            "bars": self.bars,
        }


@dataclass
class ExcursionTracker:
    """Running excursion for one open position, fed one closed bar at a time.

    The runtime already walks every bar while a position is open, so the figures cost one
    comparison per bar and no extra memory. A bar with no trades moves nothing.
    """

    entry_px: Decimal
    direction: Direction
    mfe_bps: Decimal = Decimal(0)
    mae_bps: Decimal = Decimal(0)
    bars_to_mfe: int = 0
    bars_to_mae: int = 0
    bars: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.entry_px, Decimal) or not self.entry_px.is_finite():
            raise CostError(f"entry_px must be a finite Decimal, got {self.entry_px!r}")
        if self.entry_px <= 0:
            raise CostError("entry_px must be positive")

    def _bps(self, price: Decimal) -> Decimal:
        sign = Decimal(self.direction.sign)
        return sign * (price - self.entry_px) / self.entry_px * BPS

    def see(self, high: Decimal | None, low: Decimal | None) -> None:
        """One closed bar. The favourable side of the bar and the adverse side both count."""
        self.bars += 1
        if high is None or low is None:
            return
        for price in (high, low):
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise CostError(f"bar prices must be positive finite Decimals, got {price!r}")
        # A long is helped by the high and hurt by the low; a short the other way round. Feeding
        # both ends of the bar through the same signed conversion settles it without branching.
        moves = (self._bps(high), self._bps(low))
        best, worst = max(moves), min(moves)
        if best > self.mfe_bps:
            self.mfe_bps = best
            self.bars_to_mfe = self.bars
        if worst < self.mae_bps:
            self.mae_bps = worst
            self.bars_to_mae = self.bars

    def close(self) -> Excursion:
        return Excursion(
            mfe_bps=self.mfe_bps,
            mae_bps=self.mae_bps,
            bars_to_mfe=self.bars_to_mfe,
            bars_to_mae=self.bars_to_mae,
            bars=self.bars,
        )


def excursion_of(
    entry_px: Decimal,
    direction: Direction,
    highs_lows: Sequence[tuple[Decimal | None, Decimal | None]],
) -> Excursion:
    """The excursion of a finished trade from the bars it was held through."""
    tracker = ExcursionTracker(entry_px=entry_px, direction=direction)
    for high, low in highs_lows:
        tracker.see(high, low)
    return tracker.close()


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The excursions of many round trips, as the figures a risk manager reads.

    `stop_distance` and `take_distance` are left to the caller: what matters to an operator is how
    close the adverse excursions came to the stop that was set, and that stop lives in the config.
    """
    mfe = [Decimal(str(r["mfe_bps"])) for r in rows if r.get("mfe_bps") is not None]
    mae = [Decimal(str(r["mae_bps"])) for r in rows if r.get("mae_bps") is not None]
    net = [Decimal(str(r["net_bps"])) for r in rows if r.get("net_bps") is not None]
    if not mfe or not mae:
        return {"round_trips": len(rows), "measured": 0}
    out: dict[str, Any] = {
        "round_trips": len(rows),
        "measured": len(mfe),
        "mfe_median_bps": str(_median(mfe).quantize(QUANTUM)),
        "mfe_max_bps": str(max(mfe).quantize(QUANTUM)),
        "mae_median_bps": str(_median(mae).quantize(QUANTUM)),
        "mae_worst_bps": str(min(mae).quantize(QUANTUM)),
    }
    if net and len(net) == len(mfe):
        kept = [n for n, f in zip(net, mfe, strict=True) if f > 0]
        given_back = [f - n for n, f in zip(net, mfe, strict=True) if f > 0]
        if given_back:
            out["given_back_median_bps"] = str(_median(given_back).quantize(QUANTUM))
            out["kept_of_best_median"] = str(
                (_median(kept) / _median(mfe)).quantize(Decimal("0.001"))
                if _median(mfe) > 0
                else Decimal(0)
            )
    return out


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
