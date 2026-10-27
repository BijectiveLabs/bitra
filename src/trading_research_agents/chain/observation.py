"""Layer 1: market observation. Does the phenomenon recur, before anyone explains it?

Layer 0 asks whether a signal, if it exists, is large enough to pay for. Layer 1 asks whether
it exists: an event the operator can mark on closed bars, and a response measured strictly
after it. This layer counts; it does not conclude. How many times the event fired, in how many
slices of the sample, and what the response looked like. No profit and loss, no statistics
(that is layer 5, behind a pre-registration), and no threshold of its own: the minimum count,
the number of slices and the event rule are all declared by the operator in `observation.toml`.

An event is decided at the close of bar t from bars t-window+1..t; the response is the close H
bars later against the close at t. Nothing looks forward. A horizon bar with no trades leaves
the response unmeasured, which is counted, never filled in.
"""

from __future__ import annotations

import json
import math
import statistics
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from trading_research_agents.data.candles import CandleError, candles_of
from trading_research_agents.data.dated_text import read_events
from trading_research_agents.data.schema import Candle, Trade
from trading_research_agents.ledger.entry import LedgerEntry
from trading_research_agents.ledger.vocabulary import Axis, Transferability, Verdict

LAYER = 1
NS_PER_S = 1_000_000_000
FEATURES = ("return_bps", "abs_return_bps", "zscore", "volume_ratio", "range_ratio", "hour_utc")
COMPARISONS = ("<", "<=", ">", ">=", "==", "between")


class ObservationError(ValueError):
    """The declaration cannot be read, or the data cannot carry it."""


class Recurrence(StrEnum):
    RECURS = "recurs"
    TOO_FEW = "does not recur: too few events"
    EPISODIC = "does not recur: episodic"


@dataclass(frozen=True)
class EventRule:
    feature: str | None
    window_bars: int
    comparison: str
    threshold: tuple[float, float] | float
    calendar: Path | None

    def matches(self, value: float) -> bool:
        t = self.threshold
        if isinstance(t, tuple):
            return t[0] <= value <= t[1]
        return {
            "<": value < t,
            "<=": value <= t,
            ">": value > t,
            ">=": value >= t,
            "==": value == t,
        }[self.comparison]

    def record(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "window_bars": self.window_bars,
            "comparison": self.comparison,
            "threshold": list(self.threshold)
            if isinstance(self.threshold, tuple)
            else self.threshold,
            "calendar": None if self.calendar is None else str(self.calendar),
        }


@dataclass(frozen=True)
class Observation:
    subject: str
    interval_s: int
    horizon_bars: int
    min_events: int
    subperiods: int
    min_subperiods_with_events: int
    event: EventRule
    claimed_magnitude_bps: Decimal | None = None

    def record(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "interval_s": self.interval_s,
            "horizon_bars": self.horizon_bars,
            "min_events": self.min_events,
            "subperiods": self.subperiods,
            "min_subperiods_with_events": self.min_subperiods_with_events,
            "claimed_magnitude_bps": (
                None if self.claimed_magnitude_bps is None else str(self.claimed_magnitude_bps)
            ),
            "event": self.event.record(),
        }


def _positive_int(raw: dict[str, Any], key: str, *, table: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ObservationError(f"[{table}].{key} is a positive integer")
    return value


def load_observation(path: Path) -> Observation:
    """Read `observation.toml`; every threshold is the operator's, none is supplied here."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ObservationError(f"{path}: {exc}") from exc
    obs = raw.get("observation")
    ev = raw.get("event")
    if not isinstance(obs, dict) or not isinstance(ev, dict):
        raise ObservationError(f"{path}: [observation] and [event] tables are required")
    subject = str(obs.get("subject", "")).strip()
    if not subject:
        raise ObservationError("[observation].subject names what is being observed")
    subperiods = _positive_int(obs, "subperiods", table="observation")
    min_with = _positive_int(obs, "min_subperiods_with_events", table="observation")
    if min_with > subperiods:
        raise ObservationError("min_subperiods_with_events cannot exceed subperiods")
    claimed = obs.get("claimed_magnitude_bps")
    magnitude = None
    if claimed is not None:
        magnitude = Decimal(str(claimed))
        if magnitude <= 0:
            raise ObservationError("claimed_magnitude_bps is positive when given")

    calendar = ev.get("calendar")
    feature = ev.get("feature")
    if calendar is not None and feature is not None:
        raise ObservationError("[event] names a feature or a calendar, not both")
    if calendar is not None:
        rule = EventRule(None, 1, "", 0.0, (path.parent / str(calendar)).resolve())
    else:
        if feature not in FEATURES:
            raise ObservationError(f"[event].feature is one of {list(FEATURES)}")
        comparison = str(ev.get("comparison", ""))
        if comparison not in COMPARISONS:
            raise ObservationError(f"[event].comparison is one of {list(COMPARISONS)}")
        threshold = ev.get("threshold")
        parsed: tuple[float, float] | float
        if comparison == "between":
            if not isinstance(threshold, list) or len(threshold) != 2:
                raise ObservationError("between takes threshold = [lo, hi]")
            lo, hi = float(threshold[0]), float(threshold[1])
            if lo > hi:
                raise ObservationError("between: lo <= hi")
            parsed = (lo, hi)
        else:
            if isinstance(threshold, bool) or not isinstance(threshold, int | float):
                raise ObservationError("[event].threshold is a number")
            parsed = float(threshold)
        window = _positive_int(ev, "window_bars", table="event") if feature != "hour_utc" else 1
        if feature in ("zscore", "volume_ratio", "range_ratio") and window < 2:
            raise ObservationError(f"{feature} needs window_bars >= 2")
        rule = EventRule(str(feature), window, comparison, parsed, None)
    return Observation(
        subject=subject,
        interval_s=_positive_int(obs, "interval_s", table="observation"),
        horizon_bars=_positive_int(obs, "horizon_bars", table="observation"),
        min_events=_positive_int(obs, "min_events", table="observation"),
        subperiods=subperiods,
        min_subperiods_with_events=min_with,
        event=rule,
        claimed_magnitude_bps=magnitude,
    )


def feature_at(bars: Sequence[Candle], i: int, name: str, window: int) -> float | None:
    """The named feature at the close of `bars[i]`, from the `window` candles ending there."""
    if name == "hour_utc":
        return float(datetime.fromtimestamp(bars[i].start_ns / NS_PER_S, tz=UTC).hour)
    if name in ("return_bps", "abs_return_bps"):
        if i < 1 or bars[i - 1].close <= 0:
            return None
        r = (float(bars[i].close) / float(bars[i - 1].close) - 1) * 1e4
        return abs(r) if name == "abs_return_bps" else r
    lo = i - window + 1
    if lo < 0:
        return None
    w = bars[lo : i + 1]
    if name == "zscore":
        closes = [float(b.close) for b in w]
        mean = sum(closes) / len(closes)
        var = sum((x - mean) ** 2 for x in closes) / (len(closes) - 1)
        return None if var <= 0 else (closes[-1] - mean) / math.sqrt(var)
    if name == "volume_ratio":
        volumes = [float(b.volume) for b in w]
        avg = sum(volumes) / len(volumes)
        return None if avg <= 0 else volumes[-1] / avg
    if name == "range_ratio":
        ranges = [float(b.high) - float(b.low) for b in w]
        avg = sum(ranges) / len(ranges)
        return None if avg <= 0 else ranges[-1] / avg
    raise ObservationError(f"unknown feature {name!r}")


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


@dataclass(frozen=True)
class ObservationReport:
    observation: Observation
    dataset: str
    instrument: str
    first_bar_ns: int
    last_bar_ns: int
    bars: int
    events: int
    measured: int
    skipped: int
    events_per_subperiod: tuple[int, ...]
    response_bps: dict[str, float | None]
    curve_bps: tuple[dict[str, float | None], ...]
    recurrence: Recurrence
    fired: tuple[str, ...]

    @property
    def recurs(self) -> bool:
        return self.recurrence is Recurrence.RECURS

    def record(self) -> dict[str, Any]:
        return {
            "layer": LAYER,
            "observation": self.observation.record(),
            "dataset": self.dataset,
            "instrument": self.instrument,
            "range": {
                "first_bar": datetime.fromtimestamp(
                    self.first_bar_ns / NS_PER_S, tz=UTC
                ).isoformat(),
                "last_bar": datetime.fromtimestamp(self.last_bar_ns / NS_PER_S, tz=UTC).isoformat(),
            },
            "bars": self.bars,
            "events": self.events,
            "measured": self.measured,
            "skipped": self.skipped,
            "events_per_subperiod": list(self.events_per_subperiod),
            "response_bps": self.response_bps,
            "curve_bps": list(self.curve_bps),
            "recurrence": self.recurrence.value,
            "fired": list(self.fired),
            "magnitude": self._magnitude_note(),
        }

    def _magnitude_note(self) -> str | None:
        claimed = self.observation.claimed_magnitude_bps
        median_abs = self.response_bps.get("median_abs")
        if claimed is None or median_abs is None:
            return None
        return (
            f"median |response| {median_abs:.2f} bps against the claimed {claimed} bps; "
            "the cost decision stays with layer 0"
        )

    def to_entry(
        self, *, entry_id: str, recorded_at: datetime, evidence: tuple[str, ...] = ()
    ) -> LedgerEntry:
        """The row a death here leaves. A phenomenon that recurs has nothing to record yet."""
        counts = ", ".join(str(n) for n in self.events_per_subperiod)
        if self.recurrence is Recurrence.TOO_FEW:
            return LedgerEntry(
                entry_id=entry_id,
                subject=self.observation.subject,
                verdict=Verdict.NOT_PROVEN,
                recorded_at=recorded_at,
                layer=LAYER,
                blocking_axis=Axis.POWER,
                evidence=evidence,
                note=(
                    f"{self.measured} measured events of {self.observation.min_events} required "
                    f"over {self.bars} bars; a longer sample decides"
                ),
            )
        if self.recurrence is Recurrence.EPISODIC:
            present = sum(1 for n in self.events_per_subperiod if n)
            return LedgerEntry(
                entry_id=entry_id,
                subject=self.observation.subject,
                verdict=Verdict.FALSIFIED,
                recorded_at=recorded_at,
                layer=LAYER,
                axis_of_death=(Axis.CONDITION,),
                transferability=Transferability.UNIVERSAL,
                evidence=evidence,
                note=(
                    f"{self.events} events in {present} of {self.observation.subperiods} "
                    f"subperiods ({counts}); {self.observation.min_subperiods_with_events} required"
                ),
            )
        raise ValueError("a phenomenon that recurs has nothing to record yet")


def observe(
    observation: Observation, trades: Iterable[Trade], *, dataset: str = ""
) -> ObservationReport:
    """Mark the events, measure the responses, count per subperiod, and say whether it recurs."""
    obs = observation
    seen: list[str] = []

    def named(rows: Iterable[Trade]) -> Iterable[Trade]:
        for t in rows:
            if not seen:
                seen.append(t.instrument)
            yield t

    try:
        bars = candles_of(named(trades), obs.interval_s * NS_PER_S)
    except CandleError as exc:
        raise ObservationError(str(exc)) from exc
    if len(bars) < 2:
        raise ObservationError("fewer than two bars with trades; nothing to observe")
    instrument = seen[0] if seen else ""
    by_start = {b.start_ns: i for i, b in enumerate(bars)}
    interval_ns = obs.interval_s * NS_PER_S
    marks: list[int] = []
    skipped = 0
    if obs.event.calendar is not None:
        # An external event is known at `when`. The mark is the last bar that closed at or
        # before it, so the close the response starts from was known before the event; if that
        # bar has no trades the event cannot be measured and is skipped, not moved.
        for e in read_events(obs.event.calendar):
            start = ((e.when_ns - interval_ns) // interval_ns) * interval_ns
            i = by_start.get(start)
            if i is None:
                skipped += 1
            elif not marks or marks[-1] != i:
                marks.append(i)
    else:
        name, window = str(obs.event.feature), obs.event.window_bars
        for i in range(len(bars)):
            value = feature_at(bars, i, name, window)
            if value is not None and obs.event.matches(value):
                marks.append(i)

    # The response for each event and each bar of the horizon, by time, not by bar count.
    responses: list[float] = []
    curve: list[list[float]] = [[] for _ in range(obs.horizon_bars)]
    for i in marks:
        t0 = bars[i]
        for k in range(1, obs.horizon_bars + 1):
            j = by_start.get(t0.start_ns + k * interval_ns)
            if j is None:
                continue
            r = (float(bars[j].close) / float(t0.close) - 1) * 1e4
            curve[k - 1].append(r)
            if k == obs.horizon_bars:
                responses.append(r)

    first, last = bars[0].start_ns, bars[-1].start_ns + interval_ns
    span = max(last - first, 1)
    per_slice = [0] * obs.subperiods
    for i in marks:
        slot = min(obs.subperiods - 1, (bars[i].start_ns - first) * obs.subperiods // span)
        per_slice[slot] += 1

    def stats(values: Sequence[float]) -> dict[str, float | None]:
        if not values:
            return {
                "n": 0,
                "mean": None,
                "median": None,
                "q25": None,
                "q75": None,
                "fraction_positive": None,
                "median_abs": None,
            }
        return {
            "n": len(values),
            "mean": round(statistics.fmean(values), 3),
            "median": round(statistics.median(values), 3),
            "q25": round(_quantile(values, 0.25), 3),
            "q75": round(_quantile(values, 0.75), 3),
            "fraction_positive": round(sum(1 for v in values if v > 0) / len(values), 3),
            "median_abs": round(statistics.median(abs(v) for v in values), 3),
        }

    fired: list[str] = []
    if len(responses) < obs.min_events:
        fired.append(f"measured events {len(responses)} < min_events {obs.min_events}")
        recurrence = Recurrence.TOO_FEW
    elif sum(1 for n in per_slice if n) < obs.min_subperiods_with_events:
        fired.append(
            f"subperiods with events {sum(1 for n in per_slice if n)} < "
            f"min_subperiods_with_events {obs.min_subperiods_with_events}"
        )
        recurrence = Recurrence.EPISODIC
    else:
        recurrence = Recurrence.RECURS
    return ObservationReport(
        observation=obs,
        dataset=dataset,
        instrument=instrument,
        first_bar_ns=first,
        last_bar_ns=last,
        bars=len(bars),
        events=len(marks),
        measured=len(responses),
        skipped=skipped,
        events_per_subperiod=tuple(per_slice),
        response_bps=stats(responses),
        curve_bps=tuple(
            {
                "bar": k + 1,
                "n": len(c),
                "median": None if not c else round(statistics.median(c), 3),
                "mean": None if not c else round(statistics.fmean(c), 3),
            }
            for k, c in enumerate(curve)
        ),
        recurrence=recurrence,
        fired=tuple(fired),
    )


def write_report(report: ObservationReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.record(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
