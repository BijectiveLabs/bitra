"""Layer 1: events marked on closed bars, responses measured after, counted per subperiod."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agent.chain.observation import (
    ObservationError,
    Recurrence,
    bars_of,
    feature_at,
    load_observation,
    observe,
)
from trading_research_agent.data import Side, Trade
from trading_research_agent.ledger import Axis, Verdict

MINUTE = 60 * 1_000_000_000
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def prints(prices: list[str | None], qty: str = "1", start_minute: int = 1) -> list[Trade]:
    """One trade per minute; a None leaves the minute empty."""
    out = []
    for i, p in enumerate(prices):
        if p is None:
            continue
        minute = start_minute + i
        out.append(
            Trade(minute * MINUTE + 1, "v", "AAAUSDT", Decimal(p), Decimal(qty), Side.BUY, str(i))
        )
    return out


def declaration(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "observation.toml"
    path.write_text(body, encoding="utf-8")
    return path


BURST = """
[observation]
subject = "a burst then a reversion"
interval_s = 60
horizon_bars = 2
min_events = {min_events}
subperiods = {subperiods}
min_subperiods_with_events = {min_with}
claimed_magnitude_bps = 20

[event]
feature = "volume_ratio"
window_bars = 4
comparison = ">"
threshold = 2.0
"""


def test_bars_fold_trades_by_interval_and_skip_empty_minutes() -> None:
    rows = [
        *prints(["100", "101", None, "99"]),
        Trade(2 * MINUTE + 30, "v", "AAAUSDT", Decimal("103"), Decimal("2"), Side.SELL, "x"),
    ]
    rows.sort(key=lambda t: t.ts_ns)
    bars = bars_of(rows, MINUTE)
    assert [b.start_ns // MINUTE for b in bars] == [1, 2, 4]
    assert bars[1].open == 101 and bars[1].high == 103 and bars[1].close == 103
    assert bars[1].volume == 3 and bars[1].trades == 2
    with pytest.raises(ObservationError, match="out of order"):
        bars_of(list(reversed(rows)), MINUTE)


def test_features_are_computed_from_the_window_ending_at_the_bar() -> None:
    bars = bars_of(prints(["100", "100", "100", "104"], qty="1"), MINUTE)
    assert feature_at(bars, 3, "return_bps", 1) == pytest.approx(400.0)
    assert feature_at(bars, 3, "abs_return_bps", 1) == pytest.approx(400.0)
    assert feature_at(bars, 0, "return_bps", 1) is None
    assert feature_at(bars, 3, "zscore", 4) == pytest.approx(1.5)
    assert feature_at(bars, 2, "zscore", 3) is None  # constant window
    assert feature_at(bars, 3, "volume_ratio", 4) == pytest.approx(1.0)
    assert feature_at(bars, 2, "volume_ratio", 4) is None  # window not yet full
    assert feature_at(bars, 3, "hour_utc", 1) == 0.0
    with pytest.raises(ObservationError, match="unknown feature"):
        feature_at(bars, 3, "rsi", 4)


def test_events_are_marked_measured_after_and_counted_per_subperiod(tmp_path: Path) -> None:
    # Four blocks of eight minutes; in each, one minute with three times the volume, and the
    # close two bars later sits 30 bps below the burst bar's close.
    prices, qtys = [], []
    for block in range(4):
        base = 1000 + block
        prices.extend([str(base)] * 8)
        qtys.extend(["1"] * 8)
        prices[block * 8 + 4] = str(base)
        qtys[block * 8 + 4] = "6"
        prices[block * 8 + 6] = str(Decimal(base) * Decimal("0.997"))
    rows = [
        Trade((i + 1) * MINUTE + 1, "v", "AAAUSDT", Decimal(p), Decimal(q), Side.BUY, str(i))
        for i, (p, q) in enumerate(zip(prices, qtys, strict=True))
    ]
    path = declaration(tmp_path, BURST.format(min_events=4, subperiods=4, min_with=4))
    obs = load_observation(path)
    report = observe(obs, rows, dataset="test")
    assert report.instrument == "AAAUSDT" and report.bars == 32
    assert report.events == 4 and report.measured == 4 and report.skipped == 0
    assert report.events_per_subperiod == (1, 1, 1, 1)
    assert report.recurrence is Recurrence.RECURS and report.recurs and report.fired == ()
    r = report.response_bps
    assert r["n"] == 4 and r["median"] == pytest.approx(-30.0, abs=0.01)
    assert r["fraction_positive"] == 0.0 and r["median_abs"] == pytest.approx(30.0, abs=0.01)
    assert [c["median"] for c in report.curve_bps] == [0.0, pytest.approx(-30.0, abs=0.01)]
    record = report.record()
    assert record["layer"] == 1 and "against the claimed 20 bps" in record["magnitude"]
    with pytest.raises(ValueError, match="nothing to record"):
        report.to_entry(entry_id="O-1", recorded_at=datetime.now(UTC))

    # The same data against a higher bar: too few events -> NOT PROVEN on power.
    few = observe(
        load_observation(
            declaration(tmp_path, BURST.format(min_events=5, subperiods=4, min_with=4))
        ),
        rows,
    )
    assert few.recurrence is Recurrence.TOO_FEW and few.fired == (
        "measured events 4 < min_events 5",
    )
    row = few.to_entry(entry_id="O-2", recorded_at=datetime.now(UTC), evidence=("test",))
    assert row.verdict is Verdict.NOT_PROVEN and row.blocking_axis is Axis.POWER and row.layer == 1

    # Events concentrated in the first half: episodic -> FALSIFIED on condition.
    half = [t for t in rows if t.ts_ns < 17 * MINUTE or t.qty == 1]
    episodic = observe(
        load_observation(
            declaration(tmp_path, BURST.format(min_events=2, subperiods=4, min_with=3))
        ),
        half,
    )
    assert episodic.events == 2 and episodic.events_per_subperiod == (1, 1, 0, 0)
    assert episodic.recurrence is Recurrence.EPISODIC
    row = episodic.to_entry(entry_id="O-3", recorded_at=datetime.now(UTC))
    assert row.verdict is Verdict.FALSIFIED and row.axis_of_death == (Axis.CONDITION,)
    assert "2 of 4 subperiods (1, 1, 0, 0); 3 required" in row.note


def test_a_horizon_bar_without_trades_leaves_the_response_unmeasured(tmp_path: Path) -> None:
    body = """
[observation]
subject = "a jump"
interval_s = 60
horizon_bars = 1
min_events = 1
subperiods = 1
min_subperiods_with_events = 1

[event]
feature = "abs_return_bps"
window_bars = 1
comparison = ">="
threshold = 100
"""
    rows = prints(["100", "102", None, "102", "104", "104"])
    report = observe(load_observation(declaration(tmp_path, body)), rows)
    assert report.events == 2 and report.measured == 1
    assert report.response_bps["n"] == 1 and report.response_bps["median"] == 0.0


def test_calendar_events_mark_the_last_bar_closed_at_or_before_when(tmp_path: Path) -> None:
    (tmp_path / "events.csv").write_text(
        "when,what\n"
        "1970-01-01T00:03:00Z,on the boundary\n"
        "1970-01-01T00:05:30Z,inside a minute\n"
        "1970-01-01T00:09:10Z,no bar closed before it\n",
        encoding="utf-8",
    )
    body = """
[observation]
subject = "a release"
interval_s = 60
horizon_bars = 1
min_events = 2
subperiods = 1
min_subperiods_with_events = 1

[event]
calendar = "events.csv"
"""
    rows = prints(["100", "101", "102", "103", "104", "105", "106", None, "108"])
    report = observe(load_observation(declaration(tmp_path, body)), rows)
    # 00:03:00 -> the bar starting 00:02 (closed at 00:03); 00:05:30 -> the bar starting 00:04.
    assert report.events == 2 and report.skipped == 1 and report.measured == 2
    assert report.recurs


def test_declarations_are_checked_and_carry_no_defaults(tmp_path: Path) -> None:
    good = load_observation(EXAMPLES / "observation.toml")
    assert good.event.feature == "volume_ratio" and good.min_events == 30
    assert good.claimed_magnitude_bps == Decimal("20")
    cases = {
        "": "tables are required",
        BURST.replace('subject = "a burst then a reversion"', 'subject = ""'): "subject",
        BURST.replace("min_events = {min_events}", "min_events = 0"): "positive integer",
        BURST.replace("threshold = 2.0", 'threshold = "big"'): "is a number",
        BURST.replace('comparison = ">"', 'comparison = "!="'): "comparison is one of",
        BURST.replace('feature = "volume_ratio"', 'feature = "rsi"'): "feature is one of",
        BURST.replace(
            'feature = "volume_ratio"', 'feature = "zscore"\ncalendar = "x.csv"'
        ): "not both",
        BURST.replace('comparison = ">"', 'comparison = "between"'): "lo, hi",
        BURST.replace("window_bars = 4", "window_bars = 1"): "window_bars >= 2",
    }
    for body, message in cases.items():
        text = body.format(min_events=3, subperiods=2, min_with=2) if "{" in body else body
        with pytest.raises(ObservationError, match=message):
            load_observation(declaration(tmp_path, text))
    between = BURST.replace('comparison = ">"', 'comparison = "between"').replace(
        "threshold = 2.0", "threshold = [1.5, 2.5]"
    )
    rule = load_observation(
        declaration(tmp_path, between.format(min_events=1, subperiods=1, min_with=1))
    ).event
    assert rule.threshold == (1.5, 2.5) and rule.matches(2.0) and not rule.matches(3.0)


def test_the_cli_observes_and_records_a_death(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agent.cli.main import main
    from trading_research_agent.data.parquet import write_trades

    rows = prints(["100"] * 12)
    dataset = tmp_path / "data" / "AAAUSDT-1970-01-01.parquet"
    dataset.parent.mkdir()
    write_trades(rows, dataset, price_scale=2, qty_scale=3)
    path = declaration(tmp_path, BURST.format(min_events=3, subperiods=2, min_with=2))
    code = main(
        [
            "chain",
            "observe",
            "--observation",
            str(path),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--id",
            "O-1",
            "--out",
            str(tmp_path / "observation.json"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert (
        code == 1 and payload["recurrence"].startswith("does not recur") and payload["events"] == 0
    )
    assert payload["recorded"] == "O-1" and (tmp_path / "observation.json").exists()
    assert "NOT PROVEN" in (tmp_path / "ledger.jsonl").read_text("utf-8")
    assert main(["chain", "observe", "--observation", str(path), "--dataset", str(dataset)]) == 1
    text = capsys.readouterr().out
    assert text.startswith("DOES NOT RECUR: TOO FEW EVENTS") and "fired measured events 0" in text


def test_the_surface_tool_returns_the_report(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agent.data.parquet import write_trades
    from trading_research_agent.surface import observe_events

    dataset = tmp_path / "d.parquet"
    write_trades(prints(["100"] * 6), dataset, price_scale=2, qty_scale=3)
    path = declaration(tmp_path, BURST.format(min_events=1, subperiods=1, min_with=1))
    out = observe_events(str(dataset), str(path))
    assert out["layer"] == 1 and out["bars"] == 6 and out["events"] == 0
    with pytest.raises(FileNotFoundError):
        observe_events(str(tmp_path / "none.parquet"), str(path))
