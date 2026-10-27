"""The `paths` row: each side read against the instrument's own candles drawn forward.

At each decision the row draws paths from the bars that had closed by then, reads how often each
side's take comes before its stop at the baselines' levels (stop 1.5 x ATR, take 3 x ATR, 24 bars),
and takes the side whose value over the paths, net of the round trip, clears the margin. Nothing
is fitted: the row is the path reading alone, so the arena says whether it chooses better than the
baselines and the `forecast` row on the same tape with the same costs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.baselines import HOLD_BARS, STOP_ATR, TAKE_ATR
from trading_research_agents.evaluation.policy import DecisionView, TradeIdea
from trading_research_agents.evaluation.settle import Costs
from trading_research_agents.forecast.decision import best_side
from trading_research_agents.forecast.paths import PathError, PathReading, PathSettings, read_paths
from trading_research_agents.market_data.bars import FlowBar

PATHS_VERSION = "1"
# Fewer paths than a live cycle draws: the arena reads every decision of every day.
ARENA_PATHS = 400
# Each side's reading at a decision, and the ATR its levels were set from.
_Read = tuple[dict[str, PathReading], float]


def _ohlc(bars: Sequence[FlowBar]) -> list[list[float]]:
    return [
        [float(b.open), float(b.high), float(b.low), float(b.close)]
        for b in bars
        if b.open is not None and b.high is not None and b.low is not None and b.close is not None
    ]


class PathsPolicy:
    name = "paths"
    knowledge_cutoff: date | None = None

    def __init__(self, costs: Costs, *, margin_bps: float = 0.0, paths: int = ARENA_PATHS) -> None:
        channel = costs.channel
        self.cost_bps = float(
            channel.entry_fee_bps + channel.exit_fee_bps + costs.spread_bps + costs.slippage_bps
        )
        self.margin_bps = margin_bps
        self.paths = paths
        # The arena asks for the idea and for the score at the same decision; one reading serves.
        self._last: tuple[tuple[str, int], _Read | None] | None = None

    def decide(self, view: DecisionView) -> TradeIdea | None:
        read = self._read(view)
        if read is None:
            return None
        readings, atr = read
        values = {side: r.expected_bps(self.cost_bps) for side, r in readings.items()}
        chosen = best_side(values["long"], values["short"], margin_bps=self.margin_bps)
        if chosen is None:
            return None
        side, value = chosen
        unit = Decimal(f"{atr:.4f}")
        taken = readings[side]
        return TradeIdea(
            side=Direction.LONG if side == "long" else Direction.SHORT,
            playbook="paths",
            stop_bps=STOP_ATR * unit,
            take_bps=TAKE_ATR * unit,
            max_hold_bars=HOLD_BARS,
            reason=(
                f"take first {taken.take_first:.3f}, stop first {taken.stop_first:.3f}, "
                f"expected {value:+.2f} bps net"
            ),
        )

    def score(self, view: DecisionView) -> float | None:
        read = self._read(view)
        if read is None:
            return None
        readings, _ = read
        return readings["long"].expected_bps(0.0) - readings["short"].expected_bps(0.0)

    def _read(self, view: DecisionView) -> _Read | None:
        key = (view.instrument, view.at_ns)
        if self._last is None or self._last[0] != key:
            self._last = (key, self._fresh(view))
        return self._last[1]

    def _fresh(self, view: DecisionView) -> _Read | None:
        atr = view.feature("atr_bps")
        if not isinstance(atr, float) or atr <= 0:
            return None
        settings = PathSettings(paths=self.paths, seed=view.at_ns % 2**32)
        try:
            readings = {
                side: read_paths(
                    _ohlc(view.bars),
                    side=side,
                    take_bps=float(TAKE_ATR) * atr,
                    stop_bps=float(STOP_ATR) * atr,
                    horizon_bars=HOLD_BARS,
                    settings=settings,
                )
                for side in ("long", "short")
            }
        except PathError:
            return None
        return readings, atr
