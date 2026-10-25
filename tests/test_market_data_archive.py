from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.archive import (
    DERIV,
    FLOW,
    ArchiveError,
    DayManifest,
    SourceFile,
    day_file,
    manifest_file,
    read_deriv_day,
    read_deriv_range,
    read_flow_day,
    read_flow_range,
    verify_archive,
    write_deriv_day,
    write_flow_day,
)
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import DAY_NS, NS_PER_S, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series, detail_of
from trading_research_agents.market_data.gaps import GapMark, read_gaps, record_gap

pytest.importorskip("pyarrow")

DAY = date(2025, 7, 1)
SOURCE = (SourceFile("BTCUSDT-2025-07-01.parquet", "ab" * 32),)
API = (SourceFile("ccxt:bybit", None),)


def day_bars(day: date = DAY) -> list[FlowBar]:
    start = midnight_ns(day)
    made = FlowBarBuilder(
        venue="bybit",
        instrument="BTCUSDT",
        source="archive:bybit",
        start_ns=start,
        large_trade_quote=Decimal("1000"),
    )
    bars: list[FlowBar] = []
    for i in range(1, 500):
        bars.extend(
            made.push(
                Trade(
                    ts_ns=start + i * 97 * NS_PER_S,
                    venue="bybit",
                    instrument="BTCUSDT",
                    price=Decimal("93530.15") + Decimal(i % 9),
                    qty=Decimal("0.013") * (1 + i % 4),
                    side=Side.BUY if i % 2 else Side.SELL,
                    trade_id=f"t{i}",
                )
            )
        )
    bars.extend(made.finish(start + DAY_NS))
    return bars


def write(root: Path, bars: list[FlowBar] | None = None) -> DayManifest:
    return write_flow_day(
        bars or day_bars(),
        root,
        day=DAY,
        price_scale=2,
        qty_scale=3,
        sources=SOURCE,
        large_trade_quote=Decimal("1000"),
    )


def test_a_day_round_trips_exactly(tmp_path: Path) -> None:
    bars = day_bars()
    manifest = write(tmp_path, bars)
    assert manifest.rows == 1440 and manifest.stream == FLOW
    assert manifest.large_trade_quote == "1000"
    assert read_flow_day(tmp_path, "bybit", "BTCUSDT", DAY) == bars
    path = day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY)
    assert path == tmp_path / "market" / "bybit" / "BTCUSDT" / "flow_1m" / "2025-07-01.parquet"
    assert DayManifest.read(manifest_file(path)) == manifest


def test_writing_the_same_day_twice_gives_the_same_bytes(tmp_path: Path) -> None:
    write(tmp_path)
    path = day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY)
    first = path.read_bytes()
    write(tmp_path)
    assert path.read_bytes() == first


def test_an_edited_day_is_refused(tmp_path: Path) -> None:
    write(tmp_path)
    path = day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY)
    path.write_bytes(path.read_bytes()[:-8] + b"\x00" * 8)
    with pytest.raises(ArchiveError, match="differs from its manifest"):
        read_flow_day(tmp_path, "bybit", "BTCUSDT", DAY)
    check = verify_archive(tmp_path)
    assert check.days == 1 and any("differs" in p for p in check.problems)


def test_a_day_is_complete_or_not_written(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="1440"):
        write(tmp_path, day_bars()[:-1])
    assert not day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY).exists()


def test_a_value_that_does_not_fit_its_scale_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="scale"):
        write_flow_day(day_bars(), tmp_path, day=DAY, price_scale=1, qty_scale=3, sources=SOURCE)
    with pytest.raises(ArchiveError, match="38"):
        write_flow_day(day_bars(), tmp_path, day=DAY, price_scale=20, qty_scale=19, sources=SOURCE)


def test_a_range_reads_an_absent_day_as_missing(tmp_path: Path) -> None:
    write(tmp_path)
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, date(2025, 7, 2))
    assert len(bars) == 2880
    assert bars[1439].gap is not GapState.MISSING
    assert all(b.gap is GapState.MISSING and b.empty for b in bars[1440:])


def test_verify_names_a_file_without_a_manifest(tmp_path: Path) -> None:
    write(tmp_path)
    manifest_file(day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY)).unlink()
    check = verify_archive(tmp_path)
    assert any("no manifest" in p for p in check.problems)
    assert verify_archive(tmp_path / "empty").problems == ()


def deriv_points(day: date = DAY) -> list[DerivPoint]:
    start = midnight_ns(day)
    return [
        DerivPoint(
            Series.FUNDING,
            start + 8 * 3600 * NS_PER_S,
            start,
            Decimal("-0.00012345"),
            detail_of(settled="true"),
        ),
        DerivPoint(
            Series.OPEN_INTEREST,
            start + 300 * NS_PER_S,
            start,
            Decimal("51234.567"),
            detail_of(measured="amount", timeframe="5m"),
        ),
        DerivPoint(Series.MARK, start + 60 * NS_PER_S, start, Decimal("93530.5")),
    ]


def test_derivatives_round_trip_exactly_and_in_order(tmp_path: Path) -> None:
    manifest = write_deriv_day(
        deriv_points(), tmp_path, venue="bybit", instrument="BTCUSDT", day=DAY, sources=API
    )
    assert manifest.stream == DERIV and manifest.rows == 3
    back = read_deriv_day(tmp_path, "bybit", "BTCUSDT", DAY)
    assert back == sorted(deriv_points(), key=lambda p: (p.series.value, p.as_of_ns))
    assert str(back[0].value) == "-0.00012345"


def test_a_point_outside_the_day_is_refused(tmp_path: Path) -> None:
    late = DerivPoint(Series.MARK, midnight_ns(date(2025, 7, 2)), 0, Decimal("1"))
    with pytest.raises(ArchiveError, match="outside"):
        write_deriv_day([late], tmp_path, venue="bybit", instrument="BTCUSDT", day=DAY, sources=API)


def test_an_absent_derivatives_day_reads_empty(tmp_path: Path) -> None:
    assert read_deriv_day(tmp_path, "bybit", "BTCUSDT", DAY) == []
    write_deriv_day(
        deriv_points(), tmp_path, venue="bybit", instrument="BTCUSDT", day=DAY, sources=API
    )
    assert len(read_deriv_range(tmp_path, "bybit", "BTCUSDT", DAY, date(2025, 7, 3))) == 3


def test_gap_marks_are_kept_and_read_back(tmp_path: Path) -> None:
    mark = GapMark(
        "bybit",
        "BTCUSDT",
        FLOW,
        midnight_ns(DAY),
        midnight_ns(DAY) + DAY_NS,
        "archive unavailable: HTTP 404",
    )
    record_gap(tmp_path, mark)
    assert read_gaps(tmp_path, "bybit", "BTCUSDT") == [mark]
    with pytest.raises(ValueError, match="reason"):
        GapMark("bybit", "BTCUSDT", FLOW, 1, 2, " ")
