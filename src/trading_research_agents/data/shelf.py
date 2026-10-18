"""Put a fetched series on a desk's shelf in one step, with lines drawn from its own history.

Declaring a series by hand means a file, a fetch table, a freshness bound and a threshold. All but
one of those follow from the source: the fetch is a preset, the file is pulled now, the freshness
is how often the source publishes, and the lines can be read off the series' own distribution.
The one thing that does not follow is which side is good for the trade, so that is asked for.
The lines are written with where they came from, as a starting point the operator owns.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.data.pull import FETCH_KEYS, PRESETS, Getter, pull
from trading_research_agents.data.series import (
    NS_PER_S,
    SeriesError,
    SeriesSpec,
    Threshold,
    load_series_map,
    read_series,
)
from trading_research_agents.data.sources.positioning import REFRESH as VENUE_REFRESH
from trading_research_agents.data.sources.positioning import TIMING as VENUE_TIMING

CLASSES = ("macro", "sentiment", "onchain", "derivatives")
SIDES = ("bullish", "bearish")
# How late each preset's figure arrives and how old it may be before it stops counting, as they
# were set on the demo desk from each source's own publishing rhythm. An http source has no
# rhythm to read, so its defaults are the package's and are said to be.
TIMING: dict[str, tuple[str, str]] = {
    "fred": ("1d", "5d"),
    "coinmetrics": ("28h", "3d"),
    "alternative.me": ("0s", "2d"),
    "blockchain.info": ("1d", "3d"),
    **VENUE_TIMING,
    "coinglass": ("0s", "2h"),
    "glassnode": ("1h", "2d"),
    "cryptoquant": ("1h", "2d"),
    "http": ("0s", "4d"),
}
REFRESH: dict[str, str] = {**VENUE_REFRESH, "coinglass": "15m"}
UNITS = {"d": 86_400, "h": 3_600, "m": 60, "s": 1}


class ShelfError(ValueError):
    """The series cannot be added as asked."""


@dataclass(frozen=True)
class Added:
    path: Path
    series: str
    rows: int
    window: int
    lower: Decimal
    upper: Decimal
    block: str


def add_series(
    project: Path,
    kind: str,
    name: str,
    fetch: Mapping[str, Any],
    *,
    high: str,
    band: int = 25,
    days: int = 365,
    asset: str = "",
    unit: str = "",
    now: datetime,
    env: Mapping[str, str] | None = None,
    get: Getter | None = None,
) -> Added:
    """Pull the series, draw its lines from the last `days` of it, and append it to the shelf."""
    if kind not in CLASSES:
        raise ShelfError(f"a series goes on one of {list(CLASSES)}, not {kind!r}")
    if high not in SIDES:
        raise ShelfError("--high is bullish or bearish: which side of this series is good")
    if not 1 <= band < 50:
        raise ShelfError("--band is a percentile between 1 and 49; 25 draws the quartiles")
    if not name.replace("_", "").isalnum():
        raise ShelfError("a series name is letters, digits and underscores")
    unknown = sorted(set(fetch) - set(FETCH_KEYS))
    if unknown:
        raise ShelfError(f"fetch has unknown {unknown}")
    origin = str(fetch.get("from", ""))
    if origin != "http" and origin not in PRESETS:
        raise ShelfError(f"--from is http or one of {sorted(PRESETS)}, not {origin!r}")
    shelf = project / "sources" / f"{kind}.toml"
    if shelf.is_file() and _declared(shelf, name):
        raise ShelfError(f"{shelf.name} already declares {name!r}")

    lag, age = TIMING.get(origin, TIMING["http"])
    spec = SeriesSpec(
        name=name,
        file=f"{kind}/{name}.csv",
        ts="date",
        ts_unit="iso8601",
        value="value",
        unit=unit,
        publish_lag=_span(lag),
        source=_source(origin, fetch),
        threshold=Threshold(),
        max_age=_span(age),
        fetch=dict(fetch),
        asset=asset.strip().upper(),
    )
    shelf.parent.mkdir(parents=True, exist_ok=True)
    pulled = pull(spec, shelf.parent, env=env, get=get)
    since = (now - timedelta(days=days)).timestamp() * NS_PER_S
    values = sorted(
        p.value for p in read_series(spec, shelf.parent).points if p.observed_ns >= since
    )
    if len(values) < 20:
        raise ShelfError(
            f"{name}: {len(values)} value(s) in the last {days} days, too few to draw lines from; "
            "widen --days or keep the file by hand"
        )
    lower, upper = _round(_percentile(values, band)), _round(_percentile(values, 100 - band))
    if lower == upper:
        raise ShelfError(f"{name}: the {band}th and {100 - band}th percentiles are equal")
    up, down = format(upper.normalize(), "f"), format(lower.normalize(), "f")
    sides = (
        f"bullish_above = {up}, bearish_below = {down}"
        if high == "bullish"
        else f"bearish_above = {up}, bullish_below = {down}"
    )
    block = _block(spec, origin, fetch, lag, age, sides, band, len(values), now)
    text = shelf.read_text(encoding="utf-8") if shelf.is_file() else ""
    shelf.write_text(text.rstrip("\n") + ("\n\n" if text.strip() else "") + block, encoding="utf-8")
    try:
        load_series_map(shelf)
    except SeriesError as exc:
        shelf.write_text(text, encoding="utf-8")
        raise ShelfError(
            f"the block did not read back, so the file was left as it was: {exc}"
        ) from exc
    return Added(shelf, name, pulled.rows, len(values), lower, upper, block)


def _declared(shelf: Path, name: str) -> bool:
    try:
        raw = tomllib.loads(shelf.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return any(isinstance(e, dict) and e.get("name") == name for e in raw.get("series") or [])


def _block(
    spec: SeriesSpec,
    origin: str,
    fetch: Mapping[str, Any],
    lag: str,
    age: str,
    sides: str,
    band: int,
    count: int,
    now: datetime,
) -> str:
    fields = ", ".join(f"{k} = {_toml(v)}" for k, v in fetch.items())
    lines = [
        "[[series]]",
        f'name = "{spec.name}"',
        f'file = "{spec.file}"',
        'ts = "date"',
        'ts_unit = "iso8601"',
        'value = "value"',
        f'unit = "{spec.unit}"',
        f'publish_lag = "{lag}"',
        f'source = "{spec.source}"',
        f'max_age = "{age}"'
        + ("" if origin in TIMING and origin != "http" else "   # yours to set"),
        f"fetch = {{ {fields} }}",
    ]
    if origin in REFRESH:
        lines.append(f'refresh = "{REFRESH[origin]}"')
    if spec.asset:
        lines.append(f'asset = "{spec.asset}"')
    lines.append(
        f"threshold = {{ {sides} }}   # the {band}th and {100 - band}th percentiles of the "
        f"{count} values to {now:%Y-%m-%d}; yours to move"
    )
    return "\n".join(lines) + "\n"


def _source(origin: str, fetch: Mapping[str, Any]) -> str:
    named = str(fetch.get("id") or "")
    if fetch.get("minus"):
        named += f" less {fetch['minus']}"
    if origin == "http":
        return str(fetch.get("url", "http")).split("?", 1)[0]
    return f"{origin} {named}".strip()


def _toml(value: Any) -> str:
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + " }"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _span(text: str) -> timedelta:
    return timedelta(seconds=float(text[:-1]) * UNITS[text[-1]])


def _percentile(values: list[Decimal], q: int) -> Decimal:
    """Linear interpolation between the two nearest ranks, the way a spreadsheet does it."""
    rank = Decimal(q) / Decimal(100) * (len(values) - 1)
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (rank - low)


def _round(value: Decimal) -> Decimal:
    """Four significant figures: a line is a judgement, not a measurement to the last digit."""
    if value == 0:
        return Decimal(0)
    places = 3 - value.copy_abs().adjusted()
    stepped = value.quantize(Decimal(1).scaleb(-places))
    return stepped if places > 0 else Decimal(int(stepped))
