from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agent.data import Side, Trade
from trading_research_agent.data.parquet import (
    ParquetError,
    read_scales,
    read_trades,
    write_trades,
)

pytest.importorskip("pyarrow")

BASE_NS = 1735689600_097_400_123  # a nanosecond tail that a datetime would silently drop


def trade(i: int, **kw: object) -> Trade:
    base: dict[str, object] = {
        "ts_ns": BASE_NS + i,
        "venue": "bybit",
        "instrument": "BTCUSDT",
        "price": Decimal("93530.00"),
        "qty": Decimal("0.003"),
        "side": Side.SELL if i % 2 else Side.BUY,
        "trade_id": f"t-{i}",
        "seq": None if i % 3 == 0 else 1000 + i,
    }
    base.update(kw)
    return Trade(**base)  # type: ignore[arg-type]


def test_round_trip_is_exact_including_nanoseconds_and_scale(tmp_path: Path) -> None:
    rows = [trade(i) for i in range(2_500)]
    result = write_trades(
        rows, tmp_path / "btc.parquet", price_scale=2, qty_scale=3, batch_rows=1_000
    )
    assert result.rows == 2_500
    assert read_scales(result.path) == (2, 3)
    back = list(read_trades(result.path, batch_rows=700))
    assert back == rows
    assert str(back[0].price) == "93530.00" and str(back[0].qty) == "0.003"
    assert back[0].ts_ns == BASE_NS
    assert back[0].seq is None and back[1].seq == 1001


def test_a_value_that_does_not_fit_the_declared_scale_is_refused(tmp_path: Path) -> None:
    rows = [trade(0), trade(1, price=Decimal("93530.005"))]
    with pytest.raises(ParquetError, match="cannot be stored exactly at scale 2"):
        write_trades(rows, tmp_path / "btc.parquet", price_scale=2, qty_scale=3)
    assert not (tmp_path / "btc.parquet").exists()
    assert not (tmp_path / "btc.parquet.part").exists()


def test_a_wider_scale_is_padded_not_rounded(tmp_path: Path) -> None:
    rows = [trade(0, price=Decimal("93530.5"))]
    write_trades(rows, tmp_path / "btc.parquet", price_scale=4, qty_scale=8)
    back = next(read_trades(tmp_path / "btc.parquet"))
    assert back.price == Decimal("93530.5") and str(back.price) == "93530.5000"
    assert str(back.qty) == "0.00300000"


def test_foreign_parquet_files_are_refused(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    foreign = tmp_path / "foreign.parquet"
    pq.write_table(pa.table({"x": [1, 2]}), foreign)
    with pytest.raises(ParquetError, match="not a trading-research-agent dataset"):
        read_scales(foreign)
    with pytest.raises(ParquetError, match="price_scale must be between"):
        write_trades([trade(0)], tmp_path / "bad.parquet", price_scale=40, qty_scale=3)
