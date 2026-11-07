"""Hour bars from a venue's own candles, and the funding settled under them, for replays of years.

The minute archive is built from trades and holds every minute; a multi-year replay of it does not
fit in memory, and the venues' trade archives download too slowly to build one. A venue's hour
candles carry what the arena's features read, provided the venue reports the taker split and the
trade count with each candle. Which venues do is stated in `market_data/venues/ccxt_klines.py`; a
candle without them is refused rather than split by a guess.

Stored outside `market/`, which holds only the trade-built archive, as one Parquet file per market
with a manifest naming its sha256; a file that differs from its manifest is refused, not repaired.
Prices and sizes are kept as the venue's own decimal strings, so nothing is rounded on the way.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.market_data.archive import ArchiveError, arrow_modules, write_table
from trading_research_agents.market_data.bars import FlowBar, GapState, MarketDataError, quiet_bar
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S
from trading_research_agents.market_data.deriv import DerivPoint, Series
from trading_research_agents.records import sha256_of, write_json

HOUR_NS = 60 * NS_PER_MINUTE
NS_PER_MS = 1_000_000
SCHEMA = "1"
PRICES = ("open", "high", "low", "close")
SIZES = ("volume_base", "volume_quote", "taker_buy_base", "taker_sell_base")


@dataclass(frozen=True)
class Klines:
    """One market's hour bars and funding points, and the sha256 of each file they came from."""

    bars: tuple[FlowBar, ...]
    points: tuple[DerivPoint, ...]
    sha256: dict[str, str]


def bars_from_klines(
    rows: Sequence[Sequence[Any]], *, venue: str, instrument: str
) -> list[FlowBar]:
    """Contiguous hour bars from candle rows laid out as open time (ms), open, high, low, close,
    base volume, close time (ms), quote volume, trades, taker buy base volume. An hour the venue
    did not return is a missing bar, never a quiet one."""
    bars: list[FlowBar] = []
    for raw in rows:
        bar = _bar(raw, venue, instrument)
        if bars and bar.start_ns <= bars[-1].start_ns:
            raise MarketDataError("candles arrive in order, each hour once")
        start = bars[-1].end_ns if bars else bar.start_ns
        while start < bar.start_ns:
            bars.append(missing_hour(venue, instrument, start))
            start += HOUR_NS
        bars.append(bar)
    return bars


def _bar(raw: Sequence[Any], venue: str, instrument: str) -> FlowBar:
    start = int(raw[0]) * NS_PER_MS
    end = (int(raw[6]) + 1) * NS_PER_MS
    if start % HOUR_NS:
        raise MarketDataError(f"a candle at {start} ns does not start on the hour")
    if end - start != HOUR_NS:
        raise MarketDataError("a candle here spans one hour")
    source = f"klines:{venue}"
    trades = int(raw[8])
    if trades == 0:
        return quiet_bar(venue, instrument, start, end, source=source, large_declared=False)
    volume = Decimal(str(raw[5]))
    bought = Decimal(str(raw[9]))
    return FlowBar(
        venue=venue,
        instrument=instrument,
        start_ns=start,
        end_ns=end,
        open=Decimal(str(raw[1])),
        high=Decimal(str(raw[2])),
        low=Decimal(str(raw[3])),
        close=Decimal(str(raw[4])),
        volume_base=volume,
        volume_quote=Decimal(str(raw[7])),
        taker_buy_base=bought,
        taker_sell_base=volume - bought,
        trade_count=trades,
        large_trade_base=None,
        late_trades=0,
        gap=GapState.NONE,
        source=source,
    )


def missing_hour(venue: str, instrument: str, start: int) -> FlowBar:
    """An hour nobody saw: no prices, no volume, and marked as a gap."""
    return quiet_bar(
        venue,
        instrument,
        start,
        start + HOUR_NS,
        source=f"klines:{venue}",
        large_declared=False,
        gap=GapState.MISSING,
    )


def funding_on_the_second(points: Sequence[DerivPoint]) -> list[DerivPoint]:
    """Funding settlements at the second they settled.

    A venue may stamp a settlement a few milliseconds after it (observed 2026-11-07: 00:00:00.002).
    The settlement clock is measured as the common step between settlements, and a few
    milliseconds of jitter would make that step one millisecond.
    """
    return [
        replace(p, as_of_ns=p.as_of_ns - p.as_of_ns % NS_PER_S) if p.series is Series.FUNDING else p
        for p in points
    ]


def _folder(root: Path, venue: str, instrument: str) -> Path:
    return root / "klines" / venue / instrument


def write_klines(
    root: Path, bars: Sequence[FlowBar], points: Sequence[DerivPoint]
) -> dict[str, Any]:
    """The bars and the funding, each with its manifest; the manifest is written last."""
    if not bars:
        raise MarketDataError("there are no bars to write")
    venue, instrument = bars[0].venue, bars[0].instrument
    folder = _folder(root, venue, instrument)
    arrow, _ = arrow_modules()
    write_table(arrow.table(_bar_columns(bars)), folder / "1h.parquet")
    write_table(arrow.table(_point_columns(points)), folder / "funding.parquet")
    manifest = {
        "schema": SCHEMA,
        "venue": venue,
        "instrument": instrument,
        "interval_ns": HOUR_NS,
        "first_ns": bars[0].start_ns,
        "last_ns": bars[-1].end_ns,
        "rows": len(bars),
        "missing_rows": sum(1 for b in bars if b.gap is GapState.MISSING),
        "funding_rows": len(points),
        "sha256": {
            "bars": sha256_of(folder / "1h.parquet"),
            "funding": sha256_of(folder / "funding.parquet"),
        },
    }
    write_json(folder / "manifest.json", manifest)
    return manifest


def read_klines(root: Path, venue: str, instrument: str) -> Klines:
    """The market's bars and funding, once both files are the ones the manifest names."""
    folder = _folder(root, venue, instrument)
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"no readable kline manifest for {venue} {instrument}: {exc}") from exc
    files = {"bars": folder / "1h.parquet", "funding": folder / "funding.parquet"}
    for name, path in files.items():
        if not path.is_file() or sha256_of(path) != manifest["sha256"][name]:
            raise ArchiveError(f"{path.name} differs from its manifest; refused, not repaired")
    _, parquet = arrow_modules()
    bars = _bars_of(parquet.read_table(files["bars"]).to_pydict(), venue, instrument)
    points = _points_of(parquet.read_table(files["funding"]).to_pydict())
    return Klines(tuple(bars), tuple(points), dict(manifest["sha256"]))


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _bar_columns(bars: Sequence[FlowBar]) -> dict[str, list[Any]]:
    columns: dict[str, list[Any]] = {"start_ns": [b.start_ns for b in bars]}
    for name in (*PRICES, *SIZES):
        columns[name] = [_text(getattr(b, name)) for b in bars]
    columns["trade_count"] = [b.trade_count for b in bars]
    columns["gap"] = [b.gap.value for b in bars]
    columns["source"] = [b.source for b in bars]
    return columns


def _bars_of(columns: dict[str, list[Any]], venue: str, instrument: str) -> list[FlowBar]:
    def number(name: str, i: int) -> Decimal | None:
        value = columns[name][i]
        return None if value is None else Decimal(value)

    out = []
    for i, start in enumerate(columns["start_ns"]):
        out.append(
            FlowBar(
                venue=venue,
                instrument=instrument,
                start_ns=start,
                end_ns=start + HOUR_NS,
                open=number("open", i),
                high=number("high", i),
                low=number("low", i),
                close=number("close", i),
                volume_base=Decimal(columns["volume_base"][i]),
                volume_quote=Decimal(columns["volume_quote"][i]),
                taker_buy_base=Decimal(columns["taker_buy_base"][i]),
                taker_sell_base=Decimal(columns["taker_sell_base"][i]),
                trade_count=columns["trade_count"][i],
                large_trade_base=None,
                late_trades=0,
                gap=GapState(columns["gap"][i]),
                source=columns["source"][i],
            )
        )
    return out


def _point_columns(points: Sequence[DerivPoint]) -> dict[str, list[Any]]:
    return {
        "as_of_ns": [p.as_of_ns for p in points],
        "received_ns": [p.received_ns for p in points],
        "rate": [str(p.value) for p in points],
        "detail": [json.dumps(dict(p.detail), sort_keys=True) for p in points],
    }


def _points_of(columns: dict[str, list[Any]]) -> list[DerivPoint]:
    return [
        DerivPoint(
            Series.FUNDING,
            columns["as_of_ns"][i],
            columns["received_ns"][i],
            Decimal(columns["rate"][i]),
            tuple(sorted(json.loads(columns["detail"][i]).items())),
        )
        for i in range(len(columns["as_of_ns"]))
    ]
