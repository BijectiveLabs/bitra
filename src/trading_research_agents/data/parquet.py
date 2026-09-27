"""Canonical trades to Parquet with DECIMAL columns, and back, without loss.

Price and quantity are stored as `decimal128(38, s)` with the scale declared by the caller,
never as floats. A trade whose value cannot be represented exactly at the declared scale is
refused rather than rounded, because a rounded price is a wrong number that looks right.

`pyarrow` is an optional dependency (`pip install trading-research-agents[data]`); importing
this module without it raises at call time with that instruction.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.data.schema import Side, Trade

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    pa = None
    pq = None

SCHEMA_VERSION = "1"
PRECISION = 38
BATCH_ROWS = 100_000
COLUMNS = ("ts", "venue", "instrument", "price", "qty", "side", "trade_id", "seq")


class ParquetError(ValueError):
    """The dataset could not be written or read exactly."""


def _arrow() -> tuple[Any, Any]:
    if pa is None or pq is None:
        raise ParquetError(
            "pyarrow is not installed; install the data extra: "
            "pip install 'trading-research-agents[data]'"
        )
    return pa, pq


def schema_for(price_scale: int, qty_scale: int) -> Any:
    arrow, _ = _arrow()
    for label, scale in (("price_scale", price_scale), ("qty_scale", qty_scale)):
        if not 0 <= scale <= PRECISION:
            raise ParquetError(f"{label} must be between 0 and {PRECISION}, got {scale}")
    return arrow.schema(
        [
            arrow.field("ts", arrow.timestamp("ns", tz="UTC"), nullable=False),
            arrow.field("venue", arrow.string(), nullable=False),
            arrow.field("instrument", arrow.string(), nullable=False),
            arrow.field("price", arrow.decimal128(PRECISION, price_scale), nullable=False),
            arrow.field("qty", arrow.decimal128(PRECISION, qty_scale), nullable=False),
            arrow.field("side", arrow.string(), nullable=False),
            arrow.field("trade_id", arrow.string(), nullable=False),
            arrow.field("seq", arrow.int64(), nullable=True),
        ],
        metadata={
            "trading_research_agents.schema": SCHEMA_VERSION,
            "trading_research_agents.price_scale": str(price_scale),
            "trading_research_agents.qty_scale": str(qty_scale),
        },
    )


def _exact(value: Decimal, scale: int, label: str, trade_id: str) -> Decimal:
    quantised = value.quantize(Decimal(1).scaleb(-scale))
    if quantised != value:
        raise ParquetError(
            f"{trade_id}: {label} {value} cannot be stored exactly at scale {scale}; "
            "declare the scale the venue actually uses"
        )
    return quantised


@dataclass(frozen=True)
class WriteResult:
    path: Path
    rows: int
    price_scale: int
    qty_scale: int


def write_trades(
    trades: Iterable[Trade],
    path: Path,
    *,
    price_scale: int,
    qty_scale: int,
    batch_rows: int = BATCH_ROWS,
) -> WriteResult:
    """Stream trades into one Parquet file. Nothing is kept in memory beyond one batch."""
    arrow, parquet = _arrow()
    schema = schema_for(price_scale, qty_scale)
    columns: dict[str, list[Any]] = {name: [] for name in COLUMNS}
    rows = 0

    def flush(writer: Any) -> None:
        if columns["ts"]:
            writer.write_batch(arrow.record_batch(columns, schema=schema))
            for name in COLUMNS:
                columns[name].clear()

    part = path.with_name(path.name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with parquet.ParquetWriter(part, schema, compression="zstd") as writer:
            for trade in trades:
                columns["ts"].append(trade.ts_ns)
                columns["venue"].append(trade.venue)
                columns["instrument"].append(trade.instrument)
                columns["price"].append(_exact(trade.price, price_scale, "price", trade.trade_id))
                columns["qty"].append(_exact(trade.qty, qty_scale, "qty", trade.trade_id))
                columns["side"].append(trade.side.value)
                columns["trade_id"].append(trade.trade_id)
                columns["seq"].append(trade.seq)
                rows += 1
                if rows % batch_rows == 0:
                    flush(writer)
            flush(writer)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(path)
    return WriteResult(path=path, rows=rows, price_scale=price_scale, qty_scale=qty_scale)


def read_scales(path: Path) -> tuple[int, int]:
    _, parquet = _arrow()
    metadata = parquet.read_schema(path).metadata or {}

    def stamp(field: str) -> bytes:
        # Datasets written before the package was renamed carry the old prefix. Reading them is
        # free; a client should not have to refetch a day because we changed a name.
        for prefix in (b"trading_research_agents.", b"trading_research_agent."):
            key = prefix + field.encode()
            if key in metadata:
                return bytes(metadata[key])
        raise KeyError(field)

    try:
        version = stamp("schema").decode()
        price_scale = int(stamp("price_scale"))
        qty_scale = int(stamp("qty_scale"))
    except (KeyError, ValueError) as exc:
        raise ParquetError(f"{path.name}: not a trading-research-agents dataset") from exc
    if version != SCHEMA_VERSION:
        raise ParquetError(f"{path.name}: dataset schema {version!r} is not {SCHEMA_VERSION!r}")
    return price_scale, qty_scale


def read_trades(path: Path, *, batch_rows: int = BATCH_ROWS) -> Iterator[Trade]:
    """Yield trades back exactly as written. The file's own scales are the source of truth."""
    arrow, parquet = _arrow()
    read_scales(path)
    handle = parquet.ParquetFile(path)
    for batch in handle.iter_batches(batch_size=batch_rows, columns=list(COLUMNS)):
        # Timestamps come back as int64 nanoseconds, not datetimes, so nothing below a
        # microsecond is lost on the way out. Decimals come back as Decimal.
        ts = batch.column("ts").cast(arrow.int64()).to_pylist()
        fields = {name: batch.column(name).to_pylist() for name in COLUMNS if name != "ts"}
        for i, ts_ns in enumerate(ts):
            yield Trade(
                ts_ns=ts_ns,
                venue=fields["venue"][i],
                instrument=fields["instrument"][i],
                price=fields["price"][i],
                qty=fields["qty"][i],
                side=Side(fields["side"][i]),
                trade_id=fields["trade_id"][i],
                seq=fields["seq"][i],
            )
