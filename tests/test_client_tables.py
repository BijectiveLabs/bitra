"""A Client's own Parquet files, read through the same column map and series spec as a CSV."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data import Rejection, Side, Trade, mapped_table, parse_mapping
from trading_research_agents.data.series import SeriesSpec, Threshold, read_series

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

NS = 1735689600_097_400_123  # a nanosecond tail a datetime would drop

MAP = parse_mapping(
    {
        "mapping": {
            "source": "vendor-x",
            "ts": "time",
            "ts_unit": "ns",
            "price": "px",
            "qty": "size",
            "trade_id": "id",
            "side": {"column": "aggressor", "buy": ["B"], "sell": ["S"]},
        }
    }
)


def trades_file(path: Path, *, zone: str | None = "UTC") -> Path:
    table = pa.table(
        {
            "time": pa.array([NS, NS + 1, NS + 2], type=pa.timestamp("ns", tz=zone)),
            "px": pa.array([Decimal("93530.10"), Decimal("93530.20"), None], pa.decimal128(12, 2)),
            "size": pa.array([0.003, 0.25, 0.1], pa.float64()),
            "aggressor": ["B", "S", "B"],
            "id": ["a", "b", "c"],
        }
    )
    pq.write_table(table, path)
    return path


def test_a_parquet_of_trades_is_read_exactly_and_a_bad_row_is_rejected_by_number(
    tmp_path: Path,
) -> None:
    rows = list(mapped_table(trades_file(tmp_path / "t.parquet"), MAP, instrument="BTCUSDT"))
    trades = [r for r in rows if isinstance(r, Trade)]
    assert [t.ts_ns for t in trades] == [NS, NS + 1]
    assert trades[0].price == Decimal("93530.10") and trades[1].qty == Decimal("0.25")
    assert [t.side for t in trades] == [Side.BUY, Side.SELL] and trades[0].venue == "vendor-x"
    # The third row's price is null: rejected with its row number, not defaulted.
    assert [(r.line, r.raw.split(",")[0]) for r in rows if isinstance(r, Rejection)] == [
        (3, str(NS + 2))
    ]


def test_a_time_without_a_zone_is_refused_rather_than_read_as_utc(tmp_path: Path) -> None:
    rows = list(mapped_table(trades_file(tmp_path / "t.parquet", zone=None), MAP, instrument="X"))
    assert rows and all(isinstance(r, Rejection) for r in rows)


def test_a_series_can_be_a_parquet_with_a_date_column(tmp_path: Path) -> None:
    pq.write_table(
        pa.table({"day": [date(2026, 10, 1), date(2026, 10, 2)], "value": [4.25, 4.5]}),
        tmp_path / "rates.parquet",
    )
    spec = SeriesSpec(
        name="rates",
        file="rates.parquet",
        ts="day",
        ts_unit="iso8601",
        value="value",
        unit="percent",
        publish_lag=timedelta(days=1),
        source="vendor-x",
        threshold=Threshold(),
    )
    series = read_series(spec, tmp_path)
    assert [p.value for p in series.points] == [Decimal("4.25"), Decimal("4.5")]
    # Read as UTC midnight, then moved by the declared publication lag.
    assert series.points[0].published_ns - series.points[0].observed_ns == 86_400 * 10**9
