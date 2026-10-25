from __future__ import annotations

import json
import random
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.envelope.model import Channel
from trading_research_agents.evaluation import rows
from trading_research_agents.evaluation.arena import ArenaConfig, run_arena
from trading_research_agents.evaluation.policy import DecisionView
from trading_research_agents.evaluation.records import write_arena
from trading_research_agents.evaluation.settle import Costs
from trading_research_agents.market_data.archive import (
    FLOW,
    SourceFile,
    day_file,
    manifest_file,
    write_deriv_day,
    write_flow_day,
)
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series

pytest.importorskip("pyarrow")

HOUR = 60 * NS_PER_MINUTE
FIRST = date(2025, 7, 1)
DAYS = 18
START, END = date(2025, 7, 6), date(2025, 7, 16)
FIXTURE = (SourceFile("fixture", None),)
COSTS = Costs(
    Channel(
        venue="bybit",
        name="taker-taker",
        entry_fee_bps=Decimal("5.5"),
        exit_fee_bps=Decimal("5.5"),
    ),
    spread_bps=Decimal("1"),
    slippage_bps=Decimal("0"),
)


def minutes(instrument: str, seed: int) -> list[FlowBar]:
    rng = random.Random(seed)  # noqa: S311
    price = 100.0
    start = midnight_ns(FIRST)
    out = []
    for i in range(DAYS * 1440):
        before = price
        price = max(1.0, price + rng.choice((-0.03, -0.01, 0.01, 0.03)))
        o, c = Decimal(f"{before:.2f}"), Decimal(f"{price:.2f}")
        buy = Decimal("0.600") if c >= o else Decimal("0.400")
        out.append(
            FlowBar(
                venue="bybit",
                instrument=instrument,
                start_ns=start + i * NS_PER_MINUTE,
                end_ns=start + (i + 1) * NS_PER_MINUTE,
                open=o,
                high=max(o, c) + Decimal("0.01"),
                low=min(o, c) - Decimal("0.01"),
                close=c,
                volume_base=Decimal("1.000"),
                volume_quote=Decimal("1.000") * c,
                taker_buy_base=buy,
                taker_sell_base=Decimal("1.000") - buy,
                trade_count=3,
                large_trade_base=None,
                late_trades=0,
                gap=GapState.NONE,
                source="archive:bybit",
            )
        )
    return out


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("archive")
    for instrument, seed in (("BTCUSDT", 3), ("ETHUSDT", 5)):
        tape = minutes(instrument, seed)
        for k in range(DAYS):
            day = FIRST + timedelta(days=k)
            bars = tape[k * 1440 : (k + 1) * 1440]
            write_flow_day(bars, root, day=day, price_scale=2, qty_scale=3, sources=FIXTURE)
            start = midnight_ns(day)
            points = [
                DerivPoint(Series.FUNDING, start + h * HOUR, start + h * HOUR, Decimal("0.0001"))
                for h in (0, 8, 16)
            ]
            write_deriv_day(
                points, root, venue="bybit", instrument=instrument, day=day, sources=FIXTURE
            )
    return root


def config(**changes: object) -> ArenaConfig:
    fields: dict[str, object] = {
        "venue": "bybit",
        "instruments": ("BTCUSDT", "ETHUSDT"),
        "start": START,
        "end": END,
        "rows": ("buy_hold", "momentum", "fade", "random"),
        "costs": COSTS,
    }
    fields.update(changes)
    return ArenaConfig(**fields)  # type: ignore[arg-type]


def test_every_row_is_reported_with_costs_and_coverage(archive: Path) -> None:
    result = run_arena(config(), root=archive)
    assert [r["row"] for r in result.rows] == ["buy_hold", "momentum", "fade", "random"]
    by_row = {r["row"]: r for r in result.rows}
    assert by_row["buy_hold"]["ideas"] == 2 and by_row["momentum"]["ideas"] > 0
    assert by_row["random"]["verdict"] == "the null"
    settled = [i for i in result.ideas if i["row"] == "momentum" and "cost" in i]
    assert settled and all(i["cost"]["certainty"]["funding"] == "measured" for i in settled)
    market = result.coverage["markets"]["BTCUSDT"]
    assert market["minutes"] == 11 * 1440 and market["missing_minutes"] == 0
    assert len(result.manifests) == 2 * DAYS * 2


def test_the_same_run_writes_the_same_bytes(archive: Path, tmp_path: Path) -> None:
    first = write_arena(run_arena(config(), root=archive), tmp_path / "a")
    second = write_arena(run_arena(config(), root=archive), tmp_path / "b")
    assert first.name == second.name
    assert (first / "arena.json").read_bytes() == (second / "arena.json").read_bytes()
    assert (first / "ideas.jsonl").read_bytes() == (second / "ideas.jsonl").read_bytes()
    assert "created_at" in json.loads((first / "run.json").read_text(encoding="utf-8"))


def test_a_missing_day_is_coverage_not_a_crash(archive: Path, tmp_path: Path) -> None:
    root = tmp_path / "archive"
    shutil.copytree(archive, root)
    gone = day_file(root, "bybit", "BTCUSDT", FLOW, date(2025, 7, 10))
    gone.unlink()
    manifest_file(gone).unlink()
    result = run_arena(config(), root=root)
    assert result.coverage["markets"]["BTCUSDT"]["missing_minutes"] == 1440
    assert result.coverage["features_absent"]["mom_z_24"]["absent"] > 0


def test_the_v1_record_is_a_labelled_row(archive: Path, tmp_path: Path) -> None:
    folder = tmp_path / "cycles" / "c1"
    folder.mkdir(parents=True)
    record = {
        "cycle_id": "c1",
        "instrument": "BTCUSDT",
        "finished_at": "2025-07-08T10:00:30+00:00",
        "analyst": {"target": "long"},
        "intent": {"mid": "100", "stop_price": "99", "take_price": "102"},
    }
    (folder / "cycle.json").write_text(json.dumps(record), encoding="utf-8")
    result = run_arena(
        config(rows=("v1_record", "random")), root=archive, cycles=tmp_path / "cycles"
    )
    v1 = result.rows[0]
    assert v1["row"] == "v1_record" and v1["ideas"] == 1
    assert v1["label"] == "live record, not replay"


def test_a_policy_that_knew_the_window_is_contaminated(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Oracle:
        name = "oracle"
        knowledge_cutoff = date(2026, 1, 1)

        def decide(self, view: DecisionView) -> None:
            return None

    monkeypatch.setattr(rows, "_plugins", lambda: {"oracle": Oracle})
    result = run_arena(config(rows=("oracle", "random")), root=archive)
    assert result.rows[0]["verdict"].startswith("contaminated")


def test_splits_report_each_test_window(archive: Path) -> None:
    result = run_arena(config(fit_days=4, test_days=3), root=archive)
    assert result.splits and set(result.splits[0]["rows"]) == {
        "buy_hold",
        "momentum",
        "fade",
        "random",
    }


def test_a_different_archive_is_a_different_run(archive: Path, tmp_path: Path) -> None:
    root = tmp_path / "archive"
    shutil.copytree(archive, root)
    gone = day_file(root, "bybit", "BTCUSDT", FLOW, date(2025, 7, 10))
    gone.unlink()
    manifest_file(gone).unlink()
    first = write_arena(run_arena(config(), root=archive), tmp_path / "out")
    second = write_arena(run_arena(config(), root=root), tmp_path / "out")
    assert first != second
    assert (first / "arena.json").is_file() and (second / "arena.json").is_file()
