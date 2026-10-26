"""Market data on disk: one Parquet file per market, stream and UTC day, with its manifest.

A file is written whole or not at all, holds only exact values, and is read back only if its bytes
are still the ones its manifest names. A file that was edited, truncated or half copied is refused
by every reader and named by `tra runs verify --market`; it is never repaired quietly, because a
backtest on a repaired file measures the repair.

While a day is being recorded it is kept as hour parts, `<day>/part-<HH>.parquet`, each verified
the same way; once the day has closed they are compacted into the one day file, which keeps the
number of files flat however long a desk records. A reader reads the day file when there is one and
the parts when there is not, and a minute no file covers reads as missing.

This module holds what every stream shares and the flow stream itself; the derivatives, book and
text streams have their own modules beside it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
BOOK = "book_1m"
TEXT = "text"
STREAMS = (FLOW, DERIV, BOOK, TEXT)
HOUR_NS = 60 * NS_PER_MINUTE
MINUTES_PER_DAY = DAY_NS // NS_PER_MINUTE
MINUTES_PER_HOUR = HOUR_NS // NS_PER_MINUTE
HOURS_PER_DAY = DAY_NS // HOUR_NS
META = "trading_research_agents.market."
FLOW_PRICES = ("open", "high", "low", "close")
FLOW_SIZES = ("volume_base", "taker_buy_base", "taker_sell_base", "large_trade_base")


class ArchiveError(MarketDataError):
    """A file or manifest that cannot be trusted, or a span that cannot be written whole."""


def arrow_modules() -> tuple[Any, Any]:
    if pa is None or pq is None:
        raise ArchiveError(f"pyarrow is not installed; {install_hint('data')}")
    return pa, pq


@dataclass(frozen=True)
class SourceFile:
    """What a span was built from: an archive file and its hash, or a stream or API with none."""

    path: str
    sha256: str | None


@dataclass(frozen=True)
class DayManifest:
    """What a day file, or one hour part of a day (`hour` set), holds and was built from."""

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
    hour: int | None = None

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {
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
        if self.hour is not None:
            out["hour"] = self.hour
        return out

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
                hour=None if raw.get("hour") is None else int(raw["hour"]),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ArchiveError(f"{path.name}: not a manifest this version reads ({exc})") from exc


def day_file(root: Path, venue: str, instrument: str, stream: str, day: date) -> Path:
    if stream not in STREAMS:
        raise ArchiveError(f"{stream!r} is not a stream; have {list(STREAMS)}")
    return root / "market" / venue / instrument / stream / f"{day.isoformat()}.parquet"


def part_file(root: Path, venue: str, instrument: str, stream: str, day: date, hour: int) -> Path:
    if not 0 <= hour < HOURS_PER_DAY:
        raise ArchiveError(f"hour {hour} is not an hour of a day")
    folder = day_file(root, venue, instrument, stream, day).with_suffix("")
    return folder / f"part-{hour:02d}.parquet"


def manifest_file(data_file: Path) -> Path:
    return data_file.with_name(data_file.name.removesuffix(".parquet") + ".manifest.json")


def hour_of(start_ns: int) -> tuple[date, int]:
    """The UTC day and hour an hour part starting at `start_ns` belongs to."""
    if start_ns % HOUR_NS:
        raise ArchiveError("an hour part starts on the hour")
    day = day_of(start_ns)
    return day, (start_ns - midnight_ns(day)) // HOUR_NS


def day_units(
    root: Path, venue: str, instrument: str, stream: str, day: date
) -> dict[int | None, Path]:
    """The files that hold a day: the day file alone when there is one, else its hour parts."""
    whole = day_file(root, venue, instrument, stream, day)
    if whole.is_file():
        return {None: whole}
    return {
        hour: path
        for hour in range(HOURS_PER_DAY)
        if (path := part_file(root, venue, instrument, stream, day, hour)).is_file()
    }


def write_unit(table: Any, path: Path, manifest: DayManifest) -> DayManifest:
    """The table, then its manifest with the table's sha256; a reader never sees half of either."""
    write_table(table, path)
    sealed = replace(manifest, sha256=sha256_of(path))
    write_json(manifest_file(path), sealed.record())
    return sealed


def write_table(table: Any, path: Path) -> None:
    _, parquet = arrow_modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.part")
    try:
        parquet.write_table(table, part, compression="zstd")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, path)


def fit_scale(value: Decimal | None, scale: int, label: str, at_ns: int) -> Decimal | None:
    """`value` at `scale`, refused when that would round it."""
    if value is None:
        return None
    quantised = value.quantize(Decimal(1).scaleb(-scale))
    if quantised != value:
        raise ArchiveError(f"{label} {value} at {at_ns} cannot be stored exactly at scale {scale}")
    return quantised


def check_scales(price_scale: int, qty_scale: int) -> None:
    if price_scale < 0 or qty_scale < 0 or price_scale + qty_scale > PRECISION:
        raise ArchiveError(
            f"price scale {price_scale} plus quantity scale {qty_scale} must fit {PRECISION} digits"
        )


def check_minutes(starts: Sequence[int], first_ns: int, count: int, what: str) -> None:
    """`count` contiguous one-minute starts from `first_ns`, or a refusal naming the first wrong."""
    if len(starts) != count:
        raise ArchiveError(f"{what} holds {count} one-minute bars, got {len(starts)}")
    for i, start in enumerate(starts):
        if start != first_ns + i * NS_PER_MINUTE:
            raise ArchiveError(f"bar {i} of {what} is not the minute it should be")


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
    """Every day file and hour part under `root/market` against its manifest."""
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
    _, parquet = arrow_modules()
    return int(parquet.ParquetFile(path).metadata.num_rows)


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
    _check_market(bars, f"a day file of {day}", midnight_ns(day), MINUTES_PER_DAY)
    path = day_file(root, bars[0].venue, bars[0].instrument, FLOW, day)
    return _write_flow(bars, path, day, None, price_scale, qty_scale, sources, large_trade_quote)


def write_flow_part(
    bars: Sequence[FlowBar],
    root: Path,
    *,
    price_scale: int,
    qty_scale: int,
    sources: Sequence[SourceFile],
    large_trade_quote: Decimal | None = None,
) -> DayManifest:
    """One whole hour of one-minute bars as a part of its day."""
    if len(bars) != MINUTES_PER_HOUR:
        raise ArchiveError(
            f"an hour part holds {MINUTES_PER_HOUR} one-minute bars, got {len(bars)}"
        )
    day, hour = hour_of(bars[0].start_ns)
    _check_market(bars, f"hour {hour:02d} of {day}", bars[0].start_ns, MINUTES_PER_HOUR)
    path = part_file(root, bars[0].venue, bars[0].instrument, FLOW, day, hour)
    return _write_flow(bars, path, day, hour, price_scale, qty_scale, sources, large_trade_quote)


def _check_market(bars: Sequence[FlowBar], what: str, first_ns: int, count: int) -> None:
    check_minutes([b.start_ns for b in bars], first_ns, count, what)
    first = bars[0]
    for i, bar in enumerate(bars):
        if bar.end_ns != bar.start_ns + NS_PER_MINUTE:
            raise ArchiveError(f"bar {i} of {what} is not one minute long")
        if (bar.venue, bar.instrument) != (first.venue, first.instrument):
            raise ArchiveError(f"{what} holds one market; bar {i} is another")


def _write_flow(
    bars: Sequence[FlowBar],
    path: Path,
    day: date,
    hour: int | None,
    price_scale: int,
    qty_scale: int,
    sources: Sequence[SourceFile],
    large_trade_quote: Decimal | None,
) -> DayManifest:
    arrow, _ = arrow_modules()
    schema = _flow_schema(arrow, price_scale, qty_scale, bars[0])
    table = arrow.table(_flow_columns(bars, price_scale, qty_scale), schema=schema)
    manifest = DayManifest(
        stream=FLOW,
        venue=bars[0].venue,
        instrument=bars[0].instrument,
        day=day,
        rows=len(bars),
        sha256="",
        partial_minutes=sum(1 for b in bars if b.gap is GapState.PARTIAL),
        missing_minutes=sum(1 for b in bars if b.gap is GapState.MISSING),
        sources=tuple(sources),
        price_scale=price_scale,
        qty_scale=qty_scale,
        large_trade_quote=None if large_trade_quote is None else str(large_trade_quote),
        hour=hour,
    )
    return write_unit(table, path, manifest)


def read_flow_day(root: Path, venue: str, instrument: str, day: date) -> list[FlowBar]:
    """The day's bars as written, after its bytes are checked against its manifest."""
    return read_flow_file(day_file(root, venue, instrument, FLOW, day), venue, instrument)


def read_flow_file(path: Path, venue: str, instrument: str) -> list[FlowBar]:
    verified(path)
    arrow, parquet = arrow_modules()
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
    """Every minute from `start` to `end` inclusive; a minute no file covers reads as missing."""
    out: list[FlowBar] = []
    day = start
    while day <= end:
        units = day_units(root, venue, instrument, FLOW, day)
        if None in units:
            out.extend(read_flow_file(units[None], venue, instrument))
        else:
            for hour in range(HOURS_PER_DAY):
                first = midnight_ns(day) + hour * HOUR_NS
                path = units.get(hour)
                if path is None:
                    out.extend(missing_minutes(venue, instrument, first, MINUTES_PER_HOUR))
                else:
                    out.extend(read_flow_file(path, venue, instrument))
        day += timedelta(days=1)
    return out


def missing_minutes(venue: str, instrument: str, first_ns: int, count: int) -> list[FlowBar]:
    return [
        quiet_bar(
            venue,
            instrument,
            first_ns + i * NS_PER_MINUTE,
            first_ns + (i + 1) * NS_PER_MINUTE,
            source="none",
            large_declared=False,
            gap=GapState.MISSING,
        )
        for i in range(count)
    ]


def _flow_schema(arrow: Any, price_scale: int, qty_scale: int, first: FlowBar) -> Any:
    check_scales(price_scale, qty_scale)
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
        columns[name] = [fit_scale(getattr(b, name), price_scale, name, b.start_ns) for b in bars]
    for name in FLOW_SIZES:
        columns[name] = [fit_scale(getattr(b, name), qty_scale, name, b.start_ns) for b in bars]
    quote_scale = price_scale + qty_scale
    columns["volume_quote"] = [
        fit_scale(b.volume_quote, quote_scale, "volume_quote", b.start_ns) for b in bars
    ]
    columns["trade_count"] = [b.trade_count for b in bars]
    columns["late_trades"] = [b.late_trades for b in bars]
    columns["gap"] = [b.gap.value for b in bars]
    columns["source"] = [b.source for b in bars]
    return columns


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
