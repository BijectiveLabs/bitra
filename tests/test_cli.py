from __future__ import annotations

import gzip
import hashlib
import json
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
from trading_research_agent.data import Rejection, Trade, read_manifest
from trading_research_agent.data.parquet import read_trades
from trading_research_agent.data.venues import register as register_venue
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

    pytest.importorskip("pyarrow")
    parquet = tmp_path / "btc.parquet"
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
            "--parquet",
            str(parquet),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["parquet"] == str(parquet)
    back = list(read_trades(parquet))
    assert len(back) == 5 and str(back[0].price) == "93530.00" and str(back[0].qty) == "0.01"

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


def test_agent_propose_and_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from trading_research_agent.agent import FakeCompleter

    cli_main = importlib.import_module("trading_research_agent.cli.main")

    memo = {
        "mechanism": "Longs paying funding are forced by margin to reduce into settlement.",
        "forced_loser": "longs paying funding",
        "forcing_structure": "margin at settlement",
    }
    draft = {
        "name": "funding settlement squeeze",
        "claim": "Net return over 4h after settlement is positive when funding exceeds 15 bps.",
        "metric": "net_return_bps",
        "horizon_seconds": 14400,
        "universe": {"instruments": ["BTCUSDT"], "rule": "top 15 by volume"},
        "era": {
            "research_start": "2022-01-01",
            "research_end": "2025-06-30",
            "sealed": [{"name": "B", "start": "2025-07-01", "end": None}],
        },
        "cost_model": [
            {"name": "fee", "source": "schedule"},
            {"name": "spread_cost", "source": "book"},
            {"name": "slippage", "source": "book"},
            {"name": "funding_accrual", "source": "settlements"},
        ],
        "power": {"required_sample": 1200, "effect_bps": "8.0", "basis": "two-sided, 80 pct"},
        "kill_criteria": [
            {
                "metric": "net_return_bps",
                "comparison": "<",
                "threshold": "0",
                "verdict": "FALSIFIED",
            }
        ],
    }
    scripted: list[dict[str, Any]] = [memo, {"accepted": True, "objection": ""}, draft]
    monkeypatch.setattr(cli_main, "_completer", lambda provider, model: FakeCompleter(scripted))
    common = [
        "agent",
        "propose",
        "--idea",
        "funding squeeze",
        "--envelope",
        str(EXAMPLES / "envelope.toml"),
        "--provider",
        "anthropic",
        "--magnitude",
        "20",
        "--ledger",
        str(tmp_path / "ledger.jsonl"),
        "--refusals",
        str(tmp_path / "refusals.jsonl"),
        "--runs",
        str(tmp_path / "runs"),
    ]
    code = main([*common, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["outcome"] == "registered" and payload["exchanges"] == 3
    assert (Path(payload["run_dir"]) / "preregistration.json").exists()
    assert next(iter(Ledger(tmp_path / "ledger.jsonl").entries())).verdict is Verdict.OPEN

    refused: list[dict[str, Any]] = [
        {
            "mechanism": "Price tends to revert after a spike for reasons.",
            "forced_loser": "-",
            "forcing_structure": "-",
        },
        {"accepted": False, "objection": "a pattern, not a mechanism"},
    ] * 3
    monkeypatch.setattr(cli_main, "_completer", lambda provider, model: FakeCompleter(refused))
    code = main(common)
    out = capsys.readouterr().out
    assert code == 1 and "refused at layer 2 after 2 revisions: a pattern" in out

    code = main(["agent", "refusals", "--path", str(tmp_path / "refusals.jsonl")])
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("1 refused") and "layer 2 after 2" in out

    monkeypatch.undo()
    code = main(
        [
            "agent",
            "propose",
            "--idea",
            "x",
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--provider",
            "openai",
        ]
    )
    assert code == 1 and "--model is required" in capsys.readouterr().err


def test_chain_requirements_and_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agent.chain import Preregistration
    from trading_research_agent.chain.seal import day_start_ns
    from trading_research_agent.data import Manifest, Source, write_manifest

    prereg = Preregistration(
        name="funding settlement squeeze",
        mechanism="Longs paying funding are forced by margin to reduce into settlement.",
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
        power=Power(20, Decimal("8.0"), "declared"),
        kill_criteria=(KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.FALSIFIED),),
        registered_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    registration = tmp_path / "preregistration.json"
    registration.write_text(json.dumps(to_record(prereg)), encoding="utf-8")

    manifest = tmp_path / "manifest.json"
    write_manifest(
        manifest,
        Manifest(
            venue="bybit",
            instrument="BTCUSDT",
            first_ts=datetime(2021, 12, 1, tzinfo=UTC),
            last_ts=datetime(2025, 8, 1, tzinfo=UTC),
            rows_accepted=5_000_000,
            rows_rejected=0,
            sources=(Source("x", "ab" * 32, 1),),
            normaliser_version="t",
            audit_passed=True,
            findings=(),
            created_at=datetime(2026, 9, 13, tzinfo=UTC),
        ),
    )
    code = main(
        ["chain", "requirements", "--registration", str(registration), "--manifest", str(manifest)]
    )
    out = capsys.readouterr().out
    assert code == 0 and out.strip().endswith("PASSED")

    trades = tmp_path / "trades.jsonl"
    start = day_start_ns(date(2022, 1, 1))
    day = 86_400 * 1_000_000_000
    with trades.open("w", encoding="utf-8") as fh:
        for i in range(40):
            fh.write(
                json.dumps(
                    {
                        "instrument": "BTCUSDT",
                        "entry_ts_ns": start + i * day,
                        "exit_ts_ns": start + i * day + 4 * 3600 * 1_000_000_000,
                        "direction": "long",
                        "weight": "1",
                        "gross_return_bps": "25.0" if i % 2 else "35.0",
                    }
                )
                + "\n"
            )
    ledger = tmp_path / "ledger.jsonl"
    code = main(
        [
            "chain",
            "validate",
            "--registration",
            str(registration),
            "--trades",
            str(trades),
            "--cost-bps",
            "10",
            "--block-length",
            "5",
            "--n-resamples",
            "1000",
            "--n-perm",
            "1000",
            "--out",
            str(tmp_path / "validation.json"),
            "--ledger",
            str(ledger),
            "--id",
            "V-1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["decision"]["verdict"] == "OPEN" and payload["recorded"] == "V-1"
    assert (tmp_path / "validation.json").exists()
    assert next(iter(Ledger(ledger).entries())).layer == 5

    code = main(
        [
            "chain",
            "validate",
            "--registration",
            str(registration),
            "--trades",
            str(trades),
            "--cost-bps",
            "40",
            "--block-length",
            "5",
            "--n-resamples",
            "1000",
            "--n-perm",
            "1000",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1 and "FALSIFIED (idea)" in out


def test_trade_replay_and_ladder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agent.data import Side, Trade
    from trading_research_agent.data.parquet import write_trades

    base = 1_735_689_600 * 1_000_000_000
    minute = 60 * 1_000_000_000
    prices = [100 + (i % 7) - 3 for i in range(240)]
    rows = [
        Trade(
            base + i * minute,
            "bybit",
            "BTCUSDT",
            Decimal(prices[i]),
            Decimal("0.01"),
            Side.BUY,
            f"t{i}",
        )
        for i in range(240)
    ]
    parquet = tmp_path / "btc.parquet"
    write_trades(rows, parquet, price_scale=2, qty_scale=3)

    config = tmp_path / "strategy.toml"
    config.write_text(
        (EXAMPLES / "strategy.toml")
        .read_text(encoding="utf-8")
        .replace('kill_switch = "KILL"', ""),
        encoding="utf-8",
    )
    code = main(
        [
            "trade",
            "replay",
            "--config",
            str(config),
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--parquet",
            str(parquet),
            "--out",
            str(tmp_path / "run"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["prints"] == 240 and payload["bars"] == 240
    assert (tmp_path / "run" / "run.json").exists()

    ladder = tmp_path / "ladder.json"
    assert main(["ladder", "show", "--path", str(ladder)]) == 0
    assert capsys.readouterr().out.startswith("tier 0 RESEARCH")
    promote = ["ladder", "promote", "--path", str(ladder), "--signed-by", "op", "--evidence"]
    assert main([*promote, "replay OPEN"]) == 0
    capsys.readouterr()
    assert main(["ladder", "demote", "--path", str(ladder), "--reason", "limit tripped"]) == 0
    capsys.readouterr()
    assert main([*promote, ""]) == 1
    assert "names its evidence" in capsys.readouterr().err


def test_audit_takes_any_csv_through_a_column_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = tmp_path / "vendor.csv"
    csv.write_text(
        "timestamp,price,amount,side,id\n"
        "1735689600097,93530.00,0.003,sell,a-1\n"
        "1735689600144,93530.00,0.991,SELL,a-2\n"
        "1735689600266,93531.50,0.010,buy,a-3\n",
        encoding="utf-8",
    )
    code = main(
        [
            "data",
            "audit",
            str(csv),
            "--mapping",
            str(EXAMPLES / "mapping.toml"),
            "--instrument",
            "BTCUSDT",
            "--max-gap",
            "5m",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("vendor-x BTCUSDT: 3 trades, 0 rejected") and out.strip().endswith(
        "PASSED"
    )

    with pytest.raises(SystemExit) as exit_:
        main(
            [
                "data",
                "audit",
                str(csv),
                "--venue",
                "bybit",
                "--mapping",
                "m.toml",
                "--instrument",
                "X",
            ]
        )
    assert exit_.value.code == 2


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class LocalVenue:
    """A venue plugin registered by a test: daily archives served from a local directory."""

    name = "local-test"
    cadence = "daily"

    def __init__(self, base: str) -> None:
        self.base = base

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str:
        if day is None:
            raise ValueError("local-test archives are daily; pass --day")
        return f"{self.base}/{instrument}{day.isoformat()}.csv.gz"

    def checksum_url(self, archive_url: str) -> str | None:
        return archive_url + ".sha256"

    def normalise(self, lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
        yield from ()


def test_fetch_goes_through_the_venue_plugin_and_its_checksum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "www"
    root.mkdir()
    body = b"timestamp,symbol\n1,BTCUSDT\n" * 50
    (root / "BTCUSDT2025-01-01.csv.gz").write_bytes(body)
    (root / "BTCUSDT2025-01-01.csv.gz.sha256").write_text(
        f"{hashlib.sha256(body).hexdigest()}  BTCUSDT2025-01-01.csv.gz\n", encoding="ascii"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        register_venue(LocalVenue(f"http://127.0.0.1:{server.server_address[1]}"))
        out_dir = tmp_path / "data"
        code = main(
            [
                "data",
                "fetch",
                "--venue",
                "local-test",
                "--instrument",
                "BTCUSDT",
                "--day",
                "2025-01-01",
                "--out",
                str(out_dir),
                "--json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["size"] == len(body)
        assert (out_dir / "BTCUSDT2025-01-01.csv.gz").read_bytes() == body

        code = main(
            ["data", "fetch", "--venue", "local-test", "--instrument", "X", "--month", "2025-01"]
        )
        assert code == 1 and "daily" in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_refuses_an_unknown_feed_before_touching_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "trade",
            "shadow",
            "--config",
            str(EXAMPLES / "strategy.toml"),
            "--envelope",
            str(EXAMPLES / "envelope.toml"),
            "--out",
            str(tmp_path / "run"),
            "--feed",
            "no-such-venue",
        ]
    )
    err = capsys.readouterr().err
    assert code == 1 and "no live feed named 'no-such-venue'" in err
