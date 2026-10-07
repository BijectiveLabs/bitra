"""External series from anywhere: macro prints, funding elsewhere, on-chain flows, sentiment.

A series is a list of (published_at, value) points declared by a column map, like trades are.
Two things make it usable in research rather than dangerous. First, alignment is as-of: the
value used at time t is the last one *published* before t, where the publication time is the
observation time plus the lag the operator declared, so a monthly print released a week after
month end is not known during that week. Second, every measurement carries its sample size and
window, because a correlation without n is not a figure. Thresholds are the operator's own
declaration, labelled as such; nothing here knows what a "bullish" level is.
"""

from __future__ import annotations

import csv
import json
import math
import tomllib
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

NS_PER_S = 1_000_000_000
DEFAULT_MAX_AGE = timedelta(days=4)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def moment(ns: int) -> datetime:
    """Epoch nanoseconds as a UTC time. fromtimestamp refuses dates before 1970 on Windows, and
    FRED serves yields back to 1962."""
    return EPOCH + timedelta(microseconds=ns // 1000)


TIME_UNITS = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}


class SeriesError(ValueError):
    """The series file or its map cannot be used as it stands."""


@dataclass(frozen=True)
class Threshold:
    """What the operator declared, not what the market says: above or below which a state holds."""

    bullish_below: Decimal | None = None
    bullish_above: Decimal | None = None
    bearish_below: Decimal | None = None
    bearish_above: Decimal | None = None

    def state(self, value: Decimal) -> str:
        if self.bullish_below is not None and value < self.bullish_below:
            return "bullish"
        if self.bullish_above is not None and value > self.bullish_above:
            return "bullish"
        if self.bearish_below is not None and value < self.bearish_below:
            return "bearish"
        if self.bearish_above is not None and value > self.bearish_above:
            return "bearish"
        return "neutral"

    @property
    def band(self) -> Decimal | None:
        """The width of the neutral zone, when both a bullish and a bearish bound are declared."""
        bull = self.bullish_below if self.bullish_below is not None else self.bullish_above
        bear = self.bearish_above if self.bearish_above is not None else self.bearish_below
        if bull is None or bear is None or bull == bear:
            return None
        return abs(bear - bull)

    @property
    def graded(self) -> bool:
        return self.band is not None

    def strength(self, value: Decimal) -> Decimal:
        """How far past the operator's line the value sits, signed, between -1 and 1.

        Measured in widths of the neutral band the operator drew: a yield 0.03 under a bullish
        line with a 0.20 band is +0.15, not the same "bullish" as a yield a full band under it.
        A threshold with only one side declared has no band to measure against, so it stays the
        plain +1, -1 or 0 it always was, and the record says it is not graded.
        """
        band = self.band
        # The same order `state` reads the lines in, so the line crossed is the one that decided.
        for bound, under, sign in (
            (self.bullish_below, True, Decimal(1)),
            (self.bullish_above, False, Decimal(1)),
            (self.bearish_below, True, Decimal(-1)),
            (self.bearish_above, False, Decimal(-1)),
        ):
            if bound is not None and (value < bound if under else value > bound):
                if band is None:
                    return sign
                past = min(Decimal(1), abs(value - bound) / band)
                return sign * past.quantize(Decimal("0.001"))
        return Decimal(0)

    def describe(self) -> str:
        """The rule in words, so a reader sees which side of the series is good for the trade."""
        said = [
            f"{state} {side} {value}"
            for state, side, value in (
                ("bullish", "below", self.bullish_below),
                ("bullish", "above", self.bullish_above),
                ("bearish", "below", self.bearish_below),
                ("bearish", "above", self.bearish_above),
            )
            if value is not None
        ]
        return ", ".join(said) + ", neutral between" if said else ""

    @property
    def declared(self) -> bool:
        return any(
            v is not None
            for v in (
                self.bullish_below,
                self.bullish_above,
                self.bearish_below,
                self.bearish_above,
            )
        )


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    file: str
    ts: str
    ts_unit: str
    value: str
    unit: str
    publish_lag: timedelta
    source: str
    threshold: Threshold
    has_header: bool = True
    delimiter: str = ","
    # A value older than this, measured from the moment it describes, is reported and not counted.
    max_age: timedelta = DEFAULT_MAX_AGE
    # Where `tra sources pull` and `tra run` refresh the file from, and how often. None: the
    # operator keeps the file up to date by hand, and max_age is what keeps them honest.
    fetch: Mapping[str, Any] | None = None
    refresh: timedelta = timedelta(hours=1)
    # The asset this series is about, such as BTC for Bitcoin's exchange flows. Empty means it is
    # about the market as a whole and counts the same for every instrument. For an instrument of
    # another asset it still counts, scaled by `elsewhere`: Bitcoin's chain says something about
    # a Zcash trade, but less than it says about Bitcoin's own.
    asset: str = ""
    elsewhere: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SeriesError("a series has a name")
        if not Decimal(0) <= self.elsewhere <= Decimal(1):
            raise SeriesError(f"{self.name}: elsewhere is between 0 and 1")
        if self.ts_unit not in TIME_UNITS and self.ts_unit != "iso8601":
            raise SeriesError(f"{self.name}: ts_unit is s, ms, us, ns or iso8601")
        if self.publish_lag < timedelta(0):
            raise SeriesError(f"{self.name}: publish_lag cannot be negative")
        if self.max_age <= timedelta(0) or self.refresh <= timedelta(0):
            raise SeriesError(f"{self.name}: max_age and refresh are positive")


@dataclass(frozen=True)
class Point:
    observed_ns: int
    published_ns: int
    value: Decimal


@dataclass(frozen=True)
class Series:
    spec: SeriesSpec
    points: tuple[Point, ...]  # sorted by published_ns

    @property
    def name(self) -> str:
        return self.spec.name

    def as_of(self, ts_ns: int) -> Point | None:
        """The last point published strictly before ts_ns. None when nothing was known yet."""
        keys = [p.published_ns for p in self.points]
        i = bisect_right(keys, ts_ns - 1)
        return self.points[i - 1] if i > 0 else None

    def latest(self) -> Point | None:
        return self.points[-1] if self.points else None


@dataclass(frozen=True)
class Measurement:
    """A number with the two things that make it one: how many samples and over what window."""

    value: float | None
    n: int
    window: str
    note: str = ""

    def record(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else round(self.value, 4),
            "n": self.n,
            "window": self.window,
            "note": self.note,
        }


# ---------------------------------------------------------------- loading


def _duration(text: str, key: str = "publish_lag") -> timedelta:
    text = text.strip()
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    if not text or text[-1] not in units or not text[:-1].isdigit():
        raise SeriesError(f"{key} {text!r} is a number followed by s, m, h or d")
    return timedelta(**{units[text[-1]]: int(text[:-1])})


def _threshold(raw: dict[str, Any], name: str) -> Threshold:
    values: dict[str, Decimal | None] = {}
    for key in ("bullish_below", "bullish_above", "bearish_below", "bearish_above"):
        v = raw.get(key)
        if v is None:
            values[key] = None
            continue
        try:
            values[key] = Decimal(str(v))
        except InvalidOperation:
            raise SeriesError(f"{name}: threshold {key} is not a number: {v!r}") from None
    return Threshold(**values)


def load_series_map(path: Path) -> tuple[SeriesSpec, ...]:
    """`series.toml`: one `[[series]]` per file the operator wants a role to see."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SeriesError(f"{path}: {exc}") from exc
    entries = raw.get("series")
    if not isinstance(entries, list) or not entries:
        raise SeriesError(f"{path}: needs at least one [[series]] table")
    specs = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SeriesError(f"{path}: [[series]] entries are tables")
        name = str(entry.get("name", ""))
        try:
            specs.append(
                SeriesSpec(
                    name=name,
                    file=str(entry.get("file", "")),
                    ts=str(entry.get("ts", "ts")),
                    ts_unit=str(entry.get("ts_unit", "iso8601")),
                    value=str(entry.get("value", "value")),
                    unit=str(entry.get("unit", "")),
                    publish_lag=_duration(str(entry.get("publish_lag", "0s"))),
                    source=str(entry.get("source", "")),
                    threshold=_threshold(entry.get("threshold") or {}, name),
                    has_header=bool(entry.get("has_header", True)),
                    delimiter=str(entry.get("delimiter", ",")),
                    max_age=(
                        _duration(str(entry["max_age"]), "max_age")
                        if "max_age" in entry
                        else DEFAULT_MAX_AGE
                    ),
                    fetch=_fetch(entry.get("fetch"), name),
                    refresh=_duration(str(entry.get("refresh", "1h")), "refresh"),
                    asset=str(entry.get("asset", "")).strip().upper(),
                    elsewhere=Decimal(str(entry.get("elsewhere", "0.5"))),
                )
            )
        except (TypeError, ValueError) as exc:
            raise SeriesError(f"{path}: series {name!r}: {exc}") from exc
    if not all(s.file for s in specs):
        raise SeriesError(f"{path}: every series names its file")
    return tuple(specs)


def _fetch(raw: Any, name: str) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not str(raw.get("from", "")).strip():
        raise SeriesError(
            f'{name}: fetch is a table naming where from, such as {{ from = "fred" }}'
        )
    return dict(raw)


def _ts_ns(text: str, unit: str) -> int:
    if unit == "iso8601":
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # A bare date is how most public series publish: one value for the day. It is read as
            # UTC midnight, and the publication lag the operator declared is what moves it to when
            # the figure was actually knowable. A date with a time but no zone is a different
            # thing: that one is genuinely ambiguous and stays refused.
            if len(text.strip()) == 10:
                return int(parsed.replace(tzinfo=UTC).timestamp() * NS_PER_S)
            raise ValueError(f"time {text!r} has a time of day but no timezone")
        return int(parsed.astimezone(UTC).timestamp() * NS_PER_S)
    return int(Decimal(text) * TIME_UNITS[unit])


def read_series(spec: SeriesSpec, base: Path) -> Series:
    """The file the spec names, as points sorted by publication time. A bad row stops the file."""
    path = (base / spec.file) if not Path(spec.file).is_absolute() else Path(spec.file)
    if not path.is_file():
        raise SeriesError(f"{spec.name}: {path} does not exist")
    text = path.read_text(encoding="utf-8")
    rows: list[dict[str, str]]
    if path.suffix.lower() == ".json":
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SeriesError(f"{spec.name}: not JSON: {exc}") from exc
        if not isinstance(loaded, list):
            raise SeriesError(f"{spec.name}: a JSON series is a list of objects")
        rows = [dict(r) for r in loaded]
    else:
        reader = csv.DictReader(text.splitlines(), delimiter=spec.delimiter)
        rows = [dict(r) for r in reader]
    points: list[Point] = []
    lag_ns = int(spec.publish_lag.total_seconds() * NS_PER_S)
    for number, row in enumerate(rows, start=2 if spec.has_header else 1):
        if spec.ts not in row or spec.value not in row:
            raise SeriesError(f"{spec.name}: row {number} lacks {spec.ts!r} or {spec.value!r}")
        try:
            observed = _ts_ns(str(row[spec.ts]).strip(), spec.ts_unit)
            value = Decimal(str(row[spec.value]).strip())
        except (ValueError, InvalidOperation, ArithmeticError) as exc:
            raise SeriesError(f"{spec.name}: row {number}: {exc}") from exc
        if not value.is_finite():
            raise SeriesError(f"{spec.name}: row {number}: value is not finite")
        points.append(Point(observed, observed + lag_ns, value))
    points.sort(key=lambda p: p.published_ns)
    return Series(spec, tuple(points))


def load_all(path: Path) -> tuple[Series, ...]:
    base = path.resolve().parent
    return tuple(read_series(spec, base) for spec in load_series_map(path))


def load_readable(path: Path) -> tuple[tuple[Series, ...], list[str]]:
    """Every series in a declaration that can be read, and why each other one cannot.

    One file that is missing or broken - a fetched series not pulled yet, a bad export - leaves
    its neighbours in the same declaration readable. The declaration itself failing is still an
    error for the caller.
    """
    base = path.resolve().parent
    found: list[Series] = []
    problems: list[str] = []
    for spec in load_series_map(path):
        try:
            found.append(read_series(spec, base))
        except SeriesError as exc:
            problems.append(str(exc))
    return tuple(found), problems


# ---------------------------------------------------------------- measuring


def align_asof(series: Series, times_ns: Sequence[int]) -> list[Decimal | None]:
    """For each time, the value known then. Never the value observed later."""
    return [None if (p := series.as_of(t)) is None else p.value for t in times_ns]


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def correlation(
    series: Series, times_ns: Sequence[int], returns: Sequence[float], *, window: str
) -> Measurement:
    """Pearson between the series' known value (as of each time) and the return at that time."""
    known = align_asof(series, times_ns)
    pairs = [(float(v), r) for v, r in zip(known, returns, strict=True) if v is not None]
    if len(pairs) < 3:
        return Measurement(None, len(pairs), window, "fewer than three aligned samples")
    if len({v for v in known if v is not None}) < 3:
        # A daily print held flat across a window of minutes is one number, not a sample; its float
        # mean is what made a constant series read as "correlation 0.0 on 60" (seen 2026-10-05).
        return Measurement(
            None,
            len(pairs),
            window,
            "slower than the bars: fewer than three values in the window",
        )
    xs, ys = zip(*pairs, strict=True)
    return Measurement(_pearson(xs, ys), len(pairs), window)


def lead_lag(
    series: Series,
    times_ns: Sequence[int],
    returns: Sequence[float],
    *,
    max_lag: int,
    window: str,
) -> list[dict[str, Any]]:
    """Correlation of the series' change with returns shifted by k steps, k in [-max_lag, max_lag].

    Positive k: the series moves first. The series is differenced so that a slow level does not
    pass for a signal, and it is aligned as-of at every step.
    """
    known = align_asof(series, times_ns)
    changes: list[float | None] = [None]
    for a, b in pairwise(known):
        changes.append(None if a is None or b is None else float(b - a))
    out: list[dict[str, Any]] = []
    for k in range(-max_lag, max_lag + 1):
        pairs = []
        for i, c in enumerate(changes):
            j = i + k
            if c is None or j < 0 or j >= len(returns):
                continue
            pairs.append((c, returns[j]))
        if len(pairs) < 3:
            out.append({"lag": k, "value": None, "n": len(pairs), "window": window})
            continue
        xs, ys = zip(*pairs, strict=True)
        r = _pearson(xs, ys)
        out.append(
            {
                "lag": k,
                "value": None if r is None else round(r, 4),
                "n": len(pairs),
                "window": window,
            }
        )
    return out


def threshold_state(series: Series, at_ns: int) -> dict[str, Any]:
    """The declared state at a time, from the value known then. Labelled as a declaration."""
    point = series.as_of(at_ns)
    spec = series.spec
    if point is None:
        return {"series": spec.name, "state": "unknown", "value": None, "known_since": None}
    age = timedelta(seconds=(at_ns - point.observed_ns) / NS_PER_S)
    origin = f"fetched from {spec.fetch['from']}" if spec.fetch else "a file kept by hand"
    observed = moment(point.observed_ns).isoformat()
    if age > spec.max_age:
        # An old reading counted as today's is a made-up reading. It is shown, so the operator
        # sees the file has gone stale, and it is never a state anyone weighs.
        return {
            "series": spec.name,
            "unit": spec.unit,
            "value": str(point.value),
            "observed_at": observed,
            "origin": origin,
            "age_h": round(age.total_seconds() / 3600, 1),
            "max_age": _span(spec.max_age),
            "state": "stale",
            "stale": True,
        }
    state = spec.threshold.state(point.value) if spec.threshold.declared else "undeclared"
    return {
        "series": spec.name,
        "unit": spec.unit,
        "value": str(point.value),
        "observed_at": observed,
        "known_since": moment(point.published_ns).isoformat(),
        "origin": origin,
        "age_h": round(age.total_seconds() / 3600, 1),
        "state": state,
        "basis": "operator-declared threshold"
        if spec.threshold.declared
        else "no threshold declared",
        # Seen live on 2026-10-04: a model read "bullish" on a yield series as yields rising, and
        # called it risk-off. The state is the operator's call for the traded instrument, and the
        # rule beside it shows which side of the series that call sits on.
        **(
            {
                "state_for": "the traded instrument, as the operator declared it",
                "threshold": spec.threshold.describe(),
                # How far past the line, in widths of the neutral band: +0.15 is barely bullish,
                # +1.0 is a full band beyond it. This is the figure conviction weighs.
                "strength": str(spec.threshold.strength(point.value)),
                "graded": spec.threshold.graded,
            }
            if spec.threshold.declared
            else {}
        ),
    }


def _span(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def zscore(series: Series, at_ns: int, *, window_points: int) -> Measurement:
    """The known value against the last `window_points` known values before it."""
    known = [p for p in series.points if p.published_ns < at_ns]
    if len(known) < 3:
        return Measurement(None, len(known), f"{window_points} points", "fewer than three points")
    tail = known[-window_points:]
    values = [float(p.value) for p in tail]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return Measurement(None, len(tail), f"{window_points} points", "no variance")
    return Measurement((values[-1] - mean) / math.sqrt(var), len(tail), f"{window_points} points")


def net_flow(series: Series, at_ns: int, *, window: timedelta) -> Measurement:
    """Sum of the known values whose observation falls inside the window ending at at_ns."""
    start = at_ns - int(window.total_seconds() * NS_PER_S)
    inside = [p for p in series.points if start <= p.observed_ns < at_ns and p.published_ns < at_ns]
    if not inside:
        return Measurement(None, 0, str(window), "no points known in the window")
    return Measurement(float(sum(p.value for p in inside)), len(inside), str(window))


# ---------------------------------------------------------------- events


@dataclass(frozen=True)
class Event:
    when_ns: int
    what: str
    source: str

    def record(self) -> dict[str, Any]:
        return {
            "when": moment(self.when_ns).isoformat(),
            "what": self.what,
            "source": self.source,
        }


def read_events(path: Path) -> tuple[Event, ...]:
    """A calendar: JSON list or CSV with `when` (ISO 8601 with timezone) and `what`."""
    if not path.is_file():
        raise SeriesError(f"events file {path} does not exist")
    text = path.read_text(encoding="utf-8")
    rows: Iterable[dict[str, Any]]
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise SeriesError("an events file is a list of objects")
        rows = loaded
    else:
        rows = csv.DictReader(text.splitlines())
    events = []
    for number, row in enumerate(rows, start=1):
        try:
            when = _ts_ns(str(row["when"]).strip(), "iso8601")
        except (KeyError, ValueError) as exc:
            raise SeriesError(f"events row {number}: {exc}") from exc
        events.append(Event(when, str(row.get("what", "")).strip(), str(row.get("source", ""))))
    return tuple(sorted(events, key=lambda e: e.when_ns))


@dataclass(frozen=True)
class NewsSpec:
    """One dated-text file a project declares: a release, a statement, a note the desk wrote."""

    name: str
    file: str
    source: str


def load_news_map(path: Path) -> tuple[NewsSpec, ...]:
    """`news.toml`: one `[[news]]` per dated-text file the operator wants a role to read.

    Dated text is its own class of data, declared the way a series is, so a desk can keep a
    curated file of its own beside a vendor's export and neither becomes the format the other
    has to match.

        [[news]]
        name = "policy"
        file = "news/policy.csv"     # columns: when, what[, source]
        source = "operator export"
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SeriesError(f"{path}: {exc}") from exc
    entries = raw.get("news")
    if not isinstance(entries, list) or not entries:
        raise SeriesError(f"{path}: a [[news]] entry naming a file is required")
    specs = []
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not entry.get("file"):
            raise SeriesError(f"{path}: news entry {number} names its file")
        specs.append(
            NewsSpec(
                name=str(entry.get("name", "")).strip() or f"news {number}",
                file=str(entry["file"]),
                source=str(entry.get("source", "")).strip(),
            )
        )
    return tuple(specs)


def load_news(path: Path) -> tuple[Event, ...]:
    """Every dated-text file a `news.toml` names, read as one list of events."""
    base = path.resolve().parent
    out: list[Event] = []
    for spec in load_news_map(path):
        target = Path(spec.file)
        for event in read_events(target if target.is_absolute() else base / target):
            # A declared source is a fallback, never an overwrite: the row knows better.
            out.append(
                Event(event.when_ns, event.what, event.source or spec.source)
                if spec.source
                else event
            )
    return tuple(sorted(out, key=lambda e: e.when_ns))


def upcoming(events: Sequence[Event], now_ns: int, *, horizon: timedelta) -> list[Event]:
    end = now_ns + int(horizon.total_seconds() * NS_PER_S)
    return [e for e in events if now_ns <= e.when_ns <= end]
