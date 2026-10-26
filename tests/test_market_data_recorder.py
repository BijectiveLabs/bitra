from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.data.series import Event
from trading_research_agents.market_data.archive import (
    FLOW,
    day_file,
    part_file,
    read_flow_range,
)
from trading_research_agents.market_data.archive_book import BOOK, read_book_range
from trading_research_agents.market_data.archive_deriv import read_deriv_range
from trading_research_agents.market_data.bars import GapState
from trading_research_agents.market_data.book import BookSample
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, NS_PER_S, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series, detail_of
from trading_research_agents.market_data.gaps import read_gaps
from trading_research_agents.market_data.record_config import RecordConfig
from trading_research_agents.market_data.recorder import Recorder, Scales
from trading_research_agents.market_data.text import read_text_range

pytest.importorskip("pyarrow")

DAY = date(2025, 7, 1)
T0 = midnight_ns(DAY)
M = NS_PER_MINUTE
HOUR = 60 * M
CONFIG = RecordConfig.from_table(
    {"venue": "bybit", "instruments": ["BTCUSDT"], "text": ["coindesk"]}
)
SCALES = {"BTCUSDT": Scales(price=2, qty=3)}


def trade(at_ns: int, ident: str, price: str = "93530.10") -> Trade:
    return Trade(
        ts_ns=at_ns,
        venue="bybit",
        instrument="BTCUSDT",
        price=Decimal(price),
        qty=Decimal("0.010"),
        side=Side.BUY,
        trade_id=ident,
    )


def recorder(root: Path, start_ns: int = T0) -> Recorder:
    return Recorder(CONFIG, root, scales=SCALES, now_ns=start_ns)


def events(root: Path) -> list[dict[str, Any]]:
    path = root / "market" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_trades(rec: Recorder, start_ns: int, minutes: int) -> None:
    for i in range(minutes):
        at = start_ns + i * M + 7 * NS_PER_S
        rec.on_trades("BTCUSDT", [trade(at, f"t{at}")], received_ns=at + 50_000_000)
        rec.tick(at + 30 * NS_PER_S)


def test_trades_become_hour_parts_through_the_one_builder(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    run_trades(rec, T0, 60)
    rec.tick(T0 + HOUR + 6 * NS_PER_S)
    assert part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0).is_file()
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)[:60]
    assert [b.trade_count for b in bars] == [1] * 60
    assert {b.gap for b in bars} == {GapState.NONE} and bars[0].source == "stream:bybit"


def test_a_disconnect_marks_the_minute_partial_and_records_the_gap(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    run_trades(rec, T0, 10)
    rec.on_trades("BTCUSDT", [trade(T0 + 10 * M + 5 * NS_PER_S, "x")], T0 + 10 * M + 6 * NS_PER_S)
    rec.on_disconnect("BTCUSDT", "trades", T0 + 10 * M + 30 * NS_PER_S, "reset by peer")
    rec.tick(T0 + 12 * M)
    rec.on_reconnect("BTCUSDT", "trades", T0 + 13 * M + 10 * NS_PER_S)
    run_trades(rec, T0 + 14 * M, 46)
    rec.tick(T0 + HOUR + 6 * NS_PER_S)
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert bars[10].gap is GapState.PARTIAL and bars[10].trade_count == 1
    assert bars[11].gap is GapState.MISSING and bars[12].gap is GapState.MISSING
    assert bars[13].gap is GapState.PARTIAL and bars[14].gap is GapState.NONE
    (mark,) = [g for g in read_gaps(tmp_path, "bybit", "BTCUSDT") if g.stream == "trades"]
    assert (mark.start_ns, mark.end_ns) == (
        T0 + 10 * M + 30 * NS_PER_S,
        T0 + 13 * M + 10 * NS_PER_S,
    )
    assert "reset by peer" in mark.reason
    said = [e["event"] for e in events(tmp_path)]
    assert "disconnected" in said and "connected" in said


def test_a_trade_after_its_hour_is_written_is_counted_not_written(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    run_trades(rec, T0, 60)
    rec.tick(T0 + HOUR + 6 * NS_PER_S)
    path = part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 0)
    first = path.read_bytes()
    rec.on_trades("BTCUSDT", [trade(T0 + 59 * M + 50 * NS_PER_S, "late")], T0 + HOUR + 7 * NS_PER_S)
    rec.tick(T0 + HOUR + 8 * NS_PER_S)
    assert path.read_bytes() == first
    late = [e for e in events(tmp_path) if e["event"] == "late_trades"]
    assert late and late[-1]["count"] == 1


def test_a_closed_day_is_compacted_on_the_next_tick(tmp_path: Path) -> None:
    start = T0 + 23 * HOUR
    rec = recorder(tmp_path, start)
    run_trades(rec, start, 60)
    rec.tick(T0 + DAY_NS + 10 * NS_PER_S)
    assert day_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY).is_file()
    assert not part_file(tmp_path, "bybit", "BTCUSDT", FLOW, DAY, 23).exists()
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert bars[0].gap is GapState.MISSING and bars[23 * 60].trade_count == 1
    assert any(e["event"] == "compacted" for e in events(tmp_path))


def test_skew_above_a_second_is_an_event(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    at = T0 + 5 * NS_PER_S
    rec.on_trades("BTCUSDT", [trade(at, "slow")], received_ns=at + 1_500_000_000)
    rec.tick(T0 + M + 6 * NS_PER_S)
    skew = [e for e in events(tmp_path) if e["event"] == "skew"]
    assert skew and skew[0]["latency_max_ms"] == 1500


def test_stop_flushes_open_minutes_as_partial(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    run_trades(rec, T0, 5)
    rec.on_trades("BTCUSDT", [trade(T0 + 5 * M + 10 * NS_PER_S, "s")], T0 + 5 * M + 11 * NS_PER_S)
    rec.stop(T0 + 5 * M + 30 * NS_PER_S)
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert {b.gap for b in bars[:5]} == {GapState.NONE}
    assert bars[5].gap is GapState.PARTIAL and bars[5].trade_count == 1
    assert {b.gap for b in bars[6:60]} == {GapState.MISSING}
    assert any(e["event"] == "stopped" for e in events(tmp_path))


def test_a_restart_in_the_same_hour_keeps_what_was_recorded(tmp_path: Path) -> None:
    first = recorder(tmp_path)
    run_trades(first, T0, 5)
    first.stop(T0 + 5 * M + 30 * NS_PER_S)
    second = recorder(tmp_path, T0 + 20 * M)
    run_trades(second, T0 + 20 * M, 40)
    second.tick(T0 + HOUR + 6 * NS_PER_S)
    bars = read_flow_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert [b.trade_count for b in bars[:5]] == [1] * 5
    assert {b.gap for b in bars[6:20]} == {GapState.MISSING}
    assert [b.trade_count for b in bars[20:60]] == [1] * 40


def test_book_points_and_text_are_kept_beside_the_flow(tmp_path: Path) -> None:
    rec = recorder(tmp_path)
    for s in range(0, 3600, 10):
        at = T0 + s * NS_PER_S
        rec.on_book(
            "BTCUSDT",
            BookSample(
                at_ns=at,
                bids=((Decimal("100.00"), Decimal("1.000")),),
                asks=((Decimal("100.10"), Decimal("1.000")),),
            ),
        )
        rec.tick(at)
    rec.on_points(
        "BTCUSDT",
        [
            DerivPoint(Series.OPEN_INTEREST, T0 + 5 * M, T0 + 6 * M, Decimal("61797.182")),
            DerivPoint(
                Series.LIQUIDATION,
                T0 + 7 * M,
                T0 + 7 * M,
                Decimal("0.5"),
                detail_of(liquidated="long"),
            ),
        ],
    )
    rec.on_text("coindesk", [Event(T0, "exchange halts withdrawals", "coindesk")], T0 + 3 * M)
    rec.tick(T0 + HOUR + 10 * M)
    assert part_file(tmp_path, "bybit", "BTCUSDT", BOOK, DAY, 0).is_file()
    book = read_book_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)
    assert book[0].samples == 6 and book[59].samples == 6
    assert len(read_deriv_range(tmp_path, "bybit", "BTCUSDT", DAY, DAY)) == 2
    (said,) = read_text_range(tmp_path, "coindesk", DAY, DAY)
    assert said.first_seen_ns == T0 + 3 * M
