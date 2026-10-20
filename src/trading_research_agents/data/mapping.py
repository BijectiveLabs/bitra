"""Any CSV becomes canonical trades through a declared column map.

Data arrives from venues, from data vendors and from a Client's own exports, in as many
layouts as there are sources. The gate does not care where a row came from; it cares that
every field is named, every unit is declared, and nothing is guessed. A `mapping.toml` says
which column is the timestamp and in what unit, which is the price, which the quantity, how the
side is spelled, and where the trade id is. The rows then pass through the same audit, manifest
and Parquet contract as everything else.
"""

from __future__ import annotations

import csv
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.data.normalise import ArchiveFormatError
from trading_research_agents.data.schema import Rejection, Side, Trade

TIME_UNITS = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}


class MappingError(ValueError):
    """The column map is incomplete or contradicts itself."""


@dataclass(frozen=True)
class ColumnMap:
    source: str
    ts: str
    ts_unit: str
    price: str
    qty: str
    side: str
    trade_id: str
    buy_values: tuple[str, ...] = ("buy",)
    sell_values: tuple[str, ...] = ("sell",)
    seq: str | None = None
    instrument_column: str | None = None
    has_header: bool = True
    delimiter: str = ","
    columns: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise MappingError("a mapping names its source (the vendor or venue it describes)")
        if self.ts_unit not in TIME_UNITS and self.ts_unit != "iso8601":
            raise MappingError(
                f"ts_unit must be one of s, ms, us, ns, iso8601; got {self.ts_unit!r}"
            )
        if not self.has_header and not self.columns:
            raise MappingError("a headerless file needs `columns` listing every column in order")
        if set(self.buy_values) & set(self.sell_values):
            raise MappingError("a value cannot mean both buy and sell")
        for label in ("ts", "price", "qty", "side", "trade_id"):
            if not getattr(self, label).strip():
                raise MappingError(f"{label} column is required")


def load_mapping(path: Path) -> ColumnMap:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise MappingError(f"{path}: not valid TOML: {exc}") from exc
    return parse_mapping(raw)


def parse_mapping(raw: dict[str, Any]) -> ColumnMap:
    m = raw.get("mapping")
    if not isinstance(m, dict):
        raise MappingError("[mapping] table is required")
    side = m.get("side", {})
    if isinstance(side, str):
        side = {"column": side}
    if not isinstance(side, dict) or "column" not in side:
        raise MappingError(
            "[mapping] side needs a column: side = { column = 'S', buy = ['Buy'], sell = ['Sell'] }"
        )
    try:
        return ColumnMap(
            source=str(m.get("source", "")),
            ts=str(m.get("ts", "")),
            ts_unit=str(m.get("ts_unit", "")),
            price=str(m.get("price", "")),
            qty=str(m.get("qty", "")),
            side=str(side["column"]),
            trade_id=str(m.get("trade_id", "")),
            buy_values=tuple(str(v) for v in side.get("buy", ["buy"])),
            sell_values=tuple(str(v) for v in side.get("sell", ["sell"])),
            seq=str(m["seq"]) if m.get("seq") else None,
            instrument_column=str(m["instrument"]) if m.get("instrument") else None,
            has_header=bool(m.get("has_header", True)),
            delimiter=str(m.get("delimiter", ",")),
            columns=tuple(str(c) for c in m.get("columns", [])),
        )
    except (TypeError, ValueError) as exc:
        raise MappingError(str(exc)) from exc


def _ts_ns(text: str, unit: str) -> int:
    if unit == "iso8601":
        from datetime import UTC, datetime

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"timestamp {text!r} has no timezone; the mapping cannot assume one")
        return int(parsed.astimezone(UTC).timestamp() * 1_000_000_000)
    value = Decimal(text)
    return int(value * TIME_UNITS[unit])


def mapped_trades(
    lines: Iterable[str], mapping: ColumnMap, *, instrument: str
) -> Iterator[Trade | Rejection]:
    """Rows to trades through the map. A missing column stops the file; a bad row is rejected."""
    reader = csv.reader(lines, delimiter=mapping.delimiter)
    if mapping.has_header:
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise ArchiveFormatError("file is empty; expected a header row") from None
        first = 2
    else:
        header = list(mapping.columns)
        first = 1
    return _mapped(header, reader, mapping, instrument=instrument, first=first)


def mapped_table(path: Path, mapping: ColumnMap, *, instrument: str) -> Iterator[Trade | Rejection]:
    """A Parquet file through the same map. Its column names are its header; row 1 is data."""
    from trading_research_agents.data import table

    return _mapped(table.header(path), table.rows(path), mapping, instrument=instrument, first=1)


def _mapped(
    header: list[str],
    reader: Iterable[list[str]],
    mapping: ColumnMap,
    *,
    instrument: str,
    first: int,
) -> Iterator[Trade | Rejection]:
    index: dict[str, int] = {name: i for i, name in enumerate(header)}
    needed = [mapping.ts, mapping.price, mapping.qty, mapping.side, mapping.trade_id]
    if mapping.seq:
        needed.append(mapping.seq)
    if mapping.instrument_column:
        needed.append(mapping.instrument_column)
    missing = [c for c in needed if c not in index]
    if missing:
        raise ArchiveFormatError(f"columns {missing} are not in the file; header is {header}")

    for number, row in enumerate(reader, start=first):
        raw = mapping.delimiter.join(row)
        if len(row) != len(header):
            yield Rejection(number, f"expected {len(header)} columns, got {len(row)}", raw)
            continue
        if mapping.instrument_column is not None:
            found = row[index[mapping.instrument_column]].strip()
            if found != instrument:
                yield Rejection(number, f"row is for {found!r}, file is for {instrument!r}", raw)
                continue
        side_text = row[index[mapping.side]].strip()
        if side_text in mapping.buy_values:
            side = Side.BUY
        elif side_text in mapping.sell_values:
            side = Side.SELL
        else:
            yield Rejection(number, f"unknown side {side_text!r}", raw)
            continue
        try:
            yield Trade(
                ts_ns=_ts_ns(row[index[mapping.ts]].strip(), mapping.ts_unit),
                venue=mapping.source,
                instrument=instrument,
                price=Decimal(row[index[mapping.price]].strip()),
                qty=Decimal(row[index[mapping.qty]].strip()),
                side=side,
                trade_id=row[index[mapping.trade_id]].strip(),
                seq=int(row[index[mapping.seq]]) if mapping.seq else None,
            )
        except (InvalidOperation, ValueError) as exc:
            yield Rejection(number, str(exc), raw)
