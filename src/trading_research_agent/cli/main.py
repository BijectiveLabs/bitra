"""`tra`: the gates from a shell.

Exit codes are the verdicts a pipeline can act on: 0 when the gate was cleared or the work is
done, 1 when a gate said no (an audit failed, an idea stopped at the cost floor, a file did not
validate), 2 for a usage error. Errors go to stderr; results go to stdout, as text or JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

from trading_research_agent import __version__
from trading_research_agent.agent import Completer, Outcome, RunConfig, propose
from trading_research_agent.agent.pipeline import read_refusals
from trading_research_agent.agent.providers import UnknownProviderError, provider, providers
from trading_research_agent.agent.providers.anthropic import DEFAULT_MODEL, ProviderError
from trading_research_agent.agent.session import SessionError
from trading_research_agent.chain import (
    EraLeakageError,
    NullMode,
    ValidationError,
    check_requirements,
    cost_floor,
    read_trades,
    validate,
)
from trading_research_agent.chain.preregistration import PreregistrationError
from trading_research_agent.chain.preregistration import from_record as prereg_from_record
from trading_research_agent.chain.requirements import to_entry as requirements_entry
from trading_research_agent.chain.validation import to_entry as validation_entry
from trading_research_agent.chain.validation import to_record as validation_record
from trading_research_agent.data import (
    NORMALISER_VERSION,
    ArchiveError,
    ArchiveFormatError,
    MappingError,
    Rejection,
    Source,
    Trade,
    UnknownVenueError,
    archive_venue,
    archive_venues,
    audit,
    load_mapping,
    manifest_from_audit,
    mapped_trades,
    open_archive_lines,
    read_manifest,
    sha256_of,
    write_manifest,
)
from trading_research_agent.data.fetch import FetchError, fetch, fetch_checksum
from trading_research_agent.data.parquet import ParquetError, write_trades
from trading_research_agent.envelope import EnvelopeError, load_envelope
from trading_research_agent.execution import FundingSchedule
from trading_research_agent.ledger import Ledger, LedgerError, Verdict
from trading_research_agent.ledger.store import to_record as ledger_to_record
from trading_research_agent.strategy import ExitError
from trading_research_agent.trading import (
    ConfigError,
    ControlError,
    Controls,
    KillSwitch,
    Ladder,
    LadderError,
    RecordedFeed,
    SimulatedVenue,
    Tier,
    TradingConfig,
    Venue,
    VenueError,
    load_strategy_config,
)
from trading_research_agent.trading import run as run_trading
from trading_research_agent.trading.canary import CanaryError, CanaryGuard
from trading_research_agent.trading.feed import FeedError
from trading_research_agent.trading.feeds import UnknownFeedError, live_feed
from trading_research_agent.trading.venues import (
    LiveVenueSettings,
    UnknownLiveVenueError,
    live_venue,
    live_venues,
)

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

    data = top.add_parser("data", help="fetch and audit trade data").add_subparsers(
        dest="command", required=True
    )
    venues = archive_venues()
    fetch_cmd = data.add_parser("fetch", help="download one venue archive and verify it")
    fetch_cmd.add_argument("--venue", choices=venues, required=True)
    fetch_cmd.add_argument("--instrument", required=True)
    when = fetch_cmd.add_mutually_exclusive_group(required=True)
    when.add_argument("--day", type=date.fromisoformat, help="daily archives: YYYY-MM-DD (UTC)")
    when.add_argument("--month", type=parse_month, help="monthly archives: YYYY-MM")
    fetch_cmd.add_argument("--out", type=Path, default=Path("data"), help="directory to write into")
    fetch_cmd.add_argument("--json", action="store_true")

    funding_cmd = data.add_parser("funding", help="settlement rates from a venue plugin")
    funding_cmd.add_argument("--venue", choices=venues, required=True)
    funding_cmd.add_argument("--instrument", required=True)
    funding_cmd.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    funding_cmd.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    funding_cmd.add_argument("--out", type=Path, required=True, help="funding.json")
    funding_cmd.add_argument("--json", action="store_true")

    audit_cmd = data.add_parser("audit", help="is this file safe to backtest on?")
    audit_cmd.add_argument("archive", type=Path, help=".csv, .csv.gz or .zip of one CSV")
    layout = audit_cmd.add_mutually_exclusive_group(required=True)
    layout.add_argument("--venue", choices=venues, help="a bundled or installed venue layout")
    layout.add_argument("--mapping", type=Path, help="a column map for any other source")
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

    req_cmd = chain.add_parser("requirements", help="layer 4: can the data measure the claim?")
    req_cmd.add_argument("--registration", type=Path, required=True)
    req_cmd.add_argument("--manifest", type=Path, required=True)
    req_cmd.add_argument("--ledger", type=Path, help="record a block here")
    req_cmd.add_argument("--id", dest="entry_id")
    req_cmd.add_argument("--json", action="store_true")

    val_cmd = chain.add_parser("validate", help="layer 5: a verdict on a set of trades")
    val_cmd.add_argument("--registration", type=Path, required=True)
    val_cmd.add_argument("--trades", type=Path, required=True, help="trades.jsonl")
    val_cmd.add_argument(
        "--cost-bps", type=parse_decimal, required=True, help="declared round trip per trade"
    )
    val_cmd.add_argument("--block-length", type=int, required=True, help="exceeds the horizon")
    val_cmd.add_argument("--n-resamples", type=int, default=5000)
    val_cmd.add_argument("--n-perm", type=int, default=1000)
    val_cmd.add_argument("--null", choices=("sign", "cross_sectional"), default="sign")
    val_cmd.add_argument("--seed", type=int, default=0)
    val_cmd.add_argument("--out", type=Path, help="write validation.json here")
    val_cmd.add_argument("--ledger", type=Path, help="record the verdict here")
    val_cmd.add_argument("--id", dest="entry_id")
    val_cmd.add_argument("--json", action="store_true")

    agent = top.add_parser("agent", help="an idea through layers 0, 2 and 3").add_subparsers(
        dest="command", required=True
    )
    propose_cmd = agent.add_parser("propose", help="bound, floor, memo, registration")
    propose_cmd.add_argument("--idea", required=True, help="the idea, in plain language")
    propose_cmd.add_argument("--envelope", type=Path, required=True)
    propose_cmd.add_argument("--provider", choices=providers(), required=True)
    propose_cmd.add_argument("--model", help="model name; anthropic defaults to " + DEFAULT_MODEL)
    propose_cmd.add_argument(
        "--base-url",
        help="an OpenAI-compatible endpoint (or TRA_OPENAI_BASE_URL) for the compatible providers",
    )
    propose_cmd.add_argument("--venue")
    propose_cmd.add_argument(
        "--magnitude", type=parse_decimal, help="declare the bound yourself, bps"
    )
    propose_cmd.add_argument("--max-revisions", type=int, default=2)
    propose_cmd.add_argument("--ledger", type=Path, default=Path("ledger.jsonl"))
    propose_cmd.add_argument("--refusals", type=Path, default=Path("refusals.jsonl"))
    propose_cmd.add_argument("--runs", type=Path, default=Path("runs"))
    propose_cmd.add_argument("--json", action="store_true")

    session_cmd = agent.add_parser(
        "session", help="the seven roles over recorded data, in the sandbox"
    )
    session_cmd.add_argument(
        "--config", type=Path, required=True, help="strategy.toml with [agents]"
    )
    session_cmd.add_argument("--envelope", type=Path, required=True)
    session_cmd.add_argument(
        "--dataset",
        action="append",
        required=True,
        metavar="INSTRUMENT=PATH",
        help="a Parquet dataset from data audit; repeat per instrument",
    )
    session_cmd.add_argument(
        "--magnitude", type=parse_decimal, required=True, help="the claimed edge, bps"
    )
    session_cmd.add_argument("--out", type=Path, required=True)
    session_cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
    session_cmd.add_argument(
        "--macro", type=Path, help="a JSON file of macro series for the macro analyst"
    )
    session_cmd.add_argument("--onchain", type=Path, help="a JSON file of on-chain series")
    session_cmd.add_argument("--env-file", type=Path)
    session_cmd.add_argument("--json", action="store_true")

    refusals_cmd = agent.add_parser("refusals", help="ideas the gates would not let through")
    refusals_cmd.add_argument("--path", type=Path, default=Path("refusals.jsonl"))
    refusals_cmd.add_argument("--json", action="store_true")

    trade = top.add_parser("trade", help="the trading agent: replay, shadow").add_subparsers(
        dest="command", required=True
    )
    replay_cmd = trade.add_parser("replay", help="recorded trades through the strategy")
    shadow_cmd = trade.add_parser("shadow", help="live public trades; zero orders sent")
    live_cmd = trade.add_parser("live", help="the canary tier: real orders, hard caps")
    # The common spellings are aliases of the tiers: backtest, paper, live.
    backtest_cmd = top.add_parser("backtest", help="alias of trade replay")
    backtest_cmd.set_defaults(command="replay")
    paper_cmd = top.add_parser("paper", help="alias of trade shadow")
    paper_cmd.set_defaults(command="shadow")
    live_alias = top.add_parser("live", help="alias of trade live")
    live_alias.set_defaults(command="live")
    for cmd in (replay_cmd, shadow_cmd, live_cmd, backtest_cmd, paper_cmd, live_alias):
        cmd.add_argument("--config", type=Path, required=True, help="strategy.toml")
        cmd.add_argument("--envelope", type=Path, required=True)
        cmd.add_argument("--out", type=Path, required=True, help="run directory")
        cmd.add_argument("--json", action="store_true")
    for cmd in (replay_cmd, backtest_cmd):
        cmd.add_argument("--parquet", type=Path, required=True, help="a dataset from data audit")
    for cmd in (shadow_cmd, live_cmd, paper_cmd, live_alias):
        cmd.add_argument("--max-reconnects", type=int, default=None)
        cmd.add_argument(
            "--feed", help="live feed plugin; defaults to the venue named in [run].venue_key"
        )
    for cmd in (live_cmd, live_alias):
        cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
        cmd.add_argument(
            "--env-file", type=Path, help="KEY=VALUE lines read explicitly; the environment wins"
        )

    mcp_p = top.add_parser("mcp", help="the gates as MCP tools").add_subparsers(
        dest="command", required=True
    )
    mcp_p.add_parser("serve", help="serve the read tools over stdio (needs the [mcp] extra)")

    init_cmd = top.add_parser("init", help="a new project directory from the templates")
    init_cmd.set_defaults(command="init")
    init_cmd.add_argument("directory", type=Path)
    init_cmd.add_argument("--json", action="store_true")

    ladder = top.add_parser("ladder", help="the promotion ladder").add_subparsers(
        dest="command", required=True
    )
    show_ladder = ladder.add_parser("show")
    show_ladder.add_argument("--path", type=Path, default=Path("ladder.json"))
    show_ladder.add_argument("--json", action="store_true")
    promote = ladder.add_parser("promote", help="one step up, with evidence and a signature")
    promote.add_argument("--path", type=Path, default=Path("ladder.json"))
    promote.add_argument("--evidence", required=True)
    promote.add_argument("--signed-by", required=True)
    promote.add_argument("--json", action="store_true")
    demote = ladder.add_parser("demote", help="down, on a reason alone")
    demote.add_argument("--path", type=Path, default=Path("ladder.json"))
    demote.add_argument("--reason", required=True)
    demote.add_argument("--to", choices=[t.name.lower() for t in Tier])
    demote.add_argument("--json", action="store_true")

    ledger_p = top.add_parser("ledger", help="the kill ledger").add_subparsers(
        dest="command", required=True
    )
    show_cmd = ledger_p.add_parser("show", help="the kill count and every row that stands")
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
        ConfigError,
        ControlError,
        EnvelopeError,
        EraLeakageError,
        ExitError,
        CanaryError,
        FeedError,
        UnknownFeedError,
        UnknownLiveVenueError,
        VenueError,
        LadderError,
        FetchError,
        MappingError,
        UnknownVenueError,
        LedgerError,
        ParquetError,
        PreregistrationError,
        ProviderError,
        SessionError,
        UnknownProviderError,
        ValidationError,
        ImportError,
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
    venue = archive_venue(args.venue)
    url = venue.archive_url(args.instrument, day=args.day, month=args.month)
    filename = url.rsplit("/", 1)[1]
    checksum_url = venue.checksum_url(url)
    expected = fetch_checksum(checksum_url, filename) if checksum_url else None
    dest = args.out / filename
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
    if args.venue is not None:
        source_name = str(args.venue)
        normalise = archive_venue(source_name).normalise
    else:
        mapping = load_mapping(args.mapping)
        source_name = mapping.source

        def normalise(lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
            return mapped_trades(lines, mapping, instrument=instrument)

    records = normalise(open_archive_lines(args.archive), instrument=args.instrument)
    report = audit(
        records,
        max_gap=args.max_gap,
        max_rejected_fraction=args.max_rejected,
        expected_venue=source_name,
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
        f"{report.venue or source_name} {report.instrument or args.instrument}: "
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
        # Second pass over the file. The audit already told us the scales the source uses.
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


def chain_requirements(args: argparse.Namespace) -> int:
    registration = prereg_from_record(json.loads(args.registration.read_text(encoding="utf-8")))
    manifest = read_manifest(args.manifest)
    requirements = check_requirements(registration, manifest)
    lines = [
        f"{f.check}: {'ok' if f.ok else 'BLOCKED'} - {f.detail}" for f in requirements.findings
    ]
    recorded = None
    if not requirements.passed and args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = requirements_entry(
            requirements, registration, entry_id=args.entry_id, recorded_at=datetime.now(UTC)
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} as NOT PROVEN in {args.ledger}")
    lines.append("PASSED" if requirements.passed else f"BLOCKED on {requirements.blocking_axis}")
    payload = {
        "passed": requirements.passed,
        "blocking_axis": (
            None if requirements.blocking_axis is None else requirements.blocking_axis.value
        ),
        "findings": [
            {"check": f.check, "ok": f.ok, "detail": f.detail, "axis": f.axis.value}
            for f in requirements.findings
        ],
        "recorded": recorded,
    }
    emit(payload, args.json, lines)
    return CLEARED if requirements.passed else REFUSED


def chain_validate(args: argparse.Namespace) -> int:
    registration = prereg_from_record(json.loads(args.registration.read_text(encoding="utf-8")))
    trades = read_trades(args.trades)
    cost = args.cost_bps
    result = validate(
        registration,
        trades,
        lambda _trade: cost,
        block_length=args.block_length,
        n_resamples=args.n_resamples,
        n_perm=args.n_perm,
        seed=args.seed,
        null_mode=NullMode(args.null),
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(validation_record(result), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    d = result.decision
    m = result.metrics
    lines = [
        f"{registration.name}: {result.n_trades} trades, {result.n_timestamps} timestamps, "
        f"power {'achieved' if result.power_achieved else 'NOT achieved'} "
        f"({result.n_timestamps} of {result.power_required})",
        f"net {m['net_return_bps']} bps, CI95 [{m['net_return_ci95_lower_bps']}, "
        f"{m['net_return_ci95_upper_bps']}], gross {m['gross_return_bps']} bps (never a verdict)",
        f"null ({result.null.mode.value}): mean {m['null_mean_bps']} bps, "
        f"p {m['permutation_p_value']}, {m['sigma_above_null']} sigma above",
        f"subperiods: {', '.join(f'{x:.2f}' for x in result.subperiods)} bps",
    ]
    for f in d.fired:
        lines.append(
            f"  fired {f.criterion.metric} {f.criterion.comparison} {f.criterion.threshold} "
            f"(observed {f.value}) -> {f.effective.value}"
        )
    recorded = None
    if args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = validation_entry(
            result, registration, entry_id=args.entry_id, recorded_at=datetime.now(UTC)
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} in {args.ledger}")
    lines.append(f"{d.verdict.value} ({d.axis.value}): {d.note}")
    payload = {**validation_record(result), "recorded": recorded}
    emit(payload, args.json, lines)
    return CLEARED if d.verdict is Verdict.OPEN else REFUSED


def _channel(args: argparse.Namespace, cfg: Any) -> Any:
    envelope = load_envelope(args.envelope)
    channel = next((c for c in envelope.channels if c.key == cfg.run.venue_key), None)
    if channel is None:
        raise ConfigError(f"venue_key {cfg.run.venue_key!r} is not a channel in {args.envelope}")
    return channel


def _agent_core(cfg: Any, out_dir: Path) -> Any:
    """The technical analyst as the core: a metered completer over the declared provider."""
    from trading_research_agent.agent.strategy import AgentStrategy
    from trading_research_agent.agent.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        UsageLedger,
    )

    a = cfg.agents
    completer = provider(a.provider).build(a.model, a.base_url)
    metered = MeteredCompleter(
        completer,
        role=a.role,
        provider=a.provider,
        limits=BudgetLimits(
            token_budget=a.token_budget,
            max_calls_per_hour=a.max_calls_per_hour,
            min_interval_s=a.min_interval_s,
            call_timeout_s=a.call_timeout_s,
        ),
        ledger=UsageLedger(out_dir / "usage.jsonl"),
        pricing={m: Pricing(i, o) for m, (i, o) in a.pricing.items()},
    )
    return AgentStrategy(
        metered,
        mechanism=a.mechanism,
        lookback_bars=a.lookback_bars,
        min_confidence=a.min_confidence,
    )


def _trading_config(
    args: argparse.Namespace,
    feed: Any,
    label: str,
    *,
    venue: Venue | None = None,
    canary: CanaryGuard | None = None,
) -> TradingConfig:
    cfg = load_strategy_config(args.config)
    strategy = cfg.strategy if cfg.agents is None else _agent_core(cfg, args.out)
    if venue is None:
        venue = SimulatedVenue(
            channel=_channel(args, cfg),
            funding=cfg.funding
            or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
            latency_ns=int(cfg.run.latency_ms * 1_000_000),
            slippage_bps=cfg.run.slippage_bps,
        )
    controls = Controls(cfg.limits, KillSwitch(cfg.kill_switch))
    return TradingConfig(
        feed=feed,
        venue=venue,
        strategy=strategy,
        exit_rule=cfg.exit_rule,
        controls=controls,
        out_dir=args.out,
        bar_interval_ns=cfg.run.bar_interval_s * 1_000_000_000,
        size=cfg.run.size,
        spread_cost_bps=cfg.run.spread_cost_bps,
        equity=cfg.run.equity,
        label=label,
        canary=canary,
    )


def _report(args: argparse.Namespace, report: Any) -> int:
    lines = [
        f"{report.label} {report.instrument}: {report.prints} prints, {report.bars} bars, "
        f"{report.intents} intents ({report.refused} refused), {report.fills} fills, "
        f"{report.round_trips} round trips, net {report.net_bps_total} bps",
    ]
    for refusal in report.refusals[:5]:
        lines.append(f"  refused {refusal}")
    if report.open_position:
        lines.append(f"open position at end: {report.open_position}")
    if report.stopped_by:
        lines.append(f"stopped by: {report.stopped_by}")
    lines.append(f"trades.jsonl, intents.jsonl, fills.jsonl, run.json in {args.out}")
    payload = {
        "label": report.label,
        "prints": report.prints,
        "bars": report.bars,
        "intents": report.intents,
        "refused": report.refused,
        "fills": report.fills,
        "round_trips": report.round_trips,
        "net_bps_total": str(report.net_bps_total),
        "stopped_by": report.stopped_by,
        "open_position": report.open_position,
        "out": str(args.out),
    }
    emit(payload, args.json, lines)
    return CLEARED if report.stopped_by is None else REFUSED


def trade_replay(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    feed = RecordedFeed.from_parquet(args.parquet, instrument=cfg.run.instrument)
    return _report(args, run_trading(_trading_config(args, feed, "replay")))


def trade_shadow(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    # The channel key is venue/channel; the venue half names the feed unless told otherwise.
    feed = _live_feed(args, cfg, args.feed or cfg.run.venue_key.partition("/")[0])
    return _report(args, run_trading(_trading_config(args, feed, "shadow")))


def _live_feed(args: argparse.Namespace, cfg: Any, name: str) -> Any:
    return live_feed(name)(
        cfg.run.instrument, stop=threading.Event(), max_reconnects=args.max_reconnects
    )


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines. Blank lines and comments are skipped; quotes are stripped."""
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            raise ValueError(f"{path}:{number}: not a KEY=VALUE line")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _environment(env_file: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    if env_file is not None:
        for key, value in read_env_file(env_file).items():
            env.setdefault(key, value)
    return env


def trade_live(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    if cfg.live is None or cfg.canary is None:
        raise ConfigError("a live session needs both [live] and [canary] in strategy.toml")
    ladder = Ladder(args.ladder, live_available=bool(live_venues()))
    tier = ladder.tier
    if tier is not Tier.CANARY:
        raise LadderError(
            f"live sessions run at {Tier.CANARY.name}; {args.ladder} is at {tier.name}. "
            "Promote with evidence and a signature first."
        )
    run_id = uuid4().hex
    settings = LiveVenueSettings(
        instrument=cfg.run.instrument,
        channel=_channel(args, cfg),
        funding=cfg.funding or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
        environment=cfg.live.environment,
        run_id=run_id,
        key_ip_unbound_ok=cfg.live.key_ip_unbound_ok,
        adopt_open_position=cfg.live.adopt_open_position,
        poll_interval_ms=cfg.live.poll_interval_ms,
    )
    venue = live_venue(cfg.live.venue)(settings, _environment(args.env_file))
    checks = venue.preflight()
    feed = _live_feed(args, cfg, args.feed or cfg.live.venue)
    config = _trading_config(args, feed, "live-canary", venue=venue, canary=CanaryGuard(cfg.canary))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "preflight.json").write_text(
        json.dumps({"run_id": run_id, "venue": venue.label, **checks}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    report = run_trading(config)
    events = getattr(venue, "events", [])
    if events:
        (args.out / "venue_events.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    return _report(args, report)


TEMPLATE_FILES = {
    "strategy.toml": "strategy.toml",
    "envelope.toml": "envelope.toml",
    "mapping.toml": "mapping.toml",
    "strategies/trailing_zscore.py": "core.py.tmpl",
    ".env.example": "env.example",
    ".gitignore": "gitignore",
    "README.md": "README.md",
}


def init_project(args: argparse.Namespace) -> int:
    target: Path = args.directory
    templates = files("trading_research_agent").joinpath("templates")
    existing = [name for name in TEMPLATE_FILES if (target / name).exists()]
    if existing:
        raise ValueError(f"{target}: already has {existing}; init never overwrites")
    for name, source in TEMPLATE_FILES.items():
        out = target / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(templates.joinpath(source).read_text(encoding="utf-8"), encoding="utf-8")
    lines = [f"{target}: {len(TEMPLATE_FILES)} files", *(f"  {n}" for n in TEMPLATE_FILES)]
    lines.append("next: tra data fetch, tra data audit, tra backtest (see README.md)")
    emit({"directory": str(target), "files": list(TEMPLATE_FILES)}, args.json, lines)
    return CLEARED


def data_funding(args: argparse.Namespace) -> int:
    venue = archive_venue(args.venue)
    fetch_rates = getattr(venue, "funding_rates", None)
    if fetch_rates is None:
        raise ValueError(f"venue {args.venue!r} does not publish funding through its plugin")
    if args.end < args.start:
        raise ValueError("--to is before --from")
    start_ms = int(datetime.combine(args.start, time.min, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime.combine(args.end, time.max, tzinfo=UTC).timestamp() * 1000)
    rates = fetch_rates(args.instrument, start_ms=start_ms, end_ms=end_ms)
    if not rates:
        raise ValueError(
            f"no settlements for {args.instrument} between {args.start} and {args.end}"
        )
    gaps = sorted((b.ts_ns - a.ts_ns) // 1_000_000_000 for a, b in pairwise(rates))
    payload: dict[str, Any] = {
        "venue": args.venue,
        "instrument": args.instrument,
        "from": args.start.isoformat(),
        "to": args.end.isoformat(),
        "fetched_at": datetime.now(UTC).isoformat(),
        "rates": {str(r.ts_ns): str(r.rate) for r in rates},
    }
    if gaps:
        # The settlement clock is measured from the data, never assumed.
        payload["interval_s"] = gaps[len(gaps) // 2]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    first = datetime.fromtimestamp(rates[0].ts_ns / 1e9, tz=UTC)
    last = datetime.fromtimestamp(rates[-1].ts_ns / 1e9, tz=UTC)
    lines = [
        f"{args.venue} {args.instrument}: {len(rates)} settlements, "
        f"{first:%Y-%m-%d %H:%M}Z to {last:%Y-%m-%d %H:%M}Z"
        + (f", every {payload['interval_s']} s" if gaps else ""),
        f"wrote {args.out}",
    ]
    emit({**payload, "count": len(rates), "out": str(args.out)}, args.json, lines)
    return CLEARED


def agent_session(args: argparse.Namespace) -> int:
    from trading_research_agent.agent.session import SessionConfig, run_session
    from trading_research_agent.agent.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        UsageLedger,
    )

    if args.env_file is not None:
        for key, value in read_env_file(args.env_file).items():
            os.environ.setdefault(key, value)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("a session needs [strategy].agent and [agents] in strategy.toml")
    datasets: dict[str, Path] = {}
    for item in args.dataset:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ValueError(f"--dataset takes INSTRUMENT=PATH, got {item!r}")
        datasets[name] = Path(path)
    a = cfg.agents
    args.out.mkdir(parents=True, exist_ok=True)
    usage = UsageLedger(args.out / "usage.jsonl")
    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {m: Pricing(i, o) for m, (i, o) in a.pricing.items()}
    inner = provider(a.provider).build(a.model, a.base_url)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        # One budget for the whole session; the ledger is shared, the cadence is per role.
        if role not in metered:
            metered[role] = MeteredCompleter(
                inner, role=role, provider=a.provider, limits=limits, ledger=usage, pricing=pricing
            )
        return metered[role]

    result = run_session(
        SessionConfig(
            strategy=cfg,
            envelope=load_envelope(args.envelope),
            datasets=datasets,
            out_dir=args.out,
            completer_for=completer_for,
            usage=usage,
            magnitude_bps=args.magnitude,
            ladder_path=args.ladder,
            kill_file=cfg.kill_switch,
            macro_series=args.macro,
            onchain_series=args.onchain,
        )
    )
    lines = [
        f"session {result.session_id}: {len(result.kept)} kept, {len(result.excluded)} excluded, "
        f"{sum(r.round_trips for r in result.reports.values())} round trips, "
        f"{usage.tokens} tokens, cost {usage.cost}",
    ]
    for name, r in result.reports.items():
        lines.append(f"  {name}: {r.round_trips} round trips, net {r.net_bps_total} bps")
    if result.risk is not None:
        lines.append(f"risk manager: {result.risk['decision']}: {result.risk['reason']}")
    if result.audit is not None:
        lines.append(f"auditor: {result.audit['verdict']}: {result.audit['summary']}")
    for refusal in result.refusals[:5]:
        lines.append(f"  refused {refusal}")
    if result.stopped_by:
        lines.append(f"stopped by: {result.stopped_by}")
    lines.append(f"session.json, audit.json, usage.jsonl in {args.out}")
    payload = {
        "session_id": result.session_id,
        "kept": result.kept,
        "excluded": result.excluded,
        "runs": {n: r.round_trips for n, r in result.reports.items()},
        "risk": result.risk,
        "allocation": result.allocation,
        "audit": result.audit,
        "tokens": usage.tokens,
        "cost": str(usage.cost),
        "refusals": result.refusals,
        "stopped_by": result.stopped_by,
        "out": str(args.out),
    }
    emit(payload, args.json, lines)
    return CLEARED if result.stopped_by is None else REFUSED


def mcp_serve(args: argparse.Namespace) -> int:
    from trading_research_agent.mcp import serve

    serve()
    return CLEARED


def ladder_show(args: argparse.Namespace) -> int:
    ladder = Ladder(args.path)
    lines = [f"tier {ladder.tier.value} {ladder.tier.name}"]
    for h in ladder.history:
        detail = h.get("evidence") or h.get("reason")
        lines.append(f"  {h['at']} {h['kind']} {h['from']} -> {h['to']}: {detail}")
    emit({"tier": ladder.tier.name, "history": list(ladder.history)}, args.json, lines)
    return CLEARED


def ladder_promote(args: argparse.Namespace) -> int:
    ladder = Ladder(args.path, live_available=bool(live_venues()))
    receipt = ladder.promote(evidence=args.evidence, signed_by=args.signed_by)
    payload = {"from": receipt.from_tier.name, "to": receipt.to_tier.name, "at": receipt.at}
    emit(payload, args.json, [f"{receipt.from_tier.name} -> {receipt.to_tier.name}"])
    return CLEARED


def ladder_demote(args: argparse.Namespace) -> int:
    to = None if args.to is None else Tier[args.to.upper()]
    demotion = Ladder(args.path).demote(reason=args.reason, to=to)
    payload = {"from": demotion.from_tier.name, "to": demotion.to_tier.name, "at": demotion.at}
    emit(payload, args.json, [f"{demotion.from_tier.name} -> {demotion.to_tier.name}"])
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


def _completer(provider_name: str, model: str | None, base_url: str | None = None) -> Completer:
    return provider(provider_name).build(model, base_url)


def agent_propose(args: argparse.Namespace) -> int:
    config = RunConfig(
        envelope=load_envelope(args.envelope),
        completer=_completer(args.provider, args.model, args.base_url),
        venue=args.venue,
        magnitude_bps=args.magnitude,
        max_revisions=args.max_revisions,
        ledger=Ledger(args.ledger),
        refusals=args.refusals,
        runs_dir=args.runs,
    )
    result = propose(args.idea, config)
    lines = [f"{result.run_id}: {result.outcome.value}"]
    if result.floor is not None:
        c = result.floor.channel
        lines.append(
            f"layer 0: {result.floor.plausible_magnitude_bps} bps against "
            f"{c.round_trip_bps} bps on {c.key} ({result.floor.outcome.value})"
        )
    if result.refusal is not None:
        lines.append(
            f"refused at layer {result.refusal.layer} after {result.refusal.revisions} "
            f"revisions: {result.refusal.reason}"
        )
    if result.preregistration is not None:
        lines.append(
            f"registered: {result.preregistration.name}, seal {result.preregistration.seal()}"
        )
    if result.ledger_entry_id is not None:
        lines.append(f"ledger: {result.ledger_entry_id} in {args.ledger}")
    lines.append(
        f"{len(result.exchanges)} model calls, {result.input_tokens} in / "
        f"{result.output_tokens} out tokens; run written to {args.runs / result.run_id}"
    )
    payload = {
        "run_id": result.run_id,
        "outcome": result.outcome.value,
        "ledger_entry_id": result.ledger_entry_id,
        "seal": None if result.preregistration is None else result.preregistration.seal(),
        "refusal": None if result.refusal is None else result.refusal.reason,
        "exchanges": len(result.exchanges),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "run_dir": str(args.runs / result.run_id),
    }
    emit(payload, args.json, lines)
    return CLEARED if result.outcome is Outcome.REGISTERED else REFUSED


def agent_refusals(args: argparse.Namespace) -> int:
    refusals = read_refusals(args.path)
    lines = [f"{len(refusals)} refused"] if refusals else ["none"]
    for r in refusals:
        lines.append(
            f"{r.run_id:22} layer {r.layer} after {r.revisions}: {r.reason[:60]}  | {r.idea[:50]}"
        )
    payload = {
        "count": len(refusals),
        "refusals": [
            {
                "run_id": r.run_id,
                "at": r.at,
                "layer": r.layer,
                "revisions": r.revisions,
                "reason": r.reason,
                "idea": r.idea,
            }
            for r in refusals
        ],
    }
    emit(payload, args.json, lines)
    return CLEARED


HANDLERS = {
    ("agent", "propose"): agent_propose,
    ("agent", "refusals"): agent_refusals,
    ("agent", "session"): agent_session,
    ("data", "fetch"): data_fetch,
    ("data", "audit"): data_audit,
    ("data", "funding"): data_funding,
    ("chain", "floor"): chain_floor,
    ("chain", "check"): chain_check,
    ("chain", "requirements"): chain_requirements,
    ("chain", "validate"): chain_validate,
    ("ledger", "show"): ledger_show,
    ("trade", "replay"): trade_replay,
    ("trade", "shadow"): trade_shadow,
    ("trade", "live"): trade_live,
    ("backtest", "replay"): trade_replay,
    ("paper", "shadow"): trade_shadow,
    ("live", "live"): trade_live,
    ("init", "init"): init_project,
    ("mcp", "serve"): mcp_serve,
    ("ladder", "show"): ladder_show,
    ("ladder", "promote"): ladder_promote,
    ("ladder", "demote"): ladder_demote,
}


if __name__ == "__main__":
    sys.exit(main())
