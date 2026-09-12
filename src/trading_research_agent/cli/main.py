"""`tra`: the gates from a shell.

Exit codes are the verdicts a pipeline can act on: 0 when the gate was cleared or the work is
done, 1 when a gate said no (an audit failed, an idea stopped at the cost floor, a file did not
validate), 2 for a usage error. Errors go to stderr; results go to stdout, as text or JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agent import __version__
from trading_research_agent.chain import cost_floor
from trading_research_agent.chain.preregistration import PreregistrationError
from trading_research_agent.chain.preregistration import from_record as prereg_from_record
from trading_research_agent.data import (
    NORMALISER_VERSION,
    ArchiveError,
    ArchiveFormatError,
    Source,
    Trade,
    audit,
    binance_spot_trades_url,
    binance_trades,
    bybit_trades,
    bybit_trades_url,
    manifest_from_audit,
    open_archive_lines,
    sha256_of,
    write_manifest,
)
from trading_research_agent.data.fetch import FetchError, fetch, fetch_checksum
from trading_research_agent.data.parquet import ParquetError, write_trades
from trading_research_agent.envelope import EnvelopeError, load_envelope
from trading_research_agent.ledger import Ledger, LedgerError
from trading_research_agent.ledger.store import to_record as ledger_to_record

CLEARED, REFUSED, USAGE = 0, 1, 2

DURATION = re.compile(r"^(\d+)([smhd])$")
DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    match = DURATION.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"{text!r} is not a duration like 30s, 5m, 1h or 2d")
    return timedelta(**{DURATION_UNITS[match.group(2)]: int(match.group(1))})


def parse_decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None


def parse_month(text: str) -> tuple[int, int]:
    match = re.match(r"^(\d{4})-(\d{2})$", text.strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2025-01")
    return int(match.group(1)), int(match.group(2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tra", description="the gates, from a shell")
    parser.add_argument("--version", action="version", version=f"tra {__version__}")
    top = parser.add_subparsers(dest="group", required=True)

    data = top.add_parser("data", help="fetch and audit public trade archives").add_subparsers(
        dest="command", required=True
    )
    fetch_cmd = data.add_parser("fetch", help="download one archive and verify it")
    fetch_cmd.add_argument("--venue", choices=("bybit", "binance"), required=True)
    fetch_cmd.add_argument("--instrument", required=True)
    when = fetch_cmd.add_mutually_exclusive_group(required=True)
    when.add_argument("--day", type=date.fromisoformat, help="bybit: one UTC day, YYYY-MM-DD")
    when.add_argument("--month", type=parse_month, help="binance: one month, YYYY-MM")
    fetch_cmd.add_argument("--out", type=Path, default=Path("data"), help="directory to write into")
    fetch_cmd.add_argument("--json", action="store_true")

    audit_cmd = data.add_parser("audit", help="is this archive safe to backtest on?")
    audit_cmd.add_argument("archive", type=Path)
    audit_cmd.add_argument("--venue", choices=("bybit", "binance"), required=True)
    audit_cmd.add_argument("--instrument", required=True)
    audit_cmd.add_argument("--max-gap", type=parse_duration, required=True, help="e.g. 5m")
    audit_cmd.add_argument("--max-rejected", type=parse_decimal, default=Decimal("0.001"))
    audit_cmd.add_argument("--manifest", type=Path, help="write manifest.json here")
    audit_cmd.add_argument(
        "--parquet", type=Path, help="if the audit passes, write the canonical dataset here"
    )
    audit_cmd.add_argument("--json", action="store_true")

    chain = top.add_parser("chain", help="the falsification chain").add_subparsers(
        dest="command", required=True
    )
    floor_cmd = chain.add_parser("floor", help="layer 0: the cost floor")
    floor_cmd.add_argument("--envelope", type=Path, required=True)
    floor_cmd.add_argument("--magnitude", type=parse_decimal, required=True, help="bps")
    floor_cmd.add_argument("--venue")
    floor_cmd.add_argument("--subject", default="unnamed hypothesis")
    floor_cmd.add_argument("--ledger", type=Path, help="record a stop here")
    floor_cmd.add_argument("--id", dest="entry_id", help="ledger entry id for a stop")
    floor_cmd.add_argument("--json", action="store_true")

    check_cmd = chain.add_parser("check", help="layer 3: validate a pre-registration file")
    check_cmd.add_argument("file", type=Path)
    check_cmd.add_argument("--json", action="store_true")

    ledger = top.add_parser("ledger", help="the kill ledger").add_subparsers(
        dest="command", required=True
    )
    show_cmd = ledger.add_parser("show", help="the kill count and every row that stands")
    show_cmd.add_argument("--path", type=Path, default=Path("ledger.jsonl"))
    show_cmd.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = HANDLERS[(args.group, args.command)]
    try:
        return handler(args)
    except (
        ArchiveError,
        ArchiveFormatError,
        EnvelopeError,
        FetchError,
        LedgerError,
        ParquetError,
        PreregistrationError,
        OSError,
        ValueError,
    ) as exc:
        print(f"tra: {exc}", file=sys.stderr)
        return REFUSED


def emit(payload: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print("\n".join(lines))


def data_fetch(args: argparse.Namespace) -> int:
    if args.venue == "bybit":
        if args.day is None:
            raise ValueError("bybit archives are daily; pass --day")
        url = bybit_trades_url(args.instrument, args.day)
        expected = None
    else:
        if args.month is None:
            raise ValueError("binance archives are monthly; pass --month")
        url = binance_spot_trades_url(args.instrument, *args.month)
        expected = fetch_checksum(url + ".CHECKSUM", url.rsplit("/", 1)[1])
    dest = args.out / url.rsplit("/", 1)[1]
    source = fetch(url, dest, expected_sha256=expected)
    payload = {"path": str(dest), "sha256": source.sha256, "size": source.size, "url": url}
    verified = "verified against the venue's checksum" if expected else "sha256 recorded"
    emit(
        payload,
        args.json,
        [f"{dest}", f"sha256 {source.sha256} ({verified})", f"{source.size} bytes"],
    )
    return CLEARED


def data_audit(args: argparse.Namespace) -> int:
    normalise = bybit_trades if args.venue == "bybit" else binance_trades
    records = normalise(open_archive_lines(args.archive), instrument=args.instrument)
    report = audit(
        records,
        max_gap=args.max_gap,
        max_rejected_fraction=args.max_rejected,
        expected_venue=args.venue,
        expected_instrument=args.instrument,
    )
    if args.manifest is not None and report.rows_accepted:
        source = Source(args.archive.name, sha256_of(args.archive), args.archive.stat().st_size)
        manifest = manifest_from_audit(
            report,
            sources=(source,),
            normaliser_version=NORMALISER_VERSION,
            created_at=datetime.now(UTC),
        )
        write_manifest(args.manifest, manifest)

    lines = [
        f"{report.venue or args.venue} {report.instrument or args.instrument}: "
        f"{report.rows_accepted} trades, {report.rows_rejected} rejected",
    ]
    if report.first_ts and report.last_ts:
        lines.append(f"{report.first_ts:%Y-%m-%d %H:%M:%S}Z to {report.last_ts:%Y-%m-%d %H:%M:%S}Z")
    for finding in report.findings:
        lines.append(f"  {finding.severity.upper():4} {finding.check}: {finding.detail}")
        for example in report.examples.get(finding.check, ()):
            lines.append(f"         {example}")
    written = None
    if args.parquet is not None and report.passed:
        # Second pass over the archive. The audit already told us the scales the venue uses.
        again = normalise(open_archive_lines(args.archive), instrument=args.instrument)
        result = write_trades(
            (r for r in again if isinstance(r, Trade)),
            args.parquet,
            price_scale=max(report.price_scales),
            qty_scale=max(report.qty_scales),
        )
        written = str(result.path)
        lines.append(
            f"wrote {result.rows} trades to {result.path} "
            f"(decimal scales {result.price_scale}/{result.qty_scale})"
        )
    lines.append("PASSED" if report.passed else "FAILED")
    payload = {
        "parquet": written,
        "passed": report.passed,
        "venue": report.venue,
        "instrument": report.instrument,
        "rows_accepted": report.rows_accepted,
        "rows_rejected": report.rows_rejected,
        "first_ts": report.first_ts,
        "last_ts": report.last_ts,
        "findings": [
            {"check": f.check, "severity": f.severity, "detail": f.detail} for f in report.findings
        ],
        "examples": report.examples,
    }
    emit(payload, args.json, lines)
    return CLEARED if report.passed else REFUSED


def chain_floor(args: argparse.Namespace) -> int:
    envelope = load_envelope(args.envelope)
    check = cost_floor(envelope, args.magnitude, venue=args.venue)
    c = check.channel
    lines = [
        f"{check.outcome.value.upper()}: {args.magnitude} bps plausible against "
        f"{c.round_trip_bps} bps round trip on {c.key} (margin {check.margin_bps} bps)"
    ]
    if check.measured_margin_bps is not None:
        lines.append(
            f"measured round trip {c.measured_round_trip_bps} bps: "
            f"margin {check.measured_margin_bps} bps"
        )
    recorded = None
    if check.stopped and args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = check.to_entry(
            entry_id=args.entry_id,
            subject=args.subject,
            recorded_at=datetime.now(UTC),
            envelope=envelope,
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} as CLOSED on cost in {args.ledger}")
    payload = {
        "outcome": check.outcome.value,
        "plausible_magnitude_bps": check.plausible_magnitude_bps,
        "channel": c.key,
        "round_trip_bps": c.round_trip_bps,
        "margin_bps": check.margin_bps,
        "measured_round_trip_bps": c.measured_round_trip_bps,
        "measured_margin_bps": check.measured_margin_bps,
        "recorded": recorded,
    }
    emit(payload, args.json, lines)
    return REFUSED if check.stopped else CLEARED


def chain_check(args: argparse.Namespace) -> int:
    record = json.loads(args.file.read_text(encoding="utf-8"))
    prereg = prereg_from_record(record)
    criteria = [
        f"  {k.metric} {k.comparison} {k.threshold} -> {k.verdict.value}"
        for k in prereg.kill_criteria
    ]
    lines = [
        f"{prereg.name}: valid, seal {prereg.seal()}",
        f"horizon {prereg.horizon}, {len(prereg.universe.instruments)} instruments, "
        f"power n={prereg.power.required_sample} for {prereg.power.effect_bps} bps",
        "kill criteria:",
        *criteria,
    ]
    if prereg.amends:
        lines.append(f"amends {prereg.amends}: {prereg.amendment_reason}")
    payload = {"name": prereg.name, "seal": prereg.seal(), "amends": prereg.amends}
    emit(payload, args.json, lines)
    return CLEARED


def ledger_show(args: argparse.Namespace) -> int:
    ledger = Ledger(args.path)
    rows = ledger.effective()
    counts = ledger.counts()
    lines = ["  ".join(f"{v.value} {n}" for v, n in counts.items() if n)] if rows else ["empty"]
    for e in rows:
        axis = ", ".join(a.value for a in e.axis_of_death) or (
            e.blocking_axis.value if e.blocking_axis else "-"
        )
        lines.append(
            f"{e.entry_id:12} {e.verdict.value:10} layer {e.layer!s:4} {axis:20} {e.subject}"
        )
    payload = {
        "counts": {v.value: n for v, n in counts.items()},
        "rows": [ledger_to_record(e) for e in rows],
    }
    emit(payload, args.json, lines)
    return CLEARED


HANDLERS = {
    ("data", "fetch"): data_fetch,
    ("data", "audit"): data_audit,
    ("chain", "floor"): chain_floor,
    ("chain", "check"): chain_check,
    ("ledger", "show"): ledger_show,
}


if __name__ == "__main__":
    sys.exit(main())
