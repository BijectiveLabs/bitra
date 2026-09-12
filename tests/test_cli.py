from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agent.chain import (
    CostModel,
    CostTerm,
    Era,
    KillCriterion,
    Power,
    Preregistration,
    SealedEra,
    Universe,
)
from trading_research_agent.chain.preregistration import to_record
from trading_research_agent.cli import main
from trading_research_agent.data import read_manifest
from trading_research_agent.ledger import Ledger, Verdict

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

BYBIT_HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional"
)


def bybit_archive(path: Path, rows: list[str]) -> Path:
    with gzip.open(path, "wb") as fh:
        fh.write(("\n".join([BYBIT_HEADER, *rows]) + "\n").encode("utf-8"))
    return path


def bybit_row(i: int, ts: str, match_id: str | None = None) -> str:
    return f"{ts},BTCUSDT,Buy,0.01,93530.00,PlusTick,{match_id or f'id-{i}'},0,0,0"


def test_version_and_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.startswith("tra ")
    with pytest.raises(SystemExit) as exit_:
        main(
            ["data", "audit", "x.gz", "--venue", "bybit", "--instrument", "X", "--max-gap", "soon"]
        )
    assert exit_.value.code == 2
    assert "not a duration" in capsys.readouterr().err


def test_floor_clears_and_stops(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    envelope = str(EXAMPLES / "envelope.toml")
    code = main(
        ["chain", "floor", "--envelope", envelope, "--magnitude", "3.0", "--venue", "hyperliquid"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(
        "CLEARED: 3.0 bps plausible against 3.0 bps round trip on hyperliquid/maker-maker"
    )

    taker_only = tmp_path / "taker.toml"
    taker_only.write_text(
        '[[channel]]\nvenue = "hyperliquid"\nname = "taker-taker"\n'
        "entry_fee_bps = 4.5\nexit_fee_bps = 4.5\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.jsonl"
    code = main(
        [
            "chain",
            "floor",
            "--envelope",
            str(taker_only),
            "--magnitude",
            "3.0",
            "--subject",
            "informed-flow burst drift",
            "--ledger",
            str(ledger),
            "--id",
            "H-001",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["outcome"] == "stop" and payload["margin_bps"] == "-6.0"
    assert payload["recorded"] == "H-001"
    rows = list(Ledger(ledger).entries())
    assert rows[0].verdict is Verdict.CLOSED and rows[0].layer == 0

    code = main(
        [
            "chain",
            "floor",
            "--envelope",
            str(taker_only),
            "--magnitude",
            "3.0",
            "--ledger",
            str(ledger),
        ]
    )
    err = capsys.readouterr().err
    assert code == 1 and "--ledger needs --id" in err


def test_ledger_show_prints_the_count_and_the_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    code = main(["ledger", "show", "--path", str(ledger)])
    assert code == 0 and capsys.readouterr().out.strip() == "empty"

    taker_only = tmp_path / "taker.toml"
    taker_only.write_text(
        '[[channel]]\nvenue = "bybit"\nname = "taker-taker"\n'
        "entry_fee_bps = 5.5\nexit_fee_bps = 5.5\n",
        encoding="utf-8",
    )
    main(
        [
            "chain",
            "floor",
            "--envelope",
            str(taker_only),
            "--magnitude",
            "3",
            "--ledger",
            str(ledger),
            "--id",
            "H-9",
        ]
    )
    capsys.readouterr()
    code = main(["ledger", "show", "--path", str(ledger), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["counts"]["CLOSED"] == 1
    assert payload["rows"][0]["entry_id"] == "H-9"
    main(["ledger", "show", "--path", str(ledger)])
    text = capsys.readouterr().out
    assert text.splitlines()[0] == "CLOSED 1"
    assert "H-9" in text and "cost" in text


def test_audit_passes_writes_a_manifest_and_fails_on_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = bybit_archive(
        tmp_path / "BTCUSDT2025-01-01.csv.gz",
        [bybit_row(i, f"1735689600.{i:04d}") for i in range(1, 6)],
    )
    manifest = tmp_path / "manifest.json"
    code = main(
        [
            "data",
            "audit",
            str(good),
            "--venue",
            "bybit",
            "--instrument",
            "BTCUSDT",
            "--max-gap",
            "5m",
            "--manifest",
            str(manifest),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "bybit BTCUSDT: 5 trades, 0 rejected" in out and out.strip().endswith("PASSED")
    m = read_manifest(manifest)
    assert m.audit_passed and m.rows_accepted == 5 and m.sources[0].path == good.name

    dupes = bybit_archive(
        tmp_path / "dupes.csv.gz",
        [bybit_row(1, "1735689600.0001"), bybit_row(2, "1735689600.0002", match_id="id-1")],
    )
    code = main(
        [
            "data",
            "audit",
            str(dupes),
            "--venue",
            "bybit",
            "--instrument",
            "BTCUSDT",
            "--max-gap",
            "5m",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["passed"] is False
    assert [f["check"] for f in payload["findings"]] == ["duplicates"]
    assert payload["examples"]["duplicates"] == ["id-1"]


def test_errors_are_reported_on_stderr_with_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "data",
            "audit",
            str(tmp_path / "missing.csv.gz"),
            "--venue",
            "bybit",
            "--instrument",
            "X",
            "--max-gap",
            "1m",
        ]
    )
    err = capsys.readouterr().err
    assert code == 1 and err.startswith("tra: ")

    drifted = tmp_path / "drift.csv.gz"
    with gzip.open(drifted, "wb") as fh:
        fh.write(b"ts,sym\n1,BTCUSDT\n")
    code = main(
        [
            "data",
            "audit",
            str(drifted),
            "--venue",
            "bybit",
            "--instrument",
            "BTCUSDT",
            "--max-gap",
            "1m",
        ]
    )
    assert code == 1 and "header changed" in capsys.readouterr().err


def test_chain_check_validates_a_registration_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prereg = Preregistration(
        name="funding settlement flow",
        mechanism="Longs paying funding are forced to reduce into settlement by margin.",
        claim="Net return over 4h after settlement is positive when funding exceeds 15 bps.",
        metric="net_return_bps",
        horizon=timedelta(hours=4),
        universe=Universe(("BTCUSDT",), "top 15 by volume"),
        era=Era(date(2022, 1, 1), date(2025, 6, 30), (SealedEra("B", date(2025, 7, 1), None),)),
        cost_model=CostModel(
            (
                CostTerm("fee", "schedule"),
                CostTerm("spread_cost", "book"),
                CostTerm("slippage", "book"),
                CostTerm("funding_accrual", "settlements"),
            )
        ),
        power=Power(1200, Decimal("8.0"), "two-sided, 80 pct"),
        kill_criteria=(KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.FALSIFIED),),
        registered_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    path = tmp_path / "preregistration.json"
    path.write_text(json.dumps(to_record(prereg)), encoding="utf-8")
    code = main(["chain", "check", str(path)])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("funding settlement flow: valid, seal " + prereg.seal())
    assert "net_return_bps < 0 -> FALSIFIED" in out

    record = json.loads(path.read_text(encoding="utf-8"))
    record["horizon_seconds"] = 60
    path.write_text(json.dumps(record), encoding="utf-8")
    code = main(["chain", "check", str(path)])
    assert code == 1 and "altered" in capsys.readouterr().err
