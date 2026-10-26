"""The `forecast` row: the expected-value decision, fitted walk-forward on the archive it is on.

At each decision the row needs a model fitted on outcomes that had finished by then. Every
`refit_days` it takes the decisions of the last `fit_days` whose ideas had already settled, labels
each side 1 when its take was reached first and 0 otherwise (a stop, or time running out), fits one
logistic model per side on the older 70 % of them and a calibrator on the newer 30 %, and keeps
both until the next refit. An idea is the side whose expected value, net of the round trip,
clears the margin; with no model yet, or a feature absent, there is no idea.

The exits are the baselines' (stop 1.5 x ATR, take 3 x ATR, 24 bars) so the row differs from
momentum only in how it chooses, and the features are a fixed, versioned list that every archive
of flow bars carries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.baselines import HOLD_BARS, STOP_ATR, TAKE_ATR
from trading_research_agents.evaluation.history import MarketHistory, Views, label_of
from trading_research_agents.evaluation.policy import DecisionView, TradeIdea
from trading_research_agents.evaluation.settle import Costs, Funding, Settlement, settle
from trading_research_agents.forecast.calibration import Calibrator, choose_calibrator
from trading_research_agents.forecast.decision import best_side, expected_bps
from trading_research_agents.forecast.logistic import ForecastError, Logistic
from trading_research_agents.market_data.days import DAY_NS

FORECAST_VERSION = "1"
FEATURES = (
    "ret_bps_1",
    "ret_bps_4",
    "ret_bps_24",
    "mom_z_4",
    "mom_z_24",
    "vol_bps",
    "range_vol_bps",
    "zscore",
    "vwap_gap_bps",
    "position_in_range",
    "trend_strength",
    "variance_ratio_4",
    "taker_imbalance_4",
    "taker_imbalance_24",
    "delta_z_24",
    "trade_intensity_z_24",
)
SIDES = (Direction.LONG, Direction.SHORT)
FIT_SHARE = 0.7
# A feature absent from more decisions than this is left out of the fit rather than costing rows.
PRESENT = 0.95


@dataclass(frozen=True)
class FitContext:
    """What the row may read to fit itself: the views, the tapes, the costs and the clock."""

    views: Views
    histories: Mapping[str, MarketHistory]
    instruments: Sequence[str]
    costs: Costs
    funding: Mapping[str, Funding]
    interval_ns: int
    first_ns: int


@dataclass(frozen=True)
class Fitted:
    models: Mapping[Direction, tuple[Logistic, Calibrator]]
    # The features the models read: those present in nearly every row they were fitted on.
    columns: tuple[str, ...]
    samples: int


@dataclass
class _Labels:
    # (instrument, decided_ns, side) -> 1.0, 0.0, or None for an idea the archive cannot settle.
    known: dict[tuple[str, int, Direction], tuple[float, int] | None] = field(default_factory=dict)


class ForecastPolicy:
    name = "forecast"
    knowledge_cutoff: date | None = None

    def __init__(
        self,
        fit: FitContext,
        *,
        fit_days: int = 60,
        refit_days: int = 7,
        min_samples: int = 200,
        margin_bps: float = 0.0,
    ) -> None:
        self.fit = fit
        self.fit_ns = fit_days * DAY_NS
        self.refit_ns = refit_days * DAY_NS
        self.min_samples = min_samples
        self.margin_bps = margin_bps
        self.cost_bps = _round_trip_bps(fit.costs)
        self._labels = _Labels()
        self._fitted: Fitted | None = None
        self._fitted_at: int | None = None

    def decide(self, view: DecisionView) -> TradeIdea | None:
        values = self._values(view)
        if values is None:
            return None
        values_by_side, atr = values
        long_bps, short_bps = (
            expected_bps(
                values_by_side[side],
                take_bps=float(TAKE_ATR) * atr,
                stop_bps=float(STOP_ATR) * atr,
                cost_bps=self.cost_bps,
            )
            for side in SIDES
        )
        chosen = best_side(long_bps, short_bps, margin_bps=self.margin_bps)
        if chosen is None:
            return None
        side = Direction.LONG if chosen[0] == "long" else Direction.SHORT
        unit = Decimal(f"{atr:.4f}")
        return TradeIdea(
            side=side,
            playbook="forecast",
            stop_bps=STOP_ATR * unit,
            take_bps=TAKE_ATR * unit,
            max_hold_bars=HOLD_BARS,
            reason=f"p {values_by_side[side]:.3f}, expected {chosen[1]:+.2f} bps net",
        )

    def score(self, view: DecisionView) -> float | None:
        values = self._values(view)
        if values is None:
            return None
        return values[0][Direction.LONG] - values[0][Direction.SHORT]

    def _values(self, view: DecisionView) -> tuple[dict[Direction, float], float] | None:
        fitted = self._model(view.at_ns)
        if fitted is None:
            return None
        row = _row(view.features[view.primary], fitted.columns)
        atr = view.feature("atr_bps")
        if row is None or not isinstance(atr, float) or atr <= 0:
            return None
        x = np.array([row])
        out = {
            side: float(cal.map(model.predict(x))[0])
            for side, (model, cal) in fitted.models.items()
        }
        return out, atr

    def _model(self, at_ns: int) -> Fitted | None:
        # Until there is a model, look again each day rather than wait a whole refit period.
        every = self.refit_ns if self._fitted is not None else DAY_NS
        if self._fitted_at is None or at_ns - self._fitted_at >= every:
            self._fitted_at = at_ns
            self._fitted = self._refit(at_ns)
        return self._fitted

    def _refit(self, at_ns: int) -> Fitted | None:
        models: dict[Direction, tuple[Logistic, Calibrator]] = {}
        counted = 0
        columns = self.columns_before(at_ns)
        if not columns:
            return None
        for side in SIDES:
            rows, labels = self.samples_before(at_ns, side, columns)
            if len(rows) < self.min_samples:
                return None
            cut = int(len(rows) * FIT_SHARE)
            try:
                model = Logistic.fit(np.array(rows[:cut]), np.array(labels[:cut]))
                scores = model.predict(np.array(rows[cut:]))
                models[side] = (model, choose_calibrator(scores, np.array(labels[cut:])))
            except ForecastError:
                return None
            counted += len(rows)
        return Fitted(models, columns, counted)

    def _decisions(self, at_ns: int) -> range:
        step = self.fit.interval_ns
        since = max(self.fit.first_ns, at_ns - self.fit_ns)
        return range(since + (-since) % step, at_ns, step)

    def settled_before(self, at_ns: int) -> list[tuple[str, int]]:
        """The decisions whose idea, on either side, had settled by `at_ns`, oldest first.

        An idea settles only on minutes the archive saw, so a decision before the archive began
        or inside a gap is not here, and cannot choose the features or teach the model.
        """
        out: list[tuple[str, int]] = []
        for decided in self._decisions(at_ns):
            for instrument in self.fit.instruments:
                labels = (self._label(instrument, decided, side) for side in SIDES)
                if any(label is not None and label[1] <= at_ns for label in labels):
                    out.append((instrument, decided))
        return out

    def columns_before(self, at_ns: int) -> tuple[str, ...]:
        """The features present in at least `PRESENT` of the settled decisions."""
        seen = [self._vector(i, d) for i, d in self.settled_before(at_ns)]
        if not seen:
            return ()
        return tuple(
            name
            for name in FEATURES
            if sum(1 for v in seen if _number(v.get(name)) is not None) >= PRESENT * len(seen)
        )

    def samples_before(
        self, at_ns: int, side: Direction, columns: Sequence[str] = FEATURES
    ) -> tuple[list[list[float]], list[float]]:
        """Rows and labels of the ideas on `side` that had settled by `at_ns`, oldest first."""
        rows: list[list[float]] = []
        labels: list[float] = []
        for instrument, decided in self.settled_before(at_ns):
            label = self._label(instrument, decided, side)
            if label is None or label[1] > at_ns:
                continue
            row = _row(self._vector(instrument, decided), columns)
            if row is not None:
                rows.append(row)
                labels.append(label[0])
        return rows, labels

    def _vector(self, instrument: str, decided: int) -> Mapping[str, object]:
        return self.fit.views.view(instrument, decided).features[_primary(self.fit)]

    def _label(self, instrument: str, decided: int, side: Direction) -> tuple[float, int] | None:
        key = (instrument, decided, side)
        if key in self._labels.known:
            return self._labels.known[key]
        found: tuple[float, int] | None = None
        atr = self.fit.views.view(instrument, decided).feature("atr_bps")
        if isinstance(atr, float) and atr > 0:
            unit = Decimal(f"{atr:.4f}")
            idea = TradeIdea(side, "forecast", STOP_ATR * unit, TAKE_ATR * unit, HOLD_BARS, "label")
            outcome = settle(
                row="forecast",
                instrument=instrument,
                idea=idea,
                decided_ns=decided,
                hold_ns=HOLD_BARS * self.fit.interval_ns,
                tape=self.fit.histories[instrument].tape,
                costs=self.fit.costs,
                funding=self.fit.funding[instrument],
            )
            if isinstance(outcome, Settlement):
                found = (1.0 if outcome.ended == "take" else 0.0, outcome.exit_ns)
        self._labels.known[key] = found
        return found


def _primary(fit: FitContext) -> str:
    return label_of(fit.interval_ns)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _row(vector: Mapping[str, object], columns: Sequence[str]) -> list[float] | None:
    out: list[float] = []
    for name in columns:
        value = _number(vector.get(name))
        if value is None:
            return None
        out.append(value)
    return out


def _round_trip_bps(costs: Costs) -> float:
    channel = costs.channel
    total = channel.entry_fee_bps + channel.exit_fee_bps + costs.spread_bps + costs.slippage_bps
    return float(total)


__all__ = ["FEATURES", "FORECAST_VERSION", "FitContext", "ForecastPolicy"]
