"""Live sources: provenance on every value, failover in the chain, nothing stale in silence."""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.data import Side, Trade
from trading_research_agents.data.candles import CandleError, candles_of
from trading_research_agents.data.schema import Candle
from trading_research_agents.data.sources import (
    BookLevel,
    ChainExhaustedError,
    DataConfigError,
    Fetched,
    FileSource,
    OrderBook,
    SourceChain,
    SourceError,
    build,
    load_data_config,
    parse_duration,
    schemes,
)
from trading_research_agents.data.sources.registry import UnknownSourceError

MINUTE = 60 * 1_000_000_000
NOW = 1_800_000_000.0


def candle(start_s: int, close: str = "100", volume: str = "1") -> Candle:
    return Candle(
        start_ns=start_s * 1_000_000_000,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal(volume),
        trades=1,
    )


class Fake:
    """A source under our control: it answers, it fails, or it answers with old data."""

    def __init__(
        self,
        name: str,
        *,
        kind: str = "ohlcv",
        age_s: float = 1.0,
        fail: str = "",
        tier: str = "free",
    ) -> None:
        self._name = name
        self.kind = kind
        self.age_s = age_s
        self.fail = fail
        self._tier = tier
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> str:
        return self._tier

    def kinds(self) -> tuple[str, ...]:
        return (self.kind,)

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        self.calls += 1
        if self.fail == "source":
            raise SourceError("RateLimitExceeded: slow down")
        if self.fail == "shape":
            raise KeyError("fundingRate")
        return Fetched(
            kind=kind,
            instrument=instrument,
            value=[candle(1)],
            source=self._name,
            tier=self._tier,
            fetched_at=NOW,
            as_of_ns=int((NOW - self.age_s) * 1_000_000_000),
            detail={"options": sorted(options)},
        )


def test_a_value_carries_its_age_and_refuses_to_exist_without_provenance() -> None:
    fetched = Fake("ccxt:bybit").fetch("ohlcv", "BTCUSDT", limit=10)
    assert fetched.age_s(NOW) == 1.0 and fetched.record(NOW)["source"] == "ccxt:bybit"
    assert fetched.record(NOW)["tier"] == "free" and fetched.record(NOW)["age_s"] == 1.0
    with pytest.raises(ValueError, match="kind is one of"):
        Fetched("candles", "BTCUSDT", [], "s", "free", NOW, 1)
    with pytest.raises(ValueError, match="tier is one of"):
        Fetched("ohlcv", "BTCUSDT", [], "s", "cheap", NOW, 1)
    with pytest.raises(ValueError, match="names itself"):
        Fetched("ohlcv", "BTCUSDT", [], "  ", "free", NOW, 1)
    with pytest.raises(ValueError, match="both clocks"):
        Fetched("ohlcv", "BTCUSDT", [], "s", "free", NOW, 0)


def test_a_book_is_two_sided_uncrossed_and_knows_its_spread() -> None:
    book = OrderBook(
        bids=(BookLevel(Decimal("100"), Decimal("2")),),
        asks=(BookLevel(Decimal("100.1"), Decimal("3")),),
    )
    assert book.mid == Decimal("100.05")
    assert book.record()["spread_bps"] == "10.00" and book.record()["levels"] == 1
    with pytest.raises(ValueError, match="both sides"):
        OrderBook(bids=(), asks=(BookLevel(Decimal("1"), Decimal("1")),))
    with pytest.raises(ValueError, match="crossed"):
        OrderBook(
            bids=(BookLevel(Decimal("101"), Decimal("1")),),
            asks=(BookLevel(Decimal("100"), Decimal("1")),),
        )
    with pytest.raises(ValueError, match="positive price and size"):
        OrderBook(
            bids=(BookLevel(Decimal("100"), Decimal("0")),),
            asks=(BookLevel(Decimal("101"), Decimal("1")),),
        )


def test_the_chain_walks_in_order_records_every_attempt_and_refuses_when_exhausted() -> None:
    first, second = Fake("a", fail="source"), Fake("b")
    chain = SourceChain(kind="ohlcv", sources=(first, second), ttl_s=30, clock=lambda: NOW)
    result = chain.fetch("BTCUSDT", limit=5)
    assert result.ok and result.fetched is not None and result.fetched.source == "b"
    assert [(a.source, a.outcome) for a in result.attempts] == [("a", "error"), ("b", "ok")]
    assert first.calls == 1 and second.calls == 1
    assert result.fetched.detail["options"] == ["limit"] and not result.stale

    # A vendor answering with a shape we cannot read is a failed source, not a crash.
    broken = SourceChain(
        kind="ohlcv", sources=(Fake("c", fail="shape"), Fake("d")), ttl_s=30, clock=lambda: NOW
    )
    assert broken.fetch("BTCUSDT").fetched is not None

    stale_only = SourceChain(
        kind="ohlcv", sources=(Fake("old", age_s=600),), ttl_s=30, clock=lambda: NOW
    )
    with pytest.raises(ChainExhaustedError, match="no source inside 30"):
        stale_only.fetch("BTCUSDT")

    allowed = SourceChain(
        kind="ohlcv",
        sources=(Fake("older", age_s=900), Fake("old", age_s=600)),
        ttl_s=30,
        on_exhausted="use_stale_with_flag",
        clock=lambda: NOW,
    )
    result = allowed.fetch("BTCUSDT")
    assert result.stale and result.fetched is not None and result.fetched.source == "old"
    assert [a.outcome for a in result.attempts] == ["stale", "stale"]


def test_a_chain_refuses_to_be_built_wrong() -> None:
    with pytest.raises(ValueError, match="names no source"):
        SourceChain(kind="ohlcv", sources=(), ttl_s=30)
    with pytest.raises(ValueError, match="ttl is positive"):
        SourceChain(kind="ohlcv", sources=(Fake("a"),), ttl_s=0)
    with pytest.raises(ValueError, match="on_exhausted is one of"):
        SourceChain(kind="ohlcv", sources=(Fake("a"),), ttl_s=30, on_exhausted="guess")
    with pytest.raises(ValueError, match="does not serve funding"):
        SourceChain(kind="funding", sources=(Fake("a", kind="ohlcv"),), ttl_s=30)


def test_candles_fold_a_tape_and_keep_decimal_prices() -> None:
    rows = [
        Trade(i * MINUTE + 1, "v", "BTCUSDT", Decimal(p), Decimal("0.5"), Side.BUY, str(i))
        for i, p in enumerate(["100", "101", "99", "103"])
    ]
    folded = candles_of(rows, MINUTE)
    assert len(folded) == 4 and folded[0].close == Decimal("100")
    assert folded[0].volume == Decimal("0.5") and folded[0].trades == 1
    one = candles_of(rows, 4 * MINUTE)
    assert len(one) == 1 and one[0].high == Decimal("103") and one[0].low == Decimal("99")
    assert one[0].volume == Decimal("2.0") and one[0].trades == 4
    with pytest.raises(CandleError, match="out of order"):
        candles_of(list(reversed(rows)), MINUTE)
    with pytest.raises(CandleError, match="positive"):
        candles_of(rows, 0)
    with pytest.raises(ValueError, match="inconsistent"):
        Candle(MINUTE, Decimal("10"), Decimal("9"), Decimal("8"), Decimal("9"), Decimal("1"))


def test_the_file_source_answers_from_the_project_without_a_network(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agents.data.parquet import write_trades

    data = tmp_path / "data"
    data.mkdir()
    rows = [
        Trade(
            1_700_000_000_000_000_000 + i * MINUTE,
            "bybit",
            "BTCUSDT",
            Decimal("93000"),
            Decimal("0.01"),
            Side.BUY,
            str(i),
        )
        for i in range(5)
    ]
    write_trades(rows, data / "BTCUSDT-2023-11-14.parquet", price_scale=2, qty_scale=3)
    (data / "BTCUSDT-funding.json").write_text(
        '{"instrument": "BTCUSDT", "interval_s": 28800, "rates": '
        '{"1700000000000000000": "0.0001", "1700028800000000000": "-0.00002"}}',
        encoding="utf-8",
    )
    source = FileSource(data, clock=lambda: NOW)
    assert source.name.startswith("file:") and source.tier == "free"
    ohlcv = source.fetch("ohlcv", "BTCUSDT", interval_s=60, limit=3)
    assert len(ohlcv.value) == 3 and ohlcv.detail["candles"] == 3
    assert ohlcv.age_s(NOW) > 0 and ohlcv.source == source.name
    funding = source.fetch("funding", "BTCUSDT")
    assert funding.value == Decimal("-0.00002") and funding.detail["settlements"] == 2
    with pytest.raises(SourceError, match="does not serve"):
        source.fetch("orderbook", "BTCUSDT")
    with pytest.raises(SourceError, match="nothing matching"):
        source.fetch("ohlcv", "ETHUSDT")
    with pytest.raises(SourceError, match="does not exist"):
        source.fetch("funding", "ETHUSDT")
    with pytest.raises(SourceError, match="positive"):
        source.fetch("ohlcv", "BTCUSDT", limit=0)


def test_durations_and_the_data_config(tmp_path: Path) -> None:
    assert parse_duration("30s") == 30 and parse_duration("5m") == 300
    assert (
        parse_duration("2h") == 7200
        and parse_duration("1d") == 86400
        and parse_duration("45") == 45
    )
    for text in ("", "soon", "-5s", "0m"):
        with pytest.raises(DataConfigError):
            parse_duration(text)

    path = tmp_path / "data.toml"
    path.write_text(
        """
[data.ohlcv]
chain = ["file:data"]
ttl = "2d"
interval_s = 60
limit = 120

[data.funding]
chain = ["file:data"]
ttl = "8h"
on_exhausted = "use_stale_with_flag"
""",
        encoding="utf-8",
    )
    cfg = load_data_config(path)
    assert [k.kind for k in cfg.kinds] == ["ohlcv", "funding"]
    ohlcv = cfg.for_kind("ohlcv")
    assert ohlcv.ttl_s == 172800 and ohlcv.options == {"interval_s": 60, "limit": 120}
    assert cfg.for_kind("funding").on_exhausted == "use_stale_with_flag"
    chain = cfg.chain("ohlcv")
    assert chain.sources[0].name.endswith("data") and chain.ttl_s == 172800
    with pytest.raises(DataConfigError, match=r"no \[data\.orderbook\]"):
        cfg.for_kind("orderbook")

    cases: list[tuple[str, str]] = [
        ("", "table is required"),
        ('[data.sentiment]\nchain = ["file:data"]\n', "not a kind of data"),
        ('[data.ohlcv]\nttl = "30s"\n', "chain is a list"),
        ('[data.ohlcv]\nchain = ["file:data"]\non_exhausted = "hope"\n', "on_exhausted is one of"),
    ]
    for body, message in cases:
        broken = tmp_path / "broken.toml"
        broken.write_text(body, encoding="utf-8")
        with pytest.raises(DataConfigError, match=message):
            load_data_config(broken)


def test_sources_are_named_in_a_file_not_in_code(tmp_path: Path) -> None:
    assert {"ccxt", "file"} <= set(schemes())
    source = build("file:data", {"root": str(tmp_path)})
    assert source.name.endswith("data") and "ohlcv" in source.kinds()
    with pytest.raises(UnknownSourceError, match="no source scheme"):
        build("quandl:gold")


def test_time_is_not_faked_anywhere_in_a_fetched_value() -> None:
    # A source that hands back a value dated in the future is a bug we want to see, not smooth
    # over: the age goes negative and a chain with any ttl accepts it, so this is a reminder in
    # test form that market time comes from the vendor and never from our own clock.
    future = Fetched(
        "ohlcv", "BTCUSDT", [], "s", "free", time.time(), int((time.time() + 60) * 1e9)
    )
    assert future.age_s() < 0


def test_a_dataset_written_under_the_old_package_name_still_reads(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq

    from trading_research_agents.data.parquet import SCHEMA_VERSION, ParquetError, read_scales

    table = pa.table({"ts": pa.array([1], type=pa.int64())})
    legacy = table.replace_schema_metadata(
        {
            "trading_research_agent.schema": SCHEMA_VERSION,
            "trading_research_agent.price_scale": "2",
            "trading_research_agent.qty_scale": "3",
        }
    )
    path = tmp_path / "legacy.parquet"
    pq.write_table(legacy, path)
    assert read_scales(path) == (2, 3)

    bare = tmp_path / "bare.parquet"
    pq.write_table(table, bare)
    with pytest.raises(ParquetError, match="not a trading-research-agents dataset"):
        read_scales(bare)
