from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.market_data.archive import SourceFile, write_flow_part
from trading_research_agents.market_data.bars import MarketDataError, quiet_bar
from trading_research_agents.market_data.budget import RequestBudget
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns
from trading_research_agents.market_data.ports import ArchiveFeed, BarFeed, BudgetStore
from trading_research_agents.market_data.record_config import RecordConfig, load_record_config

T0 = midnight_ns(date(2025, 7, 1))


def test_record_config_reads_the_table_with_its_defaults(tmp_path: Path) -> None:
    path = tmp_path / "data.toml"
    path.write_text(
        '[data.ohlcv]\nchain = ["ccxt"]\n\n'
        "[record]\n"
        'venue = "bybit"\n'
        'instruments = ["BTCUSDT", "ETHUSDT"]\n'
        'text = ["coindesk"]\n'
        "[record.large_trade_quote]\n"
        "BTCUSDT = 100000\n",
        encoding="utf-8",
    )
    cfg = load_record_config(path)
    assert cfg.venue == "bybit" and cfg.instruments == ("BTCUSDT", "ETHUSDT")
    assert cfg.streams == ("trades", "book", "liquidations") and cfg.book_sample_ms == 1000
    assert cfg.large_trade_quote == {"BTCUSDT": Decimal(100000)}
    assert cfg.context == ("BTCUSDT", "ETHUSDT") and cfg.text == ("coindesk",)
    assert cfg.markets == ("BTCUSDT", "ETHUSDT")


def test_record_config_refuses_an_unknown_key_with_the_closest_name() -> None:
    with pytest.raises(MarketDataError, match="did you mean book_sample_ms"):
        RecordConfig.from_table({"venue": "bybit", "instruments": ["X"], "book_sample": 500})
    with pytest.raises(MarketDataError, match="streams"):
        RecordConfig.from_table({"venue": "bybit", "instruments": ["X"], "streams": ["tape"]})
    with pytest.raises(MarketDataError, match="instruments"):
        RecordConfig.from_table({"venue": "bybit"})


def test_the_ports_are_met_by_the_budget_and_the_archive(tmp_path: Path) -> None:
    assert isinstance(RequestBudget(60), BudgetStore)
    hour = [
        quiet_bar(
            "bybit",
            "BTCUSDT",
            T0 + i * NS_PER_MINUTE,
            T0 + (i + 1) * NS_PER_MINUTE,
            source="stream:bybit",
            large_declared=False,
        )
        for i in range(60)
    ]
    pytest.importorskip("pyarrow")
    write_flow_part(hour, tmp_path, price_scale=2, qty_scale=3, sources=(SourceFile("s", None),))
    feed: BarFeed = ArchiveFeed(tmp_path)
    bars = feed.flow("bybit", "BTCUSDT", date(2025, 7, 1), date(2025, 7, 1))
    assert bars[:60] == hour and len(bars) == 1440
