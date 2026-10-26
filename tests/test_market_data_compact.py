from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.archive import (
    FLOW,
    ArchiveError,
    SourceFile,
    day_file,
    part_file,
    read_flow_range,
    verify_archive,
    write_flow_part,
)
from trading_research_agents.market_data.archive_book import BOOK, read_book_range, write_book_part
from trading_research_agents.market_data.archive_deriv import read_deriv_day, write_deriv_part
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.book import BookBar, BookBarBuilder, BookSample
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.compact import compact_day
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, NS_PER_S, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series
from trading_research_agents.market_data.gaps import read_gaps

pytest.importorskip("pyarrow")

DAY = date(2025, 7, 1)
T0 = midnight_ns(DAY)
HOUR = 60 * NS_PER_MINUTE
AFTER = T0 + DAY_NS + HOUR
STREAMED = (SourceFile("stream:bybit", None),)


def hour_of_bars(hour: int) -> list[FlowBar]:
    start = T0 + hour * HOUR
    made = FlowBarBuilder(
        venue="bybit", instrument="BTCUSDT", source="stream:bybit", start_ns=start
    )
    bars: list[FlowBar] = []
    for i in range(1, 60):
        bars.extend(
            made.push(
                Trade(
                    ts_ns=start + i * 60 * NS_PER_S + 5,
                    venue="bybit",
                    instrument="BTCUSDT",
                    price=Decimal("93530.10"),
                    qty=Decimal("0.010"),
                    side=Side.BUY,
                    trade_id=f"h{hour}-{i}",
                )
            )
        )
    bars.extend(made.finish(start + HOUR))
    return bars


def write_hours(root: Path, hours: range | list[int]) -> None:
    for hour in hours:
        write_flow_part(hour_of_bars(hour), root, price_scale=2, qty_scale=3, sources=STREAMED)


def test_compaction_joins_the_hours_into_one_verified_day(tmp_path: Path) -> None:
    write_hours(tmp_path, range(24))
    before = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    result = compact_day(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, now_ns=AFTER)
    assert result.written and result.removed == 24 and result.missing_minutes == 0
    assert day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY).is_file()
    assert not part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0).exists()
    assert not part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0).parent.exists()
    assert read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY) == before
    assert verify_archive(tmp_path).problems == ()


def test_compaction_fills_uncovered_hours_as_missing(tmp_path: Path) -> None:
    write_hours(tmp_path, [0, 1, 5])
    result = compact_day(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, now_ns=AFTER)
    assert result.missing_minutes == 21 * 60
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert bars[120].gap is GapState.MISSING and bars[300].gap is GapState.NONE
    marks = read_gaps(tmp_path, "bybit", "BTCUSDT")
    assert len(marks) == 21 and marks[0].start_ns == T0 + 2 * HOUR
    assert "not recorded" in marks[0].reason


def test_compaction_refuses_an_edited_part(tmp_path: Path) -> None:
    write_hours(tmp_path, [0, 1])
    path = part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 1)
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ArchiveError, match="differs"):
        compact_day(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, now_ns=AFTER)
    assert part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0).is_file()
    assert not day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY).exists()


def test_compaction_is_idempotent(tmp_path: Path) -> None:
    write_hours(tmp_path, [3])
    compact_day(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, now_ns=AFTER)
    path = day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY)
    first = path.read_bytes()
    again = compact_day(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, now_ns=AFTER)
    assert not again.written and again.removed == 0 and path.read_bytes() == first


def test_an_open_day_is_not_compacted(tmp_path: Path) -> None:
    write_hours(tmp_path, [0])
    with pytest.raises(ArchiveError, match="still open"):
        compact_day(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, now_ns=T0 + DAY_NS - 1)


def book_hour(hour: int) -> list[BookBar]:
    start = T0 + hour * HOUR
    made = BookBarBuilder(venue="bybit", instrument="BTCUSDT", start_ns=start)
    bars: list[BookBar] = []
    for s in range(0, 3600, 30):
        bars.extend(
            made.push(
                BookSample(
                    at_ns=start + s * NS_PER_S,
                    bids=((Decimal("100.0"), Decimal("1.000")),),
                    asks=((Decimal("100.1"), Decimal("2.000")),),
                )
            )
        )
    bars.extend(made.finish(start + HOUR, gap=GapState.NONE))
    return bars


def test_book_and_derivatives_compact_the_same_way(tmp_path: Path) -> None:
    write_book_part(book_hour(4), tmp_path, price_scale=1, qty_scale=3)
    start = T0 + 4 * HOUR
    point = DerivPoint(Series.FUNDING, start, start, Decimal("0.0001"))
    write_deriv_part(
        [point], tmp_path, venue="bybit", instrument="BTCUSDT", hour_start_ns=start, sources=()
    )
    book = compact_day(tmp_path, "bybit", "BTCUSDT", BOOK, DAY, now_ns=AFTER)
    deriv = compact_day(tmp_path, "bybit", "BTCUSDT", "deriv", DAY, now_ns=AFTER)
    assert book.written and book.missing_minutes == 23 * 60 and deriv.written
    assert read_book_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)[240:300] == book_hour(4)
    assert read_deriv_day(tmp_path, "bybit", "BTCUSDT", DAY) == [point]
    assert read_deriv_day(tmp_path, "bybit", "BTCUSDT", DAY + timedelta(days=1)) == []
