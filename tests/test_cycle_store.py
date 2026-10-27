"""The working store: one snapshot per cycle, a cache that expires, a cycle directory to replay."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.data.schema import Candle
from trading_research_agents.data.sources import Fetched, SourceChain, SourceError
from trading_research_agents.store import (
    CacheError,
    CycleDir,
    FileCache,
    Snapshot,
    cycle_id,
    cycles_in,
    key_for,
    latest_cycle,
    take_snapshot,
)

NOW = 1_800_000_000.0


def fetched(kind: str, age_s: float = 2.0, source: str = "ccxt:bybit") -> Fetched:
    return Fetched(
        kind=kind,
        instrument="BTCUSDT",
        value=Decimal("0.0001") if kind == "funding" else [],
        source=source,
        tier="free",
        fetched_at=NOW,
        as_of_ns=int((NOW - age_s) * 1_000_000_000),
        detail={"note": kind},
    )


class Stub:
    def __init__(self, kind: str, *, age_s: float = 2.0, fail: bool = False) -> None:
        self.kind = kind
        self.age_s = age_s
        self.fail = fail

    @property
    def name(self) -> str:
        return f"stub:{self.kind}"

    @property
    def tier(self) -> str:
        return "free"

    def kinds(self) -> tuple[str, ...]:
        return (self.kind,)

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        if self.fail:
            raise SourceError("RateLimitExceeded")
        return fetched(kind, self.age_s, self.name)


def test_a_cycle_id_is_its_own_moment_in_utc() -> None:
    from datetime import UTC, datetime

    assert cycle_id(datetime(2026, 9, 27, 8, 0, tzinfo=UTC)) == "2026-09-27T08-00-00Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        cycle_id(datetime(2026, 9, 27, 8, 0))


def test_a_snapshot_keeps_what_answered_and_names_what_it_refused(tmp_path: Path) -> None:
    chains = {
        "ohlcv": SourceChain(kind="ohlcv", sources=(Stub("ohlcv"),), ttl_s=30, clock=lambda: NOW),
        "funding": SourceChain(
            kind="funding", sources=(Stub("funding", fail=True),), ttl_s=30, clock=lambda: NOW
        ),
    }
    snapshot = take_snapshot(
        "BTCUSDT", chains, cycle="2026-09-27T08-00-00Z", options={"ohlcv": {"limit": 10}}
    )
    assert snapshot.get("ohlcv").source == "stub:ohlcv"
    assert "funding" in snapshot.refused and "RateLimitExceeded" in snapshot.refused["funding"]
    assert not snapshot.usable and snapshot.stale == []
    with pytest.raises(KeyError, match="not in the snapshot"):
        snapshot.get("orderbook")

    written = snapshot.write(tmp_path / "snapshot.json", now=NOW)
    payload = json.loads(written.read_text("utf-8"))
    assert payload["cycle_id"] == "2026-09-27T08-00-00Z" and payload["instrument"] == "BTCUSDT"
    assert payload["values"]["ohlcv"]["age_s"] == 2.0
    assert [a["outcome"] for a in payload["attempts"]] == ["ok"]
    assert list(payload["refused"]) == ["funding"]

    # A kind with no chain at all is refused by name, not silently skipped.
    bare = take_snapshot("BTCUSDT", {}, kinds=["orderbook"])
    assert bare.refused == {"orderbook": "no chain declared"}


def test_a_stale_value_is_marked_and_makes_the_snapshot_unusable() -> None:
    chain = SourceChain(
        kind="ohlcv",
        sources=(Stub("ohlcv", age_s=900),),
        ttl_s=30,
        on_exhausted="use_stale_with_flag",
        clock=lambda: NOW,
    )
    snapshot = take_snapshot("BTCUSDT", {"ohlcv": chain})
    assert snapshot.stale == ["ohlcv"] and not snapshot.usable
    assert snapshot.get("ohlcv").age_s(NOW) == 900.0

    fresh = take_snapshot(
        "BTCUSDT",
        {"ohlcv": SourceChain(kind="ohlcv", sources=(Stub("ohlcv"),), ttl_s=30, clock=lambda: NOW)},
    )
    assert fresh.usable


def test_the_cache_returns_only_what_is_still_fresh(tmp_path: Path) -> None:
    clock = [1000.0]
    cache = FileCache(tmp_path / "cache", clock=lambda: clock[0])
    key = key_for({"source": "ccxt:bybit", "kind": "ohlcv", "limit": 10})
    assert key == key_for({"limit": 10, "kind": "ohlcv", "source": "ccxt:bybit"})
    assert cache.get(key, 30) is None
    cache.put(key, {"rows": 3})
    assert cache.get(key, 30) == {"rows": 3}
    clock[0] += 31
    assert cache.get(key, 30) is None  # past its time to live
    assert cache.get(key, 120) == {"rows": 3}
    assert cache.get(key, 0) is None  # a zero ttl means never from cache
    assert cache.sweep(30) == 1 and cache.get(key, 120) is None
    with pytest.raises(CacheError, match="not a cache key"):
        cache.put("../escape", {})

    corrupt = tmp_path / "cache" / "bad.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert cache.get("bad", 30) is None and cache.sweep(30) == 1


def test_a_cycle_directory_names_every_artefact(tmp_path: Path) -> None:
    root = tmp_path / "cycles"
    first = CycleDir(root=root, cycle="2026-09-27T08-00-00Z").make()
    assert first.brief("structure_lead").parent.name == "briefs"
    assert first.answer("structure_lead").parent.name == "answers"
    assert first.snapshot.name == "snapshot.json" and first.decision.name == "decision.json"
    assert cycles_in(root) == [] and latest_cycle(root) is None  # no snapshot written yet

    Snapshot(cycle=first.cycle, instrument="BTCUSDT").write(first.snapshot, now=NOW)
    second = CycleDir(root=root, cycle="2026-09-27T09-00-00Z").make()
    Snapshot(cycle=second.cycle, instrument="BTCUSDT").write(second.snapshot, now=NOW)
    assert [c.cycle for c in cycles_in(root)] == [first.cycle, second.cycle]
    newest = latest_cycle(root)
    assert newest is not None and newest.cycle == second.cycle

    summary = second.write_summary({"instrument": "BTCUSDT", "round_trips": 0})
    assert json.loads(summary.read_text("utf-8"))["instrument"] == "BTCUSDT"


def test_candles_in_a_snapshot_survive_the_round_trip(tmp_path: Path) -> None:
    candles = [
        Candle(
            start_ns=1_700_000_000_000_000_000,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("3"),
            trades=7,
        )
    ]
    snapshot = Snapshot(cycle="c", instrument="BTCUSDT")
    snapshot.put(
        Fetched(
            kind="ohlcv",
            instrument="BTCUSDT",
            value=candles,
            source="file:data",
            tier="free",
            fetched_at=NOW,
            as_of_ns=1_700_000_060_000_000_000,
            detail={"candles": 1, "last_close": "100.5"},
        )
    )
    payload = json.loads(snapshot.write(tmp_path / "s.json", now=NOW).read_text("utf-8"))
    # The file carries the figures a reader needs, not a dump of every candle.
    assert payload["values"]["ohlcv"]["last_close"] == "100.5"
    assert payload["values"]["ohlcv"]["candles"] == 1
    assert snapshot.get("ohlcv").value[0].close == Decimal("100.5")
