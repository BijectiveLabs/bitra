from __future__ import annotations

import gzip
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data import (
    NORMALISER_VERSION,
    ArchiveError,
    ArchiveFormatError,
    ManifestError,
    Rejection,
    Side,
    Source,
    Trade,
    UnknownVenueError,
    archive_venue,
    archive_venues,
    audit,
    manifest_from_audit,
    open_archive_lines,
    read_manifest,
    scale_of,
    sha256_of,
    write_manifest,
)

# First rows of the venues' own archives, read on 2026-09-12. The Bybit grossValue column
# carries float artefacts as published; it is not used, and the row still normalises.
BYBIT_HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional"
)
BYBIT_ROWS = [
    "1735689600.0974,BTCUSDT,Sell,0.003,93530.00,ZeroMinusTick,e807b277-6d22-5f60-a520-76835094a401,2.8059000000000004e+10,0.003,280.59000000000003",
    "1735689600.1446,BTCUSDT,Sell,0.991,93530.00,ZeroMinusTick,e4fe05c9-c8f6-580f-ad25-a690169c52bd,9.268823e+12,0.991,92688.23",
    "1735689600.1446,BTCUSDT,Sell,0.009,93530.00,ZeroMinusTick,cf4cca3d-8a61-551f-9029-72628bbf2106,8.4177e+10,0.009,841.77",
]
BINANCE_ROWS = [
    "4359935386,93576.00000000,0.00136000,127.26336000,1735689600010866,True,True",
    "4359935387,93576.00000000,0.00212000,198.38112000,1735689600074095,True,True",
    "4359935391,93576.01000000,0.00182000,170.30833820,1735689600266955,False,True",
]


bybit_trades = archive_venue("bybit").normalise
binance_trades = archive_venue("binance").normalise


def trades(rows: list[Trade | Rejection]) -> list[Trade]:
    return [r for r in rows if isinstance(r, Trade)]


def test_bybit_rows_normalise_to_exact_decimals_and_nanoseconds() -> None:
    out = list(bybit_trades([BYBIT_HEADER, *BYBIT_ROWS], instrument="BTCUSDT"))
    assert all(isinstance(r, Trade) for r in out)
    first = trades(out)[0]
    assert first.ts_ns == 1735689600097400000
    assert first.ts == datetime(2025, 1, 1, 0, 0, 0, 97400, tzinfo=UTC)
    assert first.price == Decimal("93530.00") and str(first.price) == "93530.00"
    assert first.qty == Decimal("0.003")
    assert first.side is Side.SELL
    assert first.venue == "bybit"
    assert first.trade_id == "e807b277-6d22-5f60-a520-76835094a401"
    assert first.seq is None


def test_bybit_reads_the_layout_with_the_rpi_column() -> None:
    # The venue republished its archive with an RPI column, read from the file for 2025-07-01
    # on 2026-10-25; a row carries one more field and normalises to the same trade.
    header = BYBIT_HEADER + ",RPI"
    rows = [row + ",0" for row in BYBIT_ROWS]
    out = list(bybit_trades([header, *rows], instrument="BTCUSDT"))
    assert trades(out) == trades(
        list(bybit_trades([BYBIT_HEADER, *BYBIT_ROWS], instrument="BTCUSDT"))
    )
    short = list(bybit_trades([header, BYBIT_ROWS[0]], instrument="BTCUSDT"))
    assert isinstance(short[0], Rejection) and "expected 11 columns" in short[0].reason


def test_bybit_header_drift_stops_the_file_not_the_row() -> None:
    changed = BYBIT_HEADER.replace("trdMatchID", "matchId")
    with pytest.raises(ArchiveFormatError, match="header changed"):
        list(bybit_trades([changed, *BYBIT_ROWS], instrument="BTCUSDT"))
    with pytest.raises(ArchiveFormatError, match="empty"):
        list(bybit_trades([], instrument="BTCUSDT"))


def test_bybit_bad_rows_are_rejected_with_line_and_reason() -> None:
    rows = [
        BYBIT_HEADER,
        BYBIT_ROWS[0],
        BYBIT_ROWS[1].replace("BTCUSDT", "ETHUSDT"),
        BYBIT_ROWS[2].replace("Sell", "Short"),
        "1735689600.3,BTCUSDT,Buy,0,93530.00,PlusTick,id-1,0,0,0",
        "not,enough,columns",
    ]
    out = list(bybit_trades(rows, instrument="BTCUSDT"))
    rejected = [r for r in out if isinstance(r, Rejection)]
    assert len(trades(out)) == 1
    assert [r.line for r in rejected] == [3, 4, 5, 6]
    assert "ETHUSDT" in rejected[0].reason
    assert "unknown side" in rejected[1].reason
    assert "qty must be a positive" in rejected[2].reason
    assert "columns" in rejected[3].reason


def test_binance_rows_infer_microseconds_and_the_taker_side() -> None:
    out = trades(list(binance_trades(BINANCE_ROWS, instrument="BTCUSDT")))
    assert len(out) == 3
    assert out[0].ts_ns == 1735689600010866000
    assert out[0].side is Side.SELL  # buyer was the maker, so the taker sold
    assert out[2].side is Side.BUY
    assert out[0].seq == 4359935386 and out[0].trade_id == "4359935386"
    assert out[2].price == Decimal("93576.01000000")
    assert scale_of(out[2].price) == 8


def test_binance_milliseconds_and_bad_rows() -> None:
    ms_row = "1,100.5,0.1,10.05,1609459200000,False,True"
    out = list(binance_trades([ms_row], instrument="BTCUSDT"))
    assert isinstance(out[0], Trade) and out[0].ts_ns == 1609459200000000000

    rows = [
        "2,100.5,0.1,10.05,16094592000,False,True",
        "3,100.5,0.1,10.05,1609459200000,maybe,True",
        "4,-100.5,0.1,-10.05,1609459200000,False,True",
        "5,100.5",
    ]
    out = list(binance_trades(rows, instrument="BTCUSDT"))
    assert all(isinstance(r, Rejection) for r in out)
    reasons = [r.reason for r in out if isinstance(r, Rejection)]
    assert "not ms, us or ns" in reasons[0]
    assert "not a boolean" in reasons[1]
    assert "price must be a positive" in reasons[2]
    assert "columns" in reasons[3]


def test_archive_urls_match_the_venues_layout() -> None:
    bybit = archive_venue("bybit")
    binance = archive_venue("binance")
    assert bybit.cadence == "daily" and binance.cadence == "monthly"
    url = bybit.archive_url("BTCUSDT", day=date(2025, 1, 1), month=None)
    assert url == "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-01.csv.gz"
    assert bybit.checksum_url(url) is None
    url = binance.archive_url("BTCUSDT", day=None, month=(2025, 1))
    assert url == (
        "https://data.binance.vision/data/spot/monthly/trades/BTCUSDT/BTCUSDT-trades-2025-01.zip"
    )
    assert binance.checksum_url(url) == url + ".CHECKSUM"
    with pytest.raises(ValueError, match="daily"):
        bybit.archive_url("BTCUSDT", day=None, month=(2025, 1))
    with pytest.raises(ValueError, match="monthly"):
        binance.archive_url("BTCUSDT", day=date(2025, 1, 1), month=None)


def test_bundled_venues_are_found_through_the_registry() -> None:
    assert set(archive_venues()) >= {"bybit", "binance"}
    with pytest.raises(UnknownVenueError, match="registered"):
        archive_venue("no-such-venue")


def test_archives_are_read_in_place(tmp_path: Path) -> None:
    text = "\n".join([BYBIT_HEADER, *BYBIT_ROWS]) + "\n"
    gz = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    with gzip.open(gz, "wb") as fh:
        fh.write(text.encode("utf-8"))
    assert [line.rstrip("\n") for line in open_archive_lines(gz)] == [BYBIT_HEADER, *BYBIT_ROWS]

    zp = tmp_path / "BTCUSDT-trades-2025-01.zip"
    with zipfile.ZipFile(zp, "w") as archive:
        archive.writestr("BTCUSDT-trades-2025-01.csv", "\n".join(BINANCE_ROWS) + "\n")
    assert [line.rstrip("\n") for line in open_archive_lines(zp)] == BINANCE_ROWS

    two = tmp_path / "two.zip"
    with zipfile.ZipFile(two, "w") as archive:
        archive.writestr("a.csv", "x")
        archive.writestr("b.csv", "y")
    with pytest.raises(ArchiveError, match="expected one CSV"):
        list(open_archive_lines(two))
    with pytest.raises(ArchiveError, match="unsupported"):
        list(open_archive_lines(tmp_path / "x.parquet"))

    plain = tmp_path / "plain.csv"
    plain.write_bytes(b"a,b\n")
    assert list(open_archive_lines(plain)) == ["a,b\n"]
    assert len(sha256_of(plain)) == 64


def make_trade(i: int, ts_ns: int, **kw: object) -> Trade:
    base: dict[str, object] = {
        "ts_ns": ts_ns,
        "venue": "bybit",
        "instrument": "BTCUSDT",
        "price": Decimal("93530.00"),
        "qty": Decimal("0.003"),
        "side": Side.BUY,
        "trade_id": f"t-{i}",
    }
    base.update(kw)
    return Trade(**base)  # type: ignore[arg-type]


BASE_NS = 1735689600_000_000_000
SECOND = 1_000_000_000


def test_a_clean_stream_passes_with_no_findings() -> None:
    stream = [make_trade(i, BASE_NS + i * SECOND) for i in range(100)]
    report = audit(stream, max_gap=timedelta(minutes=1))
    assert report.passed
    assert report.findings == ()
    assert report.rows_accepted == 100
    assert report.venue == "bybit" and report.instrument == "BTCUSDT"
    assert report.first_ts_ns == BASE_NS and report.last_ts_ns == BASE_NS + 99 * SECOND
    assert report.price_scales == (2,) and report.qty_scales == (3,)


def test_duplicates_out_of_order_and_sequence_resets_fail() -> None:
    stream = [
        make_trade(1, BASE_NS, seq=10),
        make_trade(2, BASE_NS + SECOND, seq=11),
        make_trade(2, BASE_NS + 2 * SECOND, seq=12),
        make_trade(4, BASE_NS + SECOND, seq=9),
    ]
    report = audit(stream, max_gap=timedelta(minutes=1))
    assert not report.passed
    checks = {f.check: f.severity for f in report.findings}
    assert checks == {"duplicates": "fail", "out_of_order": "fail", "sequence_resets": "fail"}
    assert report.examples["duplicates"] == ("t-2",)
    assert "seq 9 after 12" in report.examples["sequence_resets"][0]


def test_gaps_and_scale_changes_warn_but_do_not_fail() -> None:
    stream = [
        make_trade(1, BASE_NS),
        make_trade(2, BASE_NS + 90 * SECOND),
        make_trade(3, BASE_NS + 91 * SECOND, price=Decimal("93530.5")),
        make_trade(4, BASE_NS + 92 * SECOND, qty=Decimal("0.0030")),
    ]
    report = audit(stream, max_gap=timedelta(minutes=1))
    assert report.passed
    checks = {f.check: f for f in report.findings}
    assert set(checks) == {"gaps", "price_scale", "qty_scale"}
    assert "90.0s after 2025-01-01 00:00:00Z" in checks["gaps"].detail
    assert report.price_scales == (1, 2) and report.qty_scales == (3, 4)
    assert "scale 1 first seen at accepted row 3" in report.examples["price_scale"][0]


def test_rejected_rows_fail_above_the_declared_fraction() -> None:
    good = [make_trade(i, BASE_NS + i * SECOND) for i in range(10)]
    one_bad: list[Trade | Rejection] = [*good, Rejection(11, "unknown side 'Short'", "raw")]
    report = audit(one_bad, max_gap=timedelta(minutes=1), max_rejected_fraction=Decimal("0.5"))
    assert report.passed
    assert report.findings[0].check == "rejected_rows" and report.findings[0].severity == "warn"
    report = audit(one_bad, max_gap=timedelta(minutes=1))
    assert not report.passed
    assert report.findings[0].severity == "fail"
    assert report.examples["rejected_rows"] == ("line 11: unknown side 'Short'",)


def test_empty_streams_and_mismatched_instruments_fail() -> None:
    report = audit([], max_gap=timedelta(minutes=1))
    assert not report.passed
    assert report.findings[0].check == "empty"

    stream = [make_trade(1, BASE_NS), make_trade(2, BASE_NS + SECOND, instrument="ETHUSDT")]
    report = audit(stream, max_gap=timedelta(minutes=1), expected_instrument="BTCUSDT")
    assert {f.check for f in report.findings} == {"mismatch"}
    with pytest.raises(ValueError, match="max_gap"):
        audit(stream, max_gap=timedelta(0))


def test_concentration_in_one_day_warns() -> None:
    day = 86_400 * SECOND
    stream = [make_trade(i, BASE_NS + i * SECOND) for i in range(90)]
    stream += [make_trade(100 + i, BASE_NS + day + i * SECOND) for i in range(10)]
    report = audit(stream, max_gap=timedelta(days=2))
    assert [f.check for f in report.findings] == ["concentration"]
    assert "2025-01-01 carries 90%" in report.findings[0].detail


def test_manifest_is_built_from_the_audit_and_round_trips(tmp_path: Path) -> None:
    stream = [make_trade(i, BASE_NS + i * SECOND) for i in range(3)]
    report = audit(stream, max_gap=timedelta(minutes=1))
    created = datetime(2026, 9, 13, tzinfo=UTC)
    manifest = manifest_from_audit(
        report,
        sources=(Source("BTCUSDT2025-01-01.csv.gz", "ab" * 32, 1234),),
        normaliser_version=NORMALISER_VERSION,
        created_at=created,
    )
    assert manifest.audit_passed and manifest.rows_accepted == 3
    assert manifest.first_ts == datetime(2025, 1, 1, tzinfo=UTC)

    path = tmp_path / "manifest.json"
    write_manifest(path, manifest)
    assert read_manifest(path) == manifest
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == 1 and record["audit"]["passed"] is True
    assert record["first_ts"].endswith("+00:00")


def test_manifest_refuses_absence_and_malformed_files(tmp_path: Path) -> None:
    empty = audit([], max_gap=timedelta(minutes=1))
    with pytest.raises(ManifestError, match="names no venue"):
        manifest_from_audit(empty, sources=(), normaliser_version="x", created_at=datetime.now(UTC))
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ManifestError, match="schema"):
        read_manifest(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        read_manifest(path)


def test_trade_refuses_defaulted_or_impossible_values() -> None:
    with pytest.raises(ValueError, match="price must be a positive"):
        make_trade(1, BASE_NS, price=Decimal("0"))
    with pytest.raises(ValueError, match="qty must be a positive"):
        make_trade(1, BASE_NS, qty=Decimal("NaN"))
    with pytest.raises(ValueError, match="positive epoch"):
        make_trade(1, 0)
    with pytest.raises(ValueError, match="must be a Decimal"):
        make_trade(1, BASE_NS, price=93530.0)
    assert scale_of(Decimal("93530")) == 0 and scale_of(Decimal("1E+2")) == 0
