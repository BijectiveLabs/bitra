from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.archive import (
    FLOW,
    SourceFile,
    manifest_file,
    part_file,
    read_flow_range,
    verify_archive,
    write_flow_part,
)
from trading_research_agents.market_data.archive_book import (
    BOOK,
    read_book_range,
    write_book_part,
)
from trading_research_agents.market_data.archive_deriv import read_deriv_range, write_deriv_part
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.book import BookBar, BookBarBuilder, BookSample
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import NS_PER_MINUTE, NS_PER_S, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series

pytest.importorskip("pyarrow")

DAY = date(2025, 7, 1)
T0 = midnight_ns(DAY)
HOUR = 60 * NS_PER_MINUTE
STREAMED = (SourceFile("stream:bybit", None),)


def hour_of_bars(hour: int) -> list[FlowBar]:
    start = T0 + hour * HOUR
    made = FlowBarBuilder(
        venue="bybit", instrument="BTCUSDT", source="stream:bybit", start_ns=start
    )
    bars: list[FlowBar] = []
    for i in range(1, 90):
        bars.extend(
            made.push(
                Trade(
                    ts_ns=start + i * 37 * NS_PER_S,
                    venue="bybit",
                    instrument="BTCUSDT",
                    price=Decimal("93530.10") + Decimal(i % 5),
                    qty=Decimal("0.010"),
                    side=Side.BUY if i % 2 else Side.SELL,
                    trade_id=f"h{hour}-{i}",
                )
            )
        )
    bars.extend(made.finish(start + HOUR))
    return bars


def write_hour(root: Path, hour: int) -> list[FlowBar]:
    bars = hour_of_bars(hour)
    write_flow_part(bars, root, price_scale=2, qty_scale=3, sources=STREAMED)
    return bars


def test_an_hour_part_round_trips_and_is_verified(tmp_path: Path) -> None:
    bars = write_hour(tmp_path, 0)
    path = part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0)
    assert path == (
        tmp_path / "market" / "bybit" / "BTCUSDT" / "flow_1m" / "2025-07-01" / "part-00.parquet"
    )
    read = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert read[:60] == bars and len(read) == 1440
    assert verify_archive(tmp_path).problems == ()


def test_a_range_reads_parts_of_an_open_day_and_marks_the_rest_missing(tmp_path: Path) -> None:
    write_hour(tmp_path, 0)
    write_hour(tmp_path, 2)
    read = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert all(b.gap is GapState.MISSING for b in read[60:120])
    assert read[120].start_ns == T0 + 2 * HOUR and read[120].gap is GapState.NONE
    assert all(b.gap is GapState.MISSING for b in read[180:])


def test_a_part_holds_one_whole_hour(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="60"):
        write_flow_part(hour_of_bars(0)[:59], tmp_path, price_scale=2, qty_scale=3, sources=())


def book_hour(hour: int) -> list[BookBar]:
    start = T0 + hour * HOUR
    made = BookBarBuilder(venue="bybit", instrument="BTCUSDT", start_ns=start)
    bars: list[BookBar] = []
    for s in range(0, 3600, 7):
        bid = Decimal("93530.1") + Decimal(s % 3)
        bars.extend(
            made.push(
                BookSample(
                    at_ns=start + s * NS_PER_S,
                    bids=((bid, Decimal("1.250")), (bid - Decimal("5"), Decimal("2.000"))),
                    asks=((bid + Decimal("0.1"), Decimal("0.750")),),
                )
            )
        )
    bars.extend(made.finish(start + HOUR, gap=GapState.NONE))
    return bars


def test_book_minutes_round_trip_exactly(tmp_path: Path) -> None:
    bars = book_hour(1)
    write_book_part(bars, tmp_path, price_scale=1, qty_scale=3)
    read = read_book_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert read[60:120] == bars
    assert read[0].samples == 0 and read[0].gap is GapState.MISSING
    assert part_file(tmp_path, "bybit", "BTCUSDT", BOOK, DAY, 1).is_file()


def test_derivatives_of_an_hour_part_are_read_with_the_day(tmp_path: Path) -> None:
    start = T0 + 3 * HOUR
    points = [
        DerivPoint(Series.MARK, start + 60 * NS_PER_S, start, Decimal("93530.5")),
        DerivPoint(
            Series.LIQUIDATION,
            start + 90 * NS_PER_S,
            start,
            Decimal("0.5"),
            (("price", "93500"), ("side", "sell")),
        ),
    ]
    write_deriv_part(
        points, tmp_path, venue="bybit", instrument="BTCUSDT", hour_start_ns=start, sources=()
    )
    assert read_deriv_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY) == sorted(
        points, key=lambda p: (p.series.value, p.as_of_ns)
    )


def test_verify_names_an_edited_part(tmp_path: Path) -> None:
    write_hour(tmp_path, 0)
    path = part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0)
    path.write_bytes(path.read_bytes() + b"x")
    assert any("differs" in p for p in verify_archive(tmp_path).problems)
    assert manifest_file(path).is_file()
