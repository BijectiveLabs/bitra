"""Market data on disk: one Parquet file per market, stream and UTC day, with its manifest.

A day file is written whole or not at all, holds only exact values, and is read back only if its
bytes are still the ones its manifest names. A file that was edited, truncated or half copied is
refused by every reader and named by `tra runs verify --market`; it is never repaired quietly,
because a backtest on a repaired file measures the repair.

One file per stream per day keeps the number of files flat however long a desk records.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.extras import install_hint
from trading_research_agents.market_data.bars import (
    FlowBar,
    GapState,
    MarketDataError,
    quiet_bar,
)
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, day_of, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series
from trading_research_agents.records import sha256_of, write_json

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    pa = None
    pq = None

SCHEMA_VERSION = "1"
BUILDER_VERSION = "1"
PRECISION = 38
FLOW = "flow_1m"
DERIV = "deriv"
STREAMS = (FLOW, DERIV)
MINUTES_PER_DAY = DAY_NS // NS_PER_MINUTE
META = "trading_research_agents.market."
FLOW_PRICES = ("open", "high", "low", "close")
FLOW_SIZES = ("volume_base", "taker_buy_base", "taker_sell_base", "large_trade_base")


class ArchiveError(MarketDataError):
    """A day file or manifest that cannot be trusted, or a day that cannot be written whole."""


def _arrow() -> tuple[Any, Any]:
    if pa is None or pq is None:
        raise ArchiveError(f"pyarrow is not installed; {install_hint('data')}")
    return pa, pq


@dataclass(frozen=True)
class SourceFile:
    """What a day was built from: an archive file and its hash, or an API with no file."""

    path: str
    sha256: str | None


@dataclass(frozen=True)
class DayManifest:
    stream: str
    venue: str
    instrument: str
    day: date
    rows: int
    sha256: str
    partial_minutes: int
    missing_minutes: int
    sources: tuple[SourceFile, ...]
    price_scale: int | None = None
    qty_scale: int | None = None
    large_trade_quote: str | None = None
    schema: str = SCHEMA_VERSION
    builder: str = BUILDER_VERSION

    def record(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "venue": self.venue,
            "instrument": self.instrument,
            "day": self.day.isoformat(),
            "rows": self.rows,
            "sha256": self.sha256,
            "partial_minutes": self.partial_minutes,
            "missing_minutes": self.missing_minutes,
            "sources": [{"path": s.path, "sha256": s.sha256} for s in self.sources],
            "price_scale": self.price_scale,
            "qty_scale": self.qty_scale,
            "large_trade_quote": self.large_trade_quote,
            "schema": self.schema,
            "builder": self.builder,
        }

    @classmethod
    def read(cls, path: Path) -> DayManifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                stream=str(raw["stream"]),
                venue=str(raw["venue"]),
                instrument=str(raw["instrument"]),
                day=date.fromisoformat(raw["day"]),
                rows=int(raw["rows"]),
                sha256=str(raw["sha256"]),
                partial_minutes=int(raw["partial_minutes"]),
                missing_minutes=int(raw["missing_minutes"]),
                sources=tuple(SourceFile(str(s["path"]), s["sha256"]) for s in raw["sources"]),
                price_scale=raw["price_scale"],
                qty_scale=raw["qty_scale"],
                large_trade_quote=raw["large_trade_quote"],
                schema=str(raw["schema"]),
                builder=str(raw["builder"]),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ArchiveError(f"{path.name}: not a manifest this version reads ({exc})") from exc


def day_file(root: Path, venue: str, instrument: str, stream: str, day: date) -> Path:
    if stream not in STREAMS:
        raise ArchiveError(f"{stream!r} is not a stream; have {list(STREAMS)}")
    return root / "market" / venue / instrument / stream / f"{day.isoformat()}.parquet"


def manifest_file(data_file: Path) -> Path:
    return data_file.with_name(data_file.name.removesuffix(".parquet") + ".manifest.json")


def write_flow_day(
    bars: Sequence[FlowBar],
    root: Path,
    *,
    day: date,
    price_scale: int,
    qty_scale: int,
    sources: Sequence[SourceFile],
    large_trade_quote: Decimal | None = None,
) -> DayManifest:
    """One whole UTC day of one-minute bars, then its manifest; nothing if any of it is wrong."""
    _check_day(bars, day)
    arrow, _ = _arrow()
    schema = _flow_schema(arrow, price_scale, qty_scale, bars[0])
    table = arrow.table(_flow_columns(bars, price_scale, qty_scale), schema=schema)
    path = day_file(root, bars[0].venue, bars[0].instrument, FLOW, day)
    _write_table(table, path)
    manifest = DayManifest(
        stream=FLOW,
        venue=bars[0].venue,
        instrument=bars[0].instrument,
        day=day,
        rows=len(bars),
        sha256=sha256_of(path),
        partial_minutes=sum(1 for b in bars if b.gap is GapState.PARTIAL),
        missing_minutes=sum(1 for b in bars if b.gap is GapState.MISSING),
        sources=tuple(sources),
        price_scale=price_scale,
        qty_scale=qty_scale,
        large_trade_quote=None if large_trade_quote is None else str(large_trade_quote),
    )
    write_json(manifest_file(path), manifest.record())
    return manifest


def read_flow_day(root: Path, venue: str, instrument: str, day: date) -> list[FlowBar]:
    """The day's bars as written, after its bytes are checked against its manifest."""
    path = day_file(root, venue, instrument, FLOW, day)
    verified(path)
    arrow, parquet = _arrow()
    table = parquet.read_table(path)
    starts = table.column("start").cast(arrow.int64()).to_pylist()
    names = [
        *FLOW_PRICES,
        *FLOW_SIZES,
        "volume_quote",
        "trade_count",
        "late_trades",
        "gap",
        "source",
    ]
    columns = {name: table.column(name).to_pylist() for name in names}
    return [_flow_bar(venue, instrument, start, columns, i) for i, start in enumerate(starts)]


def read_flow_range(
    root: Path, venue: str, instrument: str, start: date, end: date
) -> list[FlowBar]:
    """Every minute from `start` to `end` inclusive; a day never written reads as missing."""
    out: list[FlowBar] = []
    day = start
    while day <= end:
        if day_file(root, venue, instrument, FLOW, day).is_file():
            out.extend(read_flow_day(root, venue, instrument, day))
        else:
            out.extend(_missing_day(venue, instrument, day))
        day += timedelta(days=1)
    return out


def verified(path: Path) -> DayManifest:
    """The manifest of `path`, once the file's bytes are the ones it names."""
    if not path.is_file():
        raise ArchiveError(f"{path} does not exist")
    manifest_path = manifest_file(path)
    if not manifest_path.is_file():
        raise ArchiveError(f"{path.name} has no manifest; refused")
    manifest = DayManifest.read(manifest_path)
    if sha256_of(path) != manifest.sha256:
        raise ArchiveError(f"{path.name} differs from its manifest; refused, not repaired")
    return manifest


@dataclass(frozen=True)
class ArchiveCheck:
    days: int
    problems: tuple[str, ...]


def verify_archive(root: Path) -> ArchiveCheck:
    """Every day file under `root/market` against its manifest, and every file with none."""
    base = root / "market"
    if not base.is_dir():
        return ArchiveCheck(days=0, problems=())
    problems: list[str] = []
    days = 0
    for path in sorted(base.rglob("*.parquet")):
        if path.name.startswith("."):
            continue
        days += 1
        try:
            manifest = verified(path)
        except ArchiveError as exc:
            problems.append(f"{path.relative_to(root)}: {exc}")
            continue
        rows = _rows_in(path)
        if rows != manifest.rows:
            problems.append(f"{path.relative_to(root)}: {rows} rows, manifest says {manifest.rows}")
    return ArchiveCheck(days=days, problems=tuple(problems))


def _rows_in(path: Path) -> int:
    _, parquet = _arrow()
    return int(parquet.ParquetFile(path).metadata.num_rows)


def _check_day(bars: Sequence[FlowBar], day: date) -> None:
    if len(bars) != MINUTES_PER_DAY:
        raise ArchiveError(f"a day file holds {MINUTES_PER_DAY} one-minute bars, got {len(bars)}")
    start = midnight_ns(day)
    first = bars[0]
    for i, bar in enumerate(bars):
        if bar.start_ns != start + i * NS_PER_MINUTE or bar.end_ns != bar.start_ns + NS_PER_MINUTE:
            raise ArchiveError(f"bar {i} of {day} is not the minute it should be")
        if (bar.venue, bar.instrument) != (first.venue, first.instrument):
            raise ArchiveError(f"a day file holds one market; bar {i} is another")


def _flow_schema(arrow: Any, price_scale: int, qty_scale: int, first: FlowBar) -> Any:
    if price_scale < 0 or qty_scale < 0 or price_scale + qty_scale > PRECISION:
        raise ArchiveError(
            f"price scale {price_scale} plus quantity scale {qty_scale} must fit {PRECISION} digits"
        )
    price = arrow.decimal128(PRECISION, price_scale)
    size = arrow.decimal128(PRECISION, qty_scale)
    fields = [arrow.field("start", arrow.timestamp("ns", tz="UTC"), nullable=False)]
    fields += [arrow.field(name, price, nullable=True) for name in FLOW_PRICES]
    fields += [arrow.field(name, size, nullable=name == "large_trade_base") for name in FLOW_SIZES]
    fields += [
        arrow.field(
            "volume_quote", arrow.decimal128(PRECISION, price_scale + qty_scale), nullable=False
        ),
        arrow.field("trade_count", arrow.int64(), nullable=False),
        arrow.field("late_trades", arrow.int64(), nullable=False),
        arrow.field("gap", arrow.string(), nullable=False),
        arrow.field("source", arrow.string(), nullable=False),
    ]
    metadata = {
        META + "schema": SCHEMA_VERSION,
        META + "stream": FLOW,
        META + "venue": first.venue,
        META + "instrument": first.instrument,
        META + "interval_ns": str(NS_PER_MINUTE),
        META + "price_scale": str(price_scale),
        META + "qty_scale": str(qty_scale),
    }
    return arrow.schema(fields, metadata=metadata)


def _flow_columns(
    bars: Sequence[FlowBar], price_scale: int, qty_scale: int
) -> dict[str, list[Any]]:
    columns: dict[str, list[Any]] = {"start": [b.start_ns for b in bars]}
    for name in FLOW_PRICES:
        columns[name] = [_fit(getattr(b, name), price_scale, name, b) for b in bars]
    for name in FLOW_SIZES:
        columns[name] = [_fit(getattr(b, name), qty_scale, name, b) for b in bars]
    quote_scale = price_scale + qty_scale
    columns["volume_quote"] = [_fit(b.volume_quote, quote_scale, "volume_quote", b) for b in bars]
    columns["trade_count"] = [b.trade_count for b in bars]
    columns["late_trades"] = [b.late_trades for b in bars]
    columns["gap"] = [b.gap.value for b in bars]
    columns["source"] = [b.source for b in bars]
    return columns


def _fit(value: Decimal | None, scale: int, label: str, bar: FlowBar) -> Decimal | None:
    if value is None:
        return None
    quantised = value.quantize(Decimal(1).scaleb(-scale))
    if quantised != value:
        raise ArchiveError(
            f"{label} {value} at {bar.start_ns} cannot be stored exactly at scale {scale}"
        )
    return quantised


def _flow_bar(
    venue: str, instrument: str, start: int, columns: dict[str, list[Any]], i: int
) -> FlowBar:
    return FlowBar(
        venue=venue,
        instrument=instrument,
        start_ns=start,
        end_ns=start + NS_PER_MINUTE,
        open=columns["open"][i],
        high=columns["high"][i],
        low=columns["low"][i],
        close=columns["close"][i],
        volume_base=columns["volume_base"][i],
        volume_quote=columns["volume_quote"][i],
        taker_buy_base=columns["taker_buy_base"][i],
        taker_sell_base=columns["taker_sell_base"][i],
        trade_count=columns["trade_count"][i],
        large_trade_base=columns["large_trade_base"][i],
        late_trades=columns["late_trades"][i],
        gap=GapState(columns["gap"][i]),
        source=columns["source"][i],
    )


def _missing_day(venue: str, instrument: str, day: date) -> list[FlowBar]:
    start = midnight_ns(day)
    return [
        quiet_bar(
            venue,
            instrument,
            start + i * NS_PER_MINUTE,
            start + (i + 1) * NS_PER_MINUTE,
            source="none",
            large_declared=False,
            gap=GapState.MISSING,
        )
        for i in range(MINUTES_PER_DAY)
    ]


def _write_table(table: Any, path: Path) -> None:
    _, parquet = _arrow()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.part")
    try:
        parquet.write_table(table, part, compression="zstd")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, path)


def write_deriv_day(
    points: Sequence[DerivPoint],
    root: Path,
    *,
    venue: str,
    instrument: str,
    day: date,
    sources: Sequence[SourceFile],
) -> DayManifest:
    """One UTC day of derivatives points, sorted; an empty day is written, since asking and
    hearing nothing is itself a fact.

    Values are stored as their exact decimal text: a funding rate, an open interest and a mark
    price have scales no single decimal column fits without rounding one of them.
    """
    outside = [p for p in points if day_of(p.as_of_ns) != day]
    if outside:
        raise ArchiveError(f"{len(outside)} point(s) fall outside {day}")
    arrow, _ = _arrow()
    ordered = sorted(points, key=lambda p: (p.series.value, p.as_of_ns, p.received_ns))
    stamp = arrow.timestamp("ns", tz="UTC")
    schema = arrow.schema(
        [
            arrow.field("series", arrow.string(), nullable=False),
            arrow.field("as_of", stamp, nullable=False),
            arrow.field("received", stamp, nullable=False),
            arrow.field("value", arrow.string(), nullable=False),
            arrow.field("detail", arrow.string(), nullable=False),
        ],
        metadata={META + "schema": SCHEMA_VERSION, META + "stream": DERIV},
    )
    columns = {
        "series": [p.series.value for p in ordered],
        "as_of": [p.as_of_ns for p in ordered],
        "received": [p.received_ns for p in ordered],
        "value": [str(p.value) for p in ordered],
        "detail": [
            json.dumps(dict(p.detail), sort_keys=True, separators=(",", ":")) for p in ordered
        ],
    }
    path = day_file(root, venue, instrument, DERIV, day)
    _write_table(arrow.table(columns, schema=schema), path)
    manifest = DayManifest(
        stream=DERIV,
        venue=venue,
        instrument=instrument,
        day=day,
        rows=len(ordered),
        sha256=sha256_of(path),
        partial_minutes=0,
        missing_minutes=0,
        sources=tuple(sources),
    )
    write_json(manifest_file(path), manifest.record())
    return manifest


def read_deriv_day(root: Path, venue: str, instrument: str, day: date) -> list[DerivPoint]:
    path = day_file(root, venue, instrument, DERIV, day)
    if not path.is_file():
        return []
    verified(path)
    arrow, parquet = _arrow()
    table = parquet.read_table(path)
    as_of = table.column("as_of").cast(arrow.int64()).to_pylist()
    received = table.column("received").cast(arrow.int64()).to_pylist()
    series = table.column("series").to_pylist()
    values = table.column("value").to_pylist()
    details = table.column("detail").to_pylist()
    return [
        DerivPoint(
            Series(series[i]),
            as_of[i],
            received[i],
            Decimal(values[i]),
            tuple(sorted(json.loads(details[i]).items())),
        )
        for i in range(len(as_of))
    ]


def read_deriv_range(
    root: Path, venue: str, instrument: str, start: date, end: date
) -> list[DerivPoint]:
    out: list[DerivPoint] = []
    day = start
    while day <= end:
        out.extend(read_deriv_day(root, venue, instrument, day))
        day += timedelta(days=1)
    return out
