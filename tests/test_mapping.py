"""Any CSV through a declared column map: the same gate, no venue in the loop."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agent.data import (
    ArchiveFormatError,
    ColumnMap,
    MappingError,
    Rejection,
    Side,
    Trade,
    audit,
    load_mapping,
    mapped_trades,
    parse_mapping,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

VENDOR_HEADER = "timestamp,price,amount,side,id"
VENDOR_ROWS = [
    "1735689600097,93530.00,0.003,sell,a-1",
    "1735689600144,93530.00,0.991,SELL,a-2",
    "1735689600266,93531.50,0.010,buy,a-3",
]


def vendor() -> ColumnMap:
    return load_mapping(EXAMPLES / "mapping.toml")


def test_the_example_map_loads_and_names_every_field() -> None:
    m = vendor()
    assert m.source == "vendor-x" and m.ts == "timestamp" and m.ts_unit == "ms"
    assert (m.price, m.qty, m.trade_id, m.side) == ("price", "amount", "id", "side")
    assert m.buy_values == ("buy", "BUY") and m.sell_values == ("sell", "SELL")
    assert m.has_header and m.delimiter == "," and m.seq is None


def test_mapped_rows_become_canonical_trades() -> None:
    out = list(mapped_trades([VENDOR_HEADER, *VENDOR_ROWS], vendor(), instrument="BTCUSDT"))
    assert all(isinstance(r, Trade) for r in out)
    first, _, third = (r for r in out if isinstance(r, Trade))
    assert first.ts_ns == 1735689600097000000
    assert first.venue == "vendor-x" and first.instrument == "BTCUSDT"
    assert first.price == Decimal("93530.00") and first.qty == Decimal("0.003")
    assert first.side is Side.SELL and third.side is Side.BUY
    assert first.trade_id == "a-1" and first.seq is None


def test_the_mapped_stream_passes_the_same_audit() -> None:
    rows = mapped_trades([VENDOR_HEADER, *VENDOR_ROWS], vendor(), instrument="BTCUSDT")
    report = audit(
        rows,
        max_gap=timedelta(minutes=5),
        max_rejected_fraction=Decimal("0.001"),
        expected_venue="vendor-x",
        expected_instrument="BTCUSDT",
    )
    assert report.passed and report.rows_accepted == 3 and report.venue == "vendor-x"


def test_column_order_in_the_file_does_not_matter() -> None:
    header = "id,side,amount,price,timestamp"
    rows = ["a-1,buy,0.5,100.0,1735689600097"]
    (trade,) = mapped_trades([header, *rows], vendor(), instrument="X")
    assert isinstance(trade, Trade)
    assert trade.trade_id == "a-1" and trade.price == Decimal("100.0")


def test_a_missing_column_stops_the_file() -> None:
    with pytest.raises(ArchiveFormatError, match="amount"):
        list(mapped_trades(["timestamp,price,side,id", "1,2,buy,x"], vendor(), instrument="X"))
    with pytest.raises(ArchiveFormatError, match="empty"):
        list(mapped_trades([], vendor(), instrument="X"))


def test_bad_rows_are_rejected_with_their_line_numbers() -> None:
    rows = [
        VENDOR_HEADER,
        "1735689600097,93530.00,0.003,short,a-1",
        "not-a-time,93530.00,0.003,buy,a-2",
        "1735689600097,93530.00,buy,a-3",
    ]
    out = list(mapped_trades(rows, vendor(), instrument="X"))
    assert all(isinstance(r, Rejection) for r in out)
    lines = [(r.line, r.reason) for r in out if isinstance(r, Rejection)]
    assert lines[0][0] == 2 and "unknown side" in lines[0][1]
    assert lines[1][0] == 3
    assert lines[2][0] == 4 and "columns" in lines[2][1]


def test_headerless_files_take_their_columns_from_the_map() -> None:
    m = parse_mapping(
        {
            "mapping": {
                "source": "recorder",
                "ts": "t",
                "ts_unit": "us",
                "price": "p",
                "qty": "q",
                "side": {"column": "s", "buy": ["B"], "sell": ["S"]},
                "trade_id": "i",
                "seq": "n",
                "has_header": False,
                "delimiter": ";",
                "columns": ["n", "t", "p", "q", "s", "i"],
            }
        }
    )
    (trade,) = mapped_trades(["7;1735689600097123;100.5;2;B;x-7"], m, instrument="X")
    assert isinstance(trade, Trade)
    assert trade.ts_ns == 1735689600097123000 and trade.seq == 7 and trade.side is Side.BUY


def test_iso8601_needs_a_timezone() -> None:
    m = ColumnMap(source="v", ts="t", ts_unit="iso8601", price="p", qty="q", side="s", trade_id="i")
    header = "t,p,q,s,i"
    good = "2025-01-01T00:00:00.5Z,1,1,buy,a"
    bad = "2025-01-01T00:00:00.5,1,1,buy,b"
    out = list(mapped_trades([header, good, bad], m, instrument="X"))
    assert isinstance(out[0], Trade) and out[0].ts_ns == 1735689600500000000
    assert isinstance(out[1], Rejection) and "timezone" in out[1].reason


def test_rows_for_another_instrument_are_rejected_when_the_file_carries_one() -> None:
    m = parse_mapping(
        {
            "mapping": {
                "source": "v",
                "ts": "t",
                "ts_unit": "s",
                "price": "p",
                "qty": "q",
                "side": "s",
                "trade_id": "i",
                "instrument": "sym",
            }
        }
    )
    rows = ["t,p,q,s,i,sym", "1,1,1,buy,a,BTCUSDT", "2,1,1,buy,b,ETHUSDT"]
    out = list(mapped_trades(rows, m, instrument="BTCUSDT"))
    assert isinstance(out[0], Trade)
    assert isinstance(out[1], Rejection) and "ETHUSDT" in out[1].reason


def full(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": "v",
        "ts": "t",
        "ts_unit": "ms",
        "price": "p",
        "qty": "q",
        "side": "s",
        "trade_id": "i",
    }
    base.update(overrides)
    return {"mapping": base}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "table is required"),
        ({"mapping": {"source": "v", "ts": "t"}}, "side needs a column"),
        (full(price=""), "column is required"),
        (full(ts_unit="weeks"), "ts_unit"),
        (full(side={"column": "s", "buy": ["x"], "sell": ["x"]}), "both buy and sell"),
        (full(has_header=False), "headerless"),
        (full(source=" "), "names its source"),
    ],
)
def test_an_incomplete_map_is_refused(raw: dict[str, object], message: str) -> None:
    with pytest.raises(MappingError, match=message):
        parse_mapping(raw)


def test_a_map_file_that_is_not_toml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "map.toml"
    path.write_text("[mapping\n", encoding="utf-8")
    with pytest.raises(MappingError, match="not valid TOML"):
        load_mapping(path)
