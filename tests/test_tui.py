"""The console face: the same figures, rendered for a person; off for scripts and --json."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from trading_research_agents.cli import tui
from trading_research_agents.cli.main import main
from trading_research_agents.trading import RunReport


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Console:
    """A console that records to a string, with the terminal rule forced on."""
    console = Console(file=io.StringIO(), force_terminal=False, width=100, record=True)
    monkeypatch.setattr(tui, "_console", console)
    monkeypatch.setattr(tui, "enabled", lambda plain=False, as_json=False: not (plain or as_json))
    return console


def test_the_wordmark_fits_or_the_short_banner_is_used() -> None:
    wide = tui.banner(140)
    assert len(wide.splitlines()) == 6 and "/____/" in wide and wide.isascii()
    assert tui.banner(80) == tui.BANNER and tui.BANNER.isascii()


def test_the_console_is_off_for_scripts_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRA_PLAIN", raising=False)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert tui.enabled() is False  # not a terminal
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert tui.enabled() is True
    assert tui.enabled(plain=True) is False and tui.enabled(as_json=True) is False
    monkeypatch.setenv("TRA_PLAIN", "1")
    assert tui.enabled() is False


def test_tra_alone_is_the_console_in_both_faces(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recorded: Console,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    text = recorded.export_text()
    assert "trading-research-agents" in text and "by Bijective" in text
    assert "todo" in text and "next: tra init my-run" in text
    assert main(["--plain"]) == 0
    out = capsys.readouterr().out
    assert (
        out.startswith("tra ") and "todo  strategy.toml" in out and "next: tra init my-run" in out
    )


def test_views_render_the_figures(recorded: Console, tmp_path: Path) -> None:
    report = RunReport(label="replay", instrument="AAAUSDT", started_at=datetime.now(UTC))
    report.prints, report.bars, report.intents, report.refused = 400, 8, 3, 1
    report.fills, report.round_trips, report.net_bps_total = 2, 1, Decimal("-8.5")
    report.stopped_by = "kill switch"
    report.open_position = {
        "direction": "short",
        "size": "0.01",
        "entry_px": "100",
        "bars_held": 2,
        "last_price": "99",
    }
    tui.show(tui.run_panel(report, "runs/x"))
    text = recorded.export_text()
    assert "replay AAAUSDT" in text and "3 (1 refused)" in text and "-8.50 bps" in text
    assert "short 0.01 at 100, 2 bars held, last 99" in text and "kill switch" in text

    tui.show(tui.check_table(["config: valid"], ["funding: none"]))
    text = recorded.export_text()
    assert "ok" in text and "config: valid" in text and "todo" in text and "funding: none" in text

    @dataclass
    class Usage:
        tokens: int = 1200
        cost: Decimal = Decimal("0.01")

    @dataclass
    class Result:
        session_id: str = "S"
        kept: list[dict[str, Any]] = field(
            default_factory=lambda: [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "r"}]
        )
        excluded: list[dict[str, Any]] = field(
            default_factory=lambda: [{"instrument": "BBBUSDT", "reason": "thin"}]
        )
        reports: dict[str, RunReport] = field(default_factory=dict)
        risk: dict[str, Any] | None = None
        allocation: list[dict[str, Any]] = field(default_factory=list)
        audit: dict[str, Any] | None = None
        refusals: list[str] = field(default_factory=list)
        stopped_by: str | None = None

    result = Result(
        reports={"AAAUSDT": report},
        risk={"decision": "tighten", "reason": "one loss"},
        allocation=[{"instrument": "AAAUSDT", "fraction": 0.5}],
        audit={"verdict": "sound", "summary": "fine"},
        refusals=["executor: x"],
    )
    tui.show(tui.session_report(result, Usage(), "runs/s"))
    text = recorded.export_text()
    assert "kept" in text and "excluded" in text and "tighten: one loss" in text
    assert "sound: fine" in text and "1200 (cost 0.01)" in text and "executor: x" in text

    with tui.SessionView(Usage()) as view:
        view.on_step("market_screener", "done", "1 kept")
        view.on_step("run:AAAUSDT", "running", "")
        assert view.order.index("run:AAAUSDT") < view.order.index("risk_manager")
    text = recorded.export_text()
    assert "market_screener" in text and "run:AAAUSDT" in text

    with tui.RunView("runs/x") as live:
        live.observe(report, None)
        assert live.bars == 8

    from trading_research_agents.agents.evals import EvalRow

    row = EvalRow(
        provider="p",
        model="m",
        instrument="AAAUSDT",
        bars=8,
        intents=1,
        refused=0,
        fills=1,
        round_trips=0,
        net_bps_total=Decimal("0"),
        calls=4,
        failed_calls=1,
        input_tokens=100,
        output_tokens=20,
        cost=None,
        latency_median_ms=5,
        latency_p90_ms=9,
        tokens_per_bps=None,
        stopped_by=None,
        out=tmp_path,
    )
    tui.show(tui.eval_table([row], "AAAUSDT", "d.parquet"))
    text = recorded.export_text()
    assert "p:m" in text and "120" in text and "same bars, same controls" in text

    from trading_research_agents.agents.trace import Event, Trace

    t = Trace(
        path=tmp_path,
        kind="run",
        header={"label": "replay", "runs": {"A": {"round_trips": 1}}, "kept": ["A"]},
        calls=[Event("t", "call", "analyst", "error: ProviderError")],
        market=[Event("t", "fill", "A", "open long")],
        roles=[Event("01", "answer", "screener", "kept")],
    )
    tui.show(tui.trace_view(t))
    text = recorded.export_text()
    assert "A: round_trips: 1" in text and "error: ProviderError" in text and "open long" in text

    tui.show(
        tui.ledger_table(
            [
                {
                    "entry_id": "H-1",
                    "verdict": "CLOSED",
                    "layer": 0,
                    "axis_of_death": ["cost"],
                    "subject": "s",
                    "note": "n",
                },
                {
                    "entry_id": "H-2",
                    "verdict": "NOT PROVEN",
                    "blocking_axis": "power",
                    "subject": "t",
                },
            ],
            {"CLOSED": 1, "NOT PROVEN": 1},
        )
    )
    text = recorded.export_text()
    assert "H-1" in text and "cost" in text and "power" in text and "CLOSED 1" in text

    tui.show(
        tui.ladder_table(
            "0 REPLAY",
            [
                {
                    "at": "now",
                    "kind": "promote",
                    "from": "T0",
                    "to": "T1",
                    "evidence": "e",
                    "signed_by": "o",
                }
            ],
        )
    )
    text = recorded.export_text()
    assert "tier 0 REPLAY" in text and "signed by o" in text


def test_observe_and_check_render_through_the_cli(
    tmp_path: Path, recorded: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    from test_observation import BURST, declaration, prints
    from trading_research_agents.data.parquet import write_trades

    dataset = tmp_path / "d.parquet"
    write_trades(prints(["100"] * 12), dataset, price_scale=2, qty_scale=3)
    path = declaration(tmp_path, BURST.format(min_events=3, subperiods=2, min_with=2))
    assert main(["chain", "observe", "--observation", str(path), "--dataset", str(dataset)]) == 1
    text = recorded.export_text()
    assert "DOES NOT RECUR: TOO FEW EVENTS" in text and "response curve" in text
    assert "measured events 0 < min_events 3" in text

    # --json is untouched by the console.
    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    assert (
        main(["chain", "observe", "--observation", str(path), "--dataset", str(dataset), "--json"])
        == 1
    )
    payload = json.loads(captured.getvalue())
    assert payload["layer"] == 1


def test_a_character_the_console_cannot_encode_does_not_end_the_run(
    monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """A model writes a non-breaking hyphen; an older console cannot encode it. The run stands."""

    class Narrow:
        def print(self, renderable: object) -> None:
            raise UnicodeEncodeError("charmap", "x", 0, 1, "cannot encode")

    monkeypatch.setattr(tui, "_console", Narrow())
    drawn: list[object] = []
    monkeypatch.setattr(
        tui, "_safe_console", lambda: type("C", (), {"print": lambda _s, r: drawn.append(r)})()
    )

    said = "a reason with a non" + chr(0x2011) + "breaking hyphen"
    tui.show(said)
    assert drawn == [said]
